"""Every committed fixture must be loadable and the set must stay inside the size budget."""

import gzip
import json
from pathlib import Path

import pytest

from incident_agent.evals.scenario import load_scenario

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SCENARIO_DIRS = sorted(p.parent for p in FIXTURES_DIR.glob("*/scenario.yaml"))
TOTAL_BUDGET_BYTES = 20 * 1000 * 1000
REQUIRED_COLUMNS = {
    "timestamp",
    "duration_ms",
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "span.kind",
    "service.name",
    "status_code",
    "status_message",
    "error",
    "is_root",
    "span_event_count",
}


def test_fixtures_exist() -> None:
    assert SCENARIO_DIRS, f"no fixtures/*/scenario.yaml under {FIXTURES_DIR}"


@pytest.mark.parametrize("scenario_dir", SCENARIO_DIRS, ids=lambda p: p.name)
def test_fixture_is_valid(scenario_dir: Path) -> None:
    spec = load_scenario(scenario_dir)  # validates against ScenarioSpec
    assert spec.id == scenario_dir.name

    events = scenario_dir / "events.jsonl.gz"
    assert events.is_file() and events.stat().st_size > 0

    with gzip.open(events, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    assert rows, "events.jsonl.gz has no rows"
    assert len(rows) == spec.capture.span_count
    assert all(REQUIRED_COLUMNS <= row.keys() for row in rows)

    services = {row["service.name"] for row in rows}
    for cause in spec.ground_truth.causes:
        assert cause.service in services, f"cause service {cause.service!r} not in fixture"
    if spec.ground_truth.healthy:
        assert spec.capture.fault_start is None
    else:
        assert spec.capture.fault_start is not None
    if spec.injection:
        # v2: the payload is echoed inside the field's text (see scripts/plant_injection.py)
        assert any(spec.injection.payload in str(row.get(spec.injection.field)) for row in rows)


def test_total_fixture_size_within_budget() -> None:
    total = sum(p.stat().st_size for p in FIXTURES_DIR.glob("*/events.jsonl.gz"))
    assert total < TOTAL_BUDGET_BYTES, f"fixtures are {total / 1e6:.1f} MB, budget is 20 MB"
