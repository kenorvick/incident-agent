"""DuckDBBackend over the tiny hand-written fixture (see telemetry_rows.py for the layout)."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from telemetry_rows import T0, write_events

from incident_agent.backends import (
    BackendError,
    FieldTypeError,
    InvalidQueryError,
    TelemetryBackend,
    UnknownFieldError,
)
from incident_agent.backends.base import (
    Calc,
    Calculation,
    Filter,
    FilterOp,
    OrderBy,
    SampleOrder,
    TimeWindow,
)
from incident_agent.backends.duckdb_backend import DuckDBBackend

COUNT = Calculation(op=Calc.COUNT)


def calc(op: Calc, field: str | None = None) -> Calculation:
    return Calculation(op=op, field=field)


def count(backend, *filters: Filter, window: TimeWindow | None = None) -> int:
    return backend.aggregate([COUNT], filters=filters, time_window=window).groups[0].values["count"]


def f(field: str, op: FilterOp | str = FilterOp.EQ, value=None) -> Filter:
    return Filter(field=field, op=FilterOp(op), value=value)


# ------------------------------------------------------------------ schema / loading


def test_satisfies_protocol(backend):
    assert isinstance(backend, TelemetryBackend)


def test_schema_basics(backend):
    s = backend.schema()
    assert s.row_count == 44
    assert s.time_start == T0
    assert s.time_end == T0 + timedelta(seconds=540.03)
    assert {(x.name, x.rows) for x in s.services} == {("checkout", 24), ("payment", 20)}
    assert s.services[0].name == "checkout"  # ordered by rows desc


def test_schema_fields(backend):
    by_name = {x.name: x for x in backend.schema().fields}
    assert by_name["timestamp"].type == "timestamp"
    assert by_name["duration_ms"].type == "float"
    assert by_name["error"].type == "boolean"
    assert by_name["http.response.status_code"].type == "integer"
    assert by_name["http.route"].fraction_present == pytest.approx(24 / 44)  # dotted, sparse
    assert by_name["user_id"].cardinality == 14  # high cardinality
    assert by_name["payment.provider"].fraction_present == pytest.approx(10 / 44)
    assert by_name["payment.provider"].cardinality == 2
    assert by_name["exception.type"].fraction_present == pytest.approx(3 / 44)


def test_conflicting_types_become_strings_with_note(backend):
    s = backend.schema()
    info = next(x for x in s.fields if x.name == "retry.count")
    assert info.type == "string" and info.mixed_types
    assert any("retry.count" in n for n in s.notes)
    # int values were stringified, not lost
    values = {v.value for v in backend.distinct_values("retry.count").values}
    assert values == {"0", "1", "2", "n/a"}


def test_int_and_float_union_to_float(backend):
    info = next(x for x in backend.schema().fields if x.name == "app.cart.total")
    assert info.type == "float"


def test_rows_with_different_keys_and_odd_column_names(tmp_path):
    rows = [
        {"timestamp": "2026-10-19T21:00:00Z", "a": True, "weird\"name'x": 1},
        {"timestamp": "2026-10-19T21:00:01Z", "b": 2.5, "weird\"name'x": 3, "mix": 1},
        {"timestamp": "2026-10-19T21:00:02Z", "c": "z", "mix": True},
        {"timestamp": "2026-10-19T21:00:03Z", "Mix": "dup-case"},
    ]
    be = DuckDBBackend(write_events(tmp_path / "e.jsonl.gz", rows))
    types = {x.name: x.type.value for x in be.schema().fields}
    assert types["a"] == "boolean" and types["b"] == "float" and types["c"] == "string"
    assert types["weird\"name'x"] == "integer"
    assert types["mix"] == "string"  # int + bool conflict
    assert "Mix" not in types and any("Mix" in n for n in be.schema().notes)
    got = be.aggregate([calc(Calc.SUM, "weird\"name'x")])
    assert got.groups[0].values["sum(weird\"name'x)"] == 4


def test_from_fixture_missing_lists_available(fixtures_dir):
    with pytest.raises(FileNotFoundError, match="tiny"):
        DuckDBBackend.from_fixture(fixtures_dir, "nope")


def test_empty_or_timestampless_file_rejected(tmp_path):
    with pytest.raises(BackendError, match="no events"):
        DuckDBBackend(write_events(tmp_path / "empty.jsonl.gz", []))
    with pytest.raises(BackendError, match="timestamp"):
        DuckDBBackend(write_events(tmp_path / "nots.jsonl.gz", [{"a": 1}]))


# ------------------------------------------------------------------ aggregate


def test_aggregate_ungrouped_all_calcs(backend):
    flt = [f("service.name", "=", "payment"), f("name", "=", "db.query")]
    res = backend.aggregate(
        [
            COUNT,
            calc(Calc.AVG, "duration_ms"),
            calc(Calc.SUM, "duration_ms"),
            calc(Calc.MIN, "duration_ms"),
            calc(Calc.MAX, "duration_ms"),
            calc(Calc.COUNT_DISTINCT, "trace_id"),
            calc(Calc.P50, "duration_ms"),
            calc(Calc.P90, "duration_ms"),
            calc(Calc.P95, "duration_ms"),
            calc(Calc.P99, "duration_ms"),
            calc(Calc.ERROR_RATE),
        ],
        filters=flt,
    )
    v = res.groups[0].values
    assert v["count"] == 10 and v["sum(duration_ms)"] == 990 and v["avg(duration_ms)"] == 99
    assert (v["min(duration_ms)"], v["max(duration_ms)"]) == (10, 900)
    assert v["count_distinct(trace_id)"] == 10
    assert v["p50(duration_ms)"] == 10
    assert v["p90(duration_ms)"] == pytest.approx(99)
    assert v["p95(duration_ms)"] == pytest.approx(499.5)
    assert v["p99(duration_ms)"] == pytest.approx(819.9)
    assert v["error_rate"] == 0
    assert res.total_groups == 1 and not res.truncated and res.matched_rows == 10


def test_error_rate_by_service(backend):
    res = backend.aggregate([calc(Calc.ERROR_RATE)], group_by=["service.name"])
    rates = {g.key["service.name"]: g.values["error_rate"] for g in res.groups}
    assert rates == {"checkout": pytest.approx(6 / 24), "payment": pytest.approx(3 / 20)}
    assert res.groups[0].key["service.name"] == "checkout"  # ordered by first calc desc


def test_group_by_truncation_reports_total_groups(backend):
    res = backend.aggregate([COUNT], group_by=["user_id"], limit=3)
    assert len(res.groups) == 3 and res.total_groups == 14 and res.truncated
    assert res.matched_rows == 44  # rows missing user_id are matched but not grouped
    assert all(g.values["count"] == 2 for g in res.groups)  # ties broken by key, stable
    assert [g.key["user_id"] for g in res.groups] == ["user-000", "user-001", "user-002"]


def test_multi_group_by_and_ordering(backend):
    res = backend.aggregate(
        [COUNT, calc(Calc.P95, "duration_ms")],
        group_by=["service.name", "name"],
        order_by=OrderBy(key="p95(duration_ms)", descending=False),
        limit=50,
    )
    assert res.total_groups == 5
    p95s = [g.values["p95(duration_ms)"] for g in res.groups]
    assert p95s == sorted(p95s)
    res2 = backend.aggregate(
        [COUNT], group_by=["name"], order_by=OrderBy(key="name", descending=False), limit=50
    )
    assert [g.key["name"] for g in res2.groups] == sorted(g.key["name"] for g in res2.groups)


def test_order_by_unknown_key_rejected(backend):
    with pytest.raises(InvalidQueryError, match="valid keys"):
        backend.aggregate([COUNT], order_by=OrderBy(key="p95(duration_ms)"))


def test_rows_missing_group_field_are_excluded(backend):
    res = backend.aggregate([COUNT], group_by=["payment.provider"])
    assert {g.key["payment.provider"]: g.values["count"] for g in res.groups} == {
        "stripe": 7,
        "adyen": 3,
    }


def test_empty_results(backend):
    res = backend.aggregate([COUNT, calc(Calc.P95, "duration_ms")], filters=[f("name", "=", "zz")])
    assert res.groups[0].values == {"count": 0, "p95(duration_ms)": None}
    assert res.matched_rows == 0
    grouped = backend.aggregate([COUNT], filters=[f("name", "=", "zz")], group_by=["name"])
    assert grouped.groups == [] and grouped.total_groups == 0 and not grouped.truncated


def test_validation_errors(backend):
    with pytest.raises(FieldTypeError, match="numeric"):
        backend.aggregate([calc(Calc.P95, "name")])
    with pytest.raises(FieldTypeError):
        backend.aggregate([calc(Calc.AVG, "error")])
    with pytest.raises(InvalidQueryError, match="timestamp"):
        backend.aggregate([COUNT], group_by=["timestamp"])
    with pytest.raises(InvalidQueryError):
        backend.aggregate([])
    with pytest.raises(InvalidQueryError):
        backend.aggregate([COUNT, COUNT])
    with pytest.raises(InvalidQueryError):
        backend.aggregate([COUNT], limit=0)
    with pytest.raises(ValidationError):
        Calculation(op=Calc.ERROR_RATE, field="error")
    with pytest.raises(ValidationError):
        Calculation(op=Calc.P95)


def test_count_distinct_allowed_on_strings(backend):
    res = backend.aggregate([calc(Calc.COUNT_DISTINCT, "user_id")])
    assert res.groups[0].values["count_distinct(user_id)"] == 14


# ------------------------------------------------------------------ unknown fields


def test_unknown_field_suggests_close_match(backend):
    with pytest.raises(UnknownFieldError) as exc:
        backend.aggregate([COUNT], group_by=["usr_id"])
    assert exc.value.field == "usr_id" and "user_id" in exc.value.suggestions
    assert "did you mean `user_id`" in str(exc.value)


def test_unknown_field_loose_suggestions(backend):
    with pytest.raises(UnknownFieldError) as exc:
        backend.distinct_values("route")
    assert "http.route" in exc.value.suggestions
    with pytest.raises(UnknownFieldError) as exc:
        backend.aggregate([calc(Calc.P95, "duration")])
    assert exc.value.suggestions[0] == "duration_ms"


def test_unknown_field_everywhere(backend):
    for call in (
        lambda: backend.aggregate([COUNT], filters=[f("srvice.name", "=", "x")]),
        lambda: backend.distinct_values("nope"),
        lambda: backend.sample_events(fields=["nope"]),
        lambda: backend.compare_distributions(selection_filters=[f("nope", "exists")]),
        lambda: backend.compare_distributions(
            selection_filters=[f("error", "=", True)], fields=["nope"]
        ),
    ):
        with pytest.raises(UnknownFieldError):
            call()


def test_no_suggestions_message(backend):
    with pytest.raises(UnknownFieldError, match="not found$"):
        backend.distinct_values("qqqqqqqq")


# ------------------------------------------------------------------ filters


@pytest.mark.parametrize(
    ("flt", "expected"),
    [
        (f("service.name", "=", "payment"), 20),
        (f("service.name", "!=", "payment"), 24),
        (f("payment.provider", "!=", "stripe"), 3),  # absent rows do not match !=
        (f("duration_ms", ">", 2000), 8),
        (f("duration_ms", ">=", 2000), 9),
        (f("duration_ms", "<", 20), 9),
        (f("duration_ms", "<=", 10), 9),
        (f("name", "in", ["charge", "db.query"]), 20),
        (f("service.name", "not_in", ["payment"]), 24),
        (f("payment.provider", "exists"), 10),
        (f("payment.provider", "does_not_exist"), 34),
        (f("status_message", "contains", "TIMEOUT"), 9),  # case-insensitive
        (f("name", "starts_with", "POST"), 20),
        (f("error", "=", True), 9),
        (f("error", "=", "true"), 9),  # coerced
        (f("http.response.status_code", "=", "500"), 6),  # coerced to integer
        (f("http.response.status_code", "in", ["500", 404]), 6),
        (f("retry.count", "=", 1), 6),  # int coerced onto a mixed-type string field
        (f("timestamp", ">=", "2026-10-19T21:08:00Z"), 8),
    ],
)
def test_filter_ops(backend, flt, expected):
    assert count(backend, flt) == expected


def test_filters_are_anded(backend):
    assert count(backend, f("service.name", "=", "payment"), f("error", "=", True)) == 3


def test_filter_type_errors(backend):
    with pytest.raises(FieldTypeError):
        count(backend, f("duration_ms", "contains", "1"))
    with pytest.raises(FieldTypeError):
        count(backend, f("name", ">", "a"))
    with pytest.raises(InvalidQueryError):
        count(backend, f("http.response.status_code", "=", "abc"))
    with pytest.raises(InvalidQueryError):
        count(backend, f("error", "=", "maybe"))
    with pytest.raises(InvalidQueryError):
        count(backend, f("timestamp", ">", "not a date"))


def test_filter_model_validation():
    with pytest.raises(ValidationError):
        Filter(field="a", op=FilterOp.EXISTS, value=1)
    with pytest.raises(ValidationError):
        Filter(field="a", op=FilterOp.IN, value="x")
    with pytest.raises(ValidationError):
        Filter(field="a", op=FilterOp.IN, value=[])
    with pytest.raises(ValidationError):
        Filter(field="a", op=FilterOp.EQ, value=None)
    with pytest.raises(ValidationError):
        Filter(field="a", op=FilterOp.EQ, value=[1])


# ------------------------------------------------------------------ SQL injection


@pytest.mark.parametrize(
    "payload",
    [
        "x' OR '1'='1",
        "payment'; DROP TABLE events; --",
        '" OR 1=1 --',
        "%",
        "payment') OR TRUE --",
    ],
)
def test_injection_in_filter_values_is_inert(backend, payload):
    assert count(backend, f("service.name", "=", payload)) == 0
    assert count(backend, f("service.name", "contains", payload)) == 0
    assert count(backend, f("service.name", "starts_with", payload)) == 0
    assert count(backend, f("service.name", "in", [payload, "nope"])) == 0
    assert count(backend) == 44  # table intact


def test_injection_in_field_names_is_rejected(backend):
    for evil in ('service.name" FROM events; DROP TABLE events; --', "1; SELECT 1", '"'):
        with pytest.raises(UnknownFieldError):
            backend.aggregate([COUNT], group_by=[evil])
        with pytest.raises(UnknownFieldError):
            backend.aggregate([calc(Calc.AVG, evil)])
        with pytest.raises(UnknownFieldError):
            count(backend, f(evil, "=", "x"))
        with pytest.raises(InvalidQueryError):
            backend.aggregate([COUNT], order_by=OrderBy(key=evil))
    assert count(backend) == 44


def test_injection_via_trace_id(backend):
    assert backend.get_trace("x' OR '1'='1").spans == []


# ------------------------------------------------------------------ time windows


def test_relative_window_uses_dataset_end(backend):
    res = backend.aggregate([COUNT], time_window=TimeWindow(last_seconds=100))
    assert res.groups[0].values["count"] == 8 and res.matched_rows == 8
    assert res.window.end == backend.schema().time_end + timedelta(microseconds=1)
    assert res.window.start == backend.schema().time_end - timedelta(seconds=100)
    assert count(backend, window=TimeWindow(last_seconds=10_000)) == 44
    assert count(backend, window=TimeWindow(last_seconds=1)) == 4  # the last trace's spans


def test_absolute_window_start_inclusive_end_exclusive(backend):
    w = TimeWindow(start=T0 + timedelta(seconds=60), end=T0 + timedelta(seconds=120))
    assert count(backend, window=w) == 4  # exactly trace 1
    w2 = TimeWindow(start=T0 + timedelta(seconds=60), end=T0 + timedelta(seconds=60.02))
    assert count(backend, window=w2) == 2  # +0.00 and +0.01; +0.02 is excluded
    only_end = TimeWindow(end=T0 + timedelta(seconds=60))
    assert count(backend, window=only_end) == 4 + 1  # trace 0 + the cart at +30 s
    only_start = TimeWindow(start=T0 + timedelta(seconds=480))
    assert count(backend, window=only_start) == 8


def test_absolute_window_from_iso_strings_naive_is_utc(backend):
    w = TimeWindow.model_validate({"start": "2026-10-19T21:01:00", "end": "2026-10-19T21:02:00Z"})
    assert w.start == T0 + timedelta(seconds=60) and w.start.tzinfo is not None
    assert count(backend, window=w) == 4
    w_offset = TimeWindow.model_validate(
        {"start": "2026-10-19T23:01:00+02:00", "end": "2026-10-19T23:02:00+02:00"}
    )
    assert count(backend, window=w_offset) == 4


def test_window_outside_dataset_is_empty(backend):
    far = datetime(2030, 1, 1, tzinfo=UTC)
    res = backend.aggregate([COUNT], time_window=TimeWindow(start=far))
    assert res.groups[0].values["count"] == 0


def test_time_window_validation():
    for bad in (
        {},
        {"last_seconds": 0},
        {"last_seconds": 5, "start": "2026-10-19T21:00:00Z"},
        {"start": "2026-10-19T22:00:00Z", "end": "2026-10-19T21:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            TimeWindow.model_validate(bad)


# ------------------------------------------------------------------ granularity buckets


def test_granularity_buckets_are_dense_and_epoch_aligned(backend):
    w = TimeWindow(start=T0, end=T0 + timedelta(seconds=600))
    res = backend.aggregate([COUNT], time_window=w, granularity_seconds=120)
    series = res.groups[0].series
    assert res.granularity_seconds == 120
    assert [b.start for b in series] == [T0 + timedelta(seconds=120 * i) for i in range(5)]
    assert [b.values["count"] for b in series] == [9, 9, 9, 9, 8]
    assert res.groups[0].values["count"] == 44


def test_granularity_zero_fills_empty_buckets(backend):
    w = TimeWindow(start=T0, end=T0 + timedelta(seconds=600))
    res = backend.aggregate(
        [COUNT, calc(Calc.AVG, "duration_ms")],
        filters=[f("error", "=", True)],
        time_window=w,
        granularity_seconds=30,
    )
    series = res.groups[0].series
    assert len(series) == 20
    assert all(b.values["count"] == 0 and b.values["avg(duration_ms)"] is None for b in series[:14])
    assert series[14].start == T0 + timedelta(seconds=420) and series[14].values["count"] == 3
    assert sum(b.values["count"] for b in series) == 9


def test_granularity_with_groups_only_for_returned_groups(backend):
    w = TimeWindow(start=T0, end=T0 + timedelta(seconds=600))
    res = backend.aggregate(
        [COUNT],
        group_by=["service.name"],
        time_window=w,
        granularity_seconds=300,
        limit=1,
    )
    assert res.truncated and len(res.groups) == 1
    top = res.groups[0]
    assert top.key == {"service.name": "checkout"}
    assert [b.values["count"] for b in top.series] == [13, 11]  # 24 checkout spans split at +300 s


def test_granularity_with_multi_field_groups(backend):
    w = TimeWindow(start=T0, end=T0 + timedelta(seconds=600))
    res = backend.aggregate(
        [COUNT],
        group_by=["service.name", "name"],
        time_window=w,
        granularity_seconds=600,
        limit=50,
    )
    assert all(
        len(g.series) == 1 and g.series[0].values["count"] == g.values["count"] for g in res.groups
    )


def test_granularity_limits(backend):
    with pytest.raises(InvalidQueryError, match="must be >= 1"):
        backend.aggregate([COUNT], granularity_seconds=0)
    w = TimeWindow(start=T0, end=T0 + timedelta(seconds=5000))
    with pytest.raises(InvalidQueryError, match="granularity_seconds >= 5"):
        backend.aggregate([COUNT], time_window=w, granularity_seconds=1)


# ------------------------------------------------------------------ distinct_values


def test_distinct_values(backend):
    res = backend.distinct_values("service.name")
    assert [(v.value, v.count) for v in res.values] == [("checkout", 24), ("payment", 20)]
    assert res.total_distinct == 2 and not res.truncated
    assert res.matched_rows == 44 and res.rows_with_field == 44


def test_distinct_values_truncation_and_presence(backend):
    res = backend.distinct_values("user_id", limit=3)
    assert len(res.values) == 3 and res.total_distinct == 14 and res.truncated
    assert res.rows_with_field == 24 and res.matched_rows == 44


def test_distinct_values_with_filters_window_and_empty(backend):
    res = backend.distinct_values(
        "payment.provider",
        filters=[f("error", "=", True)],
        time_window=TimeWindow(last_seconds=100),
    )
    assert [(v.value, v.count) for v in res.values] == [("adyen", 2)]
    empty = backend.distinct_values("name", filters=[f("name", "=", "zz")])
    assert empty.values == [] and empty.total_distinct == 0 and empty.matched_rows == 0


def test_distinct_values_numeric_and_bool(backend):
    codes = backend.distinct_values("http.response.status_code")
    assert {v.value: v.count for v in codes.values} == {200: 18, 500: 6}


# ------------------------------------------------------------------ sample_events


def test_sample_events_default_newest(backend):
    res = backend.sample_events(limit=2)
    assert res.total_matching == 44 and res.truncated and len(res.events) == 2
    assert res.events[0]["timestamp"] == "2026-10-19T21:09:00.030000Z"
    db_row = res.events[0]
    assert db_row["name"] == "db.query" and "http.route" not in db_row  # absent attrs omitted
    assert db_row["duration_ms"] == 10.0 and db_row["is_root"] is False


def test_sample_events_fields_filters_order(backend):
    res = backend.sample_events(
        filters=[f("error", "=", True)],
        fields=["name", "duration_ms", "exception.type"],
        order=SampleOrder.SLOWEST,
        limit=3,
    )
    assert res.total_matching == 9
    assert [e["duration_ms"] for e in res.events] == [2200.0, 2190.0, 2180.0]
    assert set(res.events[0]) == {"name", "duration_ms", "exception.type"}
    assert "exception.type" not in res.events[1]  # only on the payment span
    oldest = backend.sample_events(order=SampleOrder.OLDEST, limit=1)
    assert oldest.events[0]["timestamp"] == "2026-10-19T21:00:00.000000Z"


def test_sample_events_empty_and_limits(backend):
    res = backend.sample_events(filters=[f("name", "=", "zz")])
    assert res.events == [] and res.total_matching == 0 and not res.truncated
    for bad in (0, 101):
        with pytest.raises(InvalidQueryError):
            backend.sample_events(limit=bad)


# ------------------------------------------------------------------ get_trace


def test_get_trace(backend):
    tid = f"{0xABC000 + 7:032x}"
    trace = backend.get_trace(tid)
    assert trace.span_count == 4 and not trace.truncated and len(trace.spans) == 4
    root, client, pay, db = trace.spans
    assert root.parent_span_id is None and root.name == "POST /checkout"
    assert client.parent_span_id == root.span_id and pay.parent_span_id == client.span_id
    assert db.parent_span_id == pay.span_id
    assert (root.service, pay.service, pay.kind) == ("checkout", "payment", "server")
    assert [s.error for s in trace.spans] == [True, True, True, False]
    assert pay.status_message == "upstream timeout"
    assert pay.attributes["payment.provider"] == "adyen"
    assert pay.attributes["exception.type"] == "TimeoutError"
    assert "http.route" in root.attributes and "duration_ms" not in root.attributes
    assert root.start == T0 + timedelta(seconds=420)
    assert trace.spans[0].duration_ms == 2170.0


def test_get_trace_case_insensitive_missing_and_truncated(backend):
    tid = f"{0xABC000 + 7:032x}"
    assert backend.get_trace(tid.upper()).span_count == 4
    missing = backend.get_trace("0" * 32)
    assert missing.spans == [] and missing.span_count == 0 and not missing.truncated
    cut = backend.get_trace(tid, max_spans=2)
    assert len(cut.spans) == 2 and cut.span_count == 4 and cut.truncated


# ------------------------------------------------------------------ compare_distributions


def test_compare_selection_vs_complement(backend):
    cmp = backend.compare_distributions(selection_filters=[f("error", "=", True)])
    assert (cmp.selection_rows, cmp.baseline_rows) == (9, 35)
    names = [x.field for x in cmp.fields]
    assert "error" not in names and "error" in cmp.skipped_fields  # tautological
    assert "timestamp" in cmp.skipped_fields and "span_id" in cmp.skipped_fields
    assert names == [x.field for x in sorted(cmp.fields, key=lambda x: (-x.score, x.field))]
    by = {x.field: x for x in cmp.fields}
    # a field that perfectly separates the sets outranks a partly-overlapping one
    assert by["status_code"].score > by["http.response.status_code"].score > by["user_id"].score
    prov = by["payment.provider"]
    adyen = next(v for v in prov.values if v.value == "adyen")
    assert adyen.selection_count == 3 and adyen.baseline_count == 0
    assert adyen.selection_fraction == pytest.approx(3 / 9) and adyen.delta > 0
    assert prov.selection_present_fraction == pytest.approx(3 / 9)
    assert prov.baseline_present_fraction == pytest.approx(7 / 35)
    exc = by["exception.type"]
    absent = next(v for v in exc.values if v.absent)
    assert absent.value is None and absent.selection_fraction == pytest.approx(6 / 9)


def test_compare_numeric_field_uses_quantile_buckets_and_stats(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("error", "=", True)], fields=["duration_ms"]
    )
    (fc,) = cmp.fields
    assert fc.kind == "numeric" and fc.type == "float"
    assert fc.selection_stats.count == 9 and fc.baseline_stats.count == 35
    assert fc.selection_stats.p50 > 2000 > 500 > fc.baseline_stats.p95
    assert fc.baseline_stats.max == 900
    top = fc.values[0]
    assert top.value.startswith(">") and top.low is not None and top.high is None
    assert (
        top.selection_fraction > 0.5 > top.baseline_fraction
        and fc.score > 0.7
        and fc.score_method.startswith("ks")
    )
    assert fc.total_values <= 10


def test_compare_low_cardinality_integer_is_categorical(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("error", "=", True)], fields=["http.response.status_code"]
    )
    fc = cmp.fields[0]
    assert fc.kind == "categorical"
    assert {v.value for v in fc.values} >= {500, 200}


def test_compare_before_after_window(backend):
    cmp = backend.compare_distributions(
        time_window=TimeWindow(start=T0 + timedelta(seconds=420)),
        baseline_window=TimeWindow(end=T0 + timedelta(seconds=420)),
    )
    assert (cmp.selection_rows, cmp.baseline_rows) == (12, 32)
    by = {x.field: x for x in cmp.fields}
    err = {v.value: v for v in by["error"].values}
    assert by["error"].score > 0.5 and err[True].selection_fraction == pytest.approx(9 / 12)
    assert cmp.baseline_window.end == T0 + timedelta(seconds=420)


def test_compare_same_filters_before_after(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("service.name", "=", "payment")],
        time_window=TimeWindow(start=T0 + timedelta(seconds=420)),
        baseline_window=TimeWindow(end=T0 + timedelta(seconds=420)),
        fields=["payment.provider"],
    )
    assert (cmp.selection_rows, cmp.baseline_rows) == (6, 14)
    vals = {v.value: v for v in cmp.fields[0].values}
    assert vals["adyen"].selection_fraction == pytest.approx(3 / 6)
    assert vals["adyen"].baseline_fraction == 0


def test_compare_explicit_baseline_filters(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("service.name", "=", "payment"), f("error", "=", True)],
        baseline_filters=[f("service.name", "=", "payment"), f("error", "=", False)],
        max_values_per_field=1,
    )
    assert (cmp.selection_rows, cmp.baseline_rows) == (3, 17)
    assert all(len(x.values) <= 1 for x in cmp.fields)
    assert cmp.fields[0].score > 0.5


def test_compare_explicit_fields_can_force_identifiers(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("error", "=", True)], fields=["trace_id", "timestamp", "user_id"]
    )
    assert {x.field for x in cmp.fields} == {"trace_id", "user_id"}
    assert "timestamp" in cmp.skipped_fields


def test_compare_empty_selection_and_bad_requests(backend):
    cmp = backend.compare_distributions(selection_filters=[f("name", "=", "zz")])
    assert cmp.selection_rows == 0 and cmp.baseline_rows == 44 and cmp.fields == []
    with pytest.raises(InvalidQueryError, match="complement"):
        backend.compare_distributions()
    with pytest.raises(InvalidQueryError):
        backend.compare_distributions(
            selection_filters=[f("error", "=", True)], max_values_per_field=0
        )


def test_compare_injection_values_are_inert(backend):
    cmp = backend.compare_distributions(
        selection_filters=[f("name", "=", "x'; DROP TABLE events; --")],
    )
    assert cmp.selection_rows == 0 and count(backend) == 44


def test_compare_near_unique_fields_are_skipped_by_default(tmp_path):
    rows = [
        {
            "timestamp": f"2026-10-19T21:{i // 60:02d}:{i % 60:02d}Z",
            "req_id": f"r{i}",
            "bucket": "a" if i % 10 else "b",
            "error": i % 10 == 0,
        }
        for i in range(300)
    ]
    be = DuckDBBackend(write_events(tmp_path / "e.jsonl.gz", rows))
    cmp = be.compare_distributions(selection_filters=[f("error", "=", True)])
    assert "req_id" in cmp.skipped_fields and [x.field for x in cmp.fields] == ["bucket"]
    assert cmp.fields[0].score > 0.8


def test_small_selection_noise_does_not_win(tmp_path):
    rows = []
    for i in range(400):
        slow = i < 6
        rows.append(
            {
                "timestamp": f"2026-10-19T21:{i // 60:02d}:{i % 60:02d}Z",
                "slow": slow,
                "user": f"user-{i % 200}",  # selection users are just a random-ish few
                "region": "eu" if slow else ("eu" if i % 5 == 0 else "us"),
            }
        )
    be = DuckDBBackend(write_events(tmp_path / "e.jsonl.gz", rows))
    cmp = be.compare_distributions(
        selection_filters=[f("slow", "=", True)], fields=["user", "region"]
    )
    by = {x.field: x.score for x in cmp.fields}
    assert by["user"] < 0.1  # 6 distinct users out of 200 is noise, not signal


# ------------------------------------------------------------------ misc


def test_queries_are_fast_on_a_larger_fixture(tmp_path):
    n = 20_000
    rows = [
        {
            "timestamp": (T0 + timedelta(milliseconds=50 * i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "duration_ms": 5.0 + (i * 7919 % 1000) / 10 + (500 if i % 97 == 0 else 0),
            "trace_id": f"{i // 4:032x}",
            "span_id": f"{i:016x}",
            "service.name": f"svc-{i % 6}",
            "error": i % 97 == 0,
            "user_id": f"u{i % 3000}",
            **{f"attr.k{k}": (i * (k + 3)) % (k + 5) for k in range(30)},
        }
        for i in range(n)
    ]
    be = DuckDBBackend(write_events(tmp_path / "big.jsonl.gz", rows))
    start = time.perf_counter()
    be.aggregate(
        [COUNT, calc(Calc.P95, "duration_ms"), calc(Calc.ERROR_RATE)],
        group_by=["service.name", "user_id"],
        granularity_seconds=60,
        limit=20,
    )
    cmp = be.compare_distributions(selection_filters=[f("error", "=", True)])
    assert time.perf_counter() - start < 1.0
    assert cmp.selection_rows == len([r for r in rows if r["error"]])
