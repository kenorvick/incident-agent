"""The server over the real MCP protocol (in-memory transport) via `open_session`."""

import asyncio
import json

from incident_agent.mcp_server.server import build_server, open_session

TOOLS = {"describe_dataset", "run_query", "find_outliers", "get_trace", "compare_windows"}


def run(coro):
    return asyncio.run(coro)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


def test_list_tools_annotations_and_schemas(fixtures_dir):
    async def main():
        async with open_session("tiny", fixtures_dir=fixtures_dir) as session:
            return (await session.list_tools()).tools

    tools = {t.name: t for t in run(main())}
    assert set(tools) == TOOLS
    for t in tools.values():
        ann = t.annotations
        assert ann.read_only_hint is True and ann.idempotent_hint is True
        assert ann.destructive_hint is False
        assert t.description and len(t.description) > 100
        assert t.input_schema["type"] == "object"
    rq = tools["run_query"].input_schema
    assert rq["properties"]["limit"]["maximum"] == 50 and rq["properties"]["limit"]["minimum"] == 1
    assert rq["properties"]["granularity_seconds"]["minimum"] == 1
    calc_op = rq["properties"]["calculations"]["items"]["properties"]["op"]
    assert "P95" in calc_op["enum"] and "ERROR_RATE" in calc_op["enum"]
    filter_op = rq["properties"]["filters"]["items"]["properties"]["op"]
    assert "in" in filter_op["enum"] and "contains" in filter_op["enum"]
    assert rq.get("required", []) == []
    assert tools["find_outliers"].input_schema["required"] == ["selection"]
    assert tools["get_trace"].input_schema["required"] == ["trace_id"]
    assert "required" not in tools["describe_dataset"].input_schema  # no required args
    # schemas are slimmed: no titles, no null branches
    blob = json.dumps([t.input_schema for t in tools.values()])
    assert '"title"' not in blob and '"type": "null"' not in blob
    assert "x-field-name" not in blob


def test_every_tool_is_callable_through_mcp(fixtures_dir):
    trace_id = f"{0xABC000 + 7:032x}"
    calls = [
        ("describe_dataset", {}),
        ("run_query", {"group_by": ["service.name"]}),
        ("find_outliers", {"selection": [{"field": "error", "value": True}]}),
        ("get_trace", {"trace_id": trace_id}),
        ("compare_windows", {"at": "21:04:30"}),
    ]

    async def main():
        async with open_session("tiny", fixtures_dir=fixtures_dir) as session:
            return [(n, await session.call_tool(n, a)) for n, a in calls]

    for name, result in run(main()):
        assert result.is_error is False, (name, text_of(result))
        assert len(result.content) == 1 and result.content[0].type == "text"
        assert "<telemetry_data>" in text_of(result)


def test_errors_are_results_not_protocol_failures(fixtures_dir):
    async def main():
        async with open_session("tiny", fixtures_dir=fixtures_dir) as session:
            out = {
                "field": await session.call_tool("run_query", {"group_by": ["service.nam"]}),
                "schema": await session.call_tool("run_query", {"limit": 9999}),
                "unknown_arg": await session.call_tool("run_query", {"group": "x"}),
                "unknown_tool": await session.call_tool("run_querry", {}),
                "missing": await session.call_tool("find_outliers", {}),
            }
            # the session is still alive after every error
            out["after"] = await session.call_tool("describe_dataset", {})
            return out

    r = run(main())
    assert r["field"].is_error and "`service.name`" in text_of(r["field"])
    assert r["schema"].is_error and "limit" in text_of(r["schema"])
    assert "errors.pydantic.dev" not in text_of(r["schema"])
    assert r["unknown_arg"].is_error and "Valid arguments:" in text_of(r["unknown_arg"])
    assert r["unknown_tool"].is_error and "Available tools:" in text_of(r["unknown_tool"])
    assert r["missing"].is_error and "selection" in text_of(r["missing"])
    assert not r["after"].is_error


def test_string_encoded_json_arguments_are_tolerated(fixtures_dir):
    async def main():
        async with open_session("tiny", fixtures_dir=fixtures_dir) as session:
            return await session.call_tool(
                "run_query", {"group_by": '["service.name"]', "limit": "5"}
            )

    result = run(main())
    assert not result.is_error and "checkout" in text_of(result)


def test_dynamic_field_enum_flag(fixtures_dir):
    async def schemas(flag):
        async with open_session(
            "tiny", fixtures_dir=fixtures_dir, dynamic_field_enum=flag
        ) as session:
            return {t.name: t.input_schema for t in (await session.list_tools()).tools}

    off, on = run(schemas(False)), run(schemas(True))
    assert "enum" not in off["run_query"]["properties"]["group_by"]["items"]
    enum = on["run_query"]["properties"]["group_by"]["items"]["enum"]
    assert "service.name" in enum and "payment.provider" in enum
    assert "x-field-name" not in json.dumps(on)
    assert "enum" in on["compare_windows"]["properties"]["group_by"]


def test_build_server_returns_an_mcp_server(tiny):
    server = build_server(tiny)
    assert server.name and server.instructions
    assert {t.name for t in run(server.list_tools())} == TOOLS
