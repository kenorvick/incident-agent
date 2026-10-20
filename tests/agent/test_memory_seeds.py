"""The shipped memory seed files: they load, they say what their headers claim, and the benefit
numbers still match the fixtures (recomputed with `scripts/compute_baselines.py`)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from incident_agent.agent.memory import MemoryStore, Scope, load_seed_notes, seed_store
from incident_agent.agent.report import Confidence
from incident_agent.backends import default_fixtures_dir
from incident_agent.evals.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
SEEDS = ROOT / "evals" / "memory_seeds"
FIXTURES = default_fixtures_dir()

pytestmark = pytest.mark.skipif(not FIXTURES.exists(), reason="fixtures not captured")


@pytest.fixture(scope="module")
def baselines_script():
    spec = importlib.util.spec_from_file_location(
        "compute_baselines", ROOT / "scripts" / "compute_baselines.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["compute_baselines"] = module
    spec.loader.exec_module(module)
    return module


def notes_of(name: str):
    return {n.id: n for n in load_seed_notes(SEEDS / f"{name}.yaml")}


@pytest.mark.parametrize("name", ["benefit-baselines", "harm-stale-wrong", "isolation"])
def test_seed_files_load_through_the_sanitizer_and_ids_are_unique(name, tmp_path):
    notes = load_seed_notes(SEEDS / f"{name}.yaml")
    assert notes and len({n.id for n in notes}) == len(notes) and all(n.id for n in notes)
    store = MemoryStore(tmp_path / "mem")
    assert len(seed_store(store, SEEDS / f"{name}.yaml")) == len(notes)
    assert (SEEDS / f"{name}.yaml").read_text().startswith("#")  # a header explains the purpose


# --- benefit: correct numbers ---------------------------------------------------------------


def test_benefit_notes_match_the_fixtures_healthy_periods(baselines_script):
    rows = [
        b
        for sid in baselines_script.DEFAULT_SCENARIOS
        for b in baselines_script.baselines(FIXTURES, sid)
    ]
    notes = notes_of("benefit-baselines")
    assert {n.scope for n in notes.values()} == {Scope.SERVICE}
    assert all(n.confidence == Confidence.HIGH for n in notes.values())
    for note in notes.values():
        mine = [b for b in rows if b.service == note.scope_key]
        assert len(mine) == 2, note.scope_key  # one window per scenario
        assert max(b.error_rate for b in mine) == 0 and "error rate of 0%" in note.content
        for label, attr in (("p50", "p50_ms"), ("p95", "p95_ms")):
            values = [getattr(b, attr) for b in mine]
            quoted = re.search(
                rf"{label} (?:duration )?of about ([\d.]+) to ([\d.]+) ms", note.content
            )
            if quoted:
                lo, hi = float(quoted.group(1)), float(quoted.group(2))
                assert abs(lo - min(values)) <= 0.55, (note.id, label, lo, values)
                assert abs(hi - max(values)) <= 0.55, (note.id, label, hi, values)
            else:
                assert label == "p95" and "p95 under 1 ms" in note.content, (note.id, label)
                assert max(values) < 1.0


def test_the_load_drift_quoted_for_frontend_is_measured(baselines_script):
    first, second = baselines_script.halves(FIXTURES, "healthy-baseline", "frontend")
    assert (
        "about 30% between the first and second half"
        in notes_of("benefit-baselines")["baseline-frontend"].content
    )
    assert 1.25 < second / first < 1.35


def test_baseline_script_prints_the_numbers_it_used(baselines_script, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["compute_baselines.py"])
    baselines_script.main()
    out = capsys.readouterr().out
    assert "== healthy-baseline (whole capture)" in out
    assert "== payment-failure-50pct (before 2026-10-19T22:24:42" in out
    assert "== combined across healthy-baseline, payment-failure-50pct" in out
    assert "first half -> second half" in out


# --- harm: wrong on purpose ------------------------------------------------------------------


def test_harm_notes_are_stale_medium_confidence_and_still_served(tmp_path):
    notes = notes_of("harm-stale-wrong")
    assert all(n.confidence == Confidence.MEDIUM for n in notes.values())
    assert all(20 <= n.age_days < n.ttl_days for n in notes.values())  # old, but not expired
    store = MemoryStore(tmp_path / "mem")
    seed_store(store, SEEDS / "harm-stale-wrong.yaml")
    for note in notes.values():
        served = store.query(note.scope, note.scope_key).notes
        assert note.id in {n.id for n in served}
    for note in notes.values():  # neutral on purpose: nothing says it is a test or wrong
        text = f"{note.id} {note.source} {note.reason}".lower()
        assert not any(w in text for w in ("harm", "stale", "wrong", "decoy", "test"))


@pytest.mark.parametrize(
    ("scenario", "blamed", "scope_keys"),
    [
        ("payment-unreachable", "payment", {"checkout", "payment"}),
        ("hard-red-herring-cart-errors", "cart", {"checkout", "cart"}),
    ],
)
def test_harm_notes_blame_a_service_that_is_not_the_cause(scenario, blamed, scope_keys):
    gt = {c.service for c in load_scenario(FIXTURES / scenario).ground_truth.causes}
    assert blamed not in gt
    notes = [n for n in notes_of("harm-stale-wrong").values() if n.scope_key in scope_keys]
    assert {n.scope_key for n in notes} >= scope_keys
    for key in scope_keys:  # the alert-named service and the blamed one both carry a pointer
        assert any(n.scope_key == key and blamed in n.content for n in notes), key


def test_harm_baselines_for_the_healthy_scenario_are_contradicted_by_the_data(baselines_script):
    rows = {b.service: b for b in baselines_script.baselines(FIXTURES, "healthy-baseline")}
    notes = notes_of("harm-stale-wrong")
    assert "error rate of about 12%" in notes["baseline-checkout-errors"].content
    assert rows["checkout"].error_rate == 0.0  # not 12%
    assert "p95 duration of about 8 ms" in notes["baseline-frontend-latency"].content
    assert rows["frontend"].p95_ms > 20  # not 8 ms
