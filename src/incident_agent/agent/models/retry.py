"""Retry with exponential backoff and jitter, honoring Retry-After."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from incident_agent.agent.models.base import ModelAPIError

# HTTP statuses worth retrying. 529 is Anthropic's "overloaded"; 5xx covers gateways.
RETRYABLE_STATUSES = frozenset(
    {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)


@dataclass
class RetryPolicy:
    """Bounded retries. `max_attempts` counts the first try."""

    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    rng: Callable[[], float] = field(default=random.random, repr=False)

    def backoff(self, attempt: int) -> float:
        """Delay before retry number `attempt` (1-based): half fixed, half jittered."""
        capped = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return capped / 2 + self.rng() * capped / 2

    def delay_for(self, error: ModelAPIError, attempt: int) -> float | None:
        """Seconds to wait, or None if the server asked for a wait longer than we allow."""
        if error.retry_after is None:
            return self.backoff(attempt)
        if error.retry_after > self.max_delay:
            return None
        return max(error.retry_after, 0.0) + self.rng() * 0.25  # small jitter on top


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


async def retry_async[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[T, int]:
    """Run `call` until it succeeds, a non-retryable error is raised, or attempts run out.

    Returns the result and the number of attempts used. The final error keeps its message and
    gains an `attempts` count so callers can see how hard we tried.
    """
    attempt = 1
    while True:
        try:
            return await call(), attempt
        except ModelAPIError as err:
            err.attempts = attempt
            if not err.retryable:
                raise
            delay = policy.delay_for(err, attempt) if attempt < policy.max_attempts else None
            if delay is None:
                err.args = (f"{err} (gave up after {attempt} attempt(s))",)
                raise
            await sleep(delay)
            attempt += 1
