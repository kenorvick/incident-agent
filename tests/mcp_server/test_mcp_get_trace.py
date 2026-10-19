import pytest
from mcp_synthetic import T0
from telemetry_rows import write_events

from incident_agent.backends.duckdb_backend import DuckDBBackend

FAILING_TRACE = f"{0xABC000 + 7:032x}"
HEALTHY_TRACE = f"{0xABC000 + 1:032x}"


def test_failing_trace_summary(tiny, call):
    text, err = call(tiny, "get_trace", trace_id=FAILING_TRACE)
    assert not err
    assert f"get_trace {FAILING_TRACE}: 4 spans, 2 services" in text
    assert "3 error spans" in text
    assert "POST /checkout [checkout]" in text and "POST /charge [payment]" in text
    assert "<- likely origin" in text
    assert "exception.type=TimeoutError" in text and "payment.provider=adyen" in text
    assert "critical path:" in text and "slowest spans by self time" in text
    assert "span_id=" in text


def test_healthy_trace_has_no_errors(tiny, call):
    text, err = call(tiny, "get_trace", trace_id=HEALTHY_TRACE)
    assert not err and "0 error spans" in text and "error spans: none" in text


def test_not_found_error_says_where_ids_come_from(tiny, call):
    text, err = call(tiny, "get_trace", trace_id="deadbeef")
    assert err and "No spans found" in text and "find_outliers" in text


def test_bad_type(tiny, call):
    text, err = call(tiny, "get_trace", trace_id=12345)
    assert err and "trace_id" in text


@pytest.fixture(scope="module")
def wide_trace(tmp_path_factory):
    """root -> 12 identical `db.query` siblings + 8 distinct children, one of them failing."""
    tid = "f" * 32
    rows = []

    def span(i, parent, name, off, ms, **kw):
        row = {
            "timestamp": (T0.replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{off:06d}Z",
            "duration_ms": ms,
            "trace_id": tid,
            "span_id": f"{i:016x}",
            "parent_span_id": parent,
            "name": name,
            "span.kind": "internal",
            "service.name": kw.pop("service", "svc"),
            "status_code": "ERROR" if kw.get("error") else "UNSET",
            "status_message": None,
            "error": kw.pop("error", False),
            "is_root": parent is None,
            "span_event_count": 0,
        }
        row.update(kw)
        rows.append(row)

    span(1, None, "root", 0, 500.0, service="gateway")
    for i in range(12):
        span(10 + i, f"{1:016x}", "db.query", 1000 * (i + 1), 5.0 + i, **{"db.system": "pg"})
    for j in range(8):
        span(
            100 + j,
            f"{1:016x}",
            f"step-{j}",
            20000 + 1000 * j,
            50.0,
            error=(j == 5),
            service=f"svc{j}",
        )
    d = tmp_path_factory.mktemp("wide")
    write_events(d / "wide" / "events.jsonl.gz", rows)
    return DuckDBBackend.from_fixture(d, "wide")


def test_repeated_siblings_are_collapsed(wide_trace, call):
    text, err = call(wide_trace, "get_trace", trace_id="f" * 32)
    assert not err
    assert "db.query [svc] x12 total 126ms avg 10.5ms max 16ms" in text
    assert text.count("db.query") < 4  # one tree line, not twelve


def test_tree_cap_hides_least_important_and_says_so(wide_trace, call):
    text, _ = call(wide_trace, "get_trace", trace_id="f" * 32, max_tree_lines=5)
    assert "more span group(s) hidden" in text
    assert "step-5 [svc5]" in text  # the erroring branch is kept over healthy ones


def test_summary_is_small(big, call):
    text, _ = call(big, "get_trace", trace_id=FAILING_TRACE)
    assert len(text) // 4 < 1500
