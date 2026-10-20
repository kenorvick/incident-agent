"""Tests for the OTLP JSON -> wide event flattener, using a tiny hand-written export."""

import base64
import gzip
import json
from pathlib import Path

from incident_agent.flatten import (
    flatten_documents,
    flatten_file,
    keep_trace,
    main,
    normalize_id,
)

TRACE_A = "0af7651916cd43dd8448eb211c80319c"
ROOT_A = "b7ad6b7169203331"
CHILD_A = "00f067aa0ba902b7"
START = 1_792_443_600_123_456_789  # 2026-10-19T21:00:00.123456789Z
NS_PER_MS = 1_000_000


def s(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def make_document() -> dict:
    """Two resources: `frontend` (hex ids, int enums) and `cart` (base64 ids, string enums)."""
    frontend_spans = [
        {
            "traceId": TRACE_A,
            "spanId": ROOT_A,
            "parentSpanId": "",
            "name": "POST /api/checkout",
            "kind": 2,
            "startTimeUnixNano": str(START),
            "endTimeUnixNano": str(START + 250 * NS_PER_MS),
            "attributes": [
                s("http.route", "/api/checkout"),
                {"key": "http.response.status_code", "value": {"intValue": "500"}},
                {"key": "demo.cart.items.count", "value": {"intValue": 3}},
                {"key": "demo.order.amount", "value": {"doubleValue": 12.5}},
                {"key": "demo.synthetic", "value": {"boolValue": True}},
                s("deployment.environment", "span-wins"),
                {
                    "key": "demo.product.ids",
                    "value": {
                        "arrayValue": {"values": [{"stringValue": "A1"}, {"stringValue": "B2"}]}
                    },
                },
                {"key": "demo.empty", "value": {}},
            ],
            "status": {"code": 2, "message": "checkout failed"},
        },
        {
            "traceId": TRACE_A,
            "spanId": CHILD_A,
            "parentSpanId": ROOT_A,
            "name": "oteldemo.PaymentService/Charge",
            "kind": 3,
            "startTimeUnixNano": str(START + 10 * NS_PER_MS),
            "endTimeUnixNano": str(START + 200 * NS_PER_MS),
            "status": {"code": "STATUS_CODE_ERROR", "message": "Invalid token"},
            "events": [
                {"name": "log", "attributes": [s("note", "not an exception")]},
                {
                    "name": "exception",
                    "attributes": [
                        s("exception.type", "Error"),
                        s("exception.message", "Payment request failed"),
                        s("exception.stacktrace", "Error: boom\n  at charge (charge.js:48)"),
                    ],
                },
                {"name": "exception", "attributes": [s("exception.message", "second, ignored")]},
            ],
        },
    ]
    b64 = lambda hex_id: base64.b64encode(bytes.fromhex(hex_id)).decode()  # noqa: E731
    cart_spans = [
        {
            "traceId": b64("11111111111111111111111111111111"),
            "spanId": b64("2222222222222222"),
            "name": "HGET",
            "kind": "SPAN_KIND_CLIENT",
            "startTimeUnixNano": str(START + 500 * NS_PER_MS),
            "endTimeUnixNano": str(START + 503 * NS_PER_MS),
            "status": {"code": "STATUS_CODE_OK"},
        },
        {  # event named "Error" (as the ad service records it) still yields exception fields
            "traceId": "22222222222222222222222222222222",
            "spanId": "3333333333333333",
            "name": "GetAds",
            "kind": 2,
            "startTimeUnixNano": str(START + 600 * NS_PER_MS),
            "endTimeUnixNano": str(START + 601 * NS_PER_MS),
            "status": {"code": 2},
            "events": [{"name": "Error", "attributes": [s("exception.message", "UNAVAILABLE")]}],
        },
    ]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        s("service.name", "frontend"),
                        s("service.version", "3.1.0"),
                        s("deployment.environment", "resource-loses"),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "next"}, "spans": frontend_spans}],
            },
            {
                "resource": {"attributes": [s("service.name", "cart")]},
                "scopeSpans": [{"spans": cart_spans}],
            },
        ]
    }


def flatten_sample(sample_rate: int = 1) -> dict[str, dict]:
    rows, _ = flatten_documents([json.dumps(make_document())], sample_rate)
    return {row["span_id"]: row for row in rows}


def test_core_fields_and_root_detection() -> None:
    by_id = flatten_sample()
    root, child = by_id[ROOT_A], by_id[CHILD_A]

    assert root["timestamp"] == "2026-10-19T21:00:00.123456Z"
    assert root["duration_ms"] == 250.0
    assert root["trace_id"] == TRACE_A
    assert root["parent_span_id"] is None and root["is_root"] is True
    assert root["name"] == "POST /api/checkout"
    assert root["span.kind"] == "server"
    assert root["service.name"] == "frontend"
    assert root["span_event_count"] == 0

    assert child["parent_span_id"] == ROOT_A and child["is_root"] is False
    assert child["span.kind"] == "client"
    assert child["duration_ms"] == 190.0
    assert child["span_event_count"] == 3


def test_attribute_flattening_and_precedence() -> None:
    root = flatten_sample()[ROOT_A]

    assert root["http.route"] == "/api/checkout"  # span attribute, key unchanged
    assert root["service.version"] == "3.1.0"  # resource attribute carried onto the span
    assert root["http.response.status_code"] == 500 and isinstance(
        root["http.response.status_code"], int
    )  # int64 arrives as a string
    assert root["demo.cart.items.count"] == 3
    assert root["demo.order.amount"] == 12.5
    assert root["demo.synthetic"] is True
    assert root["deployment.environment"] == "span-wins"  # span beats resource on collision
    assert json.loads(root["demo.product.ids"]) == ["A1", "B2"]  # arrays become JSON strings
    assert isinstance(root["demo.product.ids"], str)
    assert "demo.empty" not in root  # valueless attributes are simply absent


def test_exception_event_extraction() -> None:
    by_id = flatten_sample()
    child = by_id[CHILD_A]

    assert child["exception.type"] == "Error"
    assert child["exception.message"] == "Payment request failed"  # first exception event wins
    assert not any("stacktrace" in key for key in child)  # stack traces are dropped

    assert "exception.type" not in by_id[ROOT_A]
    ad_style = by_id["3333333333333333"]  # no `exception` event, but another event carries it
    assert ad_style["exception.message"] == "UNAVAILABLE"
    assert "exception.type" not in ad_style


def test_error_flag_and_status_variants() -> None:
    by_id = flatten_sample()

    assert by_id[ROOT_A]["status_code"] == "ERROR" and by_id[ROOT_A]["error"] is True
    assert by_id[ROOT_A]["status_message"] == "checkout failed"
    assert by_id[CHILD_A]["status_code"] == "ERROR"  # string enum form
    assert by_id[CHILD_A]["error"] is True

    ok = by_id["2222222222222222"]  # base64 ids, string enums, no parent key at all
    assert ok["status_code"] == "OK" and ok["error"] is False
    assert ok["status_message"] is None
    assert ok["span.kind"] == "client"
    assert ok["trace_id"] == "11111111111111111111111111111111"
    assert ok["is_root"] is True and ok["parent_span_id"] is None

    unset_error = by_id["3333333333333333"]  # integer code 2 without a message
    assert unset_error["error"] is True and unset_error["status_message"] is None


def test_normalize_id_accepts_hex_and_base64() -> None:
    assert normalize_id(TRACE_A.upper(), 32) == TRACE_A
    assert normalize_id(base64.b64encode(bytes.fromhex(ROOT_A)).decode(), 16) == ROOT_A
    assert normalize_id("", 16) is None
    assert normalize_id("0" * 16, 16) is None  # all-zero id means "no parent"
    assert normalize_id("not an id", 16) is None


def test_rows_sorted_and_bad_lines_skipped() -> None:
    lines = ["", "{truncated json", json.dumps(make_document()), "[1, 2]"]
    rows, stats = flatten_documents(lines)

    assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
    assert stats.bad_lines == 2 and stats.spans_kept == 4 and stats.traces_kept == 3


def trace_documents(n_traces: int, spans_per_trace: int = 3) -> list[str]:
    lines = []
    for t in range(1, n_traces + 1):
        trace_id = f"{t:032x}"
        spans = [
            {
                "traceId": trace_id,
                "spanId": f"{t * 10 + i + 1:016x}",
                "parentSpanId": "" if i == 0 else f"{t * 10 + 1:016x}",
                "name": f"span-{i}",
                "kind": 1,
                "startTimeUnixNano": str(START + (t * 10 + i) * NS_PER_MS),
                "endTimeUnixNano": str(START + (t * 10 + i + 1) * NS_PER_MS),
            }
            for i in range(spans_per_trace)
        ]
        doc = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [s("service.name", "svc")]},
                    "scopeSpans": [{"spans": spans}],
                }
            ]
        }
        lines.append(json.dumps(doc))
    return lines


def test_sampling_is_deterministic_and_whole_trace() -> None:
    lines = trace_documents(400)
    rows_1, stats_1 = flatten_documents(lines, sample_rate=4)
    rows_2, _ = flatten_documents(list(reversed(lines)), sample_rate=4)  # input order irrelevant

    assert rows_1 == rows_2
    kept = {r["trace_id"] for r in rows_1}
    assert len(kept) == stats_1.traces_kept
    assert 60 <= len(kept) <= 140  # about 1 in 4 of 400 traces
    assert len(rows_1) == 3 * len(kept)  # every span of a kept trace, no span of any other
    assert kept == {f"{t:032x}" for t in range(1, 401) if keep_trace(f"{t:032x}", 4)}

    rows_all, _ = flatten_documents(lines, sample_rate=1)
    assert len(rows_all) == 1200
    assert {r["trace_id"] for r in rows_1} <= {r["trace_id"] for r in rows_all}


def test_flatten_file_writes_identical_gzip_jsonl(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text("\n".join(trace_documents(50)) + "\n")
    out_a, out_b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"

    stats = flatten_file(raw, out_a, sample_rate=2)
    flatten_file(raw, out_b, sample_rate=2)

    assert out_a.read_bytes() == out_b.read_bytes()  # reproducible bytes, safe to re-run
    with gzip.open(out_a, "rt") as f:
        rows = [json.loads(line) for line in f]
    assert len(rows) == stats.spans_kept > 0
    assert all(r["service.name"] == "svc" for r in rows)
    assert all({"timestamp", "duration_ms", "is_root", "error"} <= r.keys() for r in rows)


def test_cli(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(json.dumps(make_document()) + "\n")
    out = tmp_path / "events.jsonl.gz"

    assert main([str(raw), str(out), "--sample-rate", "1"]) == 0
    with gzip.open(out, "rt") as f:
        assert len(f.readlines()) == 4
