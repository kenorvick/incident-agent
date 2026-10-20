"""Compute the healthy-period baselines quoted in `evals/memory_seeds/benefit-baselines.yaml`.

    uv run python scripts/compute_baselines.py
    uv run python scripts/compute_baselines.py -s healthy-baseline -s payment-unreachable

For each scenario the "healthy period" is the whole capture when the scenario is healthy, and
otherwise everything before `capture.fault_start` (ground truth, never shown to the agent). Per
service it prints, over server spans only (`span.kind = server`): the number of spans, p50 and p95
of `duration_ms`, and the error rate, all through the DuckDB backend (the same queries an agent
can make). It then prints the combined ranges across the scenarios (the numbers the benefit seed
notes are written from) and, for each healthy scenario, p95 in the first versus second half of
the window (load ramps up during a capture, so p95 drifts up without any regression).

Sampling note: fixtures keep 1 in `capture.sample_rate` traces, so counts are of sampled spans;
percentiles and error rates are not affected.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from incident_agent.backends import default_fixtures_dir
from incident_agent.backends.base import (
    AggregateResult,
    Calc,
    Calculation,
    Filter,
    FilterOp,
    OrderBy,
    TimeWindow,
)
from incident_agent.backends.duckdb_backend import DuckDBBackend
from incident_agent.evals.scenario import load_scenario

DEFAULT_SCENARIOS = ["healthy-baseline", "payment-failure-50pct"]
SERVICES = ["frontend", "frontend-proxy", "checkout", "payment", "cart", "product-catalog"]
SERVER = Filter(field="span.kind", op=FilterOp.EQ, value="server")
CALCS = [
    Calculation(op=Calc.COUNT),
    Calculation(op=Calc.P50, field="duration_ms"),
    Calculation(op=Calc.P95, field="duration_ms"),
    Calculation(op=Calc.ERROR_RATE),
]


@dataclass
class Baseline:
    scenario: str
    service: str
    spans: int
    p50_ms: float
    p95_ms: float
    error_rate: float


def _aggregate(backend: DuckDBBackend, window: TimeWindow | None) -> AggregateResult:
    return backend.aggregate(
        CALCS,
        filters=[SERVER],
        group_by=["service.name"],
        time_window=window,
        limit=100,
        order_by=OrderBy(key="service.name", descending=False),
    )


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def healthy_window(fixtures: Path, scenario_id: str) -> tuple[DuckDBBackend, TimeWindow | None]:
    spec = load_scenario(fixtures / scenario_id)
    backend = DuckDBBackend.from_fixture(fixtures, scenario_id)
    if spec.ground_truth.healthy or spec.capture.fault_start is None:
        return backend, None
    return backend, TimeWindow(end=_utc(spec.capture.fault_start))


def baselines(fixtures: Path, scenario_id: str) -> list[Baseline]:
    backend, window = healthy_window(fixtures, scenario_id)
    result = _aggregate(backend, window)
    return [
        Baseline(
            scenario=scenario_id,
            service=str(g.key["service.name"]),
            spans=int(g.values["count"] or 0),
            p50_ms=float(g.values["p50(duration_ms)"] or 0.0),
            p95_ms=float(g.values["p95(duration_ms)"] or 0.0),
            error_rate=float(g.values["error_rate"] or 0.0),
        )
        for g in result.groups
    ]


def halves(fixtures: Path, scenario_id: str, service: str) -> tuple[float, float]:
    """p95 of `service` server spans in the first and second half of a healthy capture."""
    backend = DuckDBBackend.from_fixture(fixtures, scenario_id)
    schema = backend.schema()
    mid = schema.time_start + (schema.time_end - schema.time_start) / 2
    out = []
    for window in (TimeWindow(end=mid), TimeWindow(start=mid)):
        res = backend.aggregate(
            [Calculation(op=Calc.P95, field="duration_ms")],
            filters=[SERVER, Filter(field="service.name", value=service)],
            time_window=window,
        )
        out.append(float(res.groups[0].values["p95(duration_ms)"] or 0.0))
    return out[0], out[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", "-s", action="append", help="Scenario id (repeatable).")
    parser.add_argument("--fixtures-dir", type=Path, default=None)
    args = parser.parse_args()
    fixtures = args.fixtures_dir or default_fixtures_dir()
    scenarios = args.scenario or DEFAULT_SCENARIOS

    rows: list[Baseline] = []
    for sid in scenarios:
        spec = load_scenario(fixtures / sid)
        healthy = spec.ground_truth.healthy
        period = "whole capture" if healthy else f"before {spec.capture.fault_start}"
        print(f"\n== {sid} ({period}), server spans")
        print(f"{'service':18} {'spans':>6} {'p50 ms':>8} {'p95 ms':>8} {'error rate':>11}")
        found = baselines(fixtures, sid)
        rows += found
        for b in found:
            print(
                f"{b.service:18} {b.spans:>6} {b.p50_ms:>8.2f} {b.p95_ms:>8.2f} "
                f"{b.error_rate:>10.2%}"
            )

    print(f"\n== combined across {', '.join(scenarios)}")
    print(f"{'service':18} {'spans':>6} {'p50 ms':>14} {'p95 ms':>14} {'max error rate':>15}")
    for service in SERVICES:
        sel = [b for b in rows if b.service == service]
        if not sel:
            continue
        p50 = f"{min(b.p50_ms for b in sel):.1f}-{max(b.p50_ms for b in sel):.1f}"
        p95 = f"{min(b.p95_ms for b in sel):.1f}-{max(b.p95_ms for b in sel):.1f}"
        print(
            f"{service:18} {sum(b.spans for b in sel):>6} {p50:>14} {p95:>14} "
            f"{max(b.error_rate for b in sel):>14.2%}"
        )

    for sid in scenarios:
        if not load_scenario(fixtures / sid).ground_truth.healthy:
            continue
        print(f"\n== {sid}: p95 ms, first half -> second half of the window (load ramps up)")
        for service in ("frontend", "frontend-proxy", "checkout"):
            first, second = halves(fixtures, sid, service)
            print(f"{service:18} {first:>8.2f} -> {second:>8.2f}  ({second / first:.2f}x)")


if __name__ == "__main__":
    main()
