"""A scripted model client for tests of the agent loop and eval builders. Never calls a network.

    client = ScriptedModelClient([
        tool_call_response(("run_query", {"group_by": "service.name"}), text="Checking services."),
        tool_call_response(("get_trace", {"trace_id": "ab"}), ("run_query", {})),  # parallel
        "Root cause: ...",  # a bare string is a final text answer
    ])
    result = await run_loop(client, ...)
    assert client.requests[1].tool_results[0].tool_use_id == "toolu_0_0"
    client.assert_exhausted()

A script is a list consumed one item per call, or a callable `(RecordedRequest) -> item`
(sync or async). An item is a `ModelResponse`, a `str`, or an exception instance (raised, to
test error handling). With `strict=True` (default) the client also rejects histories that
leave a tool call unanswered, which is the usual bug in a hand-written loop.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from incident_agent.agent.models.base import (
    Message,
    ModelResponse,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

ScriptItem = ModelResponse | str | BaseException
ScriptFn = Callable[["RecordedRequest"], ScriptItem | Awaitable[ScriptItem]]

PROVIDER = "fake"
MODEL = "fake-model"


class ScriptExhaustedError(AssertionError):
    """The loop asked for more model turns than the script provides."""


@dataclass
class RecordedRequest:
    """One `complete()` call, with deep copies so later mutation cannot change the record."""

    system: str
    messages: list[Message]
    tools: list[ToolSpec]
    max_tokens: int
    index: int = 0

    @property
    def last_message(self) -> Message:
        return self.messages[-1]

    @property
    def tool_results(self) -> list[ToolResultBlock]:
        """Tool results in the final user message (what the loop just fed back)."""
        return [b for b in self.last_message.content if isinstance(b, ToolResultBlock)]

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]

    def text(self) -> str:
        """All text blocks of the final message joined."""
        return "\n".join(b.text for b in self.last_message.content if isinstance(b, TextBlock))


def text_response(
    text: str,
    *,
    stop_reason: StopReason = "end_turn",
    usage: Usage | None = None,
    cost_usd: float = 0.0,
    model: str = MODEL,
) -> ModelResponse:
    return ModelResponse(
        text=text,
        stop_reason=stop_reason,
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        cost_usd=cost_usd,
        cost_source="estimated",
        model=model,
        provider=PROVIDER,
    )


def tool_call_response(
    *calls: tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], str] | ToolUseBlock,
    text: str = "",
    usage: Usage | None = None,
    cost_usd: float = 0.0,
    model: str = MODEL,
) -> ModelResponse:
    """A response requesting one or more (parallel) tool calls: `(name, input)` or
    `(name, input, id)`. Ids left blank become `toolu_<call>_<n>` when served."""
    blocks = [
        c
        if isinstance(c, ToolUseBlock)
        else ToolUseBlock(id=c[2] if len(c) > 2 else "", name=c[0], input=c[1])
        for c in calls
    ]
    return ModelResponse(
        text=text,
        tool_calls=blocks,
        stop_reason="tool_use",
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        cost_usd=cost_usd,
        cost_source="estimated",
        model=model,
        provider=PROVIDER,
    )


@dataclass
class ScriptedModelClient:
    """Returns pre-scripted responses and records every request."""

    script: Sequence[ScriptItem] | ScriptFn
    repeat_last: bool = False
    strict: bool = True
    requests: list[RecordedRequest] = field(default_factory=list)
    provider: str = PROVIDER
    model: str = MODEL

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> RecordedRequest:
        return self.requests[-1]

    @property
    def remaining(self) -> int:
        """Scripted items not yet served (0 for callable scripts)."""
        if callable(self.script):
            return 0
        return max(len(self.script) - len(self.requests), 0)

    def assert_exhausted(self) -> None:
        if self.remaining:
            raise AssertionError(f"{self.remaining} scripted response(s) were never requested")

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        request = RecordedRequest(
            system=system,
            messages=[m.model_copy(deep=True) for m in messages],
            tools=[t.model_copy(deep=True) for t in tools],
            max_tokens=max_tokens,
            index=len(self.requests),
        )
        if self.strict:
            _check_tool_results(request.messages)
        self.requests.append(request)
        item = await self._next(request)
        if isinstance(item, BaseException):
            raise item
        response = text_response(item) if isinstance(item, str) else item.model_copy(deep=True)
        for i, call in enumerate(response.tool_calls):
            if not call.id:
                call.id = f"toolu_{request.index}_{i}"
        return response

    async def _next(self, request: RecordedRequest) -> ScriptItem:
        if callable(self.script):
            item = self.script(request)
            return await item if inspect.isawaitable(item) else item
        if request.index < len(self.script):
            return self.script[request.index]
        if self.repeat_last and self.script:
            return self.script[-1]
        raise ScriptExhaustedError(
            f"Script has {len(self.script)} response(s) but call #{request.index + 1} was made."
        )


def _check_tool_results(messages: list[Message]) -> None:
    """Every tool_use in an assistant turn must be answered in the very next user turn."""
    for i, msg in enumerate(messages):
        if msg.role != "assistant":
            continue
        wanted = [b.id for b in msg.content if isinstance(b, ToolUseBlock)]
        if not wanted:
            continue
        if i + 1 >= len(messages):
            raise ValueError(f"History ends with unanswered tool call(s) {wanted}.")
        nxt = messages[i + 1]
        got = {b.tool_use_id for b in nxt.content if isinstance(b, ToolResultBlock)}
        missing = [w for w in wanted if w not in got]
        if missing:
            raise ValueError(
                f"Tool call(s) {missing} in message {i} have no tool_result in message {i + 1}."
            )
