"""OpenRouter client: wire translation, caching, usage/cost, errors and retries (mock HTTP)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from incident_agent.agent.models.base import (
    Message,
    ModelAPIError,
    ModelAuthError,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from incident_agent.agent.models.openrouter import OpenRouterClient
from incident_agent.agent.models.retry import RetryPolicy

pytestmark = pytest.mark.anyio

SYSTEM = "You are an incident investigator."
TOOLS = [
    ToolSpec(
        name="run_query",
        description="Aggregate events.",
        input_schema={"type": "object", "properties": {"group_by": {"type": "string"}}},
    ),
    ToolSpec(name="get_trace", description="Fetch a trace.", input_schema={"type": "object"}),
]


def completion(
    *,
    content: str | None = "done",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
    extra_message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, **(extra_message or {})}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "gen-123",
        "model": "anthropic/claude-sonnet-5.5-20260928",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage
        or {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": 0.0012},
    }


def tool_call(call_id: str, name: str, args: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


class Wire:
    """Mock OpenRouter: serves queued responses and records requests."""

    def __init__(self, *responses: httpx.Response | dict[str, Any] | Exception) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    @property
    def last_body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def make_client(
    wire: Callable[[httpx.Request], httpx.Response], policy: RetryPolicy, sleeper: Any, **kw: Any
) -> OpenRouterClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return OpenRouterClient("test-key", http_client=http, retry_policy=policy, sleep=sleeper, **kw)


def error_response(status: int, message: str = "boom", **headers: str) -> httpx.Response:
    body = {"error": {"code": status, "message": message}}
    return httpx.Response(status, json=body, headers=headers)


# --- text only -------------------------------------------------------------------------


async def test_text_only_roundtrip(policy, sleeper):
    wire = Wire(completion(content="All quiet."))
    client = make_client(wire, policy, sleeper)

    resp = await client.complete(SYSTEM, [Message.user("alert!")], [], 1000)

    req = wire.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key"
    body = wire.last_body
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["max_tokens"] == 1000
    assert "tools" not in body and "tool_choice" not in body
    assert body["messages"][1] == {"role": "user", "content": "alert!"}
    assert resp.text == "All quiet."
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
    assert resp.provider == "openrouter"
    assert resp.model == "anthropic/claude-sonnet-5.5-20260928"
    assert resp.request_id == "gen-123"
    assert resp.latency_ms >= 0
    assert resp.attempts == 1


async def test_reasoning_effort_is_sent_and_configurable(policy, sleeper):
    wire = Wire(completion())
    await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert wire.last_body["reasoning"] == {"effort": "medium"}

    wire = Wire(completion())
    await make_client(wire, policy, sleeper, effort="low").complete(
        SYSTEM, [Message.user("x")], [], 100
    )
    assert wire.last_body["reasoning"] == {"effort": "low"}

    wire = Wire(completion())
    await make_client(wire, policy, sleeper, effort=None).complete(
        SYSTEM, [Message.user("x")], [], 100
    )
    assert "reasoning" not in wire.last_body


# --- tool calls ------------------------------------------------------------------------


async def test_tools_are_encoded_openai_style(policy, sleeper):
    wire = Wire(completion())
    await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], TOOLS, 100)

    body = wire.last_body
    assert body["tool_choice"] == "auto"
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": "Aggregate events.",
            "parameters": TOOLS[0].input_schema,
        },
    }
    assert [t["function"]["name"] for t in body["tools"]] == ["run_query", "get_trace"]


async def test_single_tool_call_is_decoded(policy, sleeper):
    wire = Wire(
        completion(
            content=None,
            tool_calls=[tool_call("call_1", "run_query", '{"group_by": "service.name"}')],
            finish_reason="tool_calls",
        )
    )
    resp = await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("x")], TOOLS, 100
    )

    assert resp.stop_reason == "tool_use"
    assert resp.raw_stop_reason == "tool_calls"
    assert resp.text == ""
    assert resp.tool_calls == [
        ToolUseBlock(id="call_1", name="run_query", input={"group_by": "service.name"})
    ]


async def test_parallel_tool_calls_are_decoded_in_order(policy, sleeper):
    wire = Wire(
        completion(
            content="Checking two things.",
            tool_calls=[
                tool_call("call_a", "run_query", '{"group_by": "service.name"}'),
                tool_call("call_b", "get_trace", '{"trace_id": "abc"}'),
                tool_call("call_c", "get_trace", ""),
            ],
            finish_reason="tool_calls",
        )
    )
    resp = await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("x")], TOOLS, 100
    )

    assert resp.text == "Checking two things."
    assert [c.id for c in resp.tool_calls] == ["call_a", "call_b", "call_c"]
    assert resp.tool_calls[1].input == {"trace_id": "abc"}
    assert resp.tool_calls[2].input == {}  # empty arguments string means no arguments


@pytest.mark.parametrize("args", ["{not json", "[1, 2]"])
async def test_bad_tool_arguments_are_flagged_not_raised(policy, sleeper, args):
    wire = Wire(
        completion(
            content=None,
            tool_calls=[tool_call("call_1", "run_query", args)],
            finish_reason="tool_calls",
        )
    )
    resp = await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("x")], TOOLS, 100
    )
    assert resp.tool_calls[0].input == {}
    assert resp.tool_calls[0].parse_error


async def test_history_with_parallel_results_and_error_is_encoded(policy, sleeper):
    assistant = Message(
        role="assistant",
        content=[
            TextBlock(text="Looking."),
            ToolUseBlock(id="call_a", name="run_query", input={"group_by": "service.name"}),
            ToolUseBlock(id="call_b", name="get_trace", input={"trace_id": "zz"}),
        ],
    )
    results = Message.tool_results(
        [
            ToolResultBlock(tool_use_id="call_a", content='{"rows": []}'),
            ToolResultBlock(tool_use_id="call_b", content="trace zz not found", is_error=True),
        ]
    )
    wire = Wire(completion())
    await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("alert"), assistant, results], TOOLS, 100
    )

    msgs = wire.last_body["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assert msgs[2]["content"] == "Looking."
    assert msgs[2]["tool_calls"] == [
        tool_call("call_a", "run_query", '{"group_by": "service.name"}'),
        tool_call("call_b", "get_trace", '{"trace_id": "zz"}'),
    ]
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_a", "content": '{"rows": []}'}
    assert msgs[4]["tool_call_id"] == "call_b"
    assert msgs[4]["content"] == "[tool error] trace zz not found"


async def test_text_after_tool_results_follows_the_tool_messages(policy, sleeper):
    assistant = Message(
        role="assistant", content=[ToolUseBlock(id="c1", name="get_trace", input={})]
    )
    user = Message(
        role="user",
        content=[
            TextBlock(text="Budget warning: 2 steps left."),
            ToolResultBlock(tool_use_id="c1", content="ok"),
        ],
    )
    wire = Wire(completion())
    await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("a"), assistant, user], TOOLS, 100
    )
    roles = [m["role"] for m in wire.last_body["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert wire.last_body["messages"][2]["content"] is None  # tool-call-only assistant turn


async def test_reasoning_details_round_trip_through_provider_data(policy, sleeper):
    details = [{"type": "reasoning.encrypted", "data": "opaque", "index": 0}]
    wire = Wire(
        completion(
            content=None,
            tool_calls=[tool_call("c1", "get_trace", "{}")],
            finish_reason="tool_calls",
            extra_message={"reasoning": "thinking...", "reasoning_details": details},
        )
    )
    client = make_client(wire, policy, sleeper)
    first = await client.complete(SYSTEM, [Message.user("a")], TOOLS, 100)
    assert first.provider_data == {"openrouter": {"reasoning_details": details}}

    history = [
        Message.user("a"),
        first.to_message(),
        Message.tool_results([ToolResultBlock(tool_use_id="c1", content="r")]),
    ]
    await client.complete(SYSTEM, history, TOOLS, 100)
    assistant_wire = wire.last_body["messages"][2]
    assert assistant_wire["reasoning_details"] == details
    assert "reasoning" not in assistant_wire  # only the structured form is echoed


# --- prompt caching --------------------------------------------------------------------


async def test_anthropic_models_get_system_breakpoint_and_automatic_caching(policy, sleeper):
    wire = Wire(completion())
    await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], TOOLS, 100)

    body = wire.last_body
    assert body["cache_control"] == {"type": "ephemeral"}  # automatic, follows the last block
    system = body["messages"][0]
    assert system["role"] == "system"
    assert system["content"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    # No other explicit breakpoints: 1 explicit + 1 automatic, within the limit of four.
    explicit = json.dumps(body["messages"][1:]).count("cache_control")
    assert explicit == 0


async def test_one_hour_ttl_is_applied_to_both_breakpoints(policy, sleeper):
    wire = Wire(completion())
    await make_client(wire, policy, sleeper, cache_ttl="1h").complete(
        SYSTEM, [Message.user("x")], [], 100
    )
    body = wire.last_body
    ttl = {"type": "ephemeral", "ttl": "1h"}
    assert body["cache_control"] == ttl
    assert body["messages"][0]["content"][0]["cache_control"] == ttl


async def test_non_anthropic_models_and_disabled_caching_send_no_cache_control(policy, sleeper):
    wire = Wire(completion())
    await make_client(wire, policy, sleeper, model="openai/gpt-5").complete(
        SYSTEM, [Message.user("x")], [], 100
    )
    assert "cache_control" not in json.dumps(wire.last_body)
    assert wire.last_body["messages"][0] == {"role": "system", "content": SYSTEM}

    wire = Wire(completion())
    await make_client(wire, policy, sleeper, prompt_caching=False).complete(
        SYSTEM, [Message.user("x")], [], 100
    )
    assert "cache_control" not in json.dumps(wire.last_body)


# --- usage and cost --------------------------------------------------------------------


async def test_usage_separates_cached_tokens_and_uses_provider_cost(policy, sleeper):
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 400,
        "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 600},
        "completion_tokens_details": {"reasoning_tokens": 250},
        "cost": 0.0123,
    }
    wire = Wire(completion(usage=usage))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)

    assert resp.usage.input_tokens == 400  # 5000 - 4000 cached - 600 written
    assert resp.usage.cache_read_tokens == 4000
    assert resp.usage.cache_write_tokens == 600
    assert resp.usage.output_tokens == 400
    assert resp.usage.reasoning_tokens == 250
    assert resp.usage.total_input_tokens == 5000
    assert resp.cost_usd == pytest.approx(0.0123)
    assert resp.cost_source == "provider"


async def test_missing_cost_falls_back_to_estimate(policy, sleeper):
    usage = {"prompt_tokens": 1000, "completion_tokens": 400}
    wire = Wire(completion(usage=usage))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    # 1000 * $2/M + 400 * $10/M
    assert resp.cost_usd == pytest.approx(0.006)
    assert resp.cost_source == "estimated"


async def test_byok_cost_uses_upstream_inference_cost(policy, sleeper):
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 10,
        "cost": 0,
        "is_byok": True,
        "cost_details": {"upstream_inference_cost": 0.5},
    }
    wire = Wire(completion(usage=usage))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.cost_usd == 0.5


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "other"),
        ("something_new", "other"),
        (None, "other"),
    ],
)
async def test_finish_reason_normalization(policy, sleeper, finish, expected):
    wire = Wire(completion(finish_reason=finish))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.stop_reason == expected
    assert resp.raw_stop_reason == finish


async def test_stop_with_tool_calls_counts_as_tool_use(policy, sleeper):
    wire = Wire(
        completion(
            content=None, tool_calls=[tool_call("c", "get_trace", "{}")], finish_reason="stop"
        )
    )
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], TOOLS, 1)
    assert resp.stop_reason == "tool_use"


# --- errors and retries ----------------------------------------------------------------


async def test_bad_key_is_not_retried_and_says_what_to_do(policy, sleeper):
    wire = Wire(error_response(401, "No auth credentials found"))
    client = make_client(wire, policy, sleeper)

    with pytest.raises(ModelAuthError) as exc:
        await client.complete(SYSTEM, [Message.user("x")], [], 100)

    assert "OPENROUTER_API_KEY" in str(exc.value)
    assert ".env" in str(exc.value)
    assert len(wire.requests) == 1
    assert sleeper.delays == []


async def test_out_of_credits_is_not_retried(policy, sleeper):
    wire = Wire(error_response(402, "Insufficient credits"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert "credits" in str(exc.value)
    assert not exc.value.retryable
    assert len(wire.requests) == 1


async def test_in_flight_budget_402_with_retry_after_is_retried(policy, sleeper):
    wire = Wire(error_response(402, "in flight budget", **{"retry-after": "2"}), completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 2
    assert sleeper.delays and 2 <= sleeper.delays[0] <= 2.25


async def test_unknown_model_error_points_at_model_id(policy, sleeper):
    wire = Wire(error_response(404, "No endpoints found"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert "MODEL_ID" in str(exc.value)


@pytest.mark.parametrize("status", [400, 403, 422])
async def test_other_client_errors_surface_without_retry(policy, sleeper, status):
    wire = Wire(error_response(status, "nope"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert exc.value.status_code == status
    assert "nope" in str(exc.value)
    assert len(wire.requests) == 1


async def test_429_honors_retry_after(policy, sleeper):
    wire = Wire(error_response(429, "slow down", **{"retry-after": "3"}), completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)

    assert resp.text == "done"
    assert resp.attempts == 2
    assert len(sleeper.delays) == 1
    assert 3 <= sleeper.delays[0] <= 3.25  # the header, not the 0.5s backoff


async def test_5xx_backs_off_exponentially_then_succeeds(policy, sleeper):
    wire = Wire(error_response(503), error_response(502), error_response(500), completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)

    assert resp.attempts == 4
    assert sleeper.delays == [0.5, 1.0, 2.0]  # rng fixed at 0: half of 1, 2, 4


async def test_gives_up_after_max_attempts(policy, sleeper):
    wire = Wire(error_response(503, "unavailable"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)

    assert len(wire.requests) == 5
    assert len(sleeper.delays) == 4
    assert exc.value.attempts == 5
    assert "gave up after 5" in str(exc.value)


async def test_retry_after_longer_than_cap_is_not_slept_on(policy, sleeper):
    wire = Wire(error_response(429, "limit", **{"retry-after": "3600"}))
    with pytest.raises(ModelAPIError):
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert len(wire.requests) == 1
    assert sleeper.delays == []


async def test_transport_errors_and_timeouts_are_retried(policy, sleeper):
    wire = Wire(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 3


async def test_error_inside_a_200_body_is_classified_by_its_code(policy, sleeper):
    in_body = {"error": {"code": 429, "message": "Rate limit exceeded"}}
    wire = Wire(in_body, completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 2

    wire = Wire({"error": {"code": 400, "message": "bad prompt"}})
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert not exc.value.retryable


async def test_mid_generation_error_finish_reason_is_retried(policy, sleeper):
    broken = completion(finish_reason="error")
    broken["choices"][0]["error"] = {"code": 502, "message": "Provider disconnected"}
    wire = Wire(broken, completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 2


async def test_empty_choices_is_retried(policy, sleeper):
    wire = Wire({"id": "gen-1", "choices": []}, completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 2


async def test_non_json_gateway_error_page_is_reported(policy, sleeper):
    wire = Wire(httpx.Response(502, text="<html>Bad gateway</html>"), completion())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 100)
    assert resp.attempts == 2


def test_api_key_is_required():
    with pytest.raises(ValueError):
        OpenRouterClient("")
