"""Runner with a fake investigator: N runs, concurrency, errors, budget stop, persistence."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from synthetic import (
    cause,
    good_evidence,
    make_report,
    make_result,
    make_spec,
    standard_log,
)

from incident_agent.agent.models.base import BudgetExceededError, Usage
from incident_agent.agent.models.budget import BudgetedModelClient, SpendLedger
from incident_agent.agent.models.fake import ScriptedModelClient, text_response
from incident_agent.agent.report import CauseCategory as Cat
from incident_agent.evals.runner import (
    RunStatus,
    load_eval_run,
    rescore,
    run_eval,
    select_scenarios,
)
from incident_agent.evals.scenario import Difficulty

pytestmark = pytest.mark.anyio

SPECS = [
    make_spec("checkout-latency", "single"),
    make_spec("cart-errors", "single", causes=[("cart", Cat.ERROR_RATE)]),
    make_spec("healthy", "negative", healthy=True),
]


class Fake:
    """An investigator that writes a real step log and returns a result. `plan[(sid, n)]` can be
    an exception to raise (after writing the log if `log_first`), or a service name to blame."""

    def __init__(self, plan: dict | None = None, delay: float = 0.0) -> None:
        self.plan = plan or {}
        self.delay = delay
        self.calls: list[tuple[str, int]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.run_ids: set[str] = set()

    async def __call__(self, spec, attempt, log_path: Path, run_id):
        self.calls.append((spec.id, attempt))
        self.run_ids.add(run_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await anyio.sleep(self.delay)
            lb, a, b = standard_log(spec.id)
            action = self.plan.get((spec.id, attempt))
            if isinstance(action, BaseException):
                lb.write(log_path)
                raise action
            if spec.ground_truth.healthy:
                report = make_report()
            else:
                gt = spec.ground_truth.causes[0]
                service = action or gt.service
                report = make_report([cause(service, gt.category)], evidence=good_evidence(a, b))
            lb.finish(report)
            lb.write(log_path)
            return make_result(report, log_path)
        finally:
            self.in_flight -= 1


async def test_runs_every_scenario_n_times_and_writes_files(tmp_path):
    inv = Fake()
    run = await run_eval(
        SPECS, inv, runs_per_scenario=2, concurrency=1, run_id="r1", runs_dir=tmp_path,
        provider="anthropic", model_id="m", config={"flag": 1}, notes="hello",
    )  # fmt: skip
    assert len(inv.calls) == 6 and inv.run_ids == {"r1"}
    # attempt-major order: every scenario once before any scenario twice
    assert [n for _, n in inv.calls] == [1, 1, 1, 2, 2, 2]
    assert run.finished and not run.partial and run.not_completed == []
    assert all(r.status == RunStatus.COMPLETED and r.score.passed for r in run.records)
    d = tmp_path / "r1"
    for sid in ("checkout-latency", "cart-errors", "healthy"):
        for n in (1, 2):
            assert (d / sid / f"{n}.jsonl").exists()
            rec = json.loads((d / sid / f"{n}.result.json").read_text())
            assert rec["scenario_id"] == sid and rec["score"]["passed"] is True
    assert (d / "scenarios.json").exists()
    saved = load_eval_run(d)
    assert saved.model_dump() == run.model_dump()
    assert saved.run_id == "r1" and saved.notes == "hello" and saved.config == {"flag": 1}
    assert saved.scenario_difficulty["healthy"] == Difficulty.NEGATIVE
    assert saved.agent_config == {"max_steps": 20, "max_cost_usd": 0.5}  # from run_start
    assert saved.git_dirty is None or isinstance(saved.git_dirty, bool)


async def test_provider_and_model_default_to_the_run_start_log(tmp_path):
    run = await run_eval(SPECS[:1], Fake(), runs_per_scenario=1, run_id="r", runs_dir=tmp_path)
    assert (run.provider, run.model_id) == ("anthropic", "claude-sonnet-5-5")


async def test_concurrency_is_bounded(tmp_path):
    inv = Fake(delay=0.02)
    await run_eval(SPECS, inv, runs_per_scenario=2, concurrency=2, run_id="c", runs_dir=tmp_path)
    assert inv.max_in_flight == 2
    inv = Fake(delay=0.02)
    await run_eval(SPECS, inv, runs_per_scenario=2, concurrency=10, run_id="c2", runs_dir=tmp_path)
    assert inv.max_in_flight == 6  # never more than there are runs
    inv = Fake(delay=0.01)
    await run_eval(SPECS, inv, runs_per_scenario=1, concurrency=1, run_id="c3", runs_dir=tmp_path)
    assert inv.max_in_flight == 1


async def test_failures_are_recorded_not_dropped(tmp_path):
    inv = Fake({("checkout-latency", 1): "cart"})  # wrong service
    run = await run_eval(SPECS[:1], inv, runs_per_scenario=2, run_id="f", runs_dir=tmp_path)
    assert [r.score.passed for r in run.records] == [False, True]


async def test_exception_becomes_an_errored_run_and_the_suite_continues(tmp_path):
    inv = Fake({("cart-errors", 1): RuntimeError("tool server crashed")})
    run = await run_eval(
        SPECS, inv, runs_per_scenario=2, concurrency=1, run_id="e", runs_dir=tmp_path
    )
    assert len(inv.calls) == 6 and len(run.records) == 6
    bad = next(r for r in run.records if r.status == RunStatus.ERRORED)
    assert (bad.scenario_id, bad.attempt) == ("cart-errors", 1)
    assert bad.error == "RuntimeError: tool server crashed"
    assert bad.result is None and bad.score.errored and not bad.score.passed
    assert bad.score.steps == 2  # numbers recovered from the partial log it wrote
    assert sum(r.score.passed for r in run.records) == 5
    assert run.finished and not run.partial
    saved = json.loads((tmp_path / "e" / "cart-errors" / "1.result.json").read_text())
    assert saved["status"] == "errored" and "crashed" in saved["error"]


async def test_budget_exceeded_stops_scheduling_and_marks_partial(tmp_path):
    inv = Fake({("cart-errors", 1): BudgetExceededError("Spend cap reached: $10.00")})
    run = await run_eval(
        SPECS, inv, runs_per_scenario=2, concurrency=1, run_id="b", runs_dir=tmp_path
    )
    assert inv.calls == [("checkout-latency", 1), ("cart-errors", 1)]  # nothing scheduled after
    assert run.partial and run.finished and "Spend cap reached" in run.partial_reason
    by_status = {r.scenario_id: r.status for r in run.records}
    assert by_status == {
        "checkout-latency": RunStatus.COMPLETED,
        "cart-errors": RunStatus.BUDGET_STOPPED,
    }
    cut = next(r for r in run.records if r.status == RunStatus.BUDGET_STOPPED)
    assert not cut.scored and cut.score.cost_usd == 0.02  # its spend is still recorded
    assert len(run.scored_records) == 1
    assert run.not_completed == [
        "checkout-latency/2", "cart-errors/1", "cart-errors/2", "healthy/1", "healthy/2",
    ]  # fmt: skip
    assert load_eval_run(tmp_path / "b").partial


async def test_budget_error_before_any_model_call_keeps_no_record(tmp_path):
    class Dead:
        async def __call__(self, *a):
            raise BudgetExceededError("cap")

    run = await run_eval(
        SPECS, Dead(), runs_per_scenario=1, concurrency=1, run_id="d", runs_dir=tmp_path
    )
    assert run.partial and run.records == []
    assert run.not_completed == ["checkout-latency/1", "cart-errors/1", "healthy/1"]


async def test_summary_is_written_incrementally(tmp_path):
    seen: list[list[str]] = []

    class Peek(Fake):
        async def __call__(self, spec, attempt, log_path, run_id):
            summary = json.loads((tmp_path / "i" / "summary.json").read_text())
            seen.append([f"{r['scenario_id']}/{r['attempt']}" for r in summary["records"]])
            assert summary["finished"] is False
            return await super().__call__(spec, attempt, log_path, run_id)

    await run_eval(SPECS, Peek(), runs_per_scenario=1, concurrency=1, run_id="i", runs_dir=tmp_path)
    assert seen == [[], ["checkout-latency/1"], ["checkout-latency/1", "cart-errors/1"]]


class Hard(BaseException):
    """Stands in for Ctrl-C / cancellation, which bypasses `except Exception`."""


async def test_crash_keeps_completed_results(tmp_path):
    class Crash(Fake):
        async def __call__(self, spec, attempt, log_path, run_id):
            if spec.id == "healthy":
                raise Hard  # not an Exception: the whole run dies
            return await super().__call__(spec, attempt, log_path, run_id)

    with pytest.raises(BaseException):  # noqa: B017  (anyio may wrap it in an exception group)
        await run_eval(
            SPECS, Crash(), runs_per_scenario=1, concurrency=1, run_id="k", runs_dir=tmp_path
        )
    saved = load_eval_run(tmp_path / "k")
    assert [r.scenario_id for r in saved.records] == ["checkout-latency", "cart-errors"]
    assert saved.partial and saved.partial_reason == "interrupted" and not saved.finished


async def test_existing_run_id_is_refused_and_logs_never_overwritten(tmp_path):
    await run_eval(SPECS[:1], Fake(), runs_per_scenario=1, run_id="x", runs_dir=tmp_path)
    with pytest.raises(FileExistsError):
        await run_eval(SPECS[:1], Fake(), runs_per_scenario=1, run_id="x", runs_dir=tmp_path)


async def test_resume_redoes_only_what_is_missing(tmp_path):
    first = Fake({("cart-errors", 1): BudgetExceededError("cap")})
    run = await run_eval(
        SPECS, first, runs_per_scenario=1, concurrency=1, run_id="rs", runs_dir=tmp_path
    )
    assert run.partial and run.not_completed == ["cart-errors/1", "healthy/1"]
    second = Fake()
    run = await run_eval(
        SPECS,
        second,
        runs_per_scenario=1,
        concurrency=1,
        run_id="rs",
        runs_dir=tmp_path,
        resume=True,
    )
    assert second.calls == [("cart-errors", 1), ("healthy", 1)]
    assert not run.partial and run.finished and run.not_completed == []
    assert len(run.records) == 3 and all(r.status == RunStatus.COMPLETED for r in run.records)
    assert (tmp_path / "rs" / "cart-errors" / "1.prev1.jsonl").exists()  # cut-off log archived


async def test_resume_skips_errored_unless_retry(tmp_path):
    first = Fake({("cart-errors", 1): RuntimeError("x")})
    await run_eval(SPECS[:2], first, runs_per_scenario=1, run_id="re", runs_dir=tmp_path)
    again = Fake()
    await run_eval(
        SPECS[:2], again, runs_per_scenario=1, run_id="re", runs_dir=tmp_path, resume=True
    )
    assert again.calls == []
    retry = Fake()
    run = await run_eval(
        SPECS[:2], retry, runs_per_scenario=1, run_id="re", runs_dir=tmp_path, resume=True,
        retry_errored=True,
    )  # fmt: skip
    assert retry.calls == [("cart-errors", 1)] and all(r.score.passed for r in run.records)


async def test_spend_label_tags_model_spend_per_run_and_ledger_cap_stops_the_suite(tmp_path):
    ledger = SpendLedger(tmp_path / "ledger.jsonl", cap_usd=0.25)

    async def investigator(spec, attempt, log_path, run_id):
        client = BudgetedModelClient(
            ScriptedModelClient([text_response("ok", cost_usd=0.2, usage=Usage(input_tokens=5))]),
            ledger,
        )
        from incident_agent.agent.models.base import Message

        await client.complete("s", [Message.user("hi")], [], 10)
        lb, a, b = standard_log(spec.id)
        report = make_report([cause("checkout", Cat.LATENCY)], evidence=good_evidence(a, b))
        lb.finish(report)
        lb.write(log_path)
        return make_result(report, log_path, cost=0.2)

    run = await run_eval(
        SPECS[:2], investigator, runs_per_scenario=2, concurrency=1, run_id="L",
        runs_dir=tmp_path, ledger=ledger,
    )  # fmt: skip
    labels = set(ledger.summary("L/").by_label)
    assert labels == {"L/checkout-latency/1", "L/cart-errors/1"}  # cap hit after two $0.20 calls
    assert run.ledger_cost_usd == pytest.approx(0.4)
    assert run.partial and "Spend cap reached" in run.partial_reason
    assert len(run.records) == 2 and run.not_completed == ["checkout-latency/2", "cart-errors/2"]


async def test_input_validation(tmp_path):
    with pytest.raises(ValueError):
        await run_eval(SPECS, Fake(), runs_per_scenario=0, runs_dir=tmp_path)
    with pytest.raises(ValueError):
        await run_eval(SPECS, Fake(), concurrency=0, runs_dir=tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        await run_eval([SPECS[0], SPECS[0]], Fake(), runs_dir=tmp_path)


def test_select_scenarios():
    assert [s.id for s in select_scenarios(SPECS, ids=["healthy"])] == ["healthy"]
    assert [s.id for s in select_scenarios(SPECS, difficulties=["single"])] == [
        "checkout-latency", "cart-errors",
    ]  # fmt: skip
    assert [
        s.id
        for s in select_scenarios(
            SPECS, ids=["healthy", "cart-errors"], difficulties=[Difficulty.SINGLE]
        )
    ] == ["cart-errors"]
    assert len(select_scenarios(SPECS)) == 3
    with pytest.raises(ValueError, match="nope"):
        select_scenarios(SPECS, ids=["nope"])


async def test_rescore_recomputes_from_saved_files_without_calling_anything(tmp_path):
    inv = Fake({("cart-errors", 1): "checkout"})
    run = await run_eval(
        SPECS[:2], inv, runs_per_scenario=1, concurrency=1, run_id="rr", runs_dir=tmp_path
    )
    assert [r.score.passed for r in run.records] == [True, False]
    calls_before = len(inv.calls)
    log_text = (tmp_path / "rr" / "cart-errors" / "1.jsonl").read_text()

    # 1. no spec change: identical scores, no investigator involved
    same = rescore(tmp_path / "rr")
    assert [r.score for r in same.records] == [r.score for r in run.records]
    assert same.rescored_at is not None

    # 2. the ground truth was corrected: the previously wrong answer is now right
    fixed = [SPECS[0], make_spec("cart-errors", "single", causes=[("checkout", Cat.ERROR_RATE)])]
    again = rescore(tmp_path / "rr", fixed)
    assert [r.score.passed for r in again.records] == [True, True]
    assert load_eval_run(tmp_path / "rr").records[1].score.passed
    on_disk = json.loads((tmp_path / "rr" / "cart-errors" / "1.result.json").read_text())
    assert on_disk["score"]["passed"] is True
    assert len(inv.calls) == calls_before
    assert (tmp_path / "rr" / "cart-errors" / "1.jsonl").read_text() == log_text  # logs untouched


async def test_rescore_keeps_errored_and_cut_off_runs(tmp_path):
    inv = Fake({
        ("cart-errors", 1): RuntimeError("x"),
        ("healthy", 1): BudgetExceededError("cap"),
    })  # fmt: skip
    await run_eval(SPECS, inv, runs_per_scenario=1, concurrency=1, run_id="rk", runs_dir=tmp_path)
    run = rescore(tmp_path / "rk")
    statuses = {r.scenario_id: (r.status, r.score.passed) for r in run.records}
    assert statuses["cart-errors"] == (RunStatus.ERRORED, False)
    assert statuses["healthy"] == (RunStatus.BUDGET_STOPPED, False)
    assert run.records[0].score.passed
    with pytest.raises(KeyError):
        rescore(tmp_path / "rk", SPECS[:1])


async def test_scoring_failure_does_not_lose_the_result(tmp_path, monkeypatch):
    import incident_agent.evals.runner as runner_mod

    def boom(*a, **k):
        raise ZeroDivisionError("bug in scoring")

    monkeypatch.setattr(runner_mod, "score_run", boom)
    run = await run_eval(SPECS[:1], Fake(), runs_per_scenario=1, run_id="sf", runs_dir=tmp_path)
    rec = run.records[0]
    assert rec.result is not None and not rec.score.passed
    assert "scoring failed" in rec.score.error
    # and rescoring after the bug is fixed recovers it
    monkeypatch.undo()
    assert rescore(tmp_path / "sf").records[0].score.passed
