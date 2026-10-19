"""Spend guard: a persistent cost ledger and a client wrapper that enforces a hard cap.

The ledger is an append-only JSONL file (`.cache/spend-ledger.jsonl`, gitignored), so spend
survives process restarts and is shared by every process that points at the same file. Each
call checks the file before it is made, which means concurrent calls can overshoot the cap by
at most the cost of the calls already in flight.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from incident_agent.agent.models.base import (
    BudgetExceededError,
    Message,
    ModelClient,
    ModelConfigError,
    ModelResponse,
    ToolSpec,
)

__all__ = [
    "BudgetExceededError",
    "BudgetedModelClient",
    "LedgerEntry",
    "SpendLedger",
    "SpendSummary",
    "spend_label",
]

log = logging.getLogger(__name__)

DEFAULT_CAP_USD = 10.0
REPO_ROOT = Path(__file__).resolve().parents[4]
_current_label: ContextVar[str | None] = ContextVar("spend_label", default=None)


def default_ledger_path() -> Path:
    """`SPEND_LEDGER_PATH`, else `<repo>/.cache/spend-ledger.jsonl` regardless of the cwd."""
    if override := os.environ.get("SPEND_LEDGER_PATH"):
        return Path(override)
    root = REPO_ROOT if (REPO_ROOT / "pyproject.toml").exists() else Path.cwd()
    return root / ".cache" / "spend-ledger.jsonl"


def cap_from_env() -> float:
    """`SPEND_CAP_USD` (default 10.0). Blank means default; garbage is an error."""
    raw = os.environ.get("SPEND_CAP_USD", "").strip()
    if not raw:
        return DEFAULT_CAP_USD
    try:
        cap = float(raw)
    except ValueError:
        raise ModelConfigError(f"SPEND_CAP_USD must be a number, got {raw!r}.") from None
    if cap < 0:
        raise ModelConfigError("SPEND_CAP_USD must not be negative.")
    return cap


@contextmanager
def spend_label(label: str) -> Iterator[None]:
    """Tag every ledger entry made inside the block (and its asyncio tasks) with `label`,
    for example `"<run_id>/<scenario>/<n>"`."""
    token = _current_label.set(label)
    try:
        yield
    finally:
        _current_label.reset(token)


class LedgerEntry(BaseModel):
    ts: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float
    cost_source: str = "estimated"
    label: str | None = None


class SpendSummary(BaseModel):
    spent_usd: float
    cap_usd: float
    remaining_usd: float
    calls: int
    by_label: dict[str, float] = Field(default_factory=dict)
    by_model: dict[str, float] = Field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"spent ${self.spent_usd:.4f} of ${self.cap_usd:.2f} cap "
            f"(${self.remaining_usd:.4f} remaining, {self.calls} calls)"
        )


class SpendLedger:
    """Append-only spend log with a cap. Reads the file on every query so it is always current."""

    def __init__(self, path: Path | str | None = None, cap_usd: float | None = None) -> None:
        self.path = Path(path) if path else default_ledger_path()
        self.cap_usd = cap_from_env() if cap_usd is None else cap_usd

    def entries(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        out: list[LedgerEntry] = []
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    out.append(LedgerEntry.model_validate_json(line))
                except ValidationError:
                    log.warning("Skipping corrupt line %d in %s", lineno, self.path)
        return out

    def total(self) -> float:
        return sum(e.cost_usd for e in self.entries())

    def remaining(self) -> float:
        return max(self.cap_usd - self.total(), 0.0)

    def check(self) -> None:
        """Raise `BudgetExceededError` if cumulative spend has reached the cap."""
        spent = self.total()
        if spent >= self.cap_usd:
            raise BudgetExceededError(
                f"Spend cap reached: ${spent:.4f} spent of ${self.cap_usd:.2f} "
                f"(ledger: {self.path}). No API call was made. Raise SPEND_CAP_USD in .env "
                "only if you really intend to spend more."
            )

    def record(self, response: ModelResponse, label: str | None = None) -> LedgerEntry:
        entry = LedgerEntry(
            ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            provider=response.provider,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            cost_usd=response.cost_usd,
            cost_source=response.cost_source,
            label=label if label is not None else _current_label.get(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:  # one write per line: append-safe
            fh.write(entry.model_dump_json() + "\n")
        return entry

    def summary(self, label_prefix: str | None = None) -> SpendSummary:
        """Totals for the whole ledger, or only entries whose label starts with `label_prefix`
        (for example one eval run id). `remaining_usd` is always against the global cap."""
        entries = self.entries()
        total_all = sum(e.cost_usd for e in entries)
        if label_prefix is not None:
            entries = [e for e in entries if (e.label or "").startswith(label_prefix)]
        by_label: dict[str, float] = defaultdict(float)
        by_model: dict[str, float] = defaultdict(float)
        for e in entries:
            by_label[e.label or "(unlabelled)"] += e.cost_usd
            by_model[e.model] += e.cost_usd
        return SpendSummary(
            spent_usd=sum(e.cost_usd for e in entries),
            cap_usd=self.cap_usd,
            remaining_usd=max(self.cap_usd - total_all, 0.0),
            calls=len(entries),
            by_label=dict(by_label),
            by_model=dict(by_model),
        )

    def describe(self) -> str:
        """One line for the eval runner: global spend so far and remaining."""
        return str(self.summary())


class BudgetedModelClient:
    """Wraps any `ModelClient`: checks the cap before each call and records the cost after."""

    def __init__(
        self, inner: ModelClient, ledger: SpendLedger | None = None, label: str | None = None
    ) -> None:
        self.inner = inner
        self.ledger = ledger or SpendLedger()
        self.label = label

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        self.ledger.check()
        response = await self.inner.complete(system, messages, tools, max_tokens)
        self.ledger.record(response, self.label)
        return response
