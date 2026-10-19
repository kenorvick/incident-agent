"""A larger, realistic synthetic wide-event dataset (~25k spans) with one injected fault.

Layout (T0 = 2026-10-19T21:00:00Z, 600 s of traffic, fault from T0 + 300 s):
- six services (frontend, cart, checkout, payment, shipping, recommendation); four request types
  (`GET /`, `GET /cart`, `GET /product`, `POST /checkout`) built from 2-7 spans each.
- every request carries `app.user.id` (about 3000 users) and `app.tenant.id` (60 tenants; one big
  tenant, `tenant-042`, sends 8% of the traffic).
- THE FAULT: after T0 + 300 s, every `POST /checkout` from `tenant-042` fails in the payment
  service (1.8-2.6 s, `CardProcessorTimeout`), and the error propagates up to checkout and
  frontend. `payment.provider` is *not* correlated with it (the distinguishing attribute is the
  high-cardinality tenant id).
- background noise: a 0.4% error rate on `GET /product` (503, no exception), pods spread evenly,
  a constant slow product (`prod-007`) in recommendation.
"""

import random
from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 10, 19, 21, 0, 0, tzinfo=UTC)
FAULT_AT_S = 300.0
SPAN_S = 600.0
BAD_TENANT = "tenant-042"
N_TRACES = 5000


class _Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.rows: list[dict] = []
        self.n = 0

    def hex(self, digits: int) -> str:
        return f"{self.rng.getrandbits(digits * 4):0{digits}x}"

    def span(
        self,
        trace: str,
        parent: dict | None,
        service: str,
        name: str,
        kind: str,
        t: float,
        ms: float,
        **attrs,
    ) -> dict:
        err = bool(attrs.pop("error", False))
        row = {
            "timestamp": (T0 + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "duration_ms": round(ms, 3),
            "trace_id": trace,
            "span_id": self.hex(16),
            "parent_span_id": parent["span_id"] if parent else None,
            "name": name,
            "span.kind": kind,
            "service.name": service,
            "status_code": "ERROR" if err else "UNSET",
            "status_message": attrs.pop("status_message", None),
            "error": err,
            "is_root": parent is None,
            "span_event_count": 1 if "exception.type" in attrs else 0,
            "k8s.pod.name": f"{service}-{self.rng.choice('abc')}",
            "deploy.version": "v1.8.0",
        }
        row.update(attrs)
        self.rows.append(row)
        return row


def make_big_rows(n_traces: int = N_TRACES, seed: int = 7) -> list[dict]:
    b = _Builder(seed)
    rng = b.rng
    for _ in range(n_traces):
        t = rng.uniform(0, SPAN_S)
        trace = b.hex(32)
        tenant = BAD_TENANT if rng.random() < 0.08 else f"tenant-{rng.randint(0, 59):03d}"
        common = {"app.user.id": f"user-{rng.randint(1, 3000):04d}", "app.tenant.id": tenant}
        kind = rng.choices(["home", "cart", "product", "checkout"], [0.22, 0.2, 0.18, 0.4])[0]
        product = f"prod-{rng.randint(0, 59):03d}"
        if kind == "home":
            _simple(b, trace, t, common, "GET /", "/", "recommendation", "ListRecommendations",
                    rng.gauss(60, 10), product)  # fmt: skip
        elif kind == "cart":
            _simple(b, trace, t, common, "GET /cart", "/cart", "cart", "GetCart",
                    rng.gauss(25, 5), product)  # fmt: skip
        elif kind == "product":
            slow = 400 if product == "prod-007" else 0
            ms = rng.gauss(45, 8) + slow
            noise = rng.random() < 0.004
            _simple(
                b, trace, t, common, "GET /product", "/product", "recommendation", "GetProduct",
                ms, product, noise=noise,
            )  # fmt: skip
        else:
            _checkout(b, trace, t, common, tenant)
    return b.rows


def _simple(b, trace, t, common, name, route, service, method, ms, product, noise=False):
    """A frontend request that makes one downstream call."""
    http = {
        "http.route": route,
        "http.request.method": "POST" if name.startswith("POST") else "GET",
    }
    http["http.response.status_code"] = 503 if noise else 200
    msg = {"status_message": "upstream unavailable"} if noise else {}
    root = b.span(
        trace, None, "frontend", name, "server", t, ms + 25, error=noise, **http, **msg, **common
    )
    attrs = {"rpc.method": method, "app.product.id": product, **common}
    b.span(trace, root, service, method, "server", t + 0.004, ms, **attrs)


def _checkout(b: _Builder, trace: str, t: float, common: dict, tenant: str) -> None:
    rng = b.rng
    failing = tenant == BAD_TENANT and t >= FAULT_AT_S
    provider = rng.choices(["stripe", "adyen", "paypal"], [0.7, 0.25, 0.05])[0]
    pay_ms = rng.uniform(1800, 2600) if failing else max(10.0, rng.gauss(80, 15))
    ship_ms, cart_ms = max(5.0, rng.gauss(40, 8)), max(5.0, rng.gauss(20, 4))
    total = pay_ms + ship_ms + cart_ms
    failed = {"status_message": "payment failed"} if failing else {}
    http = {"http.route": "/checkout", "http.request.method": "POST"}
    http["http.response.status_code"] = 502 if failing else 200
    root = b.span(trace, None, "frontend", "POST /checkout", "server", t, total + 60,
                  error=failing, **http, **failed, **common)  # fmt: skip
    cart_total = round(rng.uniform(10, 400), 2)
    place = b.span(trace, root, "checkout", "PlaceOrder", "server", t + 0.01, total + 30,
                   error=failing, **{"rpc.method": "PlaceOrder", "app.cart.total": cart_total},
                   **failed, **common)  # fmt: skip
    b.span(trace, place, "cart", "EmptyCart", "server", t + 0.02, cart_ms,
           **{"rpc.method": "EmptyCart"}, **common)  # fmt: skip
    exc = {}
    if failing:
        exc = {
            "exception.type": "CardProcessorTimeout",
            "exception.message": "processor timeout after 2000ms",
            "status_message": "card processor timeout",
        }
    pay_attrs = {"http.route": "/charge", "payment.provider": provider}
    pay = b.span(trace, place, "payment", "POST /charge", "server", t + 0.03, pay_ms,
                 error=failing, **pay_attrs, **exc, **common)  # fmt: skip
    db_ms = 8.0 if failing else rng.gauss(8, 2)
    b.span(
        trace, pay, "payment", "db.query", "internal", t + 0.04, db_ms, **{"db.system": "postgres"}
    )
    b.span(trace, place, "shipping", "ShipOrder", "server", t + 0.03 + pay_ms / 1000, ship_ms,
           **{"rpc.method": "ShipOrder"}, **common)  # fmt: skip
