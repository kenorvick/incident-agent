"""find_outliers: BubbleUp-style comparison of a selection of events against a baseline.

For every attribute the backend compares the selection's value distribution with the baseline's
and scores how much they differ (see `backends/_common.py`). This module turns that into the
shortest readable answer:

1. *Perfect separators*: attributes whose value marks exactly the selected events (e.g.
   `exception.type=TimeoutError` when selecting `error = true`) are collapsed into one line instead
   of occupying ranking slots. Fields that merely restate the selection (`status_code` when the
   selection filters on `error`) are dropped.
2. The remaining attributes, ranked by score, each with its top values as "selection % vs
   baseline %" (numeric fields also show p50/p95/max for both sets).
3. A few example selected events with `trace_id`, as pointers for get_trace.
Attributes below a minimum score are not shown, so a healthy system answers "nothing stands out".
"""

import json

from pydantic import Field

from incident_agent.backends.base import (
    BackendError,
    Calc,
    Calculation,
    DistributionComparison,
    FieldComparison,
    FieldType,
    Filter,
    FilterOp,
    NumericStats,
    SampleOrder,
    TelemetryBackend,
    TimeWindow,
    ValueShare,
)
from incident_agent.mcp_server.tools._common import (
    FieldName,
    FilterInput,
    ToolDef,
    WindowInput,
    clean,
    data_block,
    explain_empty,
    filters_text,
    fmt_num,
    fmt_pct,
    fmt_scalar,
    fmt_window,
    make_window,
    resolve_checked,
    table,
)

NAME = "find_outliers"

DESCRIPTION = """\
BubbleUp: find which attributes distinguish a SELECTION of events (the bad ones) from a BASELINE \
(default: all other events in the window). Use it after run_query shows where the symptom is, to \
find WHY. Returns attributes ranked by score (0-1, higher = the selection differs more), each \
with its top values as selection % vs baseline %; numeric fields show p50/p95/max for both sets; \
attributes that mark exactly the selected events come first as "perfect separators"; plus \
example selected events with trace_id (feed those to get_trace).
Selection examples: [{"field":"error","op":"=","value":true}]; slow requests: \
[{"field":"service.name","op":"=","value":"checkout"},{"field":"duration_ms","op":">","value":1000}]. \
Scope the selection to one service when you can: mixing services dilutes the signal. High-score \
high-cardinality attributes (a user, tenant, product or version id) are often the answer; \
cross-check with a run_query grouped by that attribute. To compare the same events before vs \
after a time instead, give `baseline_start`/`baseline_end` (the selection window is then \
`start`/`end`).
Do not use it for counts or percentiles (run_query) or to read one request (get_trace). If \
nothing scores above ~0.1 the selection looks like normal background: say so rather than \
guessing."""  # noqa: E501

MIN_SCORE = 0.1
# The "where" part of a selection; the rest of its conditions describe the symptom.
SCOPE_FIELDS = {"service.name", "name", "span.kind", "http.route", "rpc.method", "rpc.service"}
VALUE_LIMIT = 80  # characters of an attribute value shown (error messages are worth reading)
PERFECT_RECALL = 0.98  # share of the selection that has the value
PERFECT_PRECISION = 0.95  # share of all events with the value that are in the selection
MIN_PERFECT_SELECTION = 5  # smaller selections can't prove a "perfect" split
SMALL_SELECTION = 20
MAX_PERFECT = 5
MISSING_MIN_DELTA = 0.25  # show a (missing) row only if presence differs this much
RARER_MIN_DELTA = 0.1  # show an under-represented value only if it is this much rarer
# Fields that merely restate "this event failed". Dropped when the selection already filters on one.
_DEFINITIONAL = {
    "error",
    "status_code",
    "status_message",
    "otel.status_code",
    "otel.status_description",
}
_EXAMPLE_FIELDS = ["trace_id", "service.name", "name", "duration_ms", "status_message"]


class FindOutliersInput(WindowInput):
    selection: list[FilterInput] = Field(
        min_length=1,
        max_length=6,
        description="Conditions (AND) that pick the events you care about, e.g. error = true.",
    )
    baseline: list[FilterInput] | None = Field(
        default=None,
        max_length=6,
        description="Comparison-set conditions. Default: every other event in the window.",
    )
    baseline_start: str | None = Field(
        default=None,
        description="Compare with the SAME selection in `baseline_start`..`baseline_end`.",
    )
    baseline_end: str | None = Field(default=None, description="End (exclusive) of that window.")
    fields: list[FieldName] | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="Only compare these fields (default: all well-populated, non-id fields).",
    )
    max_attributes: int = Field(
        default=8, ge=1, le=20, description="Ranked attributes to return (1-20)."
    )
    max_values: int = Field(default=3, ge=1, le=8, description="Top values per attribute (1-8).")


def _pct_n(frac: float, n: int) -> str:
    return f"{fmt_pct(frac)} ({n})"


def _name(fc: FieldComparison) -> str:
    return clean(fc.field, 60)


def _label(v: ValueShare) -> str:
    return "(missing)" if v.absent else fmt_scalar(v.value, VALUE_LIMIT)


def _perfect(fc: FieldComparison, n_sel: int, n_base: int) -> str | None:
    """Describe a field whose value marks exactly the selected events, else None."""
    if fc.kind != "categorical" or n_sel < MIN_PERFECT_SELECTION:
        return None
    for v in fc.values:
        if v.absent:
            continue
        total = v.selection_count + v.baseline_count
        if (
            v.selection_fraction >= PERFECT_RECALL
            and v.selection_count / total >= PERFECT_PRECISION
        ):
            outside = f" (baseline {fmt_pct(v.baseline_fraction)})" if v.baseline_count else ""
            return f"{_name(fc)}={_label(v)}{outside}"
    sp, bp = fc.selection_present_fraction, fc.baseline_present_fraction
    if sp is not None and bp is not None and sp >= PERFECT_RECALL:
        sel_n, base_n = sp * n_sel, bp * n_base
        if sel_n / (sel_n + base_n) >= PERFECT_PRECISION:
            tops = ", ".join(
                f"{_label(v)} {fmt_pct(v.selection_fraction)}" for v in fc.values if not v.absent
            )
            return f"{_name(fc)} only exists on selected events (values: {tops})"
    return None


def _stats_rows(sel: NumericStats, base: NumericStats) -> list[list[str]]:
    def row(label: str, s: NumericStats) -> list[str]:
        return [
            label,
            f"p50 {fmt_num(s.p50)}",
            f"p95 {fmt_num(s.p95)}",
            f"max {fmt_num(s.max)}",
            f"(n {fmt_num(s.count)})",
        ]

    return [row("selection", sel), row("baseline", base)]


def _visible(fc: FieldComparison) -> list[ValueShare]:
    """Values worth printing: over-represented ones, plus clear absences. A "(missing)" row only
    matters when presence itself differs a lot; a value that is merely rarer is noise."""
    return [
        v
        for v in fc.values
        if v.delta > 0
        and not v.absent
        or abs(v.delta) >= (MISSING_MIN_DELTA if v.absent else RARER_MIN_DELTA)
    ]


def _entry(i: int, fc: FieldComparison, max_values: int, same: list[str]) -> list[str]:
    shape = fc.type.value + (f", {fc.total_values} values" if fc.total_values else "")
    lines = [f"{i}. {_name(fc)}  ({shape}; score {fc.score:.2f})"]
    if same:
        lines.append(f"   same split: {', '.join(clean(n, 40) for n in same)}")
    if fc.selection_stats and fc.baseline_stats:
        lines += [
            "   " + ln for ln in table(None, _stats_rows(fc.selection_stats, fc.baseline_stats))
        ]
    values = _visible(fc)
    rows = [
        [
            _label(v),
            f"selection {_pct_n(v.selection_fraction, v.selection_count)}",
            f"baseline {_pct_n(v.baseline_fraction, v.baseline_count)}",
        ]
        for v in values[:max_values]
    ]
    lines += ["   " + ln for ln in table(None, rows)]
    return lines


def _enriched(fc: FieldComparison) -> ValueShare | None:
    """The value most over-represented in the selection (categorical fields only)."""
    if fc.kind != "categorical":
        return None
    best = [v for v in fc.values if not v.absent and v.delta > 0]
    return max(best, key=lambda v: v.delta, default=None)


def _dedupe(
    backend: TelemetryBackend,
    args: "FindOutliersInput",
    tw: TimeWindow | None,
    ranked: list[FieldComparison],
) -> tuple[list[FieldComparison], dict[str, list[str]]]:
    """Merge attributes that mark the very same selected events (e.g. exception.type,
    exception.message and span_event_count on failing spans) into the best-scored one.

    Candidates are pairs whose most-enriched value has identical selection/baseline counts; each
    is confirmed with one count query, so coincidences (same counts, different events) are kept.
    """
    kept: list[tuple[FieldComparison, ValueShare]] = []
    merged: dict[str, list[str]] = {}
    for fc in ranked:
        top = _enriched(fc)
        twin = None
        if top is not None:
            for other, ov in kept:
                if (ov.selection_count, ov.baseline_count) == (
                    top.selection_count,
                    top.baseline_count,
                ):
                    both = [
                        *args.selection,
                        Filter(field=other.field, value=ov.value),
                        Filter(field=fc.field, value=top.value),
                    ]
                    try:
                        n = backend.aggregate(
                            [Calculation(op=Calc.COUNT)], filters=both, time_window=tw
                        ).matched_rows
                    except BackendError:
                        continue
                    if (n or 0) >= PERFECT_RECALL * top.selection_count:
                        twin = other
                        break
        if twin is not None:
            merged.setdefault(twin.field, []).append(fc.field)
        elif top is not None:
            kept.append((fc, top))
        else:
            kept.append(
                (
                    fc,
                    ValueShare(
                        value=None,
                        selection_count=-1,
                        baseline_count=-1,
                        selection_fraction=0,
                        baseline_fraction=0,
                        delta=0,
                    ),
                )
            )
    return [fc for fc, _ in kept], merged


def _example_value(key: str, value: object) -> str:
    text = fmt_scalar(value, VALUE_LIMIT)
    quote = isinstance(value, str) and (" " in value or key == "status_message")
    return json.dumps(text, ensure_ascii=False) if quote else text


def _examples(backend: TelemetryBackend, args: FindOutliersInput, tw, names: set[str]) -> list[str]:
    if "trace_id" not in names:
        return []
    fields = [f for f in _EXAMPLE_FIELDS if f in names]
    slowest = "duration_ms" in names
    try:
        res = backend.sample_events(
            filters=args.selection,
            fields=fields,
            time_window=tw,
            limit=3,
            order=SampleOrder.SLOWEST if slowest else SampleOrder.NEWEST,
        )
    except BackendError:
        return []  # e.g. Honeycomb has no raw events
    if not res.events:
        return []
    out = [
        f"Example selected events ({'slowest' if slowest else 'newest'} first; "
        "pass trace_id to get_trace):"
    ]
    for ev in res.events:
        out.append("  " + " ".join(f"{k}={_example_value(k, v)}" for k, v in ev.items()))
    return out


def _where(
    backend: TelemetryBackend, args: FindOutliersInput, tw: TimeWindow | None, n_sel: int
) -> str | None:
    """Which services the selected events are in, when the selection is not already scoped."""
    if any(f.field in SCOPE_FIELDS for f in args.selection):
        return None
    try:
        res = backend.aggregate(
            [Calculation(op=Calc.COUNT)],
            filters=args.selection,
            group_by=["service.name"],
            time_window=tw,
            limit=5,
        )
    except BackendError:
        return None
    if len(res.groups) < 2 and not res.truncated:
        return None
    parts = [
        f"{fmt_scalar(g.key['service.name'], 40)} {fmt_pct(g.values['count'] / n_sel)} "
        f"({g.values['count']})"
        for g in res.groups
    ]
    more = f", +{res.total_groups - len(parts)} more" if res.truncated else ""
    return (
        f"Selected events by service.name: {', '.join(parts)}{more}. The selection spans several "
        "services, which blurs the ranking; for sharper results add a service.name condition."
    )


def _with_continuous_fields(
    backend: TelemetryBackend,
    args: FindOutliersInput,
    cmp: DistributionComparison,
    baseline: list[Filter] | None,
    tw: TimeWindow | None,
    bw: TimeWindow | None,
) -> DistributionComparison:
    """Workaround for a backend gap: the DuckDB backend skips float fields as "near-unique per
    event" (a latency in ms rarely repeats), but those are exactly the fields to compare. Ask for
    them explicitly and merge, so `duration_ms` is always ranked."""
    if args.fields is not None:
        return cmp
    schema = backend.schema()
    floats = {f.name for f in schema.fields if f.type is FieldType.FLOAT}
    extra = [n for n, why in cmp.skipped_fields.items() if n in floats and "near-unique" in why]
    if not extra or not cmp.selection_rows or not cmp.baseline_rows:
        return cmp
    more = backend.compare_distributions(
        selection_filters=args.selection,
        baseline_filters=baseline,
        fields=extra,
        time_window=tw,
        baseline_window=bw,
        max_values_per_field=args.max_values + 1,
    )
    merged = sorted([*cmp.fields, *more.fields], key=lambda f: (-f.score, f.field))
    skipped = {n: why for n, why in cmp.skipped_fields.items() if n not in extra}
    return cmp.model_copy(update={"fields": merged, "skipped_fields": skipped})


def _skipped(cmp: DistributionComparison) -> str | None:
    names = [n for n, why in cmp.skipped_fields.items() if "selection filters" not in why]
    if not names:
        return None
    shown = ", ".join(clean(n, 40) for n in names[:6])
    more = f" +{len(names) - 6} more" if len(names) > 6 else ""
    return (
        "Not compared (identifiers or near-unique per event; pass in `fields` to force): "
        f"{shown}{more}"
    )


_NEGATED = {
    FilterOp.GT: FilterOp.LTE, FilterOp.GTE: FilterOp.LT, FilterOp.LT: FilterOp.GTE,
    FilterOp.LTE: FilterOp.GT, FilterOp.IN: FilterOp.NOT_IN, FilterOp.NOT_IN: FilterOp.IN,
    FilterOp.EXISTS: FilterOp.DOES_NOT_EXIST, FilterOp.DOES_NOT_EXIST: FilterOp.EXISTS,
    FilterOp.NE: FilterOp.EQ,
}  # fmt: skip


def _negate(f: Filter) -> Filter | None:
    if f.op is FilterOp.EQ:
        if isinstance(f.value, bool):
            return Filter(field=f.field, op=FilterOp.EQ, value=not f.value)
        return Filter(field=f.field, op=FilterOp.NE, value=f.value)
    if f.op in _NEGATED:
        return Filter(field=f.field, op=_NEGATED[f.op], value=f.value)
    return None


def _scoped_baseline(selection: list[FilterInput]) -> tuple[list[Filter], str] | None:
    """For a selection like `service.name = X AND error = true`, compare against the same scope
    without the symptom (X's healthy events), not against every other service, whose attributes
    differ for reasons unrelated to the symptom. Returns (baseline filters, mode) or None."""
    scope = [f for f in selection if f.field in SCOPE_FIELDS and f.op in (FilterOp.EQ, FilterOp.IN)]
    symptom = [f for f in selection if f not in scope]
    if not scope or not symptom:
        return None
    neg = _negate(symptom[0]) if len(symptom) == 1 else None
    return ([*scope, neg], "scope_negated") if neg else (list(scope), "scope_only")


def _describe_baseline(
    args: FindOutliersInput, cmp: DistributionComparison, scoped: list[Filter] | None, mode: str
) -> str:
    if args.baseline is not None:
        where = (
            f" in {fmt_window(cmp.baseline_window)}"
            if cmp.baseline_window != cmp.selection_window
            else ""
        )
        return f"baseline {filters_text(args.baseline)}{where}"
    if args.baseline_start or args.baseline_end:
        return f"same selection in {fmt_window(cmp.baseline_window)}"
    if scoped is not None:
        extra = " (includes the selected events)" if mode == "scope_only" else ""
        return f"baseline (auto, same scope) {filters_text(scoped)}{extra}"
    return "baseline all other events in the window"


def run(backend: TelemetryBackend, args: FindOutliersInput) -> str:
    schema = backend.schema()
    tw = args.time_window(schema)
    resolve_checked(schema, tw)
    bw = None
    if args.baseline_start or args.baseline_end:
        bw = make_window(schema, None, args.baseline_start, args.baseline_end, "baseline_start")
        resolve_checked(schema, bw, "baseline window")

    baseline: list[Filter] | None = args.baseline
    scoped, mode = None, ""
    if baseline is None and bw is None and (auto := _scoped_baseline(args.selection)):
        baseline, mode = auto
        scoped = baseline
    cmp = backend.compare_distributions(
        selection_filters=args.selection,
        baseline_filters=baseline,
        fields=args.fields,
        time_window=tw,
        baseline_window=bw,
        max_values_per_field=args.max_values + 1,
    )
    cmp = _with_continuous_fields(backend, args, cmp, baseline, tw, bw)
    n_sel, n_base = cmp.selection_rows, cmp.baseline_rows
    share = ""
    if baseline is None and bw is None and n_sel + n_base:
        share = f" ({fmt_pct(n_sel / (n_sel + n_base))} of the window)"
    head = (
        f"find_outliers: selection {filters_text(args.selection)} -> {n_sel} events{share} | "
        f"{_describe_baseline(args, cmp, scoped, mode)} -> {n_base} events | "
        f"window {fmt_window(cmp.selection_window)}"
    )
    if n_sel == 0:
        why = explain_empty(backend, args.selection, tw)
        return f"{head}\nThe selection matches no events.\n{why}"
    if n_base == 0:
        return (
            f"{head}\nThe baseline is empty, so there is nothing to compare with (the selection "
            "covers everything). Give `baseline` conditions, `baseline_start`/`baseline_end`, or a "
            "narrower selection."
        )

    restated = {f.field for f in args.selection} & _DEFINITIONAL
    perfect: list[str] = []
    ranked: list[FieldComparison] = []
    for fc in cmp.fields:
        if fc.score < MIN_SCORE:
            continue
        line = _perfect(fc, n_sel, n_base)
        if line is None:
            if fc.kind == "numeric" or _visible(fc):
                ranked.append(fc)
        elif fc.field not in _DEFINITIONAL or not restated:
            perfect.append(line)
    n_above = len(ranked)
    ranked, same_split = _dedupe(backend, args, tw, ranked[: args.max_attributes * 2])
    shown = ranked[: args.max_attributes]

    body: list[str] = []
    where = _where(backend, args, tw, n_sel) if "service.name" in schema.field_names() else None
    if where:
        body.append(where)
    if perfect:
        body.append(
            "Perfect separators (each value is on >=98% of the selected events and almost "
            "nowhere else; likely the same events):"
        )
        body.append("  " + "; ".join(perfect[:MAX_PERFECT]))
        if len(perfect) > MAX_PERFECT:
            body.append(f"  +{len(perfect) - MAX_PERFECT} more")
    if shown:
        body.append("Ranked attributes:")
        for i, fc in enumerate(shown, 1):
            body += _entry(i, fc, args.max_values, same_split.get(fc.field, []))
    elif not perfect:
        best = cmp.fields[0] if cmp.fields else None
        top = f" (best: {_name(best)}, score {best.score:.2f})" if best else ""
        body.append(f"No attribute separates the selection from the baseline{top}.")
    body += _examples(backend, args, tw, set(schema.field_names()))
    skipped = _skipped(cmp)
    if skipped:
        body.append(skipped)

    out = [head, data_block(body)]
    out.append(
        f"score = how strongly the selection's values differ from the baseline (0-1, adjusted for "
        f"sample size); only attributes scoring >= {MIN_SCORE} are shown; percentages are shares "
        "of "
        "each group's events."
    )
    n_merged = sum(len(v) for v in same_split.values())
    if n_above > len(shown) + n_merged:
        out.append(
            f"Truncated: showing {len(shown)} of {n_above} attributes above the cutoff "
            f"({len(cmp.fields)} compared). To narrow: add a service/route condition to "
            "`selection`, pass `fields`, or raise `max_attributes` (max 20)."
        )
    if shown or perfect:
        out.append(
            "A difference is a lead, not proof: confirm with run_query on the suspect attribute "
            "or get_trace on an example."
        )
    else:
        out.append(
            "Nothing stands out: this selection looks like normal background. Try a different "
            "selection or window (run_query by service.name / name to locate the symptom first)."
        )
    if n_sel < SMALL_SELECTION:
        out.append(f"Caution: only {n_sel} selected events; patterns may be chance.")
    return "\n".join(out)


TOOL = ToolDef(
    name=NAME,
    title="Find outlier attributes",
    description=DESCRIPTION,
    input_model=FindOutliersInput,
    run=run,
)
