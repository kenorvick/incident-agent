"""OpenRouter model client (OpenAI-style chat completions over httpx).

Wire format: https://openrouter.ai/docs (chat completions, tool calling, prompt caching).
- Tool calls arrive as `message.tool_calls`; results go back as `role: "tool"` messages, one
  per call, directly after the assistant message.
- Usage and cost are always returned in `usage` (no opt-in parameter exists any more). OpenRouter's
  `prompt_tokens` includes cached and cache-write tokens; we subtract them to match `Usage`.
- Anthropic prompt caching: an explicit `cache_control` breakpoint on the system prompt (the
  prefix order is tools, then system, so it covers both), plus top-level automatic
  `cache_control`, which OpenRouter/Anthropic advance to the last block of the conversation
  on every call. That is two of the four allowed breakpoints.
- Reasoning: Claude Sonnet 5.5 always reasons (billed as output). `reasoning.effort` caps it,
  and `reasoning_details` must be echoed back on assistant turns that made tool calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

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

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
PROVIDER = "openrouter"

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


# --- encoding (neutral -> wire) ---------------------------------------------------------


def cache_control(ttl: CacheTTL = "5m") -> dict[str, str]:
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def encode_messages(
    system: str, messages: Sequence[Message], *, cache_ttl: CacheTTL | None = None
) -> list[dict[str, Any]]:
    """Neutral turns to OpenAI-style messages. `cache_ttl` marks the system prompt cacheable."""
    wire: list[dict[str, Any]] = []
    if cache_ttl:
        block = {"type": "text", "text": system, "cache_control": cache_control(cache_ttl)}
        wire.append({"role": "system", "content": [block]})
    else:
        wire.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == "assistant":
            wire.extend(_encode_assistant(msg))
        else:
            wire.extend(_encode_user(msg))
    return wire


def _encode_assistant(msg: Message) -> list[dict[str, Any]]:
    text = "\n\n".join(b.text for b in msg.content if isinstance(b, TextBlock) and b.text)
    calls = [
        {
            "id": b.id,
            "type": "function",
            "function": {"name": b.name, "arguments": json.dumps(b.input)},
        }
        for b in msg.content
        if isinstance(b, ToolUseBlock)
    ]
    if not text and not calls:
        return []
    out: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        out["tool_calls"] = calls
    details = msg.provider_data.get(PROVIDER, {}).get("reasoning_details")
    if details:
        out["reasoning_details"] = details
    return [out]


def _encode_user(msg: Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            body = block.content or "(empty result)"
            if block.is_error:
                body = f"[tool error] {body}"
            out.append({"role": "tool", "tool_call_id": block.tool_use_id, "content": body})
        elif isinstance(block, TextBlock) and block.text:
            text_parts.append(block.text)
    if text_parts:  # text must follow the tool messages it accompanies
        out.append({"role": "user", "content": "\n\n".join(text_parts)})
    return out


# --- decoding (wire -> neutral) ---------------------------------------------------------


def decode_response(
    data: dict[str, Any], *, requested_model: str, latency_ms: float, cache_ttl: CacheTTL = "5m"
) -> ModelResponse:
    """Parse a 200 chat completion. Raises `ModelAPIError` for in-body provider errors."""
    if data.get("error") and not data.get("choices"):
        raise _error_from_body(502, data, None)
    choices = data.get("choices") or []
    if not choices:
        raise ModelAPIError(
            f"OpenRouter returned no choices (response id {data.get('id')}).",
            status_code=502,
            retryable=True,
        )
    choice = choices[0]
    if choice.get("error"):
        raise _error_from_body(502, {"error": choice["error"]}, None)

    message = choice.get("message") or {}
    tool_calls = [_decode_tool_call(i, tc) for i, tc in enumerate(message.get("tool_calls") or [])]
    raw_finish = choice.get("finish_reason")
    stop_reason = _FINISH_REASONS.get(raw_finish or "", "other")
    if tool_calls and stop_reason == "end_turn":
        stop_reason = "tool_use"  # some providers report "stop" alongside tool calls

    usage, cost, source = _decode_usage(data.get("usage") or {}, requested_model, cache_ttl)
    details = message.get("reasoning_details")
    return ModelResponse(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        raw_stop_reason=raw_finish,
        usage=usage,
        cost_usd=cost,
        cost_source=source,
        model=data.get("model") or requested_model,
        provider=PROVIDER,
        latency_ms=latency_ms,
        request_id=data.get("id"),
        provider_data={PROVIDER: {"reasoning_details": details}} if details else {},
    )


def _decode_tool_call(index: int, raw: dict[str, Any]) -> ToolUseBlock:
    fn = raw.get("function") or {}
    call_id = raw.get("id") or f"call_{index}"
    name = fn.get("name") or ""
    args = fn.get("arguments")
    if args is None or (isinstance(args, str) and not args.strip()):
        return ToolUseBlock(id=call_id, name=name, input={})
    if isinstance(args, dict):  # tolerate providers that skip the JSON string
        return ToolUseBlock(id=call_id, name=name, input=args)
    try:
        parsed = json.loads(args)
    except (TypeError, ValueError) as exc:
        return ToolUseBlock(
            id=call_id, name=name, parse_error=f"arguments are not valid JSON: {exc}"
        )
    if not isinstance(parsed, dict):
        return ToolUseBlock(id=call_id, name=name, parse_error="arguments are not a JSON object")
    return ToolUseBlock(id=call_id, name=name, input=parsed)


def _decode_usage(raw: dict[str, Any], model: str, cache_ttl: CacheTTL) -> tuple[Usage, float, str]:
    prompt = raw.get("prompt_tokens") or 0
    details = raw.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    written = details.get("cache_write_tokens") or 0
    usage = Usage(
        input_tokens=max(prompt - cached - written, 0),
        output_tokens=raw.get("completion_tokens") or 0,
        cache_read_tokens=cached,
        cache_write_tokens=written,
        reasoning_tokens=(raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
    )
    cost = raw.get("cost")
    if raw.get("is_byok") and not cost:  # BYOK: OpenRouter bills 0, the provider bills us
        cost = (raw.get("cost_details") or {}).get("upstream_inference_cost")
    if isinstance(cost, int | float):
        return usage, float(cost), "provider"
    return usage, estimate_cost(model, usage, cache_ttl), "estimated"


# --- errors -----------------------------------------------------------------------------

_CREDITS_URL = "https://openrouter.ai/settings/credits"


def _error_from_body(
    status: int, body: Any, headers: httpx.Headers | None, model: str = ""
) -> ModelAPIError:
    """Build a `ModelAPIError` from an HTTP status plus an OpenRouter error body."""
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    code = err.get("code")
    status = code if isinstance(code, int) else status
    message = str(err.get("message") or body)[:500]
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    raw = meta.get("raw")
    if raw and len(message) < 60:  # generic "Provider returned error": show the provider's text
        message = f"{message} ({str(raw)[:300]})"
    retry_after = parse_retry_after(headers.get("retry-after") if headers else None)

    if status == 401:
        return ModelAuthError(
            f"OpenRouter rejected the API key ({message}). "
            "Set a valid OPENROUTER_API_KEY in .env (see .env.example).",
            status_code=status,
        )
    if status == 402:
        if retry_after is not None or meta.get("limit_source") == "openrouter_in_flight_budget":
            return ModelAPIError(
                f"OpenRouter in-flight spending budget is full ({message}).",
                status_code=status,
                retryable=True,
                retry_after=retry_after,
            )
        return ModelAPIError(
            f"OpenRouter reports insufficient credits or a key limit ({message}). "
            f"Add credits at {_CREDITS_URL} or raise the key's limit.",
            status_code=status,
        )
    if status == 403:
        return ModelAPIError(
            f"OpenRouter blocked the request ({message}); check key permissions, "
            "guardrails and budgets.",
            status_code=status,
        )
    if status == 404:
        return ModelAPIError(
            f"OpenRouter could not find the model or endpoint ({message}). "
            f"Check MODEL_ID{f' (currently {model!r})' if model else ''}.",
            status_code=status,
        )
    if status == 400:
        return ModelAPIError(
            f"OpenRouter rejected the request as invalid ({message}).", status_code=status
        )
    return ModelAPIError(
        f"OpenRouter error {status}: {message}",
        status_code=status,
        retryable=status in RETRYABLE_STATUSES or status >= 500,
        retry_after=retry_after,
    )


def _error_from_response(resp: httpx.Response, model: str) -> ModelAPIError:
    try:
        body: Any = resp.json()
    except ValueError:
        body = {"error": {"message": resp.text[:200] or resp.reason_phrase}}
    return _error_from_body(resp.status_code, body, resp.headers, model)


# --- client -----------------------------------------------------------------------------


class OpenRouterClient:
    """`ModelClient` for OpenRouter. Pass `http_client` to inject a mock transport in tests."""

    provider = PROVIDER

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        effort: str | None = "medium",
        prompt_caching: bool = True,
        cache_ttl: CacheTTL = "5m",
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
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
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._timeout = timeout

    @property
    def caches(self) -> bool:
        """Cache breakpoints only apply to Anthropic models."""
        return self.prompt_caching and self.model.lstrip("~").startswith("anthropic/")

    def build_payload(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": encode_messages(
                system, messages, cache_ttl=self.cache_ttl if self.caches else None
            ),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = encode_tools(tools)
            payload["tool_choice"] = "auto"
        if self.effort:
            payload["reasoning"] = {"effort": self.effort}
        if self.caches:
            payload["cache_control"] = cache_control(self.cache_ttl)
        return payload

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        payload = self.build_payload(system, messages, tools, max_tokens)

        async def attempt() -> ModelResponse:
            start = time.perf_counter()
            data = await self._post(payload)
            latency_ms = (time.perf_counter() - start) * 1000
            return decode_response(
                data, requested_model=self.model, latency_ms=latency_ms, cache_ttl=self.cache_ttl
            )

        response, attempts = await retry_async(attempt, self._retry, self._sleep)
        response.attempts = attempts
        return response

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
        else:  # per-call client: safe across event loops, negligible next to model latency
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                yield client

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}", "X-Title": "incident-agent"}
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise ModelAPIError(f"OpenRouter request timed out: {exc!r}", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ModelAPIError(f"OpenRouter connection failed: {exc!r}", retryable=True) from exc
        if resp.status_code != 200:
            raise _error_from_response(resp, self.model)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelAPIError(
                "OpenRouter returned a non-JSON 200 response.", status_code=502, retryable=True
            ) from exc
        if not isinstance(data, dict):
            raise ModelAPIError(
                "OpenRouter returned an unexpected body.", status_code=502, retryable=True
            )
        return data
