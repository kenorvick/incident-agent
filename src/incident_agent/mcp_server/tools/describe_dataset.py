"""describe_dataset: the orientation call (services, time range, fields, how to use them)."""

from pydantic import Field

from incident_agent.backends.base import (
    Calc,
    Calculation,
    FieldInfo,
    FieldType,
    TelemetryBackend,
)
from incident_agent.mcp_server.tools._common import (
    ToolDef,
    ToolInput,
    clean,
    data_block,
    fmt_num,
    fmt_pct,
    fmt_ts,
    is_id_like,
    table,
)

NAME = "describe_dataset"

DESCRIPTION = """\
Orientation: what telemetry exists. Returns the services, event count, time range and the most \
useful fields (type, number of distinct values, % of events that have the field, and the values of \
small enums). Call this FIRST in an investigation, and again whenever you need an exact field name \
(use `name_contains` to search, e.g. "http", "user", "payment").
Each event is one span with many attributes; any field can be filtered or grouped. Do not use this \
to analyse a symptom: use run_query for numbers, find_outliers for what differs, get_trace for one \
request."""

# Shown first, in this order, when present. The rest follow by how many events have them.
_KEY_FIELDS = [
    "service.name", "name", "span.kind", "duration_ms", "error", "status_code", "status_message",
    "is_root", "http.route", "http.request.method", "http.response.status_code", "rpc.method",
    "exception.type", "exception.message", "service.version", "deploy.version", "trace_id",
    "span_id", "parent_span_id", "timestamp",
]  # fmt: skip
_ENUM_MAX = 8  # list all values of string/boolean fields with at most this many distinct values
_STATS_MAX = 4  # numeric fields that get a p50/p95/max line


class DescribeDatasetInput(ToolInput):
    name_contains: str | None = Field(
        default=None,
        max_length=100,
        description="Only list fields whose name contains this text (case-insensitive).",
    )
    max_fields: int = Field(
        default=40, ge=5, le=200, description="How many fields to list (default 40, max 200)."
    )


def _rank(info: FieldInfo) -> tuple[int, float, str]:
    try:
        key = _KEY_FIELDS.index(info.name)
    except ValueError:
        key = len(_KEY_FIELDS)
    return (key, -(info.fraction_present or 0.0), info.name)


def _notes(backend: TelemetryBackend, info: FieldInfo, rows: int | None, stats: bool) -> str:
    if info.type is FieldType.TIMESTAMP:
        return "event start time; use start/end or granularity_seconds"
    if is_id_like(info, rows):
        return "high-cardinality id: filter on it, don't group by it"
    if info.mixed_types:
        return "mixed types across events (stored as strings)"
    if info.type.is_numeric and stats:
        res = backend.aggregate(
            [Calculation(op=op, field=info.name) for op in (Calc.P50, Calc.P95, Calc.MAX)],
            limit=1,
        )
        v = res.groups[0].values if res.groups else {}
        return " ".join(f"{k.split('(')[0]} {fmt_num(x)}" for k, x in v.items() if x is not None)
    if (
        info.type in (FieldType.STRING, FieldType.BOOLEAN)
        and info.cardinality is not None
        and 0 < info.cardinality <= _ENUM_MAX
    ):
        dv = backend.distinct_values(info.name, limit=_ENUM_MAX)
        return "values: " + ", ".join(clean(fmt_enum(v.value), 40) for v in dv.values)
    return ""


def fmt_enum(value: object) -> str:
    return fmt_num(value) if isinstance(value, bool | int | float) else str(value)


def run(backend: TelemetryBackend, args: DescribeDatasetInput) -> str:
    schema = backend.schema()
    infos = schema.fields
    if args.name_contains:
        needle = args.name_contains.lower()
        infos = [f for f in infos if needle in f.name.lower()]
    ordered = sorted(infos, key=_rank)
    shown = ordered[: args.max_fields]

    rows: list[list[str]] = []
    stats_left = _STATS_MAX
    for f in shown:
        is_stat = f.type.is_numeric and not is_id_like(f, schema.row_count) and stats_left > 0
        stats_left -= is_stat
        try:
            note = _notes(backend, f, schema.row_count, is_stat)
        except Exception:  # orientation must not fail because one field's detail did
            note = ""
        rows.append(
            [
                clean(f.name, 60),
                f.type.value,
                "?" if f.cardinality is None else str(f.cardinality),
                "?" if f.fraction_present is None else fmt_pct(f.fraction_present),
                note,
            ]
        )

    span = (schema.time_end - schema.time_start).total_seconds()
    n_rows = "?" if schema.row_count is None else str(schema.row_count)
    head = (
        f"Dataset: {n_rows} events (one per span) | {fmt_ts(schema.time_start)} .. "
        f"{fmt_ts(schema.time_end)} ({round(span)}s; relative windows count back from the end) | "
        f"{len(schema.services)} services | {len(schema.fields)} fields"
    )
    body: list[str] = []
    if schema.services:
        body.append(
            "services (events): "
            + ", ".join(f"{clean(s.name, 50)} {fmt_num(s.rows)}" for s in schema.services[:30])
        )
    title = f"fields matching {args.name_contains!r}" if args.name_contains else "fields"
    body.append(f"{title} ({len(shown)} of {len(ordered)} shown):")
    if rows:
        body += table(["field", "type", "distinct", "present", "notes"], rows, right=(2, 3))
    else:
        body.append("No field name contains that text; try a shorter fragment or omit it.")
    body += [f"note: {clean(n, 200)}" for n in schema.notes[:3]]

    out = [head, data_block(body)]
    if len(ordered) > len(shown):
        out.append(
            f"{len(ordered) - len(shown)} more fields not shown: pass `name_contains` to search "
            "or raise `max_fields` (max 200)."
        )
    out.append(_guide({f.name for f in schema.fields}))
    return "\n".join(out)


def _guide(names: set[str]) -> str:
    """Static usage hints, tailored to the fields that exist."""
    lines = ["How to use these fields:"]
    parts = []
    if "duration_ms" in names:
        parts.append("latency = duration_ms (milliseconds)")
    if "error" in names:
        parts.append("failures = error (true/false), plus status_code and exception.* if present")
    if "service.name" in names:
        parts.append("where = service.name, name (operation), http.route")
    if parts:
        lines.append("- " + "; ".join(parts) + ".")
    if "is_root" in names:
        lines.append(
            "- is_root=true events are request entry points: use them for user-facing numbers."
        )
    lines.append(
        "- Counts are of the loaded (possibly sampled) data; rates and percentiles are unaffected."
    )
    lines.append(
        "- Next: run_query to size the symptom, find_outliers to see what differs, "
        "get_trace on an example trace_id, compare_windows for before/after a time."
    )
    return "\n".join(lines)


TOOL = ToolDef(
    name=NAME,
    title="Describe dataset",
    description=DESCRIPTION,
    input_model=DescribeDatasetInput,
    run=run,
)
