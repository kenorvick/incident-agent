"""run_query: aggregate (count, percentiles, error rate, ...) by up to 2 fields, over time too."""

import math

from pydantic import Field

from incident_agent.backends.base import (
    AggregateResult,
    Calc,
    DatasetSchema,
    Group,
    OrderBy,
    TelemetryBackend,
)
from incident_agent.mcp_server.tools._common import (
    CalcInput,
    FieldName,
    FilterInput,
    ToolDef,
    ToolFailure,
    WindowInput,
    clean,
    data_block,
    explain_empty,
    filters_text,
    fmt_num,
    fmt_pct,
    fmt_scalar,
    fmt_ts,
    fmt_window,
    is_id_like,
    resolve_checked,
    table,
)

NAME = "run_query"

DESCRIPTION = """\
Aggregate events: count, avg, p50-p99, max, error rate or count_distinct, grouped by up to 2 \
fields, with filters and a time window, optionally as a coarse time series. Use it to size and \
locate a symptom ("p95 latency and error rate by service.name", "errors by http.route", \
"error_rate over time for checkout"). Returns the top `limit` groups with `total_groups` and a \
truncated note.
Tips: group by LOW-cardinality fields (service.name, name, http.route, status_code). Failures: \
filters=[{"field":"error","op":"=","value":true}]. Latency: duration_ms with P95/P99 (averages \
hide outliers). Time series: set `granularity_seconds` (one calculation, at most 40 buckets, at \
most 6 groups).
Do NOT group by trace_id, span_id or user/cart/order ids: that returns one row per event. To \
find which attributes explain slow or failing events use find_outliers; to inspect one request \
use get_trace."""

MAX_SERIES_BUCKETS = 40
MAX_SERIES_GROUPS = 6


class RunQueryInput(WindowInput):
    calculations: list[CalcInput] = Field(
        default_factory=lambda: [CalcInput(op=Calc.COUNT)],
        min_length=1,
        max_length=4,
        description="What to compute (default COUNT); up to 4, exactly 1 with granularity_seconds.",
    )
    group_by: list[FieldName] = Field(
        default_factory=list,
        max_length=2,
        description="Up to 2 fields. Events missing one are left out.",
    )
    filters: list[FilterInput] = Field(
        default_factory=list, max_length=8, description="Conditions, ANDed."
    )
    order_by: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Sort by a calculation key (`count`, `p95(duration_ms)`, `error_rate`) or a group_by "
            "field. Default: the first calculation."
        ),
    )
    ascending: bool = Field(default=False, description="Sort ascending.")
    limit: int = Field(default=10, ge=1, le=50, description="Max groups (1-50).")
    granularity_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Time series with buckets of this many seconds (UTC, zero-filled).",
    )


def _cell(key: str, value: object) -> str:
    if key == "error_rate":
        return fmt_pct(value) if isinstance(value, float | int) else "-"
    return fmt_num(value)


def _group_label(group: Group, group_by: list[str]) -> str:
    return " / ".join(fmt_scalar(group.key[g]) for g in group_by) or "(all)"


def _check_series(schema: DatasetSchema, args: RunQueryInput, seconds: float) -> None:
    if len(args.calculations) != 1:
        raise ToolFailure(
            "A time series (`granularity_seconds`) takes exactly one calculation; "
            f"you gave {len(args.calculations)}. Use one, or drop granularity_seconds."
        )
    g = args.granularity_seconds or 1
    buckets = math.ceil(seconds / g)
    if buckets > MAX_SERIES_BUCKETS:
        need = math.ceil(seconds / MAX_SERIES_BUCKETS)
        raise ToolFailure(
            f"granularity_seconds={g} over a {round(seconds)}s window gives {buckets} buckets "
            f"(max {MAX_SERIES_BUCKETS}). Use granularity_seconds >= {need} or a shorter window."
        )


def run(backend: TelemetryBackend, args: RunQueryInput) -> str:
    schema = backend.schema()
    tw = args.time_window(schema)
    win = resolve_checked(schema, tw)
    series = args.granularity_seconds is not None
    limit = args.limit
    if series:
        _check_series(schema, args, win.seconds)
        limit = min(limit, MAX_SERIES_GROUPS)
    order = None
    if args.order_by:
        order = OrderBy(key=args.order_by, descending=not args.ascending)

    res = backend.aggregate(
        args.calculations,
        filters=args.filters,
        group_by=args.group_by,
        time_window=tw,
        order_by=order,
        limit=limit,
        granularity_seconds=args.granularity_seconds,
    )
    calc_text = ", ".join(res.calcs)
    by = f" by {', '.join(clean(g, 60) for g in res.group_by)}" if res.group_by else ""
    head = (
        f"run_query: {calc_text}{by} | filters: {filters_text(args.filters)} | "
        f"window {fmt_window(res.window)} | {res.matched_rows} events matched"
    )
    if res.matched_rows == 0:
        return f"{head}\n{explain_empty(backend, args.filters, tw)}"
    if not res.groups:
        return (
            f"{head}\nNo groups: the matched events all lack the group-by field(s) "
            f"{', '.join(res.group_by)}. Try another field (describe_dataset shows how many "
            "events have each)."
        )

    body = _series_lines(res) if series else _table_lines(res)
    out = [head, data_block(body), _trailer(res, limit, schema, args)]
    return "\n".join(out)


def _table_lines(res: AggregateResult) -> list[str]:
    header = (
        [*[clean(g, 60) for g in res.group_by], *res.calcs] if res.group_by else ["", *res.calcs]
    )
    rows = []
    for grp in res.groups:
        keys = [fmt_scalar(grp.key[g]) for g in res.group_by] or ["(all)"]
        rows.append([*keys, *(_cell(k, grp.values.get(k)) for k in res.calcs)])
    first_numeric = len(res.group_by) or 1
    return table(header, rows, right=range(first_numeric, len(header)))


def _series_lines(res: AggregateResult) -> list[str]:
    key = res.calcs[0]
    g = res.granularity_seconds
    labels = [_group_label(grp, res.group_by) for grp in res.groups]
    times = [b.start for b in res.groups[0].series or []]
    same_day = len({t.date() for t in times}) == 1
    rows = []
    for i, t in enumerate(times):
        stamp = fmt_ts(t)
        rows.append(
            [
                stamp[11:] if same_day else stamp,
                *(_cell(key, (grp.series or [])[i].values.get(key)) for grp in res.groups),
            ]
        )
    day = f" on {fmt_ts(times[0])[:10]}" if same_day and times else ""
    return [
        f"{key} per {g}s bucket, bucket start times UTC{day}:",
        *table(["time", *labels], rows, right=range(1, len(labels) + 1)),
    ]


def _trailer(res: AggregateResult, limit: int, schema: DatasetSchema, args: RunQueryInput) -> str:
    lines = []
    if res.truncated:
        lines.append(
            f"Truncated: showing {len(res.groups)} of {res.total_groups} groups. To narrow, add "
            "filters (e.g. on one of the shown values), or raise `limit` (max 50, series max "
            f"{MAX_SERIES_GROUPS}). To find which attributes distinguish bad events use "
            "find_outliers."
        )
    else:
        lines.append(f"All {res.total_groups} group(s) shown.")
    infos = {f.name: f for f in schema.fields}
    for g in res.group_by:
        info = infos.get(g)
        if info and is_id_like(info, schema.row_count):
            lines.append(
                f"Warning: `{g}` has {info.cardinality} distinct values (an identifier); "
                "grouping by it is rarely useful. Use find_outliers or get_trace instead."
            )
    return "\n".join(lines)


TOOL = ToolDef(
    name=NAME,
    title="Run aggregate query",
    description=DESCRIPTION,
    input_model=RunQueryInput,
    run=run,
)
