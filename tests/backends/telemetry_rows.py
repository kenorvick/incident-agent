"""Hand-written wide-event rows for backend tests (CONTRACTS fixture format).

Layout (T0 = 2026-10-19T21:00:00Z, dataset end = T0 + 540.03 s):
- traces 0..9 start at T0 + 60*i s; each has 4 spans: checkout root `POST /checkout`,
  checkout client `charge`, payment server `POST /charge`, payment internal `db.query`
  (offsets +0, +0.01, +0.02, +0.03 s).
- traces 7, 8, 9 fail: provider `adyen`, payment span ~2 s with exception.type TimeoutError;
  the root/client/payment spans of those traces have error = true (9 error spans in total).
- trace 3's db.query is a 900 ms slow outlier among healthy spans.
- traces 10..13 are single-span `GET /cart` roots at T0 + 30 + 120*j s.
- `user_id` is unique per trace (high cardinality); `payment.provider` exists only on payment
  spans; `retry.count` is an int on checkout spans and the string "n/a" on payment spans
  (conflicting types); `http.route` is a dotted field.
"""

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

T0 = datetime(2026, 10, 19, 21, 0, 0, tzinfo=UTC)
FAILING = (7, 8, 9)


def _ts(offset_s: float) -> str:
    return (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _span(trace: int, k: int, offset: float, **fields) -> dict:
    row = {
        "timestamp": _ts(offset),
        "trace_id": f"{0xABC000 + trace:032x}",
        "span_id": f"{trace:04x}{k:012x}",
        "status_code": "UNSET",
        "status_message": None,
        "error": False,
        "span_event_count": 0,
    }
    row.update(fields)
    row["is_root"] = row.get("parent_span_id") is None
    return row


def make_rows() -> list[dict]:
    rows: list[dict] = []
    for i in range(10):
        bad = i in FAILING
        t = 60.0 * i
        trace_status = (
            {"status_code": "ERROR", "status_message": "upstream timeout", "error": True}
            if bad
            else {}
        )
        exc = (
            {
                "exception.type": "TimeoutError",
                "exception.message": "adyen timed out",
                "span_event_count": 1,
            }
            if bad
            else {}
        )
        root = _span(
            i, 1, t,
            parent_span_id=None, name="POST /checkout", **{"span.kind": "server"},
            duration_ms=2100.0 + 10 * i if bad else 100.0 + 10 * i,
            **{"service.name": "checkout", "http.route": "/checkout"},
            user_id=f"user-{i:03d}", **{"retry.count": i % 3},
            **{"http.response.status_code": 500 if bad else 200},
            **{"app.cart.total": 20 + i if i % 2 else 20.5 + i},
            **trace_status,
        )  # fmt: skip
        client = _span(
            i, 2, t + 0.01,
            parent_span_id=root["span_id"], name="charge", **{"span.kind": "client"},
            duration_ms=2050.0 + 10 * i if bad else 60.0 + 5 * i,
            **{"service.name": "checkout"}, user_id=f"user-{i:03d}", **{"retry.count": i % 3},
            **trace_status,
        )  # fmt: skip
        pay = _span(
            i, 3, t + 0.02,
            parent_span_id=client["span_id"], name="POST /charge", **{"span.kind": "server"},
            duration_ms=2000.0 + 100 * (i - 7) if bad else 40.0 + 5 * i,
            **{"service.name": "payment", "http.route": "/charge"},
            **{"payment.provider": "adyen" if bad else "stripe", "retry.count": "n/a"},
            **{"http.response.status_code": 500 if bad else 200},
            **trace_status, **exc,
        )  # fmt: skip
        db = _span(
            i, 4, t + 0.03,
            parent_span_id=pay["span_id"], name="db.query", **{"span.kind": "internal"},
            duration_ms=900.0 if i == 3 else 10.0,
            **{"service.name": "payment", "db.system": "postgres"},
        )  # fmt: skip
        rows += [root, client, pay, db]
    for j in range(4):
        rows.append(
            _span(
                10 + j,
                1,
                30.0 + 120 * j,
                parent_span_id=None,
                name="GET /cart",
                **{"span.kind": "server"},
                duration_ms=20.0 + 5 * j,
                **{"service.name": "checkout", "http.route": "/cart"},
                user_id=f"user-{100 + j:03d}",
                **{"http.response.status_code": 200},
            )  # fmt: skip
        )
    return rows


def write_events(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path
