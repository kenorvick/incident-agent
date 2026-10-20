"""Tool executors: submit_report validation, composition, and the MCP adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent_fakes import FakeExecutor, report_args

from incident_agent.agent.models.base import ToolSpec
from incident_agent.agent.report import InvestigationReport
from incident_agent.agent.tools import (
    CompositeToolExecutor,
    McpToolExecutor,
    SubmitReportTool,
    ToolExecutor,
)

pytestmark = pytest.mark.anyio


def test_submit_report_schema_is_the_report_schema():
    tool = SubmitReportTool()
    assert tool.spec.name == "submit_report"
    assert tool.spec.input_schema == InvestigationReport.model_json_schema()
    assert "tool call" in tool.spec.description


async def test_valid_report_is_stored():
    tool = SubmitReportTool()
    text, is_error = await tool.call(report_args())
    assert not is_error and "submitted" in text
    assert tool.report == InvestigationReport.model_validate(report_args())


async def test_invalid_report_lists_each_problem_and_is_not_stored():
    tool = SubmitReportTool()
    text, is_error = await tool.call({"verdict": "maybe", "confidence": "high"})
    assert is_error and tool.report is None
    assert "verdict" in text and "root_cause_found" in text
    assert "submit_report again" in text


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"causes": []}, "root_cause_found but `causes` is empty"),
        ({"verdict": "no_significant_issue"}, "no_significant_issue but `causes` is not empty"),
    ],
)
async def test_verdict_and_causes_must_agree(overrides, fragment):
    tool = SubmitReportTool()
    text, is_error = await tool.call(report_args(**overrides))
    assert is_error and fragment in text and tool.report is None


async def test_no_significant_issue_without_causes_is_valid():
    tool = SubmitReportTool()
    _, is_error = await tool.call(
        report_args(verdict="no_significant_issue", causes=[], evidence=[], confidence="medium")
    )
    assert not is_error


async def test_second_submission_keeps_the_first_report():
    tool = SubmitReportTool()
    await tool.call(report_args())
    text, is_error = await tool.call(report_args(verdict="inconclusive", causes=[]))
    assert not is_error and "already submitted" in text
    assert tool.report.verdict == "root_cause_found"


async def test_composite_routes_local_and_delegate_calls():
    remote = FakeExecutor({"run_query": "rows"})
    submit = SubmitReportTool()
    tools = CompositeToolExecutor([submit], remote)
    assert isinstance(tools, ToolExecutor)
    assert [t.name for t in tools.list_tools()] == ["run_query", "submit_report"]
    assert await tools.call("run_query", {"x": 1}) == ("rows", False)
    assert remote.calls == [("run_query", {"x": 1})]
    _, is_error = await tools.call("submit_report", report_args())
    assert not is_error and submit.report is not None


def test_composite_rejects_a_local_tool_shadowing_a_delegate_tool():
    with pytest.raises(ValueError, match="shadow"):
        CompositeToolExecutor([SubmitReportTool()], FakeExecutor({"submit_report": "x"}))


# --- MCP adapter against a stub session ----------------------------------------------------


class StubSession:
    def __init__(self, tools, result=None, error: Exception | None = None):
        self._tools, self._result, self._error = tools, result, error
        self.calls = []

    async def list_tools(self, *, params=None):
        return SimpleNamespace(tools=self._tools, next_cursor=None)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        return self._result


def mcp_tool(name="run_query", description="Run a query", schema=None):
    return SimpleNamespace(
        name=name, description=description, input_schema=schema or {"type": "object"}
    )


def text(value):
    return SimpleNamespace(type="text", text=value)


async def test_mcp_executor_converts_tool_listing():
    session = StubSession([mcp_tool(), mcp_tool("get_trace", None, {"type": "object", "x": 1})])
    executor = await McpToolExecutor.connect(session)
    assert executor.list_tools() == [
        ToolSpec(name="run_query", description="Run a query", input_schema={"type": "object"}),
        ToolSpec(name="get_trace", description="", input_schema={"type": "object", "x": 1}),
    ]


async def test_mcp_executor_returns_joined_text_content():
    result = SimpleNamespace(
        content=[text("a"), text("b")], structured_content=None, is_error=False
    )
    session = StubSession([mcp_tool()], result)
    executor = await McpToolExecutor.connect(session)
    assert await executor.call("run_query", {"q": 1}) == ("a\nb", False)
    assert session.calls == [("run_query", {"q": 1})]


async def test_mcp_executor_passes_is_error_through():
    result = SimpleNamespace(
        content=[text("field not found")], structured_content=None, is_error=True
    )
    executor = await McpToolExecutor.connect(StubSession([mcp_tool()], result))
    assert await executor.call("run_query", {}) == ("field not found", True)


async def test_mcp_executor_turns_transport_errors_into_error_results():
    session = StubSession([mcp_tool()], error=ConnectionError("pipe closed"))
    executor = await McpToolExecutor.connect(session)
    out, is_error = await executor.call("run_query", {})
    assert is_error and "ConnectionError: pipe closed" in out


async def test_mcp_executor_describes_empty_and_non_text_results():
    image = SimpleNamespace(type="image")
    empty = SimpleNamespace(content=[], structured_content=None, is_error=False)
    only_image = SimpleNamespace(content=[image], structured_content=None, is_error=False)
    assert await (await McpToolExecutor.connect(StubSession([mcp_tool()], empty))).call(
        "t", {}
    ) == (
        "(tool returned no output)",
        False,
    )
    out, _ = await (await McpToolExecutor.connect(StubSession([mcp_tool()], only_image))).call(
        "t", {}
    )
    assert "image" in out


# --- citation verification (production guardrail) -------------------------------------------

RESULT = "services: checkout error_rate 45.2% count 1,234\nstatus_message: Invalid token"


def cite(ref, *values, claim="a claim"):
    return {"claim": claim, "tool_call_id": ref, "cited_values": list(values)}


def checking(**kwargs):
    events = []
    tool = SubmitReportTool(verify_citations=True, on_check=events.append, **kwargs)
    tool.note_result("c1", RESULT)
    tool.note_result("c2", "p95_ms 1203.4 for POST /api/checkout")
    return tool, events


async def test_verified_citations_are_accepted():
    tool, events = checking()
    evidence = [cite("c1", "45.2%", "Invalid token"), cite("[ref c2]", "1203.4")]
    text, is_error = await tool.call(report_args(evidence=evidence))
    assert not is_error and tool.report is not None
    assert [(e["attempt"], e["ok"], e["forced"]) for e in events] == [(1, True, False)]


async def test_unverified_value_is_rejected_with_where_it_was_found():
    tool, events = checking()
    evidence = [cite("c1", "45.2%", "1203.4"), cite("c2", "checkout error_rate 45.2%")]
    text, is_error = await tool.call(report_args(evidence=evidence))
    assert is_error and tool.report is None
    assert "'1203.4' does not appear in the result of `c1`" in text and "appear in: c2" in text
    assert "'checkout error_rate 45.2%' does not appear in the result of `c2`" in text
    assert "45.2%'" not in text.split("c1`")[0]  # verified values are not listed as problems
    assert "Refs you have: c1, c2" in text and "1 more rejection(s)" in text
    assert events[0]["ok"] is False and len(events[0]["problems"]) == 2


async def test_unknown_ref_is_rejected_even_without_values():
    tool, _ = checking()
    text, is_error = await tool.call(report_args(evidence=[cite("c9"), cite("toolu_01abc")]))
    assert is_error and "`c9` is not a tool call you made" in text and "`toolu_01abc`" in text


async def test_third_submission_is_accepted_as_it_is_and_logged_as_forced():
    tool, events = checking(max_citation_rejections=2)
    bad = report_args(evidence=[cite("c1", "99.9%")])
    for _ in range(2):
        _, is_error = await tool.call(bad)
        assert is_error and tool.report is None
    text, is_error = await tool.call(bad)
    assert not is_error and tool.report is not None and "submitted" in text
    assert [(e["attempt"], e["ok"], e["forced"]) for e in events] == [
        (1, False, False),
        (2, False, False),
        (3, False, True),
    ]


async def test_zero_allowed_rejections_never_blocks():
    tool, events = checking(max_citation_rejections=0)
    _, is_error = await tool.call(report_args(evidence=[cite("c1", "99.9%")]))
    assert not is_error and events[0]["forced"] is True


async def test_verification_is_off_by_default_and_no_evidence_always_passes():
    tool = SubmitReportTool()
    _, is_error = await tool.call(report_args(evidence=[cite("whatever", "nope")]))
    assert not is_error
    tool, _ = checking()
    _, is_error = await tool.call(report_args(evidence=[]))
    assert not is_error
