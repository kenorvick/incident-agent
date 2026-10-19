"""Anthropic client: wire translation, cache placement, usage/cost, errors (mock HTTP).

The 1.x `anthropic` SDK is built on `httpx2`, so the mock transport comes from there.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from incident_agent.agent.models.anthropic import AnthropicClient
from incident_agent.agent.models.base import (
    Message,
    ModelAPIError,
    ModelAuthError,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

pytestmark = pytest.mark.anyio

SYSTEM = "You are an incident investigator."
TOOLS = [
    ToolSpec(name="run_query", description="Aggregate.", input_schema={"type": "object"}),
    ToolSpec(name="get_trace", description="Trace.", input_schema={"type": "object"}),
]


def message_body(
    content: list[dict[str, Any]] | None = None,
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content if content is not None else [{"type": "text", "text": "done"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 20},
    }


def error_body(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


class Wire:
    def __init__(self, *responses: httpx2.Response | dict[str, Any] | Exception) -> None:
        self.queue = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, httpx2.Response) else httpx2.Response(200, json=item)

    @property
    def last_body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def make_client(wire: Wire, policy: Any, sleeper: Any, **kw: Any) -> AnthropicClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(wire))
    return AnthropicClient("test-key", http_client=http, retry_policy=policy, sleep=sleeper, **kw)


def http_error(status: int, kind: str, message: str, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=error_body(kind, message), headers=headers)


# --- translation -----------------------------------------------------------------------


async def test_text_only_roundtrip(policy, sleeper):
    wire = Wire(message_body([{"type": "text", "text": "All quiet."}]))
    resp = await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("alert")], [], 500
    )

    req = wire.requests[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == "test-key"
    body = wire.last_body
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 500
    assert body["output_config"] == {"effort": "medium"}
    assert body["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "alert", "cache_control": {"type": "ephemeral"}}],
        }
    ]
    assert "tools" not in body
    assert resp.text == "All quiet."
    assert resp.stop_reason == "end_turn"
    assert resp.provider == "anthropic"
    assert resp.model == "claude-sonnet-5-5"
    assert resp.request_id == "msg_01"


async def test_parallel_tool_use_blocks_are_decoded(policy, sleeper):
    wire = Wire(
        message_body(
            [
                {"type": "text", "text": "Two lookups."},
                {"type": "tool_use", "id": "toolu_a", "name": "run_query", "input": {"g": "svc"}},
                {"type": "tool_use", "id": "toolu_b", "name": "get_trace", "input": {}},
            ],
            stop_reason="tool_use",
        )
    )
    resp = await make_client(wire, policy, sleeper).complete(
        SYSTEM, [Message.user("x")], TOOLS, 500
    )

    assert resp.stop_reason == "tool_use"
    assert resp.text == "Two lookups."
    assert resp.tool_calls == [
        ToolUseBlock(id="toolu_a", name="run_query", input={"g": "svc"}),
        ToolUseBlock(id="toolu_b", name="get_trace", input={}),
    ]


async def test_history_with_parallel_results_and_error_is_encoded(policy, sleeper):
    assistant = Message(
        role="assistant",
        content=[
            TextBlock(text="Looking."),
            ToolUseBlock(id="toolu_a", name="run_query", input={"g": "svc"}),
            ToolUseBlock(id="toolu_b", name="get_trace", input={"trace_id": "zz"}),
        ],
    )
    results = Message(
        role="user",
        content=[
            TextBlock(text="2 steps left."),
            ToolResultBlock(tool_use_id="toolu_a", content='{"rows": []}'),
            ToolResultBlock(tool_use_id="toolu_b", content="not found", is_error=True),
        ],
    )
    wire = Wire(message_body())
    await make_client(wire, policy, sleeper, prompt_caching=False).complete(
        SYSTEM, [Message.user("alert"), assistant, results], TOOLS, 500
    )

    msgs = wire.last_body["messages"]
    assert msgs[1]["content"] == [
        {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "toolu_a", "name": "run_query", "input": {"g": "svc"}},
        {"type": "tool_use", "id": "toolu_b", "name": "get_trace", "input": {"trace_id": "zz"}},
    ]
    # Results come first (the API requires it), in one user message, errors flagged.
    assert msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_a", "content": '{"rows": []}'},
        {"type": "tool_result", "tool_use_id": "toolu_b", "content": "not found", "is_error": True},
        {"type": "text", "text": "2 steps left."},
    ]


async def test_thinking_blocks_round_trip_unchanged_and_uncached(policy, sleeper):
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    redacted = {"type": "redacted_thinking", "data": "xyz"}
    wire = Wire(
        message_body(
            [
                thinking,
                redacted,
                {"type": "tool_use", "id": "t1", "name": "get_trace", "input": {}},
            ],
            stop_reason="tool_use",
        )
    )
    client = make_client(wire, policy, sleeper)
    first = await client.complete(SYSTEM, [Message.user("a")], TOOLS, 500)
    assert first.provider_data["anthropic"]["thinking_blocks"] == [thinking, redacted]

    history = [
        Message.user("a"),
        first.to_message(),
        Message.tool_results([ToolResultBlock(tool_use_id="t1", content="r")]),
    ]
    await client.complete(SYSTEM, history, TOOLS, 500)

    assistant_wire = wire.last_body["messages"][1]["content"]
    assert assistant_wire[:2] == [thinking, redacted]  # first, unmodified
    assert assistant_wire[2]["type"] == "tool_use"
    # Stored state must not pick up request-time cache marks.
    assert "cache_control" not in first.provider_data["anthropic"]["thinking_blocks"][0]


# --- caching ---------------------------------------------------------------------------


def count_breakpoints(body: dict[str, Any]) -> int:
    return json.dumps(body).count('"cache_control"')


async def test_cache_breakpoints_on_tools_system_and_two_latest_user_turns(policy, sleeper):
    history = [
        Message.user("alert"),
        Message(role="assistant", content=[ToolUseBlock(id="t1", name="get_trace", input={})]),
        Message.tool_results([ToolResultBlock(tool_use_id="t1", content="r1")]),
        Message(role="assistant", content=[ToolUseBlock(id="t2", name="get_trace", input={})]),
        Message.tool_results([ToolResultBlock(tool_use_id="t2", content="r2")]),
    ]
    wire = Wire(message_body())
    await make_client(wire, policy, sleeper).complete(SYSTEM, history, TOOLS, 500)

    body = wire.last_body
    mark = {"type": "ephemeral"}
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == mark
    assert body["system"] == [{"type": "text", "text": SYSTEM, "cache_control": mark}]
    msgs = body["messages"]
    assert "cache_control" not in msgs[0]["content"][-1]  # oldest user turn: not marked
    assert msgs[2]["content"][-1]["cache_control"] == mark  # where the previous call wrote
    assert msgs[4]["content"][-1]["cache_control"] == mark  # newest turn
    assert all("cache_control" not in m["content"][-1] for m in (msgs[1], msgs[3]))
    assert count_breakpoints(body) == 4  # the provider limit, never more


async def test_breakpoint_count_never_exceeds_four_in_long_conversations(policy, sleeper):
    history: list[Message] = [Message.user("alert")]
    for i in range(12):
        history.append(
            Message(
                role="assistant", content=[ToolUseBlock(id=f"t{i}", name="get_trace", input={})]
            )
        )
        history.append(Message.tool_results([ToolResultBlock(tool_use_id=f"t{i}", content="r")]))
    wire = Wire(message_body())
    await make_client(wire, policy, sleeper).complete(SYSTEM, history, TOOLS, 500)
    assert count_breakpoints(wire.last_body) == 4


async def test_one_hour_ttl_and_disabled_caching(policy, sleeper):
    wire = Wire(message_body())
    await make_client(wire, policy, sleeper, cache_ttl="1h").complete(
        SYSTEM, [Message.user("x")], TOOLS, 500
    )
    assert wire.last_body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    wire = Wire(message_body())
    await make_client(wire, policy, sleeper, prompt_caching=False).complete(
        SYSTEM, [Message.user("x")], TOOLS, 500
    )
    assert count_breakpoints(wire.last_body) == 0


async def test_effort_can_be_changed_or_omitted(policy, sleeper):
    wire = Wire(message_body())
    await make_client(wire, policy, sleeper, effort="low").complete(
        SYSTEM, [Message.user("x")], [], 500
    )
    assert wire.last_body["output_config"] == {"effort": "low"}

    wire = Wire(message_body())
    await make_client(wire, policy, sleeper, effort=None).complete(
        SYSTEM, [Message.user("x")], [], 500
    )
    assert "output_config" not in wire.last_body


# --- usage, cost, stop reasons ---------------------------------------------------------


async def test_usage_and_estimated_cost(policy, sleeper):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 400,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 0,
        "output_tokens_details": {"thinking_tokens": 150},
    }
    wire = Wire(message_body(usage=usage))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)

    assert resp.usage.input_tokens == 1000
    assert resp.usage.cache_read_tokens == 4000
    assert resp.usage.cache_write_tokens == 0
    assert resp.usage.output_tokens == 400
    assert resp.usage.reasoning_tokens == 150
    assert resp.usage.total_input_tokens == 5000
    # Sonnet 5.5: $2/M in, $10/M out, $0.10/M cache read.
    assert resp.cost_usd == pytest.approx(1000 * 2e-6 + 400 * 10e-6 + 4000 * 0.1e-6)
    assert resp.cost_source == "estimated"


async def test_cache_write_cost_uses_write_rate(policy, sleeper):
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 4000}
    wire = Wire(message_body(usage=usage))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert resp.cost_usd == pytest.approx(4000 * 2.5e-6)

    wire = Wire(message_body(usage=usage))
    resp = await make_client(wire, policy, sleeper, cache_ttl="1h").complete(
        SYSTEM, [Message.user("x")], [], 500
    )
    assert resp.cost_usd == pytest.approx(4000 * 4e-6)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("pause_turn", "other"),
        ("refusal", "other"),
        ("model_context_window_exceeded", "other"),
    ],
)
async def test_stop_reason_normalization(policy, sleeper, raw, expected):
    wire = Wire(message_body(stop_reason=raw))
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert resp.stop_reason == expected
    assert resp.raw_stop_reason == raw


# --- errors and retries ----------------------------------------------------------------


async def test_bad_key_is_not_retried_and_says_what_to_do(policy, sleeper):
    wire = Wire(http_error(401, "authentication_error", "invalid x-api-key"))
    with pytest.raises(ModelAuthError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert len(wire.requests) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize("status", [400, 403, 404, 413])
async def test_client_errors_are_not_retried(policy, sleeper, status):
    wire = Wire(http_error(status, "invalid_request_error", "nope"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert exc.value.status_code == status
    assert len(wire.requests) == 1


async def test_404_points_at_model_id(policy, sleeper):
    wire = Wire(http_error(404, "not_found_error", "model: nope"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert "MODEL_ID" in str(exc.value)


async def test_429_honors_retry_after(policy, sleeper):
    wire = Wire(http_error(429, "rate_limit_error", "slow", **{"retry-after": "4"}), message_body())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert resp.attempts == 2
    assert 4 <= sleeper.delays[0] <= 4.25
    assert len(wire.requests) == 2  # the SDK's own retries are off; ours are the only ones


async def test_overloaded_529_and_5xx_back_off_then_succeed(policy, sleeper):
    wire = Wire(
        http_error(529, "overloaded_error", "Overloaded"),
        http_error(500, "api_error", "oops"),
        message_body(),
    )
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert resp.attempts == 3
    assert sleeper.delays == [0.5, 1.0]


async def test_gives_up_after_max_attempts(policy, sleeper):
    wire = Wire(http_error(529, "overloaded_error", "Overloaded"))
    with pytest.raises(ModelAPIError) as exc:
        await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert len(wire.requests) == 5
    assert exc.value.attempts == 5


async def test_connection_errors_are_retried(policy, sleeper):
    wire = Wire(httpx2.ConnectError("refused"), message_body())
    resp = await make_client(wire, policy, sleeper).complete(SYSTEM, [Message.user("x")], [], 500)
    assert resp.attempts == 2


def test_api_key_is_required():
    with pytest.raises(ValueError):
        AnthropicClient("")
