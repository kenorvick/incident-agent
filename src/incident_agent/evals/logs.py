"""Parse an agent step log (JSONL) into typed records.

The loop builder owns the log format (see the contracts); this reader is deliberately tolerant:
unknown event types become `UnknownRecord`, extra fields are kept, null fields fall back to
defaults, and lines that are not valid JSON (a crashed run can leave a truncated last line) are
counted in `StepLog.skipped_lines` instead of raising.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

__all__ = [
    "ContextEvent",
    "Final",
    "LogRecord",
    "ModelCall",
    "RunEnd",
    "RunStart",
    "StepLog",
    "ToolCallRef",
    "ToolResult",
    "UnknownRecord",
    "parse_log",
    "parse_log_lines",
]


class LogRecord(BaseModel):
    """Common shape of every record. Unknown fields are kept as extras."""

    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    type: str = "unknown"

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        """A null field means 'absent': let the default apply instead of failing validation."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class ToolCallRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    arguments: Any = None


class RunStart(LogRecord):
    run_id: str = ""
    scenario_id: str = ""
    alert: str = ""
    provider: str | None = None
    model_id: str | None = None
    config: dict[str, Any] = {}


class ModelCall(LogRecord):
    step: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None
    stop_reason: str | None = None
    text: str = ""
    tool_calls: list[ToolCallRef] = []

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class ToolResult(LogRecord):
    step: int | None = None
    tool_call_id: str = ""
    name: str = ""
    arguments: Any = None
    is_error: bool = False
    result_chars: int | None = None
    approx_tokens: int | None = None
    truncated_by_harness: bool = False
    latency_ms: float | None = None
    content: str = ""

    @field_validator("content", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str, sort_keys=True)


class ContextEvent(LogRecord):
    kind: str = ""
    details: Any = None


class Final(LogRecord):
    report: dict[str, Any] = {}
    terminated_reason: str | None = None


class RunEnd(LogRecord):
    steps: int | None = None
    tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: float | None = None


class UnknownRecord(LogRecord):
    """An event type this reader does not know (or a known one that failed validation)."""


_BY_TYPE: dict[str, type[LogRecord]] = {
    "run_start": RunStart,
    "model_call": ModelCall,
    "tool_result": ToolResult,
    "context_event": ContextEvent,
    "final": Final,
    "run_end": RunEnd,
}


@dataclass
class StepLog:
    """All records of one run, in file order, with typed accessors."""

    records: list[LogRecord] = field(default_factory=list)
    skipped_lines: int = 0
    missing: bool = False  # the log file did not exist

    @property
    def run_start(self) -> RunStart | None:
        return next((r for r in self.records if isinstance(r, RunStart)), None)

    @property
    def run_end(self) -> RunEnd | None:
        return next((r for r in reversed(self.records) if isinstance(r, RunEnd)), None)

    @property
    def final(self) -> Final | None:
        return next((r for r in reversed(self.records) if isinstance(r, Final)), None)

    @property
    def model_calls(self) -> list[ModelCall]:
        return [r for r in self.records if isinstance(r, ModelCall)]

    @property
    def tool_results(self) -> list[ToolResult]:
        return [r for r in self.records if isinstance(r, ToolResult)]

    @property
    def context_events(self) -> list[ContextEvent]:
        return [r for r in self.records if isinstance(r, ContextEvent)]

    def tool_results_by_id(self) -> dict[str, list[ToolResult]]:
        out: dict[str, list[ToolResult]] = {}
        for tr in self.tool_results:
            out.setdefault(tr.tool_call_id, []).append(tr)
        return out

    def wall_clock_ms(self) -> float | None:
        """Span between the first and last timestamp, or None if fewer than two parse."""
        stamps = [t for r in self.records if (t := _parse_ts(r.ts)) is not None]
        if len(stamps) < 2:
            return None
        return (max(stamps) - min(stamps)).total_seconds() * 1000


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_log_lines(lines: Iterable[str]) -> StepLog:
    log = StepLog()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            log.skipped_lines += 1
            continue
        if not isinstance(raw, dict):
            log.skipped_lines += 1
            continue
        model = _BY_TYPE.get(str(raw.get("type")), UnknownRecord)
        try:
            log.records.append(model.model_validate(raw))
        except ValidationError:
            log.records.append(UnknownRecord.model_validate(raw))
    return log


def parse_log(path: Path | str, *, missing_ok: bool = False) -> StepLog:
    """Read a step-log file. With `missing_ok`, a missing file yields an empty log flagged
    `missing` (used by scoring so a run without a log scores as ungrounded, not as a crash)."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as fh:
            return parse_log_lines(fh)
    except FileNotFoundError:
        if not missing_ok:
            raise
        return StepLog(missing=True)
