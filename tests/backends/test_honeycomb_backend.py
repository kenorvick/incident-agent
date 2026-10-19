"""HoneycombBackend against a fake Honeycomb (httpx.MockTransport); no network.

The smoke test at the bottom talks to the real API and only runs when HONEYCOMB_API_KEY is set.
"""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from incident_agent.backends import (
    BackendError,
    BackendUnavailableError,
    InvalidQueryError,
    UnknownFieldError,
    UnsupportedOperationError,
)
from incident_agent.backends.base import (
    Calc,
    Calculation,
    Filter,
    FilterOp,
    OrderBy,
    TimeWindow,
)
from incident_agent.backends.honeycomb_backend import HoneycombBackend

KEY = "secret-key-123"
NOW = datetime(2026, 10, 19, 21, 0, 0, tzinfo=UTC).timestamp()
COUNT = Calculation(op=Calc.COUNT)
COLUMNS = [
    {"key_name": "duration_ms", "type": "float"},
    {"key_name": "trace.trace_id", "type": "string"},
    {"key_name": "trace.span_id", "type": "string"},
    {"key_name": "trace.parent_id", "type": "string"},
    {"key_name": "service.name", "type": "string"},
    {"key_name": "name", "type": "string"},
    {"key_name": "error", "type": "boolean"},
    {"key_name": "status_code", "type": "integer"},
    {"key_name": "payment.provider", "type": "string"},
    {"key_name": "internal.hidden", "type": "string", "hidden": True},
]


def f(field, op="=", value=None) -> Filter:
    return Filter(field=field, op=FilterOp(op), value=value)


class Clock:
    def __init__(self):
        self.now = NOW
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeHoneycomb:
    """Implements create query / create query result / get query result / columns."""

    def __init__(self, polls_before_complete: int = 1):
        self.respond = self.schema_responder
        self.polls_before_complete = polls_before_complete
        self.requests: list[httpx.Request] = []
        self.specs: list[dict] = []
        self.result_bodies: list[dict] = []
        self._query_of_result: dict[str, dict] = {}
        self._polls: dict[str, int] = {}
        self.fail_create_result: list[httpx.Response] = []
        self.get_override: httpx.Response | None = None

    @staticmethod
    def schema_responder(spec: dict) -> dict:
        if spec["breakdowns"] == ["service.name"]:
            rows = [
                {"service.name": "checkout", "COUNT": 70},
                {"service.name": "payment", "COUNT": 30},
            ]
        else:
            rows = [{"COUNT": 100}]
        return {"results": [{"data": r} for r in rows]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/1/columns/"):
            return httpx.Response(200, json=COLUMNS)
        if request.method == "POST" and path.startswith("/1/queries/"):
            spec = json.loads(request.content)
            self.specs.append(spec)
            return httpx.Response(200, json={"id": f"q{len(self.specs)}", **spec})
        if request.method == "POST" and path.startswith("/1/query_results/"):
            if self.fail_create_result:
                return self.fail_create_result.pop(0)
            body = json.loads(request.content)
            self.result_bodies.append(body)
            rid = f"r{len(self.result_bodies)}"
            self._query_of_result[rid] = self.specs[int(body["query_id"][1:]) - 1]
            return httpx.Response(
                201, json={"id": rid, "complete": False}, headers={"Location": f"{path}/{rid}"}
            )
        if request.method == "GET" and path.startswith("/1/query_results/"):
            if self.get_override is not None:
                return self.get_override
            rid = path.rsplit("/", 1)[1]
            self._polls[rid] = self._polls.get(rid, 0) + 1
            if self._polls[rid] <= self.polls_before_complete:
                return httpx.Response(200, json={"id": rid, "complete": False})
            data = self.respond(self._query_of_result[rid])
            return httpx.Response(
                200, json={"id": rid, "complete": True, "data": {"series": [], **data}}
            )
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})


@pytest.fixture
def hc():
    """(server, backend, clock) with the schema already loaded and request logs cleared."""

    def build(**kwargs):
        server = FakeHoneycomb(**kwargs.pop("server", {}))
        clock = Clock()
        client = httpx.Client(
            base_url="https://api.honeycomb.io", transport=httpx.MockTransport(server)
        )
        be = HoneycombBackend(KEY, client=client, sleep=clock.sleep, clock=clock, **kwargs)
        be.schema()
        server.specs.clear()
        server.result_bodies.clear()
        server.requests.clear()
        server._query_of_result.clear()
        server._polls.clear()
        clock.sleeps.clear()
        clock.now = NOW
        return server, be, clock

    return build


# ------------------------------------------------------------------ construction / schema


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("HONEYCOMB_API_KEY", raising=False)
    with pytest.raises(BackendUnavailableError, match="HONEYCOMB_API_KEY"):
        HoneycombBackend()


def test_schema_maps_names_skips_hidden_and_reports_services(hc):
    server = FakeHoneycomb()
    clock = Clock()
    client = httpx.Client(
        base_url="https://api.honeycomb.io", transport=httpx.MockTransport(server)
    )
    be = HoneycombBackend(KEY, client=client, sleep=clock.sleep, clock=clock)
    s = be.schema()
    names = s.field_names()
    assert {"trace_id", "span_id", "parent_span_id", "duration_ms", "is_root"} <= set(names)
    assert "internal.hidden" not in names and "trace.trace_id" not in names
    assert next(x for x in s.fields if x.name == "duration_ms").type == "float"
    # integer status_code is exposed as canonical strings (OK/ERROR/UNSET) and translated
    assert next(x for x in s.fields if x.name == "status_code").type == "string"
    assert s.row_count == 100 and [(x.name, x.rows) for x in s.services][0] == ("checkout", 70)
    assert s.time_end > s.time_start and s.notes
    first = server.requests[0]
    assert first.headers["X-Honeycomb-Team"] == KEY and first.url.path == "/1/columns/__all__"
    be.schema()
    assert sum(r.url.path.startswith("/1/columns") for r in server.requests) == 1  # cached


def test_unknown_field_suggestion_uses_canonical_names(hc):
    _, be, _ = hc()
    with pytest.raises(UnknownFieldError) as exc:
        be.aggregate([COUNT], group_by=["servce.name"])
    assert exc.value.suggestions == ["service.name"]
    with pytest.raises(UnknownFieldError):
        be.aggregate([COUNT], group_by=["trace.trace_id"])  # Honeycomb's name is not canonical


# ------------------------------------------------------------------ query-spec construction


def test_aggregate_builds_expected_spec(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {
        "results": [{"data": {"service.name": "payment", "COUNT": 5, "P95(duration_ms)": 120.0}}]
    }
    res = be.aggregate(
        [COUNT, Calculation(op=Calc.P95, field="duration_ms")],
        filters=[
            f("name", "starts_with", "POST"),
            f("trace_id", "in", ["a", "b"]),
            f("payment.provider", "exists"),
            f("is_root", "=", True),
            f("name", "!=", "x"),
            f("duration_ms", ">=", 5),
            f("payment.provider", "not_in", ["z"]),
            f("name", "contains", "ch"),
            f("payment.provider", "does_not_exist"),
        ],
        group_by=["service.name"],
        time_window=TimeWindow(last_seconds=300),
        limit=5,
    )
    assert server.specs == [
        {
            "calculations": [{"op": "COUNT"}, {"op": "P95", "column": "duration_ms"}],
            "breakdowns": ["service.name"],
            "filters": [
                {"column": "name", "op": "starts-with", "value": "POST"},
                {"column": "trace.trace_id", "op": "in", "value": ["a", "b"]},
                {"column": "payment.provider", "op": "exists"},
                {"column": "trace.parent_id", "op": "does-not-exist"},
                {"column": "name", "op": "!=", "value": "x"},
                {"column": "duration_ms", "op": ">=", "value": 5.0},
                {"column": "payment.provider", "op": "not-in", "value": ["z"]},
                {"column": "name", "op": "contains", "value": "ch"},
                {"column": "payment.provider", "op": "does-not-exist"},
            ],
            "filter_combination": "AND",
            "orders": [{"op": "COUNT", "order": "descending"}],
            "limit": 1000,
            "start_time": int(NOW) - 300,
            "end_time": int(NOW),
        }
    ]
    assert server.result_bodies == [
        {
            "query_id": "q1",
            "disable_series": True,
            "disable_total_by_aggregate": True,
            "disable_other_by_aggregate": True,
            "limit": 10000,
        }
    ]
    g = res.groups[0]
    assert g.key == {"service.name": "payment"}
    assert g.values == {"count": 5, "p95(duration_ms)": 120.0}
    assert (
        res.total_groups == 1 and not res.truncated and res.calcs == ["count", "p95(duration_ms)"]
    )
    for req in server.requests:
        assert req.headers["X-Honeycomb-Team"] == KEY


def test_aggregate_orders_and_absolute_window(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {"results": []}
    start, end = (
        datetime(2026, 10, 19, 20, 0, tzinfo=UTC),
        datetime(2026, 10, 19, 20, 30, tzinfo=UTC),
    )
    res = be.aggregate(
        [Calculation(op=Calc.AVG, field="duration_ms")],
        group_by=["name"],
        order_by=OrderBy(key="avg(duration_ms)", descending=False),
        time_window=TimeWindow(start=start, end=end),
    )
    spec = server.specs[0]
    assert spec["orders"] == [{"op": "AVG", "column": "duration_ms", "order": "ascending"}]
    assert (spec["start_time"], spec["end_time"]) == (int(start.timestamp()), int(end.timestamp()))
    assert res.groups == [] and res.total_groups == 0
    be.aggregate([COUNT], group_by=["name"], order_by=OrderBy(key="name"))
    assert server.specs[1]["orders"] == [{"column": "name", "order": "descending"}]


def test_aggregate_ungrouped_empty_result_counts_zero(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {"results": []}
    res = be.aggregate([COUNT, Calculation(op=Calc.P95, field="duration_ms")])
    assert res.groups[0].values == {"count": 0, "p95(duration_ms)": None}


def test_group_truncation_reports_total(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {
        "results": [{"data": {"name": f"n{i}", "COUNT": 100 - i}} for i in range(12)]
    }
    res = be.aggregate([COUNT], group_by=["name"], limit=5)
    assert len(res.groups) == 5 and res.total_groups == 12 and res.truncated


def test_error_rate_runs_second_query_with_error_filter(hc):
    server, be, _ = hc()

    def respond(spec):
        errors = any(x["column"] == "error" for x in spec["filters"])
        counts = {"payment": 2 if errors else 8, "checkout": 1 if errors else 2}
        rows = [{"service.name": k, "COUNT": v} for k, v in counts.items()]
        if errors:
            rows = rows[:1]  # checkout has no errors -> absent group
        return {"results": [{"data": r} for r in rows]}

    server.respond = respond
    res = be.aggregate(
        [Calculation(op=Calc.ERROR_RATE)],
        group_by=["service.name"],
        filters=[f("name", "=", "x")],
        order_by=OrderBy(key="error_rate", descending=True),
    )
    assert len(server.specs) == 2
    assert server.specs[0]["calculations"] == [{"op": "COUNT"}]
    assert server.specs[1]["filters"][-1] == {"column": "error", "op": "=", "value": True}
    assert server.specs[1]["filters"][0] == {"column": "name", "op": "=", "value": "x"}
    rates = {g.key["service.name"]: g.values["error_rate"] for g in res.groups}
    assert rates == {"payment": pytest.approx(0.25), "checkout": 0.0}
    assert [g.key["service.name"] for g in res.groups] == ["payment", "checkout"]


def test_granularity_series_is_dense(hc):
    server, be, _ = hc()

    def respond(spec):
        if "granularity" in spec:
            t = lambda sec: datetime.fromtimestamp(NOW - sec, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            series = [
                {"time": t(180), "data": {"service.name": "payment", "COUNT": 3}},
                {"time": t(60), "data": {"service.name": "payment", "COUNT": 5}},
            ]
            return {"series": series, "results": []}
        return {"results": [{"data": {"service.name": "payment", "COUNT": 8}}]}

    server.respond = respond
    res = be.aggregate(
        [COUNT],
        group_by=["service.name"],
        time_window=TimeWindow(last_seconds=180),
        granularity_seconds=60,
    )
    series = res.groups[0].series
    assert [b.values["count"] for b in series] == [3, 0, 5, 0]
    assert server.specs[1]["granularity"] == 60
    assert server.result_bodies[1]["disable_series"] is False
    assert "limit" not in server.result_bodies[1]


def test_status_code_values_translated_when_column_is_integer(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {"results": [{"data": {"status_code": 2, "COUNT": 4}}]}
    res = be.aggregate([COUNT], group_by=["status_code"], filters=[f("status_code", "=", "ERROR")])
    assert server.specs[0]["filters"] == [{"column": "status_code", "op": "=", "value": 2}]
    assert res.groups[0].key == {"status_code": "ERROR"}


def test_window_over_seven_days_rejected(hc):
    _, be, _ = hc()
    with pytest.raises(InvalidQueryError, match="7 days"):
        be.aggregate([COUNT], time_window=TimeWindow(last_seconds=8 * 86400))


def test_is_root_only_supports_equality(hc):
    _, be, _ = hc()
    with pytest.raises(InvalidQueryError, match="is_root"):
        be.aggregate([COUNT], filters=[f("is_root", "exists")])


# ------------------------------------------------------------------ polling, retries, errors


def test_polls_with_backoff_until_complete(hc):
    server, be, clock = hc(server={"polls_before_complete": 3})
    server.respond = lambda spec: {"results": [{"data": {"COUNT": 1}}]}
    be.aggregate([COUNT])
    assert clock.sleeps == [0.25, 0.375, 0.5625]
    gets = [r for r in server.requests if r.method == "GET"]
    assert len(gets) == 4 and gets[0].url.path == "/1/query_results/__all__/r1"


def test_poll_timeout(hc):
    server, be, clock = hc(poll_timeout=2.0)
    server.polls_before_complete = 10**9
    with pytest.raises(BackendError, match="did not complete"):
        be.aggregate([COUNT])
    assert sum(clock.sleeps) >= 2.0


def test_failed_query_result_surfaces_error(hc):
    server, be, _ = hc()
    server.get_override = httpx.Response(
        200, json={"id": "r1", "complete": True, "error": "we had trouble fetching your results"}
    )
    with pytest.raises(BackendError, match="trouble fetching"):
        be.aggregate([COUNT])


def test_rate_limit_backoff_uses_ratelimit_header(hc):
    server, be, clock = hc()
    server.respond = lambda spec: {"results": [{"data": {"COUNT": 1}}]}
    server.fail_create_result = [
        httpx.Response(429, headers={"RateLimit": "limit=10, remaining=0, reset=7"}, json={})
    ]
    be.aggregate([COUNT])
    assert 7.5 in clock.sleeps


def test_rate_limit_without_header_uses_exponential_then_gives_up(hc):
    server, be, clock = hc(max_retries=2)
    server.fail_create_result = [httpx.Response(429, json={}) for _ in range(5)]
    with pytest.raises(BackendUnavailableError, match="10/minute"):
        be.aggregate([COUNT])
    assert clock.sleeps == [1.0, 2.0]


def test_proactive_pacing_stays_under_query_limit(hc):
    server, be, clock = hc(queries_per_minute=2)
    server.respond = lambda spec: {"results": [{"data": {"COUNT": 1}}]}
    spec = be._spec([{"op": "COUNT"}], be._window(None))
    for _ in range(3):
        be._query(spec)
    assert any(s >= 59 for s in clock.sleeps)  # the third query waited out the window


def test_http_errors_are_translated_without_leaking_the_key(hc):
    server, be, _ = hc()
    server.fail_create_result = [httpx.Response(403, json={"error": "forbidden"})]
    with pytest.raises(BackendUnavailableError) as exc:
        be.aggregate([COUNT])
    assert "Manage Queries and Columns" in str(exc.value) and "Run Queries" in str(exc.value)
    assert KEY not in str(exc.value)
    server.fail_create_result = [httpx.Response(422, json={"error": "bad column"})]
    with pytest.raises(InvalidQueryError, match="bad column"):
        be.aggregate([COUNT])
    server.fail_create_result = [httpx.Response(404, json={"error": "dataset not found"})]
    with pytest.raises(BackendError, match="HONEYCOMB_DATASET"):
        be.aggregate([COUNT])


# ------------------------------------------------------------------ documented limitations


def test_sample_events_is_documented_as_unsupported(hc):
    _, be, _ = hc()
    with pytest.raises(UnsupportedOperationError, match="docs.honeycomb.io"):
        be.sample_events()


# ------------------------------------------------------------------ distinct values / trace


def test_distinct_values(hc):
    server, be, _ = hc()
    server.respond = lambda spec: {
        "results": [{"data": {"name": n, "COUNT": c}} for n, c in [("a", 5), ("b", 3), ("c", 1)]]
    }
    res = be.distinct_values("name", limit=2)
    assert [(v.value, v.count) for v in res.values] == [("a", 5), ("b", 3)]
    assert res.total_distinct == 3 and res.truncated and res.rows_with_field == 9
    assert server.specs[0]["breakdowns"] == ["name"]


def test_get_trace_is_rebuilt_from_grouped_queries(hc):
    server, be, _ = hc()

    def respond(spec):
        assert spec["filters"][0] == {"column": "trace.trace_id", "op": "=", "value": "abc"}
        if spec["breakdowns"] == ["trace.span_id", "name", "service.name"]:
            rows = [
                {"trace.span_id": "s2", "name": "charge", "service.name": "checkout",
                 "MAX(duration_ms)": 60.0},
                {"trace.span_id": "s1", "name": "POST /checkout", "service.name": "checkout",
                 "MAX(duration_ms)": 100.0},
            ]  # fmt: skip
        elif spec["breakdowns"] == ["trace.span_id", "trace.parent_id"]:
            rows = [{"trace.span_id": "s2", "trace.parent_id": "s1", "COUNT": 1}]
        else:
            assert spec["filters"][-1] == {"column": "error", "op": "=", "value": True}
            rows = [{"trace.span_id": "s2", "COUNT": 1}]
        return {"results": [{"data": r} for r in rows]}

    server.respond = respond
    trace = be.get_trace("ABC")
    assert [s.span_id for s in trace.spans] == ["s1", "s2"]  # root first
    root, child = trace.spans
    assert root.parent_span_id is None and child.parent_span_id == "s1"
    assert (root.error, child.error) == (False, True)
    assert child.duration_ms == 60.0 and child.service == "checkout" and child.start is None
    assert trace.span_count == 2 and not trace.truncated
    assert (server.specs[0]["end_time"] - server.specs[0]["start_time"]) < 7 * 86400


# ------------------------------------------------------------------ compare_distributions


def _compare_responder(spec):
    comp = spec["filter_combination"]
    sel = comp == "AND"
    ops = [c["op"] for c in spec["calculations"]]
    by = spec["breakdowns"]
    if not by and ops == ["COUNT"]:
        return {"results": [{"data": {"COUNT": 10 if sel else 90}}]}
    if by == ["service.name"]:
        counts = {"payment": 8, "checkout": 2} if sel else {"checkout": 70, "payment": 20}
        return {"results": [{"data": {"service.name": k, "COUNT": v}} for k, v in counts.items()]}
    if by == ["name"]:
        counts = {"POST /charge": 9, "charge": 1} if sel else {"GET /cart": 60, "charge": 30}
        return {"results": [{"data": {"name": k, "COUNT": v}} for k, v in counts.items()]}
    if by == ["payment.provider"]:
        counts = {"adyen": 3} if sel else {"stripe": 20}
        return {
            "results": [{"data": {"payment.provider": k, "COUNT": v}} for k, v in counts.items()]
        }
    if "P50" in ops and "P10" not in ops:
        stats = {"AVG": 2000, "P50": 2000, "P90": 2200, "P95": 2300, "MAX": 2500}
        return {"results": [{"data": {f"{k}(duration_ms)": v for k, v in stats.items()}}]}
    quantiles = {10: 10, 20: 20, 25: 25, 50: 50, 75: 75, 80: 80, 90: 90, 95: 95, 99: 900}
    base = {f"P{p}(duration_ms)": v for p, v in quantiles.items()}
    return {"results": [{"data": {**base, "AVG(duration_ms)": 60, "MAX(duration_ms)": 900}}]}


def test_compare_complement_uses_negated_or_filters_and_absent_bucket(hc):
    server, be, _ = hc()
    server.respond = _compare_responder
    cmp = be.compare_distributions(
        selection_filters=[f("error", "=", True), f("name", "starts_with", "POST")],
        fields=["service.name", "payment.provider"],
    )
    assert (cmp.selection_rows, cmp.baseline_rows) == (10, 90)
    base_count_spec = server.specs[1]
    assert base_count_spec["filter_combination"] == "OR"
    assert base_count_spec["filters"] == [
        {"column": "error", "op": "!=", "value": True},
        {"column": "name", "op": "does-not-start-with", "value": "POST"},
    ]
    by = {x.field: x for x in cmp.fields}
    svc = {v.value: v for v in by["service.name"].values}
    assert svc["payment"].selection_fraction == pytest.approx(0.8)
    assert svc["payment"].baseline_fraction == pytest.approx(20 / 90)
    prov = by["payment.provider"]
    assert prov.selection_present_fraction == pytest.approx(0.3)
    assert any(v.absent and v.value is None for v in prov.values)
    assert cmp.fields[0].score >= cmp.fields[1].score
    assert by["service.name"].score_method == "max_value_delta_lcb"


def test_compare_caps_fields_and_reports_skipped(hc):
    server, be, _ = hc(max_compare_fields=2)
    server.respond = _compare_responder
    cmp = be.compare_distributions(selection_filters=[f("error", "=", True)])
    assert len(cmp.fields) == 2
    assert "error" in cmp.skipped_fields  # used in selection
    assert any("10 query results/minute" in r for r in cmp.skipped_fields.values())
    assert "trace_id" in cmp.skipped_fields
    # 2 count queries + 2 per categorical field (selection + baseline)
    assert len(server.specs) <= 2 + 2 * 2 + 2


def test_compare_numeric_field_uses_median_shift(hc):
    server, be, _ = hc()
    server.respond = _compare_responder
    cmp = be.compare_distributions(
        selection_filters=[f("error", "=", True)], fields=["duration_ms"]
    )
    (fc,) = cmp.fields
    assert fc.kind == "numeric" and fc.values == []
    assert fc.score == 1.0 and fc.score_method == "median_shift_vs_baseline_quantiles"
    assert fc.selection_stats.p95 == 2300 and fc.baseline_stats.p50 == 50


def test_compare_baseline_modes(hc):
    server, be, _ = hc()
    server.respond = _compare_responder
    with pytest.raises(InvalidQueryError, match="complement"):
        be.compare_distributions()
    be.compare_distributions(
        selection_filters=[f("error", "=", True)],
        time_window=TimeWindow(last_seconds=60),
        baseline_window=TimeWindow(last_seconds=3600),
        fields=["service.name"],
    )
    sel_spec, base_spec = server.specs[0], server.specs[1]
    assert sel_spec["filters"] == base_spec["filters"]  # same slice, different time
    assert sel_spec["start_time"] != base_spec["start_time"]
    be.compare_distributions(
        selection_filters=[f("error", "=", True)],
        baseline_filters=[f("error", "=", False)],
        fields=["service.name"],
    )
    assert server.specs[-4]["filters"] == [{"column": "error", "op": "=", "value": True}]


# ------------------------------------------------------------------ live smoke test


@pytest.mark.skipif(not os.environ.get("HONEYCOMB_API_KEY"), reason="HONEYCOMB_API_KEY not set")
def test_live_smoke():
    be = HoneycombBackend.from_env(lookback_seconds=3600)
    try:
        schema = be.schema()
        assert schema.fields and schema.time_end > schema.time_start
        res = be.aggregate([COUNT], time_window=TimeWindow(last_seconds=600))
        assert res.groups[0].values["count"] >= 0
        assert be.distinct_values("service.name", limit=5).total_distinct >= 0
    finally:
        be.close()
    assert timedelta(0) < schema.time_end - schema.time_start
