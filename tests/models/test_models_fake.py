"""ScriptedModelClient: the fake the loop and eval tests are built on."""

from __future__ import annotations

import pytest

from incident_agent.agent.models.base import (
    Message,
    ModelClient,
    ModelResponse,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from incident_agent.agent.models.fake import (
    ScriptedModelClient,
    ScriptExhaustedError,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.anyio

TOOL = ToolSpec(name="run_query", description="d", input_schema={"type": "object"})


async def test_serves_responses_in_order_and_records_requests():
    client = ScriptedModelClient(
        [tool_call_response(("run_query", {"q": 1}), text="Looking."), "Final answer."]
    )
    assert isinstance(client, ModelClient)

    first = await client.complete("sys", [Message.user("alert")], [TOOL], 123)
    assert first.stop_reason == "tool_use"
    assert first.text == "Looking."
    assert first.tool_calls == [ToolUseBlock(id="toolu_0_0", name="run_query", input={"q": 1})]

    history = [
        Message.user("alert"),
        first.to_message(),
        Message.tool_results([ToolResultBlock(tool_use_id="toolu_0_0", content="rows")]),
    ]
    second = await client.complete("sys", history, [TOOL], 123)
    assert (second.text, second.stop_reason, second.tool_calls) == ("Final answer.", "end_turn", [])

    assert client.calls == 2
    assert client.requests[0].system == "sys"
    assert client.requests[0].max_tokens == 123
    assert client.requests[0].tool_names == ["run_query"]
    assert client.last_request.tool_results[0].content == "rows"
    client.assert_exhausted()


async def test_parallel_calls_get_distinct_ids_per_turn():
    client = ScriptedModelClient(
        [
            tool_call_response(("a", {}), ("b", {}), ("c", {}, "custom-id")),
            tool_call_response(("a", {})),
        ]
    )
    first = await client.complete("s", [Message.user("x")], [], 10)
    assert [c.id for c in first.tool_calls] == ["toolu_0_0", "toolu_0_1", "custom-id"]
    history = [
        Message.user("x"),
        first.to_message(),
        Message.tool_results(
            [ToolResultBlock(tool_use_id=c.id, content="r") for c in first.tool_calls]
        ),
    ]
    second = await client.complete("s", history, [], 10)
    assert second.tool_calls[0].id == "toolu_1_0"


async def test_requests_are_deep_copied():
    client = ScriptedModelClient(["a", "b"])
    history = [Message.user("original")]
    await client.complete("s", history, [], 10)
    history[0].content[0].text = "mutated"  # type: ignore[union-attr]
    history.append(Message.user("more"))
    assert client.requests[0].text() == "original"
    assert len(client.requests[0].messages) == 1


async def test_served_responses_are_copies_of_the_script():
    scripted = tool_call_response(("a", {}))
    client = ScriptedModelClient([scripted])
    resp = await client.complete("s", [Message.user("x")], [], 10)
    assert scripted.tool_calls[0].id == ""  # the script itself was not modified
    assert resp.tool_calls[0].id == "toolu_0_0"


async def test_exhausted_script_raises_clearly_and_repeat_last_loops():
    client = ScriptedModelClient(["only"])
    await client.complete("s", [Message.user("x")], [], 10)
    with pytest.raises(ScriptExhaustedError, match="1 response"):
        await client.complete("s", [Message.user("x")], [], 10)

    looping = ScriptedModelClient(["same"], repeat_last=True)
    for _ in range(3):
        assert (await looping.complete("s", [Message.user("x")], [], 10)).text == "same"


async def test_assert_exhausted_flags_unused_responses():
    client = ScriptedModelClient(["a", "b"])
    await client.complete("s", [Message.user("x")], [], 10)
    with pytest.raises(AssertionError, match="1 scripted"):
        client.assert_exhausted()


async def test_exceptions_in_the_script_are_raised():
    client = ScriptedModelClient(["ok", TimeoutError("model down")])
    await client.complete("s", [Message.user("x")], [], 10)
    with pytest.raises(TimeoutError, match="model down"):
        await client.complete("s", [Message.user("x")], [], 10)
    assert client.calls == 2  # the failing request is still recorded


async def test_callable_scripts_sync_and_async():
    def by_history(req):
        return "done" if len(req.messages) > 1 else tool_call_response(("run_query", {}))

    client = ScriptedModelClient(by_history)
    first = await client.complete("s", [Message.user("x")], [], 10)
    assert first.tool_calls
    done = await client.complete(
        "s",
        [
            Message.user("x"),
            first.to_message(),
            Message.tool_results([ToolResultBlock(tool_use_id="toolu_0_0", content="r")]),
        ],
        [],
        10,
    )
    assert done.text == "done"

    async def slow(req):
        return text_response(f"call {req.index}")

    assert (
        await ScriptedModelClient(slow).complete("s", [Message.user("x")], [], 10)
    ).text == "call 0"


async def test_strict_mode_catches_unanswered_tool_calls():
    client = ScriptedModelClient([tool_call_response(("a", {}), ("b", {})), "x"], repeat_last=True)
    first = await client.complete("s", [Message.user("x")], [], 10)
    partial = [
        Message.user("x"),
        first.to_message(),
        Message.tool_results([ToolResultBlock(tool_use_id="toolu_0_0", content="r")]),
    ]
    with pytest.raises(ValueError, match="toolu_0_1"):
        await client.complete("s", partial, [], 10)
    with pytest.raises(ValueError, match="unanswered"):
        await client.complete("s", [Message.user("x"), first.to_message()], [], 10)

    lenient = ScriptedModelClient(["x"], strict=False)
    await lenient.complete("s", [Message.user("x"), first.to_message()], [], 10)


def test_response_builders_set_usage_and_cost_defaults():
    resp = text_response("hi", cost_usd=0.01)
    assert isinstance(resp, ModelResponse)
    assert resp.usage.input_tokens > 0 and resp.usage.output_tokens > 0
    assert (resp.provider, resp.cost_usd) == ("fake", 0.01)
