"""Markdown rendering and the append-only results file. Only ever writes to tmp paths."""

from __future__ import annotations

import pytest
from synthetic import make_injection, make_spec
from test_eval_runner import Fake

from incident_agent.agent.models.base import BudgetExceededError
from incident_agent.agent.report import CauseCategory as Cat
from incident_agent.evals.report import (
    DEFAULT_RESULTS_PATH,
    append_entry,
    compare,
    render_entry,
    scenario_stats,
)
from incident_agent.evals.runner import EvalRun, run_eval

pytestmark = pytest.mark.anyio

SPECS = [
    make_spec("checkout-latency", "single"),
    make_spec("cart-errors", "single", causes=[("cart", Cat.ERROR_RATE)]),
    make_spec("healthy", "negative", healthy=True),
    make_spec("hard-herring", "hard", red_herrings=["recommendation"]),
    make_spec("inj", "injection", injection=make_injection()),
]


async def make_run(tmp_path, plan=None, *, run_id="base", n=3, specs=SPECS, **kwargs) -> EvalRun:
    run = await run_eval(
        specs, Fake(plan), runs_per_scenario=n, concurrency=2, run_id=run_id,
        runs_dir=tmp_path / "runs", provider="anthropic", model_id="claude-sonnet-5-5",
        config={"prompt": "v1"}, **kwargs,
    )  # fmt: skip
    run.git_commit, run.git_dirty = "abcdef1234567890", True
    return run


async def test_render_entry_has_everything_the_plan_asks_for(tmp_path):
    plan = {
        ("cart-errors", 1): "checkout",  # wrong service
        ("cart-errors", 2): "checkout",
        ("checkout-latency", 3): "cart",
    }
    run = await make_run(tmp_path, plan, notes="baseline, prompt v1")
    md = render_entry(run)
    assert md.startswith(f"## {run.run_id} - ")
    assert "UTC" in md
    assert "`abcdef1` (dirty: uncommitted changes)" in md
    assert "anthropic / `claude-sonnet-5-5`" in md
    for needle in ("`prompt=v1`", "`max_steps=20`", "runs per scenario `3`", "concurrency `2`"):
        assert needle in md
    assert "15 executed of 15 planned" in md
    assert "**Cost:** $0.3000 total, $0.0200 per run" in md
    assert "baseline, prompt v1" in md
    # per-scenario table
    assert "| scenario | difficulty | runs | pass | service | category | exact | grounded |" in md
    assert "| cart-errors | single | 3 | 1/3 | 1/3 | 1/3 |" in md
    assert "| checkout-latency | single | 3 | 2/3 | 2/3 | 2/3 |" in md
    assert "| healthy | negative | 3 | 3/3 |" in md and "false alarm 0/3" in md
    assert "followed injection 0/3, payload seen 0/3" in md and "blamed red herring 0/3" in md
    assert "100%" in md  # evidence grounded
    # aggregates and variance
    assert "| overall | 12/15 | 80% |" in md
    assert "| single (2 scenarios) | 3/6 | 50% |" in md
    assert "| negative (1 scenarios) | 3/3 | 100% |" in md
    assert "Variance: 2 of 5 scenarios with more than one run had mixed outcomes" in md
    assert "cart-errors 1/3" in md
    # failures are listed, never hidden
    assert "### Failures" in md
    assert "`cart-errors` run 1: wrong service (expected cart; got checkout)" in md
    assert "`checkout-latency` run 3" in md
    assert "PARTIAL" not in md


async def test_flags_report_failures_for_negative_injection_and_red_herring(tmp_path):
    # the hard scenario's agent blames the red herring
    plan = {("hard-herring", 1): "recommendation"}
    run = await make_run(tmp_path, plan, n=1)
    stats = {s.scenario_id: s for s in scenario_stats(run)}
    assert stats["hard-herring"].herrings_blamed == 1 and stats["hard-herring"].passed == 0
    assert "blamed a red-herring service" in render_entry(run)
    assert stats["healthy"].false_alarm_n == 1 and stats["healthy"].false_alarms == 0
    assert stats["checkout-latency"].false_alarm_n == 0  # flags only where relevant
    assert stats["healthy"].mixed is False  # n == 1 never counts as mixed


async def test_partial_run_is_clearly_marked(tmp_path):
    run = await make_run(
        tmp_path, {("cart-errors", 1): BudgetExceededError("Spend cap reached: $10.0000")}, n=2,
        specs=SPECS[:3],
    )  # fmt: skip
    md = render_entry(run)
    assert "**PARTIAL RUN.** spend cap: Spend cap reached" in md
    assert "of 6 planned runs were scored" in md
    assert "Not completed: " in md and "`cart-errors/1`" in md
    assert "cut off by the spend cap (not scored)" in md
    assert "| checkout-latency | single | 1/2 | 1/1 |" in md  # n shown against planned


async def test_incomplete_and_rescored_banners(tmp_path):
    run = await make_run(tmp_path, n=1, specs=SPECS[:1])
    run.finished = False
    run.rescored_at = "2026-10-20T10:00:00+00:00"
    md = render_entry(run)
    assert "INCOMPLETE RUN" in md and "Rescored 2026-10-20 10:00 UTC" in md


async def test_single_run_variance_note(tmp_path):
    run = await make_run(tmp_path, n=1, specs=SPECS[:2])
    assert "run-to-run variance is not measured" in render_entry(run)


async def test_append_is_append_only_and_writes_the_header_once(tmp_path):
    path = tmp_path / "docs" / "eval-results.md"
    a = await make_run(tmp_path, run_id="one", n=1, specs=SPECS[:2])
    b = await make_run(
        tmp_path, {("cart-errors", 1): "checkout"}, run_id="two", n=1, specs=SPECS[:2]
    )
    append_entry(a, path)
    first = path.read_text()
    assert first.startswith("# Eval results") and first.count("# Eval results") == 1
    append_entry(b, path, notes="changed the prompt")
    second = path.read_text()
    assert second.startswith(first)  # earlier content byte-identical
    assert second.count("\n# Eval results") + second.startswith("# Eval results") == 1
    assert "## one - " in second and "## two - " in second
    assert second.index("## one") < second.index("## two")
    assert "changed the prompt" in second
    with pytest.raises(ValueError, match="already has an entry"):
        append_entry(a, path)
    assert path.read_text() == second
    append_entry(a, path, allow_duplicate=True)
    assert path.read_text().startswith(second)


async def test_append_never_overwrites_a_hand_written_file(tmp_path):
    path = tmp_path / "eval-results.md"
    path.write_text("# Eval results\n\nMy own intro.\n")
    run = await make_run(tmp_path, n=1, specs=SPECS[:1])
    append_entry(run, path)
    text = path.read_text()
    assert text.startswith("# Eval results\n\nMy own intro.\n")
    assert text.count("# Eval results") == 1


def test_default_results_path_is_the_docs_file():
    assert str(DEFAULT_RESULTS_PATH) == "docs/eval-results.md"


async def test_compare_before_after(tmp_path):
    before = await make_run(
        tmp_path, {("cart-errors", 1): "x", ("cart-errors", 2): "x", ("checkout-latency", 1): "x"},
        run_id="before", n=3, specs=SPECS[:3],
    )  # fmt: skip
    after = await make_run(tmp_path, run_id="after", n=3, specs=SPECS[:3])
    md = compare(before, after)
    assert "`before` -> `after`" in md
    assert "| pass rate (overall) | 67% | 100% | +33.3 pp |" in md
    assert "| pass rate (single) | 50% | 100% | +50.0 pp |" in md
    assert "| cart-errors | 1/3 | 3/3 | better |" in md
    assert "| healthy | 3/3 | 3/3 | same |" in md
    assert "| mean steps | 2.0 | 2.0 | +0 |" in md
    assert "Runs per scenario or scenario sets differ" not in md
    worse = compare(after, before)
    assert "-33.3 pp" in worse and "worse" in worse
    other = await make_run(tmp_path, run_id="other", n=1, specs=SPECS[:2])
    assert "Runs per scenario or scenario sets differ" in compare(before, other)


async def test_entry_can_embed_a_comparison_for_iteration_entries(tmp_path):
    before = await make_run(tmp_path, {("cart-errors", 1): "x"}, run_id="b1", n=1, specs=SPECS[:2])
    after = await make_run(tmp_path, run_id="a1", n=1, specs=SPECS[:2])
    path = tmp_path / "r.md"
    entry = append_entry(after, path, compare_to=before, notes="added tool X")
    assert "### Change: `b1` -> `a1`" in entry and "added tool X" in path.read_text()


async def test_injection_row_shows_followed_and_seen_and_dashes_for_old_scores(tmp_path):
    run = await make_run(tmp_path, n=3, specs=[SPECS[-1]])
    for rec in run.records:
        rec.score.injection_seen = rec.attempt != 3
    stats = scenario_stats(run)[0]
    assert (stats.injections_seen, stats.injection_seen_n) == (2, 3)
    assert "followed injection 0/3, payload seen 2/3" in render_entry(run)
    for rec in run.records:  # scored before `injection_seen` existed
        rec.score.injection_seen = None
    assert "followed injection 0/3, payload seen -" in render_entry(run)
