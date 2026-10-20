"""Test doubles for the agent loop: a fake `ToolExecutor` and report-building helpers."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from incident_agent.agent.models.base import ToolSpec
from incident_agent.agent.tools import ToolOutput

Handler = Callable[[dict[str, Any]], ToolOutput | Awaitable[ToolOutput]]


def spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name} tool", input_schema={"type": "object"})


class FakeExecutor:
    """Serves named tools; a handler is a string, a `(text, is_error)` pair or a callable."""

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {"describe_dataset": "services: checkout, payment"}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolSpec]:
        return [spec(n) for n in self.handlers]

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutput:
        self.calls.append((name, args))
        handler = self.handlers[name]
        if callable(handler):
            handler = handler(args)
            if hasattr(handler, "__await__"):
                handler = await handler
        if isinstance(handler, str):
            return handler, False
        return handler


def report_args(**overrides: Any) -> dict[str, Any]:
    """A valid `submit_report` payload."""
    args: dict[str, Any] = {
        "verdict": "root_cause_found",
        "causes": [
            {
                "service": "payment",
                "category": "dependency_failure",
                "summary": "adyen calls time out",
            }
        ],
        "evidence": [],  # citations are verified against real results; tests add their own
        "confidence": "high",
        "uncertainties": [],
    }
    args.update(overrides)
    return args


def read_log(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
