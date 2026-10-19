"""Neutral types, price table, and retry helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from pydantic import TypeAdapter

from incident_agent.agent.models.base import (
    ContentBlock,
    Message,
    ModelAPIError,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from incident_agent.agent.models.pricing import (
    FALLBACK_PRICE,
    estimate_cost,
    normalize_model_id,
    price_for,
)
from incident_agent.agent.models.retry import RetryPolicy, parse_retry_after, retry_async

pytestmark = pytest.mark.anyio


def test_message_constructors_and_json_round_trip():
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="hi"),
            ToolUseBlock(id="t1", name="x", input={"a": [1, 2]}),
        ],
        provider_data={"anthropic": {"thinking_blocks": []}},
    )
    assert Message.model_validate_json(msg.model_dump_json()) == msg
    assert Message.user("q").role == "user"
    results = Message.tool_results([ToolResultBlock(tool_use_id="t1", content="r", is_error=True)])
    assert results.role == "user" and results.content[0].is_error  # type: ignore[union-attr]
    parsed = TypeAdapter(ContentBlock).validate_python(
        {"type": "tool_result", "tool_use_id": "a", "content": "b"}
    )
    assert isinstance(parsed, ToolResultBlock) and parsed.is_error is False


def test_response_to_message_keeps_text_calls_and_provider_data():
    resp = ModelResponse(
        text="plan",
        tool_calls=[ToolUseBlock(id="t1", name="x", input={})],
        provider_data={"openrouter": {"reasoning_details": [{"a": 1}]}},
    )
    msg = resp.to_message()
    assert msg.role == "assistant"
    assert [b.type for b in msg.content] == ["text", "tool_use"]
    assert msg.provider_data == resp.provider_data
    assert ModelResponse().to_message().content == []


def test_usage_total_input_includes_cache_tokens():
    usage = Usage(input_tokens=10, cache_read_tokens=100, cache_write_tokens=1000, output_tokens=5)
    assert usage.total_input_tokens == 1110


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-5.5",
        "claude-sonnet-5-5",
        "~anthropic/claude-sonnet-5.5",
        "anthropic/claude-sonnet-5.5-20260928",
    ],
)
def test_model_id_normalization(model):
    assert normalize_model_id(model) == "claude-sonnet-5-5"
    assert price_for(model)[1] is True


def test_cost_estimate_matches_the_published_rates():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    # Sonnet 5.5: $2 in + $10 out + $0.10 cache read + $2.50 (5m) / $4 (1h) cache write
    assert estimate_cost("claude-sonnet-5-5", usage) == pytest.approx(14.60)
    assert estimate_cost("claude-sonnet-5-5", usage, "1h") == pytest.approx(16.10)


def test_unknown_models_are_priced_high_never_free():
    price, known = price_for("someone/new-model")
    assert not known and price is FALLBACK_PRICE
    assert estimate_cost("someone/new-model", Usage(output_tokens=1_000_000)) == pytest.approx(50.0)


def test_backoff_doubles_and_caps_with_jitter_inside_bounds():
    low = RetryPolicy(base_delay=1, max_delay=10, rng=lambda: 0.0)
    high = RetryPolicy(base_delay=1, max_delay=10, rng=lambda: 1.0)
    assert [low.backoff(n) for n in (1, 2, 3, 4, 5)] == [0.5, 1, 2, 4, 5]
    assert [high.backoff(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 8, 10]


def test_parse_retry_after_variants():
    assert parse_retry_after("7") == 7
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after(None) is None and parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= parse_retry_after(future) <= 30  # type: ignore[operator]
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(past) == 0


async def test_retry_async_only_retries_retryable_errors():
    delays: list[float] = []

    async def sleep(s: float) -> None:
        delays.append(s)

    calls = 0

    async def fatal() -> str:
        nonlocal calls
        calls += 1
        raise ModelAPIError("no", status_code=400)

    with pytest.raises(ModelAPIError):
        await retry_async(fatal, RetryPolicy(rng=lambda: 0), sleep)
    assert calls == 1 and delays == []

    async def other() -> str:
        raise KeyError("not ours")  # unexpected errors pass straight through

    with pytest.raises(KeyError):
        await retry_async(other, RetryPolicy(), sleep)
