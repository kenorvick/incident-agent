"""DuckDB telemetry backend: loads a fixture's wide-event JSONL into an in-memory table.

Safety: column names come from telemetry (untrusted) and contain dots. Every identifier that
reaches SQL has first been validated against the loaded schema and is then double-quoted; every
value is a bound parameter. Only integers we validated ourselves (limits, granularity) and
quantile constants from an enum are formatted into SQL text.
"""

import gzip
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from incident_agent.backends._common import (
    CheckedFilter,
    FieldCatalog,
    check_granularity,
    densify,
    is_continuous_numeric,
    naive_utc,
    score_distribution,
    score_ordered,
    validate_aggregate,
)
from incident_agent.backends.base import (
    MAX_GROUP_LIMIT,
    PERCENTILES,
    AggregateResult,
    BackendError,
    Bucket,
    Calc,
    Calculation,
    DatasetSchema,
    DistinctValues,
    DistributionComparison,
    FieldComparison,
    FieldInfo,
    FieldType,
    Filter,
    FilterOp,
    Group,
    InvalidQueryError,
    NumericStats,
    OrderBy,
    ResolvedWindow,
    SampleOrder,
    SampleResult,
    Scalar,
    ServiceInfo,
    Span,
    TimeWindow,
    Trace,
    ValueCount,
    ValueShare,
    resolve_window,
)

TS = '"timestamp"'
_MAX_INT64 = 2**63


def _q(name: str) -> str:
    """Quote an identifier. Only ever called with names validated against the schema."""
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    """SQL string literal (used only for column specs built from the file's own keys)."""
    return "'" + text.replace("'", "''") + "'"


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"
    if isinstance(v, Decimal):
        return float(v)
    return v


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _fmt(x: float) -> str:
    return f"{x:.4g}"


class DuckDBBackend:
    """TelemetryBackend over a gzip/plain JSONL wide-event file (format: docs/architecture.md)."""

    def __init__(self, events_path: Path | str):
        self._path = Path(events_path)
        if not self._path.is_file():
            raise FileNotFoundError(f"events file not found: {self._path}")
        self._con = duckdb.connect(":memory:")
        self._notes: list[str] = []
        self._mixed: set[str] = set()
        self._load()
        self._schema = self._build_schema()
        self._catalog = FieldCatalog(self._schema)

    @classmethod
    def from_fixture(cls, fixtures_dir: Path | str, scenario_id: str) -> "DuckDBBackend":
        base = Path(fixtures_dir)
        for name in ("events.jsonl.gz", "events.jsonl"):
            path = base / scenario_id / name
            if path.is_file():
                return cls(path)
        available = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        raise FileNotFoundError(
            f"no events file for scenario `{scenario_id}` under {base}; "
            f"available scenarios: {available or 'none'}"
        )

    # ------------------------------------------------------------------ loading

    def _infer_columns(self) -> dict[str, str]:
        """Full-sample type inference over every row (union of keys; conflicts -> VARCHAR)."""
        kinds: dict[str, set[str]] = {}
        opener = gzip.open if self._path.suffix == ".gz" else open
        with opener(self._path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                for key, v in json.loads(line).items():
                    seen = kinds.setdefault(key, set())
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        seen.add("bool")
                    elif isinstance(v, int):
                        seen.add("int" if abs(v) < _MAX_INT64 else "float")
                    elif isinstance(v, float):
                        seen.add("float")
                    else:  # str, or an unexpected nested value (stored as JSON text)
                        seen.add("str")
        if not kinds:
            raise BackendError(f"{self._path} contains no events")
        if "timestamp" not in kinds:
            raise BackendError(f"{self._path} has no `timestamp` key on any row")

        columns: dict[str, str] = {}
        taken: dict[str, str] = {}
        for key, seen in kinds.items():
            if not key or key.lower() in taken:
                self._notes.append(
                    f"dropped field `{key}`: empty name or collides case-insensitively "
                    f"with `{taken.get(key.lower(), '')}`"
                )
                continue
            taken[key.lower()] = key
            if key == "timestamp":
                columns[key] = "TIMESTAMP"
            elif seen == {"bool"}:
                columns[key] = "BOOLEAN"
            elif seen == {"int"}:
                columns[key] = "BIGINT"
            elif seen and seen <= {"int", "float"}:
                columns[key] = "DOUBLE"
            else:
                columns[key] = "VARCHAR"
                if len(seen) > 1:
                    self._mixed.add(key)
                    self._notes.append(
                        f"field `{key}` has conflicting types across rows "
                        f"({', '.join(sorted(seen))}); values stored as strings"
                    )
        return columns

    def _load(self) -> None:
        columns = self._infer_columns()
        spec = ", ".join(f"{_lit(k)}: {_lit(t)}" for k, t in columns.items())
        self._con.execute(
            "CREATE TABLE events AS SELECT * FROM "
            f"read_json(?, format='newline_delimited', columns={{{spec}}})",
            [str(self._path)],
        )

    def _run(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        cur = self._con.cursor()
        try:
            return cur.execute(sql, list(params)).fetchall()
        finally:
            cur.close()

    def _build_schema(self) -> DatasetSchema:
        info = self._run(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'events' ORDER BY ordinal_position"
        )
        names = [r[0] for r in info]
        types = {
            "BOOLEAN": FieldType.BOOLEAN,
            "BIGINT": FieldType.INTEGER,
            "DOUBLE": FieldType.FLOAT,
            "TIMESTAMP": FieldType.TIMESTAMP,
        }
        parts = ["count(*)", f"min({TS})", f"max({TS})"]
        for n in names:
            parts += [f"count({_q(n)})", f"count(DISTINCT {_q(n)})"]
        row = self._run(f"SELECT {', '.join(parts)} FROM events")[0]
        rows, t0, t1 = row[0], row[1], row[2]
        if t0 is None:
            raise BackendError("no event has a parseable `timestamp`")
        fields = []
        for i, (n, dtype) in enumerate(info):
            present, distinct = row[3 + 2 * i], row[4 + 2 * i]
            fields.append(
                FieldInfo(
                    name=n,
                    type=types.get(dtype, FieldType.STRING),
                    cardinality=distinct,
                    fraction_present=present / rows,
                    mixed_types=n in self._mixed,
                )
            )
        services: list[ServiceInfo] = []
        if "service.name" in names:
            services = [
                ServiceInfo(name=s, rows=c)
                for s, c in self._run(
                    'SELECT "service.name", count(*) c FROM events '
                    'WHERE "service.name" IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1'
                )
            ]
        return DatasetSchema(
            fields=fields,
            row_count=rows,
            time_start=_utc(t0),
            time_end=_utc(t1),
            services=services,
            notes=self._notes,
        )

    def close(self) -> None:
        self._con.close()

    # ------------------------------------------------------------------ sql helpers

    def schema(self) -> DatasetSchema:
        return self._schema

    def _window(self, tw: TimeWindow | None) -> ResolvedWindow:
        return resolve_window(tw, self._schema.time_start, self._schema.time_end)

    @staticmethod
    def _window_sql(w: ResolvedWindow) -> tuple[str, list]:
        return f"{TS} >= ? AND {TS} < ?", [naive_utc(w.start), naive_utc(w.end)]

    @staticmethod
    def _pred(cf: CheckedFilter) -> tuple[str, list]:
        q, v = _q(cf.field), cf.value
        match cf.op:
            case FilterOp.EXISTS:
                return f"{q} IS NOT NULL", []
            case FilterOp.DOES_NOT_EXIST:
                return f"{q} IS NULL", []
            case FilterOp.IN | FilterOp.NOT_IN:
                assert isinstance(v, list)
                marks = ", ".join("?" for _ in v)
                neg = "NOT " if cf.op is FilterOp.NOT_IN else ""
                return f"{q} {neg}IN ({marks})", list(v)
            case FilterOp.CONTAINS:
                return f"contains(lower({q}), lower(?))", [v]
            case FilterOp.STARTS_WITH:
                return f"starts_with({q}, ?)", [v]
            case _:
                sql_op = "<>" if cf.op is FilterOp.NE else cf.op.value
                return f"{q} {sql_op} ?", [v]

    def _filters_sql(self, filters: Sequence[CheckedFilter]) -> tuple[str, list]:
        parts, params = [], []
        for cf in filters:
            sql, p = self._pred(cf)
            parts.append(f"({sql})")
            params += p
        return (" AND ".join(parts) or "TRUE"), params

    def _where(
        self, filters: Sequence[CheckedFilter], w: ResolvedWindow, not_null: Sequence[str] = ()
    ) -> tuple[str, list]:
        wsql, wp = self._window_sql(w)
        fsql, fp = self._filters_sql(filters)
        extra = "".join(f" AND {_q(n)} IS NOT NULL" for n in not_null)
        return f"WHERE {wsql} AND {fsql}{extra}", wp + fp

    @staticmethod
    def _calc_sql(c: Calculation) -> str:
        q = _q(c.field) if c.field else ""
        match c.op:
            case Calc.COUNT:
                return "count(*)"
            case Calc.COUNT_DISTINCT:
                return f"count(DISTINCT {q})"
            case Calc.ERROR_RATE:
                return 'avg(CASE WHEN "error" THEN 1.0 ELSE 0.0 END)'
            case Calc.AVG | Calc.SUM | Calc.MIN | Calc.MAX:
                return f"{c.op.value.lower()}({q})"
            case _:
                return f"quantile_cont({q}, {PERCENTILES[c.op]})"

    # ------------------------------------------------------------------ aggregate

    def aggregate(
        self,
        calcs: Sequence[Calculation],
        *,
        filters: Sequence[Filter] = (),
        group_by: Sequence[str] = (),
        time_window: TimeWindow | None = None,
        order_by: OrderBy | None = None,
        limit: int = 10,
        granularity_seconds: int | None = None,
    ) -> AggregateResult:
        keys, group_by = validate_aggregate(self._catalog, calcs, group_by, order_by, limit)
        checked = [self._catalog.check_filter(f) for f in filters]
        win = self._window(time_window)
        if granularity_seconds is not None:
            check_granularity(granularity_seconds, win)

        order_expr = "c0 DESC NULLS LAST"
        if order_by is not None:
            direction = "DESC" if order_by.descending else "ASC"
            if order_by.key in keys:
                order_expr = f"c{keys.index(order_by.key)} {direction} NULLS LAST"
            else:
                order_expr = f"{_q(order_by.key)} {direction} NULLS LAST"

        gcols = [_q(g) for g in group_by]
        calc_sql = [f"{self._calc_sql(c)} AS c{i}" for i, c in enumerate(calcs)]
        where_all, params_all = self._where(checked, win)
        matched = self._run(f"SELECT count(*) FROM events {where_all}", params_all)[0][0]

        if group_by:
            where, params = self._where(checked, win, not_null=group_by)
            sql = (
                f"SELECT {', '.join(gcols + calc_sql)}, count(*) OVER () AS _total "
                f"FROM events {where} GROUP BY {', '.join(gcols)} "
                f"ORDER BY {order_expr}, {', '.join(gcols)} LIMIT {int(limit)}"
            )
            rows = self._run(sql, params)
            total = rows[0][-1] if rows else 0
        else:
            rows = self._run(f"SELECT {', '.join(calc_sql)} FROM events {where_all}", params_all)
            total = 1
        ng = len(group_by)
        groups = [
            Group(
                key={g: _jsonable(r[i]) for i, g in enumerate(group_by)},
                values={k: _jsonable(r[ng + i]) for i, k in enumerate(keys)},
            )
            for r in rows
        ]
        if granularity_seconds is not None and groups:
            self._attach_series(groups, calcs, checked, group_by, win, granularity_seconds)
        return AggregateResult(
            calcs=keys,
            group_by=group_by,
            groups=groups,
            total_groups=total,
            truncated=total > len(groups),
            matched_rows=matched,
            window=win,
            granularity_seconds=granularity_seconds,
        )

    def _attach_series(
        self,
        groups: list[Group],
        calcs: Sequence[Calculation],
        checked: Sequence[CheckedFilter],
        group_by: list[str],
        win: ResolvedWindow,
        g: int,
    ) -> None:
        where, params = self._where(checked, win, not_null=group_by)
        gcols = [_q(x) for x in group_by]
        if group_by:
            tuples = [tuple(grp.key[x] for x in group_by) for grp in groups]
            if len(group_by) == 1:
                cond = f"{gcols[0]} IN ({', '.join('?' for _ in tuples)})"
                params += [t[0] for t in tuples]
            else:
                one = f"({', '.join('?' for _ in group_by)})"
                cond = f"({', '.join(gcols)}) IN ({', '.join(one for _ in tuples)})"
                params += [v for t in tuples for v in t]
            where += f" AND {cond}"
        bucket = f"epoch_ms(CAST(floor(epoch({TS}) / {int(g)}) * {int(g)} * 1000 AS BIGINT))"
        calc_sql = [self._calc_sql(c) for c in calcs]
        sql = (
            f"SELECT {', '.join(gcols + [f'{bucket} AS _b'])}, {', '.join(calc_sql)} "
            f"FROM events {where} GROUP BY {', '.join(gcols + ['_b'])}"
        )
        by_group: dict[tuple, dict[datetime, dict[str, Any]]] = defaultdict(dict)
        ng = len(group_by)
        for r in self._run(sql, params):
            by_group[tuple(r[:ng])][_utc(r[ng])] = {
                c.key: _jsonable(r[ng + 1 + i]) for i, c in enumerate(calcs)
            }
        for grp in groups:
            found = by_group.get(tuple(grp.key[x] for x in group_by), {})
            grp.series = [Bucket(start=t, values=v) for t, v in densify(found, calcs, win, g)]

    # ------------------------------------------------------------------ distinct / sample

    def distinct_values(
        self,
        field: str,
        *,
        filters: Sequence[Filter] = (),
        time_window: TimeWindow | None = None,
        limit: int = 20,
    ) -> DistinctValues:
        if not 1 <= limit <= MAX_GROUP_LIMIT:
            raise InvalidQueryError(f"limit must be between 1 and {MAX_GROUP_LIMIT}")
        self._catalog.get(field)
        checked = [self._catalog.check_filter(f) for f in filters]
        win = self._window(time_window)
        q = _q(field)
        where_all, params_all = self._where(checked, win)
        matched, with_field = self._run(
            f"SELECT count(*), count({q}) FROM events {where_all}", params_all
        )[0]
        where, params = self._where(checked, win, not_null=[field])
        rows = self._run(
            f"SELECT {q}, count(*) AS c, count(*) OVER () FROM events {where} "
            f"GROUP BY {q} ORDER BY c DESC, {q} LIMIT {int(limit)}",
            params,
        )
        total = rows[0][2] if rows else 0
        return DistinctValues(
            field=field,
            values=[ValueCount(value=_jsonable(r[0]), count=r[1]) for r in rows],
            total_distinct=total,
            truncated=total > len(rows),
            matched_rows=matched,
            rows_with_field=with_field,
            window=win,
        )

    def sample_events(
        self,
        *,
        filters: Sequence[Filter] = (),
        fields: Sequence[str] | None = None,
        time_window: TimeWindow | None = None,
        limit: int = 5,
        order: SampleOrder = SampleOrder.NEWEST,
    ) -> SampleResult:
        if not 1 <= limit <= 100:
            raise InvalidQueryError("limit must be between 1 and 100")
        cols = list(dict.fromkeys(fields)) if fields else self._catalog.names
        for c in cols:
            self._catalog.get(c)
        checked = [self._catalog.check_filter(f) for f in filters]
        win = self._window(time_window)
        if order is SampleOrder.SLOWEST:
            order_sql = f"{_q(self._need('duration_ms'))} DESC NULLS LAST, {TS}"
        else:
            order_sql = f"{TS} {'DESC' if order is SampleOrder.NEWEST else 'ASC'}"
        where, params = self._where(checked, win)
        rows = self._run(
            f"SELECT {', '.join(_q(c) for c in cols)}, count(*) OVER () FROM events {where} "
            f"ORDER BY {order_sql} LIMIT {int(limit)}",
            params,
        )
        total = (
            rows[0][-1] if rows else self._run(f"SELECT count(*) FROM events {where}", params)[0][0]
        )
        events = [
            {c: _jsonable(v) for c, v in zip(cols, r[:-1], strict=True) if v is not None}
            for r in rows
        ]
        return SampleResult(
            events=events, total_matching=total, truncated=total > len(events), window=win
        )

    def _need(self, name: str) -> str:
        self._catalog.get(name)
        return name

    # ------------------------------------------------------------------ trace

    def get_trace(self, trace_id: str, *, max_spans: int = 2000) -> Trace:
        if "trace_id" not in self._catalog or "span_id" not in self._catalog:
            raise BackendError("dataset has no `trace_id`/`span_id` fields")
        cur = self._con.cursor()
        try:
            cur.execute(
                f"SELECT *, count(*) OVER () AS _total FROM events WHERE trace_id = ? "
                f"ORDER BY {TS}, span_id LIMIT {int(max_spans)}",
                [trace_id.strip().lower()],
            )
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchall()
        finally:
            cur.close()
        lifted = {
            "trace_id", "span_id", "parent_span_id", "name", "service.name", "span.kind",
            "timestamp", "duration_ms", "error", "status_message", "_total",
        }  # fmt: skip
        spans = []
        for r in rows:
            d = dict(zip(cols, r, strict=True))
            spans.append(
                Span(
                    span_id=d["span_id"],
                    parent_span_id=d.get("parent_span_id"),
                    name=d.get("name"),
                    service=d.get("service.name"),
                    kind=d.get("span.kind"),
                    start=_utc(d["timestamp"]) if d.get("timestamp") else None,
                    duration_ms=d.get("duration_ms"),
                    error=bool(d.get("error")),
                    status_message=d.get("status_message"),
                    attributes={
                        k: _jsonable(v) for k, v in d.items() if k not in lifted and v is not None
                    },
                )
            )
        total = rows[0][-1] if rows else 0
        return Trace(trace_id=trace_id, spans=spans, span_count=total, truncated=total > len(spans))

    # ------------------------------------------------------------------ compare_distributions

    def compare_distributions(
        self,
        *,
        selection_filters: Sequence[Filter] = (),
        baseline_filters: Sequence[Filter] | None = None,
        fields: Sequence[str] | None = None,
        time_window: TimeWindow | None = None,
        baseline_window: TimeWindow | None = None,
        max_values_per_field: int = 5,
    ) -> DistributionComparison:
        if not 1 <= max_values_per_field <= 50:
            raise InvalidQueryError("max_values_per_field must be between 1 and 50")
        sel = [self._catalog.check_filter(f) for f in selection_filters]
        sel_win = self._window(time_window)
        sel_sql, sel_p = self._filters_sql(sel)
        sel_w_sql, sel_w_p = self._window_sql(sel_win)
        sel_pred, sel_params = f"({sel_w_sql}) AND ({sel_sql})", sel_w_p + sel_p

        if baseline_filters is not None:
            base_win = self._window(baseline_window or time_window)
            base = [self._catalog.check_filter(f) for f in baseline_filters]
            b_sql, b_p = self._filters_sql(base)
            b_w_sql, b_w_p = self._window_sql(base_win)
            base_pred, base_params = f"({b_w_sql}) AND ({b_sql})", b_w_p + b_p
        elif baseline_window is not None:
            base_win = self._window(baseline_window)
            b_w_sql, b_w_p = self._window_sql(base_win)
            base_pred, base_params = f"({b_w_sql}) AND ({sel_sql})", b_w_p + sel_p
        else:
            if not sel:
                raise InvalidQueryError(
                    "selection has no filters, so its complement is empty; pass baseline_filters "
                    "or baseline_window"
                )
            base_win = sel_win
            base_pred = f"({sel_w_sql}) AND NOT COALESCE(({sel_sql}), FALSE)"
            base_params = sel_w_p + sel_p

        names, skipped = self._catalog.comparable_fields(
            self._schema.row_count, fields, {f.field for f in sel}
        )
        cols = ", ".join(_q(n) for n in names)
        cur = self._con.cursor()
        try:
            cur.execute(
                f"CREATE TEMP TABLE _cmp AS SELECT * FROM (SELECT {cols + ', ' if cols else ''}"
                f"COALESCE(({sel_pred}), FALSE) AS _sel, COALESCE(({base_pred}), FALSE) AS _base "
                "FROM events) WHERE _sel OR _base",
                sel_params + base_params,
            )
            n_sel, n_base = cur.execute(
                "SELECT count(*) FILTER (WHERE _sel), count(*) FILTER (WHERE _base) FROM _cmp"
            ).fetchone() or (0, 0)
            results: list[FieldComparison] = []
            if n_sel and n_base:
                for name in names:
                    results.append(
                        self._compare_field(cur, name, n_sel, n_base, max_values_per_field)
                    )
        finally:
            cur.execute("DROP TABLE IF EXISTS _cmp")
            cur.close()
        results.sort(key=lambda f: (-f.score, f.field))
        return DistributionComparison(
            selection_rows=n_sel,
            baseline_rows=n_base,
            selection_window=sel_win,
            baseline_window=base_win,
            fields=results,
            skipped_fields=skipped,
        )

    def _compare_field(
        self, cur: Any, name: str, n_sel: int, n_base: int, max_values: int
    ) -> FieldComparison:
        info = self._catalog.get(name)
        q = _q(name)
        continuous = is_continuous_numeric(info)
        edges: list[float] = []
        stats: tuple[NumericStats | None, NumericStats | None] = (None, None)
        if continuous:
            decile = [i / 10 for i in range(1, 10)]
            qs = cur.execute(f"SELECT quantile_cont({q}, {decile}) FROM _cmp").fetchone()[0]
            edges = sorted({float(e) for e in qs or []})
            stats = self._numeric_stats(cur, q)
        if edges:
            whens = "".join(f"WHEN {q} <= ? THEN {i} " for i in range(len(edges)))
            expr = f"CASE WHEN {q} IS NULL THEN -1 {whens}ELSE {len(edges)} END"
            params: list = list(edges)
        else:
            expr, params = q, []
        rows = cur.execute(
            f"SELECT {expr} AS v, count(*) FILTER (WHERE _sel), count(*) FILTER (WHERE _base) "
            "FROM _cmp GROUP BY 1",
            params,
        ).fetchall()
        absent = -1 if edges else None
        sel_counts = {r[0]: r[1] for r in rows if r[1]}
        base_counts = {r[0]: r[2] for r in rows if r[2]}
        score, scored = score_distribution(sel_counts, base_counts, n_sel, n_base)
        if edges:
            score = max(score, score_ordered(sel_counts, base_counts, n_sel, n_base))

        values = []
        for sv in scored[:max_values]:
            is_absent = sv.key == absent
            low = high = None
            value: Scalar = None if is_absent else _jsonable(sv.key)
            if edges and not is_absent:
                i = int(sv.key)
                low = edges[i - 1] if i > 0 else None
                high = edges[i] if i < len(edges) else None
                value = (
                    f"<= {_fmt(high)}" if low is None else
                    f"> {_fmt(low)}" if high is None else
                    f"({_fmt(low)}, {_fmt(high)}]"
                )  # fmt: skip
            values.append(
                ValueShare(
                    value=value,
                    absent=is_absent,
                    low=low,
                    high=high,
                    selection_count=sel_counts.get(sv.key, 0),
                    baseline_count=base_counts.get(sv.key, 0),
                    selection_fraction=sv.p_sel,
                    baseline_fraction=sv.p_base,
                    delta=sv.delta,
                )
            )
        present = {k for k in sel_counts.keys() | base_counts.keys() if k != absent}
        return FieldComparison(
            field=name,
            type=info.type,
            kind="numeric" if edges else "categorical",
            score=score,
            score_method="ks_lcb_over_quantile_buckets" if edges else "max_value_delta_lcb",
            selection_present_fraction=1 - sel_counts.get(absent, 0) / n_sel,
            baseline_present_fraction=1 - base_counts.get(absent, 0) / n_base,
            total_values=len(present),
            values=values,
            selection_stats=stats[0],
            baseline_stats=stats[1],
        )

    @staticmethod
    def _numeric_stats(cur: Any, q: str) -> tuple[NumericStats | None, NumericStats | None]:
        def part(flag: str) -> str:
            return (
                f"count({q}) FILTER (WHERE {flag}), avg({q}) FILTER (WHERE {flag}), "
                f"quantile_cont({q}, [0.5, 0.9, 0.95]) FILTER (WHERE {flag}), "
                f"max({q}) FILTER (WHERE {flag})"
            )

        r = cur.execute(f"SELECT {part('_sel')}, {part('_base')} FROM _cmp").fetchone()

        def build(c: int, mean: Any, qs: Any, mx: Any) -> NumericStats:
            p50, p90, p95 = qs if qs else (None, None, None)
            return NumericStats(count=c, mean=mean, p50=p50, p90=p90, p95=p95, max=mx)

        return build(*r[:4]), build(*r[4:])
