"""Structured step log: one JSON object per line, flushed after every event.

The event types and fields are a contract with the eval harness (`evals/logs.py` reads them).
`tool_result.content` is the exact text the model saw, which the anti-fabrication check relies on;
it starts with the `[ref cN]` line, and `tool_result.ref` is that `cN` (absent in older logs).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from incident_agent.agent.models.base import ModelResponse


def approx_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token); good enough for sizing and logs."""
    return (len(text) + 3) // 4


class StepLogger:
    """Appends events to a JSONL file. A crash keeps everything written so far."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def _write(self, event_type: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "type": event_type}
        record.update(fields)
        self._fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._fh.flush()

    def run_start(
        self,
        *,
        run_id: str,
        scenario_id: str | None,
        alert: str,
        provider: str | None,
        model_id: str | None,
        config: dict[str, Any],
    ) -> None:
        self._write(
            "run_start",
            run_id=run_id,
            scenario_id=scenario_id,
            alert=alert,
            provider=provider,
            model_id=model_id,
            config=config,
        )

    def model_call(self, step: int, response: ModelResponse) -> None:
        usage = response.usage
        self._write(
            "model_call",
            step=step,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=response.cost_usd,
            latency_ms=round(response.latency_ms, 1),
            stop_reason=response.stop_reason,
            text=response.text,
            tool_calls=[
                {"id": c.id, "name": c.name, "arguments": c.input} for c in response.tool_calls
            ],
        )

    def tool_result(
        self,
        *,
        step: int,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
        content: str,
        is_error: bool,
        latency_ms: float,
        truncated_by_harness: bool = False,
        ref: str | None = None,
    ) -> None:
        self._write(
            "tool_result",
            step=step,
            tool_call_id=tool_call_id,
            ref=ref,
            name=name,
            arguments=arguments,
            is_error=is_error,
            result_chars=len(content),
            approx_tokens=approx_tokens(content),
            truncated_by_harness=truncated_by_harness,
            latency_ms=round(latency_ms, 1),
            content=content,
        )

    def context_event(self, kind: Literal["truncate", "clear", "compact"], details: Any) -> None:
        self._write("context_event", kind=kind, details=details)

    def final(self, report: dict[str, Any], terminated_reason: str) -> None:
        self._write("final", report=report, terminated_reason=terminated_reason)

    def run_end(
        self,
        *,
        steps: int,
        tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        self._write(
            "run_end",
            steps=steps,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
