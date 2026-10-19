"""Anthropic API model client (official `anthropic` SDK, async Messages API).

- Tool use: assistant `tool_use` blocks, and `tool_result` blocks (with `is_error`) in the next
  user turn. Parallel results all go in one user message.
- Prompt caching: up to four breakpoints, placed on the last tool, the system prompt, and the
  last block of the two most recent user turns. The prefix order is tools, system, messages, so
  the first two keep the stable prefix cached; the last two give incremental caching (the older
  one is exactly where the previous call wrote its entry, so no 20-block lookback is needed).
- Sonnet 5.5 always thinks (billed as output); `output_config.effort` caps it. Thinking blocks
  must be passed back unchanged within a tool-use turn, so they ride in `provider_data`.
- The SDK's own retries are disabled (`max_retries=0`); `retry.py` handles them uniformly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anthropic as sdk

from incident_agent.agent.models.base import (
    Message,
    ModelAPIError,
    ModelAuthError,
    ModelResponse,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from incident_agent.agent.models.pricing import CacheTTL, estimate_cost
from incident_agent.agent.models.retry import (
    RETRYABLE_STATUSES,
    RetryPolicy,
    parse_retry_after,
    retry_async,
)

DEFAULT_MODEL = "claude-sonnet-5-5"
PROVIDER = "anthropic"

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
}  # pause_turn, refusal, model_context_window_exceeded -> "other" (see raw_stop_reason)

_KEY_HINT = "Set a valid ANTHROPIC_API_KEY in .env (see .env.example)."


# --- encoding (neutral -> wire) ---------------------------------------------------------


def cache_control(ttl: CacheTTL = "5m") -> dict[str, str]:
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def encode_tools(tools: Sequence[ToolSpec], *, cache_ttl: CacheTTL | None = None) -> list[dict]:
    wire = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]
    if cache_ttl and wire:
        wire[-1]["cache_control"] = cache_control(cache_ttl)
    return wire


def encode_system(system: str, *, cache_ttl: CacheTTL | None = None) -> list[dict[str, Any]]:
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache_ttl:
        block["cache_control"] = cache_control(cache_ttl)
    return [block]


def encode_messages(
    messages: Sequence[Message], *, cache_ttl: CacheTTL | None = None
) -> list[dict[str, Any]]:
    """Neutral turns to Anthropic messages; the last block of the two newest user turns is
    marked cacheable when `cache_ttl` is set. Empty assistant turns are dropped."""
    wire: list[dict[str, Any]] = []
    user_positions: list[int] = []
    for msg in messages:
        content = _encode_assistant(msg) if msg.role == "assistant" else _encode_user(msg)
        if not content:
            continue
        wire.append({"role": msg.role, "content": content})
        if msg.role == "user":
            user_positions.append(len(wire) - 1)
    if cache_ttl:
        for pos in user_positions[-2:]:
            wire[pos]["content"][-1]["cache_control"] = cache_control(cache_ttl)
    return wire


def _encode_assistant(msg: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = list(
        msg.provider_data.get(PROVIDER, {}).get("thinking_blocks", [])
    )
    blocks = [dict(b) for b in blocks]  # copies: cache marks must not leak into stored state
    for block in msg.content:
        if isinstance(block, TextBlock) and block.text:
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    if all(b["type"] in ("thinking", "redacted_thinking") for b in blocks):
        return []  # nothing but reasoning: the API rejects an assistant turn without content
    return blocks


def _encode_user(msg: Message) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            item: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content or "(empty result)",
            }
            if block.is_error:
                item["is_error"] = True
            results.append(item)
        elif isinstance(block, TextBlock) and block.text:
            texts.append({"type": "text", "text": block.text})
    return results + texts  # the API requires tool results first in a user turn


# --- decoding (wire -> neutral) ---------------------------------------------------------


def decode_response(
    msg: Any, *, requested_model: str, latency_ms: float, cache_ttl: CacheTTL = "5m"
) -> ModelResponse:
    """Parse an SDK `Message` into a `ModelResponse`."""
    texts: list[str] = []
    calls: list[ToolUseBlock] = []
    thinking: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == "text":
            if block.text:
                texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {})))
        elif block.type in ("thinking", "redacted_thinking"):
            thinking.append(block.model_dump(mode="json", exclude_none=True))

    u = msg.usage
    details = getattr(u, "output_tokens_details", None)
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        reasoning_tokens=getattr(details, "thinking_tokens", 0) or 0,
    )
    model = msg.model or requested_model
    return ModelResponse(
        text="\n\n".join(texts),
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(msg.stop_reason or "", "other"),
        raw_stop_reason=msg.stop_reason,
        usage=usage,
        cost_usd=estimate_cost(model, usage, cache_ttl),
        cost_source="estimated",
        model=model,
        provider=PROVIDER,
        latency_ms=latency_ms,
        request_id=msg.id,
        provider_data={PROVIDER: {"thinking_blocks": thinking}} if thinking else {},
    )


# --- errors -----------------------------------------------------------------------------


def translate_error(exc: Exception, model: str = "") -> ModelAPIError:
    """Map an SDK exception to `ModelAPIError` (or `ModelAuthError`)."""
    if isinstance(exc, sdk.APIStatusError):
        status = exc.status_code
        message = str(exc.message)[:500]
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        if status == 401:
            return ModelAuthError(
                f"Anthropic rejected the API key ({message}). {_KEY_HINT}", status_code=status
            )
        if status == 403:
            return ModelAPIError(
                f"Anthropic denied permission ({message}); check the key's access.",
                status_code=status,
            )
        if status == 404:
            hint = f" (currently {model!r})" if model else ""
            return ModelAPIError(
                f"Anthropic could not find the model ({message}). Check MODEL_ID{hint}.",
                status_code=status,
            )
        retryable = status in RETRYABLE_STATUSES or status >= 500
        return ModelAPIError(
            f"Anthropic API error {status}: {message}",
            status_code=status,
            retryable=retryable,
            retry_after=retry_after if retryable else None,
        )
    if isinstance(exc, sdk.APIConnectionError):  # includes timeouts
        return ModelAPIError(f"Anthropic connection failed: {exc!r}", retryable=True)
    if isinstance(exc, sdk.CredentialsError):
        return ModelAuthError(f"Anthropic credentials problem: {exc}. {_KEY_HINT}")
    if isinstance(exc, sdk.AnthropicError):
        return ModelAPIError(f"Anthropic client error: {exc}")
    raise exc


# --- client -----------------------------------------------------------------------------


class AnthropicClient:
    """`ModelClient` for the Anthropic API. Pass `http_client` (an `httpx2.AsyncClient`, the
    HTTP library the 1.x SDK uses) to inject a mock transport in tests."""

    provider = PROVIDER

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        effort: str | None = "medium",
        prompt_caching: bool = True,
        cache_ttl: CacheTTL = "5m",
        base_url: str | None = None,
        http_client: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 180.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.model = model
        self.effort = effort
        self.prompt_caching = prompt_caching
        self.cache_ttl = cache_ttl
        self._api_key = api_key
        self._base_url = base_url
        self._http_client = http_client
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._timeout = timeout

    def build_request(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> dict[str, Any]:
        ttl = self.cache_ttl if self.prompt_caching else None
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": encode_system(system, cache_ttl=ttl),
            "messages": encode_messages(messages, cache_ttl=ttl),
        }
        if tools:
            request["tools"] = encode_tools(tools, cache_ttl=ttl)
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        return request

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        request = self.build_request(system, messages, tools, max_tokens)

        async def attempt() -> ModelResponse:
            start = time.perf_counter()
            try:
                async with self._sdk() as client:
                    msg = await client.messages.create(**request)
            except Exception as exc:  # noqa: BLE001 - translated or re-raised below
                raise translate_error(exc, self.model) from exc
            latency_ms = (time.perf_counter() - start) * 1000
            return decode_response(
                msg, requested_model=self.model, latency_ms=latency_ms, cache_ttl=self.cache_ttl
            )

        response, attempts = await retry_async(attempt, self._retry, self._sleep)
        response.attempts = attempts
        return response

    @asynccontextmanager
    async def _sdk(self) -> AsyncIterator[sdk.AsyncAnthropic]:
        client = sdk.AsyncAnthropic(
            api_key=self._api_key,
            base_url=self._base_url,
            http_client=self._http_client,
            max_retries=0,
            timeout=self._timeout,
        )
        try:
            yield client
        finally:
            if self._http_client is None:  # never close an injected client
                await client.close()
