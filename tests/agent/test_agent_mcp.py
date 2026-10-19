"""The loop end to end through a real MCP `ClientSession` (in-memory transport, no subprocess)."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from agent_fakes import read_log, report_args
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.report import TerminatedReason
from incident_agent.agent.tools import McpToolExecutor

pytestmark = pytest.mark.anyio


def build_stub_server() -> MCPServer:
    """A minimal stand-in for the telemetry server."""
    server = MCPServer("stub-telemetry")

    @server.tool()
    def describe_dataset() -> str:
        """Describe the dataset."""
        return "services: checkout, payment"

    @server.tool()
    def run_query(group_by: str, limit: int = 5) -> str:
        """Count rows grouped by a field."""
        if group_by != "service.name":
            raise ToolError("field `usr_id` not found; did you mean `service.name`?")
        return f"{group_by}: payment=12 checkout=30 (limit {limit})"

    return server


@asynccontextmanager
async def stub_session():
    async with Client(build_stub_server()) as client:
        yield client.session


async def test_investigation_through_a_real_mcp_session(tmp_path):
    model = ScriptedModelClient(
        [
            tool_call_response(
                ("describe_dataset", {}, "t1"),
                ("run_query", {"group_by": "service.name"}, "t2"),
                ("run_query", {"group_by": "usr_id"}, "t3"),  # server-side error
                ("run_query", {"limit": "not-a-number"}, "t4"),  # schema violation
            ),
            tool_call_response(("submit_report", report_args(), "t5")),
        ]
    )
    async with stub_session() as session:
        executor = await McpToolExecutor.connect(session)
        assert {t.name for t in executor.list_tools()} == {"describe_dataset", "run_query"}
        result = await investigate(
            "alert",
            tools=executor,
            model=model,
            config=AgentConfig(),
            log_path=tmp_path / "run.jsonl",
            run_id="mcp",
        )

    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert [t.name for t in model.requests[0].tools] == [
        "describe_dataset",
        "run_query",
        "submit_report",
    ]
    schema = next(t for t in model.requests[0].tools if t.name == "run_query").input_schema
    assert "group_by" in schema["properties"]

    r1, r2, r3, r4 = model.requests[1].tool_results
    assert (r1.content, r1.is_error) == ("services: checkout, payment", False)
    assert "payment=12 checkout=30" in r2.content and not r2.is_error
    assert r3.is_error and "did you mean `service.name`" in r3.content
    assert r4.is_error  # an invalid call is an error result, not a crash

    logged = {
        e["tool_call_id"]: e for e in read_log(tmp_path / "run.jsonl") if e["type"] == "tool_result"
    }
    assert logged["t2"]["content"] == r2.content


def _real_open_session():
    if importlib.util.find_spec("incident_agent.mcp_server.server") is None:
        return None
    from incident_agent.mcp_server import server

    return getattr(server, "open_session", None)


@pytest.mark.skipif(_real_open_session() is None, reason="mcp_server.open_session not built yet")
async def test_investigation_through_the_real_open_session(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1] / "backends"))
    from telemetry_rows import make_rows, write_events

    fixtures = tmp_path / "fixtures"
    write_events(fixtures / "tiny" / "events.jsonl.gz", make_rows())

    model = ScriptedModelClient(
        [
            tool_call_response(("describe_dataset", {}, "t1")),
            tool_call_response(("submit_report", report_args(), "t2")),
        ]
    )
    async with _real_open_session()("tiny", fixtures_dir=fixtures) as session:
        executor = await McpToolExecutor.connect(session)
        names = {t.name for t in executor.list_tools()}
        assert {"describe_dataset", "run_query", "find_outliers", "get_trace"} <= names
        result = await investigate(
            "payment errors",
            tools=executor,
            model=model,
            config=AgentConfig(),
            log_path=tmp_path / "run.jsonl",
            run_id="real",
            scenario_id="tiny",
        )
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    (describe,) = model.requests[1].tool_results
    assert not describe.is_error and "payment" in describe.content
