"""compare_windows: the same metric before vs after a time (or between two windows), with deltas."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from incident_agent.backends.base import (
    AggregateResult,
    Calc,
    Calculation,
    Group,
    TelemetryBackend,
    TimeWindow,
)
from incident_agent.mcp_server.tools._common import (
    CalcInput,
    FieldName,
    FilterInput,
    ToolDef,
    ToolFailure,
    ToolInput,
    clean,
    data_block,
    filters_text,
    fmt_delta,
    fmt_num,
    fmt_pct,
    fmt_ts,
    fmt_window,
    make_window,
    parse_ts,
    resolve_checked,
    table,
)

NAME = "compare_windows"

DESCRIPTION = """\
Compare a metric BEFORE vs AFTER a moment (a deploy, a flag change, the time an alert fired), \
optionally per value of one field, with absolute and relative changes. Answers "what changed at \
T?". Give `at` (a timestamp; the windows are the equal-length stretches just before and just \
after it) or two explicit windows `before` and `after`. Default metrics: error_rate and \
p95(duration_ms) (event counts n are always shown). With `group_by` the groups are ranked by \
how much the FIRST metric changed, so the biggest mover is first; each row shows the event \
counts (n) in both windows so you can judge how much to trust it.
Use it to confirm onset and to see which service/route/version moved. Do not use it to explain \
WHY (use find_outliers, optionally with baseline_start/baseline_end) or to read one request \
(get_trace). Find a good `at` first with run_query using granularity_seconds."""

FETCH_GROUPS = 200  # groups fetched per window before ranking by change
SMALL_N = 10


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(description="Inclusive start: ISO-8601 UTC or HH:MM:SS.")
    end: str = Field(description="Exclusive end: ISO-8601 UTC or HH:MM:SS.")


class CompareWindowsInput(ToolInput):
    at: str | None = Field(
        default=None,
        description="The moment to split on (ISO-8601 UTC or HH:MM:SS). Use this OR before+after.",
    )
    window_seconds: int | None = Field(
        default=None,
        ge=1,
        description="With `at`: length of each side in seconds (default: the longest equal-length "
        "windows the data allows).",
    )
    before: TimeRange | None = Field(default=None, description="Explicit 'before' window.")
    after: TimeRange | None = Field(default=None, description="Explicit 'after' window.")
    calculations: list[CalcInput] | None = Field(
        default=None,
        max_length=3,
        description="Metrics to compare (max 3). Default: ERROR_RATE and P95 of duration_ms.",
    )
    group_by: FieldName | None = Field(
        default=None, description="Compare per value of this field (low cardinality)."
    )
    filters: list[FilterInput] = Field(
        default_factory=list, max_length=8, description="Conditions combined with AND."
    )
    limit: int = Field(default=10, ge=1, le=30, description="Max groups returned (1-30).")


def _windows(
    args: CompareWindowsInput, backend: TelemetryBackend
) -> tuple[TimeWindow, TimeWindow, str]:
    schema = backend.schema()
    if args.at and (args.before or args.after):
        raise ToolFailure("Give `at` OR `before` and `after`, not both.")
    if args.at:
        at = parse_ts(args.at, schema, "at")
        end = schema.time_end + timedelta(microseconds=1)
        half = args.window_seconds
        if half is None:
            half = int(min((at - schema.time_start).total_seconds(), (end - at).total_seconds()))
        if at <= schema.time_start or at >= end or half < 1:
            raise ToolFailure(
                f"`at` {fmt_ts(at)} leaves no data on one side. The dataset covers "
                f"{fmt_ts(schema.time_start)} .. {fmt_ts(schema.time_end)}; choose a time inside."
            )
        delta = timedelta(seconds=half)
        return (
            TimeWindow(start=at - delta, end=at),
            TimeWindow(start=at, end=at + delta),
            f"around {fmt_ts(at)}, {half}s each side",
        )
    if args.before and args.after:
        b = make_window(schema, None, args.before.start, args.before.end)
        a = make_window(schema, None, args.after.start, args.after.end)
        return b, a, "explicit windows"
    raise ToolFailure(
        "Say when to compare: give `at` (a timestamp), or both `before` and `after` windows "
        '({"start": ..., "end": ...}).'
    )


def _calcs(args: CompareWindowsInput, names: set[str]) -> list[Calculation]:
    """Default metrics, most telling first (groups are ranked by the first one)."""
    if args.calculations:
        return list(args.calculations)
    out = []
    if "error" in names:
        out.append(Calculation(op=Calc.ERROR_RATE))
    if "duration_ms" in names:
        out.append(Calculation(op=Calc.P95, field="duration_ms"))
    return out or [Calculation(op=Calc.COUNT)]


def _fmt(key: str, v: float | int | None) -> str:
    return fmt_pct(v) if key == "error_rate" and v is not None else fmt_num(v)


def _change(key: str, before: float | int | None, after: float | int | None) -> str:
    delta = fmt_delta(before, after, pct=key == "error_rate")
    return f"{_fmt(key, before)} -> {_fmt(key, after)} ({delta})"


def _by_key(res: AggregateResult, group_by: str | None) -> dict[str, Group]:
    return {(str(g.key[group_by]) if group_by else ""): g for g in res.groups}


def run(backend: TelemetryBackend, args: CompareWindowsInput) -> str:
    schema = backend.schema()
    before_w, after_w, how = _windows(args, backend)
    rb = resolve_checked(schema, before_w, "before window")
    ra = resolve_checked(schema, after_w, "after window")
    calcs = _calcs(args, set(schema.field_names()))
    keys = [c.key for c in calcs]
    fetch = calcs if "count" in keys else [*calcs, Calculation(op=Calc.COUNT)]

    def agg(win: TimeWindow, group: list[str], limit: int) -> AggregateResult:
        return backend.aggregate(
            fetch, filters=args.filters, group_by=group, time_window=win, limit=limit
        )

    gb = [args.group_by] if args.group_by else []
    all_b, all_a = agg(before_w, [], 1), agg(after_w, [], 1)
    head = (
        f"compare_windows: {', '.join(keys)}{f' by {clean(args.group_by, 60)}' if gb else ''} | "
        f"filters: {filters_text(args.filters)} | {how}\n"
        f"before {fmt_window(rb)} -> {all_b.matched_rows} events | "
        f"after {fmt_window(ra)} -> {all_a.matched_rows} events"
    )
    if not all_b.matched_rows or not all_a.matched_rows:
        side = "before" if not all_b.matched_rows else "after"
        return (
            f"{head}\nNo events match in the {side} window, so there is nothing to compare. "
            "Check the filters, or choose `at`/windows inside the data (describe_dataset shows "
            "the time range)."
        )

    cols = ["", "n", *keys]
    rows = [
        [
            "(all)",
            f"{all_b.matched_rows} -> {all_a.matched_rows}",
            *(
                _change(k, all_b.groups[0].values.get(k), all_a.groups[0].values.get(k))
                for k in keys
            ),
        ]
    ]
    total_groups = 0
    caution = False
    if gb:
        gb_res, ga_res = agg(before_w, gb, FETCH_GROUPS), agg(after_w, gb, FETCH_GROUPS)
        before, after = _by_key(gb_res, gb[0]), _by_key(ga_res, gb[0])
        names = list(dict.fromkeys([*before, *after]))
        total_groups = len(names)

        def mover(name: str) -> float:
            b = before.get(name)
            a = after.get(name)
            bv = (b.values.get(keys[0]) if b else None) or 0
            av = (a.values.get(keys[0]) if a else None) or 0
            return abs(av - bv)

        ranked = sorted(names, key=lambda n: (-mover(n), n))[: args.limit]
        for name in ranked:
            b, a = before.get(name), after.get(name)
            nb = b.values.get("count") if b else 0
            na = a.values.get("count") if a else 0
            small = min(nb or 0, na or 0) < SMALL_N
            caution |= small
            mark = "*" if small else ""
            cells = [
                _change(
                    k,
                    b.values.get(k) if b else (0 if k == "count" else None),
                    a.values.get(k) if a else (0 if k == "count" else None),
                )
                for k in keys
            ]
            rows.append([clean(name, 60) + mark, f"{nb} -> {na}", *cells])

    out = [head, data_block(table(cols, rows))]
    if gb:
        capped = gb_res.truncated or ga_res.truncated  # more than FETCH_GROUPS groups in a window
        shown = len(rows) - 1
        if total_groups > shown or capped:
            out.append(
                f"Truncated: showing the {shown} biggest movers of {total_groups}"
                f"{'+' if capped else ''} groups (ranked by change in {keys[0]}). To narrow, add "
                "filters or raise `limit` (max 30)."
            )
        else:
            out.append(f"All {total_groups} group(s) shown, biggest change in {keys[0]} first.")
    if caution:
        out.append(f"* fewer than {SMALL_N} events in a window: treat the change with caution.")
    out.append(
        "To see WHY a group changed, use find_outliers with baseline_start/baseline_end set to "
        "the before window."
    )
    return "\n".join(out)


TOOL = ToolDef(
    name=NAME,
    title="Compare two time windows",
    description=DESCRIPTION,
    input_model=CompareWindowsInput,
    run=run,
)
