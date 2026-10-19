"""Honeycomb telemetry backend (optional, live). Never imported on the default path.

Built from Honeycomb's current API docs (read 2026-10; re-check before relying on details):

- Query Data API  https://docs.honeycomb.io/api/query-data/
    1. POST /1/queries/{dataset}                      create (validate + store) a query spec
    2. POST /1/query_results/{dataset}                create a result for a query id (async, 201)
    3. GET  /1/query_results/{dataset}/{result_id}    poll until `complete` is true
  `{dataset}` may be `__all__` (environment-wide). Result rows are `{"data": {...}}`; a failed
  query is HTTP 200 with `complete: true` and an `error` string instead of `data`.
- Columns API     https://docs.honeycomb.io/api/columns/   GET /1/columns/{dataset} (schema)
- Auth: `X-Honeycomb-Team: <configuration key>`. The key needs the **Manage Queries and
  Columns** and **Run Queries** permissions (Columns also needs Manage Queries and Columns).
  The Query Data API is an Enterprise-plan feature.
- Rate limits https://docs.honeycomb.io/api/rate-limit : creating a Query Result is limited to
  10/minute (1/minute for queries with relational fields). Exceeding it is a 429 *without* a
  Retry-After header on this API; the `RateLimit: limit=X, remaining=Y, reset=Z` header is used
  when present, otherwise exponential backoff. We also pace ourselves proactively.

Documented limitations (each raises `UnsupportedOperationError` or is reported in results):

- No raw events. The Query Data API only returns aggregates, so `sample_events` is unsupported.
- Queries only see the last 7 days and time bounds are truncated to 1 min (<= 6 h), 5 min
  (<= 2 days) or 30 min (<= 7 days). "Dataset end time" is *now* for this backend.
- A query result must finish in 10 s; results hold at most 10,000 groups (1,000 with a series).
- No BubbleUp endpoint: `compare_distributions` costs ~2 queries per compared field, so only
  `max_compare_fields` fields (default 4) are compared per call; others are listed in
  `skipped_fields`. With `fields=None` a shortlist of common attributes is used.
- Numeric (float) fields are compared by summary stats and a median-shift score against baseline
  quantiles, not quantile buckets (the score is not directly comparable to the DuckDB score).
- `get_trace` is rebuilt from grouped queries (group by span id): no start times, no free-form
  attributes, and spans missing `name`/`service.name` would be dropped by group-by.
- ASSUMPTIONS not verifiable without a key (the docs do not list them): the canonical->Honeycomb
  field map below, `status_code` being an integer (0/1/2) on OTLP data, and the `series` entry
  shape `{"time", "data"}`. Treat the live smoke test as the check.
"""

import math
import os
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from incident_agent.backends._common import (
    CheckedFilter,
    FieldCatalog,
    check_granularity,
    densify,
    score_distribution,
    validate_aggregate,
)
from incident_agent.backends.base import (
    AggregateResult,
    BackendError,
    BackendUnavailableError,
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
    UnsupportedOperationError,
    ValueCount,
    ValueShare,
    resolve_window,
)

DEFAULT_URL = "https://api.honeycomb.io"
DOCS = "https://docs.honeycomb.io/api/query-data/"
PERMISSIONS = "'Manage Queries and Columns' and 'Run Queries'"
MAX_RESULT_ROWS = 10_000
MAX_SPEC_LIMIT = 1_000
MAX_RANGE_S = 7 * 86400

# canonical (fixture) field name -> Honeycomb column name. Unmapped names pass through.
CANONICAL_TO_HONEYCOMB = {
    "trace_id": "trace.trace_id",
    "span_id": "trace.span_id",
    "parent_span_id": "trace.parent_id",
    "span_event_count": "meta.span_event_count",
}
HONEYCOMB_TO_CANONICAL = {v: k for k, v in CANONICAL_TO_HONEYCOMB.items()}
STATUS_CODES = {"UNSET": 0, "OK": 1, "ERROR": 2}  # only if `status_code` is an integer column
DEFAULT_COMPARE_FIELDS = (
    "service.name",
    "name",
    "http.route",
    "http.response.status_code",
    "rpc.method",
    "exception.type",
    "db.system",
    "duration_ms",
)

_OP = {
    FilterOp.EQ: "=",
    FilterOp.NE: "!=",
    FilterOp.GT: ">",
    FilterOp.GTE: ">=",
    FilterOp.LT: "<",
    FilterOp.LTE: "<=",
    FilterOp.IN: "in",
    FilterOp.NOT_IN: "not-in",
    FilterOp.EXISTS: "exists",
    FilterOp.DOES_NOT_EXIST: "does-not-exist",
    FilterOp.CONTAINS: "contains",
    FilterOp.STARTS_WITH: "starts-with",
}
_NEGATED = {
    FilterOp.EQ: "!=",
    FilterOp.NE: "=",
    FilterOp.GT: "<=",
    FilterOp.GTE: "<",
    FilterOp.LT: ">=",
    FilterOp.LTE: ">",
    FilterOp.IN: "not-in",
    FilterOp.NOT_IN: "in",
    FilterOp.EXISTS: "does-not-exist",
    FilterOp.DOES_NOT_EXIST: "exists",
    FilterOp.CONTAINS: "does-not-contain",
    FilterOp.STARTS_WITH: "does-not-start-with",
}
_COLUMN_TYPES = {
    "string": FieldType.STRING,
    "integer": FieldType.INTEGER,
    "float": FieldType.FLOAT,
    "boolean": FieldType.BOOLEAN,
}


def hc_name(canonical: str) -> str:
    return CANONICAL_TO_HONEYCOMB.get(canonical, canonical)


def canonical_name(honeycomb: str) -> str:
    return HONEYCOMB_TO_CANONICAL.get(honeycomb, honeycomb)


class HoneycombBackend:
    """TelemetryBackend over Honeycomb's Query Data + Columns APIs (see module docstring)."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        dataset: str = "__all__",
        base_url: str = DEFAULT_URL,
        lookback_seconds: int = 7200,
        max_compare_fields: int = 4,
        queries_per_minute: int = 10,
        poll_timeout: float = 30.0,
        max_retries: int = 4,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        key = api_key or os.environ.get("HONEYCOMB_API_KEY")
        if not key:
            raise BackendUnavailableError("HONEYCOMB_API_KEY is not set")
        self._dataset = dataset
        self._lookback = lookback_seconds
        self._max_compare = max_compare_fields
        self._qpm = queries_per_minute
        self._poll_timeout = poll_timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._sent: deque[float] = deque()
        self._client = client or httpx.Client(base_url=base_url, timeout=30.0)
        self._headers = {"X-Honeycomb-Team": key, "User-Agent": "incident-agent"}
        self._schema: DatasetSchema | None = None
        self._catalog: FieldCatalog | None = None
        self._hc_types: dict[str, str] = {}

    @classmethod
    def from_env(cls, **kwargs: Any) -> "HoneycombBackend":
        """Reads HONEYCOMB_API_KEY, HONEYCOMB_DATASET (default `__all__`), HONEYCOMB_API_URL."""
        return cls(
            dataset=os.environ.get("HONEYCOMB_DATASET", "__all__"),
            base_url=os.environ.get("HONEYCOMB_API_URL", DEFAULT_URL),
            **kwargs,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, json: Any = None) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            resp = self._client.request(method, path, json=json, headers=self._headers)
            if resp.status_code == 429 or resp.status_code in (502, 503, 504):
                if attempt == self._max_retries:
                    raise BackendUnavailableError(
                        f"Honeycomb kept returning HTTP {resp.status_code} (rate limit: "
                        "creating query results is limited to 10/minute); retry in a minute"
                    )
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code in (401, 403):
                raise BackendUnavailableError(
                    f"Honeycomb rejected the API key (HTTP {resp.status_code}). The key needs the "
                    f"{PERMISSIONS} permissions and the environment needs the Query Data API "
                    f"(Enterprise plan). See {DOCS}"
                )
            if resp.status_code == 404:
                raise BackendError(f"Honeycomb: not found ({path}); check HONEYCOMB_DATASET")
            if resp.status_code in (400, 422):
                raise InvalidQueryError(f"Honeycomb rejected the query: {self._detail(resp)}")
            if resp.status_code >= 400:
                raise BackendError(f"Honeycomb HTTP {resp.status_code}: {self._detail(resp)}")
            return resp
        raise AssertionError("unreachable")

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300]
        return str(body.get("detail") or body.get("error") or body)[:300]

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        match = re.search(r"reset=(\d+)", resp.headers.get("RateLimit", ""))
        if match:
            return min(float(match.group(1)) + 0.5, 65.0)
        return min(2.0**attempt, 30.0)

    def _pace(self) -> None:
        """Stay under the Create Query Result limit instead of provoking 429s."""
        now = self._clock()
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        if len(self._sent) >= self._qpm:
            self._sleep(max(0.0, 60 - (now - self._sent[0])) + 0.1)
            self._sent.popleft()
        self._sent.append(self._clock())

    def _query(
        self, spec: dict, *, series: bool = False, result_limit: int = MAX_RESULT_ROWS
    ) -> tuple[list[dict], list[dict]]:
        """create query -> create query result -> poll. Returns (result rows, series rows)."""
        ds = self._dataset
        query_id = self._request("POST", f"/1/queries/{ds}", spec).json()["id"]
        self._pace()
        body: dict[str, Any] = {
            "query_id": query_id,
            "disable_series": not series,
            "disable_total_by_aggregate": True,
            "disable_other_by_aggregate": True,
        }
        if not series:
            body["limit"] = result_limit
        created = self._request("POST", f"/1/query_results/{ds}", body)
        result_id = created.json()["id"]
        deadline = self._clock() + self._poll_timeout
        delay = 0.25
        while True:
            data = self._request("GET", f"/1/query_results/{ds}/{result_id}").json()
            if data.get("complete"):
                if data.get("error"):
                    raise BackendError(f"Honeycomb query failed: {data['error']}")
                payload = data.get("data") or {}
                return (
                    [r["data"] for r in payload.get("results", [])],
                    list(payload.get("series", [])),
                )
            if self._clock() >= deadline:
                raise BackendError("Honeycomb query did not complete in time; narrow the window")
            self._sleep(delay)
            delay = min(delay * 1.5, 2.0)

    # ------------------------------------------------------------------ time and filters

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def _window(self, tw: TimeWindow | None, *, lookback: int | None = None) -> ResolvedWindow:
        now = self._now()
        win = resolve_window(tw, now - timedelta(seconds=lookback or self._lookback), now)
        if win.seconds > MAX_RANGE_S:
            raise InvalidQueryError("Honeycomb only queries the last 7 days; use a shorter window")
        return win

    @staticmethod
    def _time_spec(win: ResolvedWindow) -> dict:
        return {
            "start_time": int(win.start.timestamp()),
            "end_time": max(math.ceil(win.end.timestamp() - 1e-3), int(win.start.timestamp()) + 1),
        }

    def _filter_spec(self, cf: CheckedFilter, negate: bool = False) -> dict:
        op = (_NEGATED if negate else _OP)[cf.op]
        if cf.field == "is_root":  # no stored column: root spans are those without a parent
            if cf.op not in (FilterOp.EQ, FilterOp.NE) or not isinstance(cf.value, bool):
                raise InvalidQueryError("`is_root` only supports = / != with true or false")
            root = cf.value == (cf.op is FilterOp.EQ)
            return {
                "column": hc_name("parent_span_id"),
                "op": "does-not-exist" if root != negate else "exists",
            }
        spec: dict[str, Any] = {"column": hc_name(cf.field), "op": op}
        if cf.op not in (FilterOp.EXISTS, FilterOp.DOES_NOT_EXIST):
            spec["value"] = self._value_in(cf.field, cf.value)
        return spec

    def _value_in(self, field: str, value: Any) -> Any:
        if field == "status_code" and self._hc_types.get("status_code") == "integer":
            if isinstance(value, list):
                return [STATUS_CODES.get(str(v), v) for v in value]
            return STATUS_CODES.get(str(value), value)
        return value

    def _value_out(self, field: str, value: Any) -> Scalar:
        if field == "status_code" and self._hc_types.get("status_code") == "integer":
            return {v: k for k, v in STATUS_CODES.items()}.get(value, value)
        return value

    def _spec(
        self,
        calcs: Sequence[dict],
        win: ResolvedWindow,
        *,
        breakdowns: Sequence[str] = (),
        filters: Sequence[dict] = (),
        combination: str = "AND",
        orders: Sequence[dict] = (),
        limit: int = MAX_SPEC_LIMIT,
        granularity: int | None = None,
    ) -> dict:
        spec: dict[str, Any] = {
            "calculations": list(calcs),
            "breakdowns": [hc_name(b) for b in breakdowns],
            "filters": list(filters),
            "filter_combination": combination,
            "orders": list(orders),
            "limit": min(limit, MAX_SPEC_LIMIT),
            **self._time_spec(win),
        }
        if granularity:
            spec["granularity"] = granularity
        return spec

    @staticmethod
    def _calc_spec(c: Calculation) -> dict:
        if c.op is Calc.COUNT:
            return {"op": "COUNT"}
        assert c.field is not None
        return {"op": c.op.value, "column": hc_name(c.field)}

    @staticmethod
    def _result_key(spec: dict) -> str:
        return f"{spec['op']}({spec['column']})" if spec.get("column") else spec["op"]

    # ------------------------------------------------------------------ schema

    def _load_schema(self) -> DatasetSchema:
        if self._schema is not None:
            return self._schema
        cols = self._request("GET", f"/1/columns/{self._dataset}").json()
        fields: list[FieldInfo] = []
        for col in cols:
            if col.get("hidden"):
                continue
            self._hc_types[col["key_name"]] = col.get("type", "string")
            name = canonical_name(col["key_name"])
            ftype = _COLUMN_TYPES.get(col.get("type", "string"), FieldType.STRING)
            if name == "status_code" and ftype is FieldType.INTEGER:
                ftype = FieldType.STRING  # translated to/from OK/ERROR/UNSET, see STATUS_CODES
            fields.append(FieldInfo(name=name, type=ftype))
        if not any(f.name == "is_root" for f in fields):
            fields.append(FieldInfo(name="is_root", type=FieldType.BOOLEAN))
        win = self._window(None)
        count_rows, _ = self._query(self._spec([{"op": "COUNT"}], win, limit=1))
        svc_rows, _ = self._query(
            self._spec(
                [{"op": "COUNT"}],
                win,
                breakdowns=["service.name"],
                orders=[{"op": "COUNT", "order": "descending"}],
            ),
            result_limit=100,
        )
        self._schema = DatasetSchema(
            fields=fields,
            row_count=int(count_rows[0].get("COUNT", 0)) if count_rows else 0,
            time_start=win.start,
            time_end=win.end,
            services=[ServiceInfo(name=r["service.name"], rows=r.get("COUNT")) for r in svc_rows],
            notes=[
                "Honeycomb: cardinality and presence are unknown; time range is the lookback "
                f"window ({self._lookback}s) ending now.",
            ],
        )
        self._catalog = FieldCatalog(self._schema)
        return self._schema

    def schema(self) -> DatasetSchema:
        return self._load_schema()

    def _cat(self) -> FieldCatalog:
        self._load_schema()
        assert self._catalog is not None
        return self._catalog

    # ------------------------------------------------------------------ aggregate

    def _group_rows(
        self,
        calc_specs: list[dict],
        breakdowns: list[str],
        filters: list[dict],
        win: ResolvedWindow,
        orders: list[dict],
        *,
        granularity: int | None = None,
        limit: int = MAX_RESULT_ROWS,
    ) -> tuple[list[dict], list[dict]]:
        spec = self._spec(
            calc_specs,
            win,
            breakdowns=breakdowns,
            filters=filters,
            orders=orders,
            limit=limit,
            granularity=granularity,
        )
        return self._query(spec, series=granularity is not None, result_limit=limit)

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
        cat = self._cat()
        keys, group_by = validate_aggregate(cat, calcs, group_by, order_by, limit)
        checked = [cat.check_filter(f) for f in filters]
        filter_specs = [self._filter_spec(cf) for cf in checked]
        win = self._window(time_window)
        if granularity_seconds is not None:
            check_granularity(granularity_seconds, win)

        server_calcs = [c for c in calcs if c.op is not Calc.ERROR_RATE]
        want_rate = len(server_calcs) != len(calcs)
        if want_rate and not any(c.op is Calc.COUNT for c in server_calcs):
            server_calcs.append(Calculation(op=Calc.COUNT))
        calc_specs = [self._calc_spec(c) for c in server_calcs]
        rate_key = Calculation(op=Calc.ERROR_RATE).key
        sort_key = order_by.key if order_by else keys[0]
        descending = order_by.descending if order_by else True
        client_sort = sort_key == rate_key
        orders: list[dict] = []
        if client_sort:
            orders = [{"op": "COUNT", "order": "descending"}]
        elif sort_key in group_by:
            orders = [{"column": hc_name(sort_key), "order": _dir(descending)}]
        else:
            target = next(c for c in calcs if c.key == sort_key)
            orders = [{**self._calc_spec(target), "order": _dir(descending)}]

        rows, _ = self._group_rows(calc_specs, group_by, filter_specs, win, orders)
        if not group_by and not rows:
            rows = [{}]
        err_counts: dict[tuple, float] = {}
        if want_rate:
            err_filter = {"column": "error", "op": "=", "value": True}
            err_rows, _ = self._group_rows(
                [{"op": "COUNT"}], group_by, filter_specs + [err_filter], win, []
            )
            err_counts = {self._gkey(r, group_by): r.get("COUNT", 0) for r in err_rows}

        groups: list[Group] = []
        for r in rows:
            values: dict[str, float | int | None] = {}
            for c in calcs:
                if c.op is Calc.ERROR_RATE:
                    n = r.get("COUNT")
                    values[c.key] = err_counts.get(self._gkey(r, group_by), 0) / n if n else None
                else:
                    v = r.get(self._result_key(self._calc_spec(c)))
                    values[c.key] = 0 if v is None and c.op.value.startswith("COUNT") else v
            groups.append(
                Group(
                    key={g: self._value_out(g, r.get(hc_name(g))) for g in group_by},
                    values=values,
                )
            )
        if client_sort:
            present = [g for g in groups if g.values[rate_key] is not None]
            present.sort(key=lambda g: g.values[rate_key] or 0.0, reverse=descending)
            groups = present + [g for g in groups if g.values[rate_key] is None]
        total = len(groups)
        groups = groups[:limit]
        if granularity_seconds is not None and groups:
            self._attach_series(
                groups, calcs, calc_specs, group_by, filter_specs, win, granularity_seconds,
                orders, want_rate, max(total, 1) if client_sort else len(groups),
            )  # fmt: skip
        matched = None
        if not group_by and rows and "COUNT" in rows[0]:
            matched = int(rows[0]["COUNT"])
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

    def _gkey(self, row: dict, group_by: Sequence[str]) -> tuple:
        """Canonical group key (tuple of canonical values) of a Honeycomb result row."""
        return tuple(self._value_out(g, row.get(hc_name(g))) for g in group_by)

    def _attach_series(
        self,
        groups: list[Group],
        calcs: Sequence[Calculation],
        calc_specs: list[dict],
        group_by: list[str],
        filter_specs: list[dict],
        win: ResolvedWindow,
        g: int,
        orders: list[dict],
        want_rate: bool,
        series_limit: int,
    ) -> None:
        limit = min(series_limit, MAX_SPEC_LIMIT)
        _, rows = self._group_rows(
            calc_specs, group_by, filter_specs, win, orders, granularity=g, limit=limit
        )
        err_rows: list[dict] = []
        if want_rate:
            err_filter = {"column": "error", "op": "=", "value": True}
            _, err_rows = self._group_rows(
                [{"op": "COUNT"}], group_by, filter_specs + [err_filter], win, [],
                granularity=g, limit=limit,
            )  # fmt: skip

        def when(row: dict) -> datetime:
            return datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(UTC)

        per_group: dict[tuple, dict[datetime, dict]] = {}
        for row in rows:
            data = row["data"]
            vals = {
                c.key: data.get(self._result_key(self._calc_spec(c)))
                for c in calcs
                if c.op is not Calc.ERROR_RATE
            }
            vals["_count"] = data.get("COUNT")
            per_group.setdefault(self._gkey(data, group_by), {})[when(row)] = vals
        errs: dict[tuple, dict[datetime, Any]] = {}
        for row in err_rows:
            errs.setdefault(self._gkey(row["data"], group_by), {})[when(row)] = row["data"].get(
                "COUNT", 0
            )
        for grp in groups:
            gk = tuple(grp.key[x] for x in group_by)
            found: dict[datetime, dict] = {}
            for t, vals in per_group.get(gk, {}).items():
                out = {k: v for k, v in vals.items() if k != "_count"}
                for c in calcs:
                    if c.op is Calc.ERROR_RATE:
                        n = vals.get("_count")
                        out[c.key] = errs.get(gk, {}).get(t, 0) / n if n else None
                found[t] = out
            grp.series = [Bucket(start=t, values=v) for t, v in densify(found, calcs, win, g)]

    # ------------------------------------------------------------------ distinct / sample / trace

    def distinct_values(
        self,
        field: str,
        *,
        filters: Sequence[Filter] = (),
        time_window: TimeWindow | None = None,
        limit: int = 20,
    ) -> DistinctValues:
        cat = self._cat()
        info = cat.get(field)
        if field == "is_root" or info.type is FieldType.TIMESTAMP:
            raise InvalidQueryError(f"cannot list values of `{field}` on Honeycomb")
        checked = [cat.check_filter(f) for f in filters]
        win = self._window(time_window)
        rows, _ = self._group_rows(
            [{"op": "COUNT"}],
            [field],
            [self._filter_spec(cf) for cf in checked],
            win,
            [{"op": "COUNT", "order": "descending"}],
        )
        values = [
            ValueCount(value=self._value_out(field, r.get(hc_name(field))), count=int(r["COUNT"]))
            for r in rows
        ]
        return DistinctValues(
            field=field,
            values=values[:limit],
            total_distinct=len(values),
            truncated=len(values) > limit,
            matched_rows=None,
            rows_with_field=sum(v.count for v in values),
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
        raise UnsupportedOperationError(
            "the Honeycomb backend cannot return raw events: the Query Data API only returns "
            f"aggregates ({DOCS}). Use aggregate/group_by on a high-cardinality field "
            "(e.g. group by trace_id with MAX(duration_ms)) to find examples, then get_trace."
        )

    def get_trace(self, trace_id: str, *, max_spans: int = 2000) -> Trace:
        cat = self._cat()
        cat.get("trace_id")
        win = self._window(None, lookback=MAX_RANGE_S - 3600)
        by_trace = [{"column": hc_name("trace_id"), "op": "=", "value": trace_id.strip().lower()}]
        limit = min(max_spans + 1, MAX_RESULT_ROWS)
        main, _ = self._group_rows(
            [{"op": "MAX", "column": "duration_ms"}],
            ["span_id", "name", "service.name"],
            by_trace,
            win,
            [],
            limit=limit,
        )
        parents, _ = self._group_rows(
            [{"op": "COUNT"}], ["span_id", "parent_span_id"], by_trace, win, [], limit=limit
        )
        errors, _ = self._group_rows(
            [{"op": "COUNT"}],
            ["span_id"],
            by_trace + [{"column": "error", "op": "=", "value": True}],
            win,
            [],
            limit=limit,
        )
        parent_of = {r[hc_name("span_id")]: r[hc_name("parent_span_id")] for r in parents}
        error_ids = {r[hc_name("span_id")] for r in errors}
        spans = [
            Span(
                span_id=r[hc_name("span_id")],
                parent_span_id=parent_of.get(r[hc_name("span_id")]),
                name=r.get("name"),
                service=r.get("service.name"),
                duration_ms=r.get("MAX(duration_ms)"),
                error=r[hc_name("span_id")] in error_ids,
            )
            for r in main
        ]
        spans.sort(key=lambda s: (s.parent_span_id is not None, -(s.duration_ms or 0)))
        return Trace(
            trace_id=trace_id,
            spans=spans[:max_spans],
            span_count=len(spans),
            truncated=len(spans) > max_spans,
        )

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
        cat = self._cat()
        if not 1 <= max_values_per_field <= 50:
            raise InvalidQueryError("max_values_per_field must be between 1 and 50")
        sel = [cat.check_filter(f) for f in selection_filters]
        sel_win = self._window(time_window)
        sel_specs, sel_comb = [self._filter_spec(cf) for cf in sel], "AND"
        if baseline_filters is not None:
            base = [cat.check_filter(f) for f in baseline_filters]
            base_win = self._window(baseline_window or time_window)
            base_specs, base_comb = [self._filter_spec(cf) for cf in base], "AND"
        elif baseline_window is not None:
            base_win = self._window(baseline_window)
            base_specs, base_comb = sel_specs, sel_comb
        else:
            if not sel:
                raise InvalidQueryError(
                    "selection has no filters, so its complement is empty; pass baseline_filters "
                    "or baseline_window"
                )
            base_win = sel_win
            # NOT (a AND b) == (NOT a) OR (NOT b)
            base_specs, base_comb = [self._filter_spec(cf, negate=True) for cf in sel], "OR"

        names, skipped = cat.comparable_fields(None, fields, {f.field for f in sel})
        names = [n for n in names if n != "is_root"]
        if fields is None:
            preferred = [n for n in DEFAULT_COMPARE_FIELDS if n in names]
            skipped.update(
                {n: "not evaluated (no explicit `fields`)" for n in names if n not in preferred}
            )
            names = preferred
        if len(names) > self._max_compare:
            for n in names[self._max_compare :]:
                skipped[n] = (
                    f"not evaluated: Honeycomb allows 10 query results/minute, so at most "
                    f"{self._max_compare} fields are compared per call; ask again with these"
                )
            names = names[: self._max_compare]

        def run(specs, comb, win, calcs, breakdown=()):
            return self._query(
                self._spec(calcs, win, breakdowns=breakdown, filters=specs, combination=comb),
                result_limit=MAX_RESULT_ROWS,
            )[0]

        def count(specs, comb, win) -> int:
            rows = run(specs, comb, win, [{"op": "COUNT"}])
            return int(rows[0].get("COUNT", 0)) if rows else 0

        n_sel, n_base = count(sel_specs, sel_comb, sel_win), count(base_specs, base_comb, base_win)
        results: list[FieldComparison] = []
        if n_sel and n_base:
            for name in names:
                info = cat.get(name)
                if info.type is FieldType.FLOAT:
                    results.append(
                        self._compare_numeric(
                            name,
                            info,
                            n_sel,
                            n_base,
                            run,
                            (sel_specs, sel_comb, sel_win),
                            (base_specs, base_comb, base_win),
                        )  # fmt: skip
                    )
                else:
                    results.append(
                        self._compare_categorical(
                            name,
                            info,
                            n_sel,
                            n_base,
                            max_values_per_field,
                            run,
                            (sel_specs, sel_comb, sel_win),
                            (base_specs, base_comb, base_win),
                        )  # fmt: skip
                    )
        results.sort(key=lambda f: (-f.score, f.field))
        return DistributionComparison(
            selection_rows=n_sel,
            baseline_rows=n_base,
            selection_window=sel_win,
            baseline_window=base_win,
            fields=results,
            skipped_fields=skipped,
        )

    def _compare_categorical(
        self, name, info, n_sel, n_base, max_values, run, sel_q, base_q
    ) -> FieldComparison:
        def counts(q) -> dict[Any, int]:
            rows = run(*q, [{"op": "COUNT"}], [name])
            return {self._value_out(name, r.get(hc_name(name))): int(r["COUNT"]) for r in rows}

        sel_counts, base_counts = counts(sel_q), counts(base_q)
        if n_sel - sum(sel_counts.values()) > 0:
            sel_counts[None] = n_sel - sum(sel_counts.values())
        if n_base - sum(base_counts.values()) > 0:
            base_counts[None] = n_base - sum(base_counts.values())
        score, scored = score_distribution(sel_counts, base_counts, n_sel, n_base)
        values = [
            ValueShare(
                value=sv.key,
                absent=sv.key is None,
                selection_count=sel_counts.get(sv.key, 0),
                baseline_count=base_counts.get(sv.key, 0),
                selection_fraction=sv.p_sel,
                baseline_fraction=sv.p_base,
                delta=sv.delta,
            )
            for sv in scored[:max_values]
        ]
        return FieldComparison(
            field=name,
            type=info.type,
            kind="categorical",
            score=score,
            score_method="max_value_delta_lcb",
            selection_present_fraction=1 - sel_counts.get(None, 0) / n_sel,
            baseline_present_fraction=1 - base_counts.get(None, 0) / n_base,
            total_values=len({k for k in sel_counts.keys() | base_counts.keys() if k is not None}),
            values=values,
        )

    def _compare_numeric(self, name, info, n_sel, n_base, run, sel_q, base_q) -> FieldComparison:
        col = hc_name(name)
        quantile_ops = ["P10", "P20", "P25", "P50", "P75", "P80", "P90", "P95", "P99"]

        def calcs(*ops: str) -> list[dict]:
            return [{"op": o, "column": col} for o in ops]

        sel_row = (run(*sel_q, calcs("AVG", "P50", "P90", "P95", "MAX")) or [{}])[0]
        base_row = (run(*base_q, calcs("AVG", "MAX", *quantile_ops)) or [{}])[0]

        def get(row: dict, op: str) -> float | None:
            return row.get(f"{op}({col})")

        def stats(row: dict) -> NumericStats:
            return NumericStats(
                mean=get(row, "AVG"),
                p50=get(row, "P50"),
                p90=get(row, "P90"),
                p95=get(row, "P95"),
                max=get(row, "MAX"),
            )

        sel_stats, base_stats = stats(sel_row), stats(base_row)
        points = [
            (v, int(o[1:]) / 100) for o in quantile_ops if (v := get(base_row, o)) is not None
        ]
        score = 0.0
        if sel_stats.p50 is not None and points:
            score = abs(2 * _quantile_position(sel_stats.p50, points) - 1)
        return FieldComparison(
            field=name,
            type=info.type,
            kind="numeric",
            score=score,
            score_method="median_shift_vs_baseline_quantiles",
            values=[],
            selection_stats=sel_stats,
            baseline_stats=base_stats,
        )


def _dir(descending: bool) -> str:
    return "descending" if descending else "ascending"


def _quantile_position(x: float, points: list[tuple[float, float]]) -> float:
    """Position of x in [0, 1] on the empirical CDF given sorted (value, quantile) points."""
    pts = sorted(points)
    if x <= pts[0][0]:
        return 0.0 if x < pts[0][0] else pts[0][1]
    if x >= pts[-1][0]:
        return 1.0 if x > pts[-1][0] else pts[-1][1]
    for (v0, q0), (v1, q1) in zip(pts, pts[1:], strict=False):
        if v0 <= x <= v1:
            return q0 if v1 == v0 else q0 + (q1 - q0) * (x - v0) / (v1 - v0)
    return 1.0
