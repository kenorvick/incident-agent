"""A fixed set of typical tool calls for any dataset, used to check response sizes.

`typical_calls(backend)` derives realistic arguments from the data itself (the noisiest service,
an erroring trace, the dataset midpoint), so the same list runs on a synthetic fixture and on a
captured scenario. `scripts/tool_sizes.py` runs it through MCP and prints a table; a test asserts
the budget. Approximate tokens = characters / 4.
"""

import math
from typing import Any

from incident_agent.backends.base import Calc, Calculation, Filter, SampleOrder, TelemetryBackend
from incident_agent.mcp_server.tools._common import fmt_ts

TOKEN_BUDGET = 2000
Call = tuple[str, str, dict[str, Any]]  # (label, tool name, arguments)


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _eq(field: str, value: Any) -> dict[str, Any]:
    return {"field": field, "op": "=", "value": value}


def typical_calls(backend: TelemetryBackend) -> list[Call]:
    schema = backend.schema()
    names = set(schema.field_names())
    span = (schema.time_end - schema.time_start).total_seconds()
    mid = schema.time_start + (schema.time_end - schema.time_start) / 2
    by = "service.name" in names
    calls: list[Call] = [
        ("orientation", "describe_dataset", {}),
        ("field search", "describe_dataset", {"name_contains": "http"}),
    ]

    calcs: list[dict[str, Any]] = [{"op": "COUNT"}]
    if "duration_ms" in names:
        calcs.append({"op": "P95", "field": "duration_ms"})
    if "error" in names:
        calcs.append({"op": "ERROR_RATE"})
    if by:
        calls.append(
            (
                "overview by service",
                "run_query",
                {"calculations": calcs, "group_by": ["service.name"]},
            )
        )
    if "duration_ms" in names and "name" in names:
        args: dict[str, Any] = {
            "calculations": [{"op": "P99", "field": "duration_ms"}, {"op": "COUNT"}],
            "group_by": ["name"],
        }
        if "is_root" in names:
            args["filters"] = [_eq("is_root", True)]
        calls.append(("slowest operations", "run_query", args))
    if "error" in names:
        step = max(1, math.ceil(span / 20))
        calls.append(
            (
                "error rate over time",
                "run_query",
                {"calculations": [{"op": "ERROR_RATE"}], "granularity_seconds": step},
            )
        )
        calls.append(("outliers: errors", "find_outliers", {"selection": [_eq("error", True)]}))

    if by and "duration_ms" in names:
        top = backend.aggregate(
            [Calculation(op=Calc.P95, field="duration_ms")], group_by=["service.name"], limit=1
        ).groups
        if top:
            svc = top[0].key["service.name"]
            p95 = top[0].values["p95(duration_ms)"]
            if p95 is not None:
                sel = [_eq("service.name", svc), {"field": "duration_ms", "op": ">", "value": p95}]
                calls.append(("outliers: slow spans", "find_outliers", {"selection": sel}))

    if "trace_id" in names:
        filters = [Filter(field="error", value=True)] if "error" in names else []
        sample = backend.sample_events(
            filters=filters,
            fields=["trace_id"],
            limit=1,
            order=SampleOrder.SLOWEST if "duration_ms" in names else SampleOrder.NEWEST,
        )
        if not sample.events and filters:
            sample = backend.sample_events(fields=["trace_id"], limit=1)
        if sample.events:
            calls.append(("one trace", "get_trace", {"trace_id": sample.events[0]["trace_id"]}))

    at = fmt_ts(mid)
    if by:
        calls.append(
            ("what changed, by service", "compare_windows", {"at": at, "group_by": "service.name"})
        )
    calls.append(("what changed, overall", "compare_windows", {"at": at}))
    return calls
