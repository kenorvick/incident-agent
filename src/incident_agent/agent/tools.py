"""Tool execution for the agent loop.

`ToolExecutor` is all the loop knows about tools. `McpToolExecutor` serves the MCP server's
telemetry tools; harness-local tools (today `submit_report`, later memory) implement `LocalTool`
and are merged with the MCP tools by `CompositeToolExecutor`. Every failure (validation, protocol,
transport) comes back as an error result the model can read and react to, never as an exception.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from incident_agent.agent.models.base import ToolSpec
from incident_agent.agent.report import InvestigationReport, Verdict

SUBMIT_REPORT = "submit_report"

ToolOutput = tuple[str, bool]  # (text shown to the model, is_error)


def call_ref(n: int) -> str:
    """The short, citable reference of the n-th tool call (1-based, in call order, per
    investigation). The provider's own tool-use IDs are opaque and never shown to the model as
    text, so the harness gives each call this ref and puts it on the first line of the result."""
    return f"c{n}"


def with_ref(ref: str, text: str) -> str:
    """A tool result as the model sees it: the ref line, then the tool's own output."""
    return f"[ref {ref}]\n{text}"


@runtime_checkable
class ToolExecutor(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutput: ...


class LocalTool(Protocol):
    """A tool implemented in the harness rather than served over MCP."""

    @property
    def spec(self) -> ToolSpec: ...

    async def call(self, args: dict[str, Any]) -> ToolOutput: ...


# --- MCP ---------------------------------------------------------------------------------


def tool_spec_from_mcp(tool: Any) -> ToolSpec:
    return ToolSpec(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.input_schema or {"type": "object", "properties": {}},
    )


def _result_text(result: Any) -> str:
    """Text content of a `CallToolResult`, falling back to structured content."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else f"[{block.type} content omitted]")
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    return json.dumps(structured) if structured else ""


class McpToolExecutor:
    """Calls tools on an MCP `ClientSession`. Build with `await McpToolExecutor.connect(session)`
    so the tool listing is fetched once (the tool set is fixed for an investigation)."""

    def __init__(self, session: Any, tools: Sequence[ToolSpec]) -> None:
        self._session = session
        self._tools = list(tools)

    @classmethod
    async def connect(cls, session: Any) -> McpToolExecutor:
        specs: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            params = None
            if cursor:
                from mcp.types import PaginatedRequestParams

                params = PaginatedRequestParams(cursor=cursor)
            listing = await session.list_tools(params=params)
            specs.extend(tool_spec_from_mcp(t) for t in listing.tools)
            cursor = listing.next_cursor
            if not cursor:
                return cls(session, specs)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools)

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutput:
        try:
            result = await self._session.call_tool(name, args)
        except Exception as exc:  # MCPError, transport closed, timeout: let the model react
            return f"Tool call failed: {type(exc).__name__}: {exc}", True
        text = _result_text(result)
        is_error = bool(getattr(result, "is_error", False))
        if not text:
            text = "Tool reported an error." if is_error else "(tool returned no output)"
        return text, is_error


# --- harness-local tools -----------------------------------------------------------------


class SubmitReportTool:
    """`submit_report`: validates the final answer. One instance per investigation; the loop
    ends once `report` is set. An invalid submission returns the validation problems so the
    model can fix and resubmit."""

    def __init__(self) -> None:
        self.report: InvestigationReport | None = None
        self._spec = ToolSpec(
            name=SUBMIT_REPORT,
            description=(
                "Submit the final investigation report. This is how you finish: call it exactly "
                "once, when you have a conclusion (a root cause, no significant issue, or "
                "inconclusive). Cite each piece of evidence by the ref of the tool call that "
                "produced it (the `[ref cN]` line at the top of its result), copying values "
                "exactly. Invalid reports are rejected with the reasons; fix them and call it "
                "again."
            ),
            input_schema=InvestigationReport.model_json_schema(),
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def call(self, args: dict[str, Any]) -> ToolOutput:
        if self.report is not None:
            return "A report was already submitted; nothing more to do.", False
        try:
            report = InvestigationReport.model_validate(args)
        except ValidationError as exc:
            return _validation_message(exc), True
        if problem := _consistency_problem(report):
            return f"Invalid report: {problem} Fix it and call {SUBMIT_REPORT} again.", True
        self.report = report
        return "Report submitted. The investigation is complete.", False


def _validation_message(exc: ValidationError) -> str:
    lines = [
        f"- {'.'.join(str(p) for p in err['loc']) or '(report)'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return (
        f"Invalid report, not submitted ({len(lines)} problem(s)):\n"
        + "\n".join(lines)
        + f"\nFix these and call {SUBMIT_REPORT} again."
    )


def _consistency_problem(report: InvestigationReport) -> str | None:
    if report.verdict == Verdict.ROOT_CAUSE_FOUND and not report.causes:
        return "verdict is root_cause_found but `causes` is empty."
    if report.verdict == Verdict.NO_SIGNIFICANT_ISSUE and report.causes:
        return "verdict is no_significant_issue but `causes` is not empty."
    return None


# --- composition -------------------------------------------------------------------------


class CompositeToolExecutor:
    """Harness-local tools plus a delegate executor (usually MCP). Local names must be unique
    and must not shadow the delegate's tools."""

    def __init__(self, local: Sequence[LocalTool], delegate: ToolExecutor) -> None:
        self._local = {t.spec.name: t for t in local}
        if len(self._local) != len(local):
            raise ValueError("duplicate harness-local tool names")
        self._delegate = delegate
        clash = self._local.keys() & {t.name for t in delegate.list_tools()}
        if clash:
            raise ValueError(f"harness-local tools shadow delegate tools: {sorted(clash)}")

    def list_tools(self) -> list[ToolSpec]:
        return self._delegate.list_tools() + [t.spec for t in self._local.values()]

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutput:
        if (tool := self._local.get(name)) is not None:
            return await tool.call(args)
        return await self._delegate.call(name, args)
