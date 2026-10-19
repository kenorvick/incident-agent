"""Per-token price table and cost estimation.

Prices are USD per million tokens, taken from Anthropic's pricing page
(platform.claude.com/docs/en/about-claude/pricing) and cross-checked against OpenRouter's
public model listing on 2026-10-19. OpenRouter reports its own `cost`, which always wins;
this table backs the direct Anthropic client and the fallback when a cost is missing.
Unknown models are priced at a deliberately high rate so the spend guard errs on the safe side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from incident_agent.agent.models.base import Usage

CacheTTL = Literal["5m", "1h"]


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.10, 2.50, 4.0),
    "claude-sonnet-5": Price(2.0, 10.0, 0.20, 2.50, 4.0),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20, 5.0, 8.0),
    # Haiku 5.5 rates for prompts up to 100k tokens.
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-fable-5-1": Price(10.0, 50.0, 0.25, 12.5, 20.0),
}

# Priciest known rates; used for models not in the table.
FALLBACK_PRICE = PRICES["claude-fable-5-1"]


def normalize_model_id(model: str) -> str:
    """`anthropic/claude-sonnet-5.5` and `claude-sonnet-5-5` both become `claude-sonnet-5-5`."""
    name = model.strip().removeprefix("~").removeprefix("anthropic/").replace(".", "-")
    return re.sub(r"-\d{8}$", "", name)


def price_for(model: str) -> tuple[Price, bool]:
    """Return the price and whether the model was found in the table."""
    price = PRICES.get(normalize_model_id(model))
    return (price, True) if price else (FALLBACK_PRICE, False)


def estimate_cost(model: str, usage: Usage, cache_ttl: CacheTTL = "5m") -> float:
    """Estimated USD cost of one call from its token usage."""
    price, _ = price_for(model)
    write_rate = price.cache_write_1h if cache_ttl == "1h" else price.cache_write_5m
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * write_rate
    )
    return total / 1_000_000
