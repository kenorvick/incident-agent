"""MCP tools, one module per tool. `TOOLS` is the ordered registry the server publishes."""

from incident_agent.mcp_server.tools import (
    compare_windows,
    describe_dataset,
    find_outliers,
    get_trace,
    run_query,
)
from incident_agent.mcp_server.tools._common import ToolDef, execute

TOOLS: list[ToolDef] = [
    describe_dataset.TOOL,
    run_query.TOOL,
    find_outliers.TOOL,
    get_trace.TOOL,
    compare_windows.TOOL,
]

__all__ = ["TOOLS", "ToolDef", "execute"]
