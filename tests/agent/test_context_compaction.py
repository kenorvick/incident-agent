"""Compaction: key facts survive, refs stay citable and gradable, failures fall back to clearing."""

from __future__ import annotations

import pytest
from agent_fakes import FakeExecutor, read_log, report_args
from context_helpers import (
    ALERT,
    assert_valid_history,
    call,
    is_summary_request,
    pad,
    run,
    text_of,
)

from incident_agent.agent.context import (
    COMPACT_REQUEST,
    COMPACT_SYSTEM,
    SUMMARY_HEADER,
    ContextConfig,
)
from incident_agent.agent.models.base import (
    BudgetExceededError,
    ModelAPIError,
    TextBlock,
    Usage,
)
from incident_agent.agent.models.fake import ScriptedModelClient, text_response, tool_call_response
from incident_agent.agent.report import TerminatedReason
from incident_agent.backends import default_fixtures_dir
from incident_agent.evals.logs import parse_log
from incident_agent.evals.scenario import load_scenario
from incident_agent.evals.scoring import score_run

pytestmark = pytest.mark.anyio

TRACE_ID = "6de69b563734ff7d27eceffd6ab8a729"
P95 = "p95(duration_ms)=25043"
ERROR_RATE = "error_rate=6.15%"

SUMMARY = f"""## Decisions made
Focused on product-catalog after the service table showed it slow.

## Findings
- product-catalog {P95} since 00:07 [ref c2]
- frontend-proxy {ERROR_RATE} [ref c2]
- one slow trace {TRACE_ID}: astronomy-db span took 29927 ms [ref c3]

## Ruled out
checkout is fast [ref c2]

## Open questions
Why is astronomy-db slow?

## Key identifiers
trace_id {TRACE_ID} [ref c3]; fields service.name, duration_ms [ref c1]
"""


def tools():
    return FakeExecutor(
        {
            "describe_dataset": pad(
                "Dataset: 16393 events | 15 services | fields: service.name", 300
            ),
            "run_query": pad("run_query: p95 by service.name", 300) + f"\n{P95}\n{ERROR_RATE}",
            "get_trace": pad(f"get_trace {TRACE_ID}: 10 spans", 300) + "\nastronomy-db 29927ms",
        }
    )


def compacting_script(*, summary=SUMMARY, then=None):
    """Three tool turns, the summary call, then the final report. Reported inputs make the
    estimate pass 75% of a 4000-token window before the fourth request."""
    evidence = [
        {"claim": "slow service", "tool_call_id": "c2", "cited_values": ["25043", "6.15%"]},
        {"claim": "slow db span", "tool_call_id": "c3", "cited_values": [TRACE_ID, "29927"]},
    ]
    final = tool_call_response(
        ("submit_report", report_args(evidence=evidence), "sub"),
        usage=Usage(input_tokens=1200, output_tokens=40),
        cost_usd=0.004,
    )
    summary_response = (
        summary
        if not isinstance(summary, str)
        else text_response(
            summary, usage=Usage(input_tokens=2800, output_tokens=300), cost_usd=0.01
        )
    )
    turns = [
        call("describe_dataset", "t1", 1000),
        call("run_query", "t2", 1800, group_by="service.name"),
        call("get_trace", "t3", 2800, trace_id=TRACE_ID),
    ]
    for turn in turns:
        turn.provider_data = {"fake": {"thinking_blocks": [f"opaque-{turn.tool_calls[0].id}"]}}
    return [*turns, summary_response, *(then or [final])]


def compact_config(window: int = 4000, **kw):
    kw.setdefault("compact_min_history_tokens", 500)  # the fake results here are small
    return ContextConfig(mode="compact", max_context_tokens=window, **kw)


def events(path, kind=None):
    found = [r for r in read_log(path) if r["type"] == "context_event"]
    return [r for r in found if kind in (None, r["kind"])]


async def test_compaction_asks_for_the_facts_and_the_history_keeps_them(tmp_path):
    model = ScriptedModelClient(compacting_script())
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert model.calls == 5

    # The summary request: its own system prompt, no tools, and everything the notes need.
    ask = model.requests[3]
    assert is_summary_request(ask) and ask.system == COMPACT_SYSTEM and ask.tools == []
    (only,) = ask.messages
    request_text = text_of(only)
    for fact in (ALERT, TRACE_ID, "25043", "6.15%", "29927", "[ref c1]", "[ref c2]", "[ref c3]"):
        assert fact in request_text
    assert "get_trace" in request_text and request_text.endswith(COMPACT_REQUEST)
    for section in ("Decisions made", "Findings", "Ruled out", "Open questions", "Key identifiers"):
        assert f"## {section}" in COMPACT_REQUEST

    # The next request: the alert plus the labelled summary, with the facts and the ref note.
    after = model.requests[4]
    assert [m.role for m in after.messages] == ["user", "user"]
    assert text_of(after.messages[0]) == f"Alert:\n\n{ALERT}"
    summary_text = text_of(after.messages[1])
    assert summary_text.startswith(SUMMARY_HEADER) and "remain valid evidence" in summary_text
    for fact in (TRACE_ID, P95, ERROR_RATE, "29927", "[ref c2]", "[ref c3]", "Ruled out"):
        assert fact in summary_text
    assert not any(m.role == "assistant" for m in after.messages)  # no stale reasoning echoed
    assert after.messages[0].provider_data == after.messages[1].provider_data == {}

    # The stable prefix is untouched.
    assert after.system == model.requests[2].system and after.tools == model.requests[2].tools
    for request in model.requests:
        assert_valid_history(request.messages)


async def test_a_pre_compaction_ref_is_still_citable_and_grounded_against_the_step_log(tmp_path):
    model = ScriptedModelClient(compacting_script())
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    log = parse_log(result.log_path)
    full = {r.ref: r for r in log.tool_results}
    assert P95 in full["c2"].content and TRACE_ID in full["c3"].content  # the log keeps it all
    spec = load_scenario(default_fixtures_dir() / "long-product-db-lock-cascade")
    score = score_run(spec, result, log)
    assert score.evidence_total == 2 and score.evidence_grounded == 2
    assert score.fabrication_free and score.invalid_tool_call_ids == []


async def test_compaction_is_logged_and_counted_in_steps_tokens_and_cost(tmp_path):
    model = ScriptedModelClient(compacting_script())
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.steps == 5 and result.tool_calls == 4  # three queries + submit_report
    assert result.input_tokens == 1000 + 1800 + 2800 + 2800 + 1200
    assert result.output_tokens == 20 * 3 + 300 + 40
    assert result.cost_usd == pytest.approx(0.014)

    log = read_log(result.log_path)
    calls = [r for r in log if r["type"] == "model_call"]
    assert [c.get("purpose") for c in calls] == [None, None, None, "compaction", None]
    assert calls[3]["text"] == SUMMARY
    (event,) = events(result.log_path, "compact")
    d = event["details"]
    assert d["status"] == "ok" and d["window"] == 4000
    assert d["tokens_before"] > 3000 > d["tokens_after"]  # passed 75%, then fell well below it
    assert (d["messages_before"], d["messages_after"]) == (7, 2)
    assert d["summary_tokens"] > 50
    end = next(r for r in log if r["type"] == "run_end")
    assert end["steps"] == 5 and end["input_tokens"] == result.input_tokens


async def test_tool_calls_after_compaction_continue_the_ref_numbering(tmp_path):
    more = call("run_query", "t4", 900, group_by="x")
    evidence = [{"claim": "c", "tool_call_id": "c4", "cited_values": ["25043"]}]
    final = tool_call_response(("submit_report", report_args(evidence=evidence), "sub"))
    model = ScriptedModelClient(compacting_script(then=[more, final]))
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    new_result = model.requests[5].tool_results[0]
    assert new_result.content.startswith("[ref c4]\n")
    assert_valid_history(model.requests[5].messages)
    refs = [r["ref"] for r in read_log(result.log_path) if r["type"] == "tool_result"]
    assert refs == ["c1", "c2", "c3", "c4", "c5"]


async def test_no_compaction_below_the_threshold(tmp_path):
    script = compacting_script()
    model = ScriptedModelClient([*script[:3], script[4]])  # no summary call expected
    result = await run(model, tools(), tmp_path, context=compact_config(window=100_000))
    assert result.terminated_reason == TerminatedReason.SUBMITTED and result.steps == 4
    assert not events(result.log_path)
    assert not any(is_summary_request(r) for r in model.requests)


# --- failure and fallback ----------------------------------------------------------------


async def test_failed_summary_call_falls_back_to_clearing_and_the_run_continues(tmp_path):
    script = compacting_script(summary=ModelAPIError("provider is down"))
    model = ScriptedModelClient(script)
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert result.steps == 4  # the failed summary call produced no response to count

    (failed,) = events(result.log_path, "compact")
    assert failed["details"]["status"] == "failed" and failed["details"]["fallback"] == "clear"
    (clear,) = events(result.log_path, "clear")
    assert clear["details"]["refs"] == ["c1"] and clear["details"]["instead_of_compaction"]
    history = model.requests[4].messages
    assert "(cleared to save context" in str(history)
    assert_valid_history(history)


async def test_empty_summary_falls_back_to_clearing(tmp_path):
    model = ScriptedModelClient(compacting_script(summary=text_response("   ")))
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert events(result.log_path, "compact")[0]["details"]["status"] == "failed"
    assert events(result.log_path, "clear")
    assert all(not isinstance(b, TextBlock) or "Context summary" not in b.text
               for m in model.requests[4].messages for b in m.content)  # fmt: skip


async def test_the_spend_cap_during_compaction_ends_the_run_as_budget(tmp_path):
    model = ScriptedModelClient(compacting_script(summary=BudgetExceededError("cap")))
    result = await run(model, tools(), tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert "Spend cap reached" in (result.error or "")


async def test_the_summary_call_uses_a_step_and_the_step_limit_still_holds(tmp_path):
    model = ScriptedModelClient(compacting_script())
    result = await run(model, tools(), tmp_path, context=compact_config(), max_steps=4)
    assert result.terminated_reason == TerminatedReason.MAX_STEPS
    assert result.steps == 4 and model.calls == 4  # the real request after the summary never ran


async def test_too_little_history_is_cleared_not_summarized(tmp_path):
    script = compacting_script()
    model = ScriptedModelClient([*script[:3], script[4]])
    config = ContextConfig(mode="compact", max_context_tokens=4000)  # default minimum: 3000
    result = await run(model, tools(), tmp_path, context=config)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert not any(is_summary_request(r) for r in model.requests)
    assert not events(result.log_path, "compact")  # a summary would not have saved anything
    assert events(result.log_path, "clear")


async def test_compaction_that_cannot_get_under_its_threshold_is_skipped_for_clearing(tmp_path):
    # The prefix alone (about 2.4k tokens here) is near 75% of this window, so a summary would
    # leave the next request over the threshold again and every step would compact.
    script = compacting_script()
    model = ScriptedModelClient([*script[:3], script[4]])
    result = await run(model, tools(), tmp_path, context=compact_config(window=3200))
    assert not any(is_summary_request(r) for r in model.requests)
    (skipped,) = events(result.log_path, "compact")  # logged once, then silently cleared
    assert skipped["details"]["status"] == "failed" and "futile" in skipped["details"]["error"]
    assert events(result.log_path, "clear")


async def test_after_the_last_allowed_compaction_it_clears_instead(tmp_path):
    summary = text_response(SUMMARY, usage=Usage(input_tokens=2800, output_tokens=300))
    script = [
        call("describe_dataset", "t1", 1000),
        call("run_query", "t2", 1800),
        call("get_trace", "t3", 2800),
        summary,  # compaction 1
        call("run_query", "t4", 3300),  # still over 75% afterwards (the provider says so)
        call("get_trace", "t5", 3300),
        call("run_query", "t6", 3300),
        tool_call_response(("submit_report", report_args(), "sub")),
    ]
    model = ScriptedModelClient(script)
    result = await run(model, tools(), tmp_path, context=compact_config(max_compactions=1))
    assert len(events(result.log_path, "compact")) == 1
    assert events(result.log_path, "clear")  # later pressure is handled by clearing
    for request in model.requests:
        assert_valid_history(request.messages)
    assert result.terminated_reason in (TerminatedReason.SUBMITTED, TerminatedReason.BUDGET)
