"""Memory diagnostics from step logs, the `### Memory` report section, and the runner wiring.

The recall results in these logs are produced by the real `RecallTool` so the scorer is tested
against the exact text the agent sees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from synthetic import (
    LogBuilder,
    cause,
    make_report,
    make_result,
    make_spec,
    standard_log,
)
from test_eval_runner import Fake

from incident_agent.agent.memory import MemoryStore, RecallTool, Scope
from incident_agent.agent.report import CauseCategory as Cat
from incident_agent.agent.report import Evidence, Verdict
from incident_agent.evals import scoring
from incident_agent.evals.logs import StepLog, parse_log_lines
from incident_agent.evals.report import render_entry
from incident_agent.evals.runner import memory_enabled, rescore, run_eval
from incident_agent.evals.scoring import MemoryScore, RunScore, memory_score, score_run

pytestmark = pytest.mark.anyio

# The scenario: checkout is the true cause (like payment-unreachable); notes blame payment.
SPEC = make_spec("payment-unreachable", "single", causes=[("checkout", Cat.DEPENDENCY_FAILURE)])
HEALTHY = make_spec("healthy-baseline", "negative", healthy=True)

STALE_PAYMENT = "The payment service had an outage about three weeks ago."
STALE_CHECKOUT = (
    "The last checkout failure was the origin of the payment service rejecting charges."
)
BASELINE = "In healthy periods payment server spans have a p95 under 1 ms."

QUERY_PAYMENT = {"filters": [{"field": "service.name", "op": "=", "value": "payment"}]}
QUERY_CHECKOUT = {"filters": [{"field": "service.name", "op": "=", "value": "checkout"}]}
QUERY_ALL = {"group_by": ["service.name"], "calculations": [{"op": "ERROR_RATE"}]}


def to_log(lb: LogBuilder) -> StepLog:
    return parse_log_lines(json.dumps(r) for r in lb.records)


def store_with(tmp_path: Path, *notes: tuple[str, str, str]) -> MemoryStore:
    """Notes are `(id, service, content)`."""
    store = MemoryStore(tmp_path / "mem")
    for note_id, service, content in notes:
        store.save(
            content=content,
            scope=Scope.SERVICE,
            scope_key=service,
            source="seed",
            reason="r",
            written_by="seed",
            note_id=note_id,
        )
    return store


async def recall(store: MemoryStore, service: str, keywords: str | None = None) -> tuple[dict, str]:
    args = {"scope": "service", "scope_key": service}
    if keywords:
        args["keywords"] = keywords
    text, _ = await RecallTool(store, investigation_id="x").call(args)
    return args, text


def new_log(spec=SPEC) -> LogBuilder:
    return LogBuilder(spec.id, refs=True)


def finished(lb: LogBuilder, report) -> StepLog:
    lb.finish(report)
    return to_log(lb)


def wrong_report(service="payment", evidence=None):
    return make_report([cause(service, Cat.DEPENDENCY_FAILURE)], evidence=evidence)


# --- recalls, returned notes, saves ---------------------------------------------------------


async def test_counts_recalls_returned_ids_and_saves(tmp_path):
    store = store_with(
        tmp_path, ("n1", "payment", STALE_PAYMENT), ("n2", "checkout", STALE_CHECKOUT)
    )
    lb = new_log()
    lb.tool("describe_dataset", {}, "services: checkout, payment")
    lb.tool("recall", *await recall(store, "payment"))
    lb.tool("recall", *await recall(store, "checkout"))
    lb.tool("recall", *await recall(store, "payment"))  # again: ids are listed once
    lb.tool("recall", *await recall(store, "cart"))  # nothing stored for cart
    lb.tool("save_memory", {"content": "x"}, "Saved note m_1 for service 'x'. Future runs...")
    lb.tool("save_memory", {"content": "y"}, "Not saved: nope", is_error=True)
    m = memory_score(SPEC, None, finished(lb, wrong_report()))
    assert m.recall_calls == 4
    assert m.memories_returned == ["n1", "n2"]
    assert m.recalled_services == ["payment", "checkout"]
    assert (m.save_calls, m.saves_accepted) == (2, 1)


async def test_recall_errors_and_empty_recalls_return_no_notes(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", {"scope": "investigation", "scope_key": "other"}, "refused", is_error=True)
    lb.tool("recall", *await recall(store, "payment", keywords="nomatch"))
    m = memory_score(SPEC, None, finished(lb, wrong_report()))
    assert m.recall_calls == 2 and m.memories_returned == [] and m.recalled_services == []
    assert m.verified_after_recall is None  # nothing was recalled, so nothing to verify


async def test_investigation_scope_recall_returns_notes_but_no_service(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    store.save(
        content="earlier this investigation payment looked fine",
        scope=Scope.INVESTIGATION,
        scope_key="inv",
        source="s",
        reason="r",
        written_by="w",
        note_id="inv-note",
    )
    text, _ = await RecallTool(store, investigation_id="inv").call(
        {"scope": "investigation", "scope_key": "current"}
    )
    lb = new_log()
    lb.tool("recall", {"scope": "investigation", "scope_key": "current"}, text)
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.memories_returned == ["inv-note"] and m.recalled_services == []
    assert m.verified_after_recall is None


# --- verified_after_recall ------------------------------------------------------------------


async def test_verified_when_the_recalled_service_is_queried_afterwards(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("describe_dataset", {}, "services: checkout, payment")
    lb.tool("recall", *await recall(store, "payment"))
    lb.tool("run_query", QUERY_PAYMENT, "payment: 0 errors")
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.verified_after_recall is True and m.unverified_services == [] and m.flags == []


async def test_not_verified_when_other_services_are_queried_or_the_query_came_first(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("run_query", QUERY_PAYMENT, "payment: 0 errors")  # before the recall: does not count
    lb.tool("recall", *await recall(store, "payment"))
    lb.tool("run_query", QUERY_CHECKOUT, "checkout: 100% errors")  # a different service
    lb.tool("run_query", QUERY_PAYMENT, "boom", is_error=True)  # an errored call verifies nothing
    lb.tool("describe_dataset", {}, "services: checkout, payment")  # lists, does not query
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.verified_after_recall is False and m.unverified_services == ["payment"]
    assert m.flags == ["recalled notes about payment but never queried it afterwards"]


async def test_a_breakdown_by_service_counts_as_checking_every_service(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))
    lb.tool("run_query", QUERY_ALL, "service.name error_rate\npayment 0.0%\ncheckout 100%")
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.verified_after_recall is True


async def test_every_recalled_service_must_be_verified(tmp_path):
    store = store_with(
        tmp_path, ("n1", "payment", STALE_PAYMENT), ("n2", "checkout", STALE_CHECKOUT)
    )
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))
    lb.tool("recall", *await recall(store, "checkout"))
    lb.tool("run_query", QUERY_CHECKOUT, "checkout: 100% errors")
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.verified_after_recall is False and m.unverified_services == ["payment"]


async def test_field_names_are_not_service_names(tmp_path):
    store = store_with(tmp_path, ("n1", "cart", "cart errors were the cause of a past slowdown."))
    lb = new_log()
    lb.tool("recall", *await recall(store, "cart"))
    lb.tool(
        "run_query",
        {"filters": [{"field": "app.cart.items", "op": "exists"}], "group_by": ["app.cart.id"]},
        "rows",
    )
    m = memory_score(SPEC, None, finished(lb, wrong_report("checkout")))
    assert m.verified_after_recall is False


# --- cited_memory_as_evidence ---------------------------------------------------------------


async def test_citing_a_recall_result_as_evidence_is_flagged(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("describe_dataset", {}, "services: checkout, payment")  # c1
    lb.tool("recall", *await recall(store, "payment"))  # c2
    lb.tool("run_query", QUERY_PAYMENT, "payment: 0 errors")  # c3
    evidence = [
        Evidence(claim="payment had an outage", tool_call_id="c2", cited_values=["three weeks"]),
        Evidence(claim="payment has no errors now", tool_call_id="c3", cited_values=["0 errors"]),
    ]
    m = memory_score(SPEC, wrong_report("payment", evidence), finished(lb, wrong_report()))
    assert m.cited_memory_as_evidence and m.memory_citations == 1
    assert "cited a recall result as evidence (c2)" in m.flags


async def test_citing_telemetry_only_is_not_flagged(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))  # c1
    lb.tool("run_query", QUERY_CHECKOUT, "checkout: 100% errors")  # c2
    evidence = [Evidence(claim="checkout fails", tool_call_id="c2", cited_values=["100%"])]
    report = wrong_report("checkout", evidence)
    m = memory_score(SPEC, report, finished(lb, report))
    assert not m.cited_memory_as_evidence and m.memory_citations == 0


# --- followed_stale_memory ------------------------------------------------------------------


async def test_wrong_top_cause_that_a_note_pointed_at_is_flagged(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))
    report = wrong_report("payment")
    m = memory_score(SPEC, report, finished(lb, report))
    assert m.followed_stale_memory
    assert "top cause payment is the service a recalled note pointed at" in m.flags


async def test_a_note_that_only_names_the_service_in_its_text_counts(tmp_path):
    store = store_with(tmp_path, ("n2", "checkout", STALE_CHECKOUT))  # scoped to checkout
    lb = new_log()
    lb.tool("recall", *await recall(store, "checkout"))
    report = wrong_report("payment")
    assert memory_score(SPEC, report, finished(lb, report)).followed_stale_memory


async def test_right_answer_or_unrelated_wrong_answer_is_not_followed_stale_memory(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    for service in ("checkout", "cart"):  # the true cause; and wrong but not what the note said
        lb = new_log()
        lb.tool("recall", *await recall(store, "payment"))
        report = wrong_report(service)
        assert not memory_score(SPEC, report, finished(lb, report)).followed_stale_memory
    # wrong answer that matches a note, but the note was never recalled
    lb = new_log()
    lb.tool("describe_dataset", {}, "services")
    lb.tool("recall", *await recall(store, "cart"))
    report = wrong_report("payment")
    assert not memory_score(SPEC, report, finished(lb, report)).followed_stale_memory


async def test_inconclusive_report_is_not_followed_stale_memory(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))
    report = make_report([cause("payment", Cat.DEPENDENCY_FAILURE)], verdict=Verdict.INCONCLUSIVE)
    assert not memory_score(SPEC, report, finished(lb, report)).followed_stale_memory


async def test_healthy_scenario_any_cause_on_a_pointed_at_service_is_followed(tmp_path):
    store = store_with(tmp_path, ("n1", "checkout", "Checkout normally has an error rate of 12%."))
    for report, followed in (
        (wrong_report("checkout"), True),
        (wrong_report("cart"), False),
        (make_report(), False),  # no_significant_issue: the right answer
    ):
        lb = new_log(HEALTHY)
        lb.tool("recall", *await recall(store, "checkout"))
        assert memory_score(HEALTHY, report, finished(lb, report)).followed_stale_memory is followed


# --- score_run integration: additive, off the pass/fail path ---------------------------------


async def test_score_run_adds_memory_without_changing_anything_else(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    lb = new_log()
    lb.tool("recall", *await recall(store, "payment"))
    report = wrong_report("payment")
    log = finished(lb, report)
    result = make_result(report, "x.jsonl")

    plain = score_run(SPEC, result, log)  # the log has recall calls: diagnostics appear anyway
    assert plain.memory is not None and plain.memory.followed_stale_memory
    no_memory_log = parse_log_lines(json.dumps(r) for r in lb.records if r.get("name") != "recall")
    bare = score_run(SPEC, result, no_memory_log)
    assert bare.memory is None  # no memory flag, no memory calls: byte-identical to before

    flagged = score_run(SPEC, result, no_memory_log, memory=True)
    assert flagged.memory == MemoryScore()  # memory on but never used
    assert flagged.model_dump(exclude={"memory"}) == bare.model_dump(exclude={"memory"})
    assert plain.passed == bare.passed and plain.failure_reasons == bare.failure_reasons


async def test_citing_memory_does_not_change_pass_or_fail(tmp_path):
    store = store_with(tmp_path, ("n1", "checkout", "checkout calls payment and it is down."))
    lb = new_log()
    lb.tool("recall", *await recall(store, "checkout"))  # c1
    lb.tool("run_query", QUERY_CHECKOUT, "checkout: 100% errors")  # c2
    report = make_report(
        [cause("checkout", Cat.DEPENDENCY_FAILURE)],
        evidence=[
            Evidence(claim="a note says payment is down", tool_call_id="c1", cited_values=["down"]),
            Evidence(claim="checkout fails", tool_call_id="c2", cited_values=["100%"]),
        ],
    )
    s = score_run(SPEC, make_result(report, "x.jsonl"), finished(lb, report))
    assert s.memory is not None and s.memory.cited_memory_as_evidence
    assert s.passed  # flagged, not failed


def test_runs_that_never_finished_still_get_diagnostics():
    lb = new_log()
    lb.tool("recall", {"scope": "service", "scope_key": "payment"}, "No unexpired memory notes")
    failed = scoring.score_failed_run(SPEC, to_log(lb), error="boom", memory=True)
    assert failed.memory is not None and failed.memory.recall_calls == 1 and not failed.passed
    assert scoring.score_failed_run(SPEC, to_log(lb), error="boom").memory is not None
    assert scoring.score_failed_run(SPEC, StepLog(), error="boom").memory is None


def test_a_bug_in_the_diagnostics_never_loses_the_run(monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(scoring, "memory_score", boom)
    lb, a, b = standard_log(SPEC.id)
    report = wrong_report("checkout")
    lb.finish(report)
    s = score_run(SPEC, make_result(report, "x.jsonl"), to_log(lb), memory=True)
    assert s.passed and s.service_correct  # scored normally
    assert (
        s.memory is not None and "memory diagnostics failed: ZeroDivisionError" in s.memory.flags[0]
    )


def test_old_scores_without_memory_still_load():
    s = score_run(SPEC, make_result(wrong_report("checkout"), "x"), StepLog(missing=True))
    data = json.loads(s.model_dump_json())
    data.pop("memory")
    assert RunScore.model_validate(data).memory is None


# --- runner wiring --------------------------------------------------------------------------


def test_memory_enabled_reads_the_recorded_config():
    assert memory_enabled({"memory_dir": "runs/memory", "memory_seed": None})
    assert not memory_enabled({})
    assert not memory_enabled({"effort": "medium", "context_mode": "none"})


async def test_run_eval_scores_memory_only_when_the_config_says_memory_is_on(tmp_path):
    specs = [make_spec("checkout-latency", "single")]
    off = await run_eval(specs, Fake(), runs_per_scenario=1, run_id="off", runs_dir=tmp_path)
    assert off.records[0].score.memory is None
    on = await run_eval(
        specs,
        Fake(),
        runs_per_scenario=1,
        run_id="on",
        runs_dir=tmp_path,
        config={"memory_dir": "m", "memory_seed": "s.yaml", "memory_per_run": True},
    )
    assert on.records[0].score.memory == MemoryScore()  # memory was on; the fake never recalled
    assert on.records[0].score.passed == off.records[0].score.passed
    # rescoring a saved memory run keeps (and recomputes) the diagnostics from the log
    assert rescore(tmp_path / "on").records[0].score.memory == MemoryScore()
    assert rescore(tmp_path / "off").records[0].score.memory is None


# --- report rendering -----------------------------------------------------------------------


class MemoryFake(Fake):
    """A fake investigator whose runs recall a note (blaming the wrong service on attempt 1)."""

    def __init__(self, store: MemoryStore, wrong_attempts: set[int]) -> None:
        super().__init__()
        self.store, self.wrong_attempts = store, wrong_attempts

    async def __call__(self, spec, attempt, log_path: Path, run_id):
        args, text = await recall(self.store, "payment")
        lb = LogBuilder(spec.id, run_id, refs=True)
        lb.tool("describe_dataset", {}, "services: checkout, payment")
        lb.tool("recall", args, text)
        if attempt in self.wrong_attempts:
            report = wrong_report("payment")  # stops after recall: never queries payment
        else:
            lb.tool("run_query", QUERY_PAYMENT, "payment: 0 errors")
            lb.tool("run_query", QUERY_CHECKOUT, "checkout: 100% errors")
            report = make_report(
                [cause("checkout", Cat.DEPENDENCY_FAILURE)],
                evidence=[Evidence(claim="c", tool_call_id="c3", cited_values=["0 errors"])],
            )
        lb.finish(report)
        lb.write(log_path)
        return make_result(report, log_path)


async def test_report_shows_a_memory_section_when_memory_was_on(tmp_path):
    store = store_with(tmp_path, ("n1", "payment", STALE_PAYMENT))
    run = await run_eval(
        [SPEC],
        MemoryFake(store, wrong_attempts={1}),
        runs_per_scenario=2,
        run_id="mem",
        runs_dir=tmp_path / "runs",
        config={"memory_dir": "runs/memory", "memory_seed": "seed.yaml", "memory_per_run": True},
    )
    md = render_entry(run)
    assert "`memory_seed=seed.yaml`" in md and "`memory_per_run=True`" in md
    assert "### Memory" in md
    assert "| scenario | memory runs | got notes | recalls/run | notes returned | verified |" in md
    row = next(line for line in md.splitlines() if line.startswith("| payment-unreachable | 2 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # got notes 2/2, 1 recall per run, note n1, verified 1/2, cited 0/2, followed stale 1/2
    assert cells[2:8] == ["2/2", "1.0", "`n1`", "1/2", "0/2", "1/2"]
    assert "Flagged:" in md
    assert "`payment-unreachable` run 1: recalled notes about payment but never queried it" in md
    assert "top cause payment is the service a recalled note pointed at" in md
    # pass/fail columns are untouched by the memory diagnostics
    assert "| payment-unreachable | single | 2 | 1/2 |" in md


async def test_report_has_no_memory_section_without_memory(tmp_path):
    run = await run_eval(
        [make_spec("checkout-latency", "single")],
        Fake(),
        runs_per_scenario=1,
        run_id="plain",
        runs_dir=tmp_path,
    )
    md = render_entry(run)
    assert "### Memory" not in md and "memory" not in md.lower()
