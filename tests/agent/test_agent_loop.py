"""The investigation loop, driven by a scripted model and a fake tool executor."""

from __future__ import annotations

import anyio
import pytest
from agent_fakes import FakeExecutor, read_log, report_args

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.models.base import (
    BudgetExceededError,
    ModelAPIError,
    ModelResponse,
    ToolUseBlock,
    Usage,
)
from incident_agent.agent.models.fake import ScriptedModelClient, text_response, tool_call_response
from incident_agent.agent.prompts import load_system_prompt
from incident_agent.agent.report import TerminatedReason, Verdict

pytestmark = pytest.mark.anyio

ALERT = "checkout p99 latency is high"


async def run(model, tools=None, tmp_path=None, **config):
    return await investigate(
        ALERT,
        tools=tools or FakeExecutor(),
        model=model,
        config=AgentConfig(**config),
        log_path=tmp_path / "run.jsonl",
        run_id="r1",
        scenario_id="s1",
    )


def query(call_id: str, name: str = "describe_dataset"):
    return tool_call_response((name, {"q": call_id}, call_id))


def submit(call_id: str = "sub", **overrides):
    return tool_call_response(("submit_report", report_args(**overrides), call_id))


async def test_happy_path_submits_report(tmp_path):
    model = ScriptedModelClient(
        [
            tool_call_response(
                ("describe_dataset", {}, "t1"),
                text="Orienting.",
                usage=Usage(input_tokens=100, cache_read_tokens=50, output_tokens=10),
                cost_usd=0.01,
            ),
            tool_call_response(
                ("submit_report", report_args(), "t2"),
                usage=Usage(input_tokens=200, cache_write_tokens=20, output_tokens=30),
                cost_usd=0.02,
            ),
        ]
    )
    tools = FakeExecutor()
    result = await run(model, tools, tmp_path)

    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert result.report.verdict == Verdict.ROOT_CAUSE_FOUND
    assert result.report.causes[0].service == "payment"
    assert result.error is None
    assert (result.steps, result.tool_calls) == (2, 2)
    assert (result.input_tokens, result.output_tokens) == (370, 40)  # cache tokens included
    assert result.cost_usd == pytest.approx(0.03)
    assert result.log_path == tmp_path / "run.jsonl"
    assert tools.calls == [("describe_dataset", {})]  # submit_report is harness-local

    first = model.requests[0]
    assert first.system == load_system_prompt()
    assert ALERT in first.text()
    assert first.max_tokens == AgentConfig().max_tokens
    assert first.tool_names == ["describe_dataset", "submit_report"]
    model.assert_exhausted()


async def test_assistant_message_is_echoed_with_provider_data(tmp_path):
    reasoning = ModelResponse(
        text="hmm",
        tool_calls=[ToolUseBlock(id="t1", name="describe_dataset", input={})],
        stop_reason="tool_use",
        provider_data={"fake": {"thinking_blocks": ["opaque"]}},
    )
    model = ScriptedModelClient([reasoning, submit()])
    await run(model, tmp_path=tmp_path)
    echoed = model.requests[1].messages[1]
    assert echoed.role == "assistant"
    assert echoed.provider_data == {"fake": {"thinking_blocks": ["opaque"]}}


async def test_parallel_tool_calls_run_concurrently_and_results_keep_order(tmp_path):
    started: list[str] = []
    both_started = anyio.Event()

    async def slow(_args):
        started.append("slow")
        await both_started.wait()
        return "slow result"

    async def fast(_args):
        started.append("fast")
        both_started.set()
        return "fast result"

    tools = FakeExecutor({"slow_tool": slow, "fast_tool": fast})
    model = ScriptedModelClient(
        [
            tool_call_response(("slow_tool", {"a": 1}, "ta"), ("fast_tool", {"b": 2}, "tb")),
            submit(),
        ]
    )
    with anyio.fail_after(5):  # would deadlock if calls ran one after the other
        result = await run(model, tools, tmp_path)

    assert result.terminated_reason == TerminatedReason.SUBMITTED
    results = model.requests[1].tool_results
    assert [(r.tool_use_id, r.content) for r in results] == [
        ("ta", "[ref c1]\nslow result"),
        ("tb", "[ref c2]\nfast result"),
    ]
    assert sorted(started) == ["fast", "slow"]
    logged = [
        e["tool_call_id"] for e in read_log(tmp_path / "run.jsonl") if e["type"] == "tool_result"
    ]
    assert logged[:2] == ["ta", "tb"]


async def test_invalid_submit_report_is_rejected_then_resubmission_succeeds(tmp_path):
    bad = {"verdict": "root_cause_found", "causes": [{"service": "payment"}], "confidence": "sure"}
    model = ScriptedModelClient(
        [
            tool_call_response(("submit_report", bad, "bad")),
            tool_call_response(("submit_report", report_args(), "good")),
        ]
    )
    result = await run(model, tmp_path=tmp_path)

    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert result.steps == 2
    rejection = model.requests[1].tool_results[0]
    assert rejection.is_error
    assert "causes.0.category" in rejection.content
    assert "confidence" in rejection.content
    assert "submit_report again" in rejection.content


async def test_ending_turn_without_submitting_is_nudged_once(tmp_path):
    model = ScriptedModelClient(["I think it's payment.", submit()])
    result = await run(model, tmp_path=tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    nudge = model.requests[1].last_message
    assert nudge.role == "user"
    assert "submit_report" in model.requests[1].text()


async def test_second_end_turn_without_submitting_stops_with_inconclusive_report(tmp_path):
    model = ScriptedModelClient(["done", "really done"])
    result = await run(model, tmp_path=tmp_path)
    assert result.terminated_reason == TerminatedReason.ERROR
    assert result.steps == 2
    assert result.report.verdict == Verdict.INCONCLUSIVE
    assert "without calling submit_report" in result.error
    assert any("without calling submit_report" in u for u in result.report.uncertainties)


async def test_step_limit_gives_inconclusive_report(tmp_path):
    model = ScriptedModelClient([query(f"t{i}") for i in range(10)])
    result = await run(model, tmp_path=tmp_path, max_steps=3)
    assert model.calls == 3
    assert result.terminated_reason == TerminatedReason.MAX_STEPS
    assert (result.steps, result.tool_calls) == (3, 3)
    assert result.report.verdict == Verdict.INCONCLUSIVE
    assert result.report.confidence == "low"
    assert any("Step limit reached (3" in u for u in result.report.uncertainties)
    events = read_log(tmp_path / "run.jsonl")
    assert [e["type"] for e in events][-2:] == ["final", "run_end"]
    assert events[-2]["terminated_reason"] == "max_steps"


async def test_input_token_budget_stops_the_run(tmp_path):
    usage = Usage(input_tokens=600, output_tokens=5)
    model = ScriptedModelClient(
        [tool_call_response(("describe_dataset", {}, f"t{i}"), usage=usage) for i in range(5)]
    )
    result = await run(model, tmp_path=tmp_path, max_input_tokens=1000)
    assert model.calls == 2  # 600, then 1200 >= 1000
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert result.report.verdict == Verdict.INCONCLUSIVE
    assert "Input token limit" in result.error


async def test_cost_budget_stops_the_run(tmp_path):
    model = ScriptedModelClient(
        [tool_call_response(("describe_dataset", {}, f"t{i}"), cost_usd=0.3) for i in range(5)]
    )
    result = await run(model, tmp_path=tmp_path, max_cost_usd=0.5)
    assert model.calls == 2
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert result.cost_usd == pytest.approx(0.6)
    assert "Cost limit" in result.error


async def test_a_report_submitted_on_the_last_allowed_step_still_counts(tmp_path):
    model = ScriptedModelClient([submit()])
    result = await run(model, tmp_path=tmp_path, max_steps=1)
    assert result.terminated_reason == TerminatedReason.SUBMITTED


async def test_budget_exceeded_from_the_model_is_a_budget_stop(tmp_path):
    model = ScriptedModelClient([query("t1"), BudgetExceededError("Spend cap reached: $10")])
    result = await run(model, tmp_path=tmp_path)
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert result.steps == 1
    assert result.report.verdict == Verdict.INCONCLUSIVE
    assert "Spend cap reached" in result.error


async def test_other_exceptions_never_crash_the_run(tmp_path):
    model = ScriptedModelClient([query("t1"), ModelAPIError("503 upstream", status_code=503)])
    result = await run(model, tmp_path=tmp_path)
    assert result.terminated_reason == TerminatedReason.ERROR
    assert "ModelAPIError: 503 upstream" in result.error
    events = read_log(tmp_path / "run.jsonl")
    assert events[-2]["type"] == "final" and events[-1]["type"] == "run_end"


async def test_wall_clock_timeout_is_a_budget_stop(tmp_path):
    async def hang(_args):
        await anyio.sleep(30)
        return "never"

    model = ScriptedModelClient([tool_call_response(("hang", {}, "t1"))])
    result = await run(model, FakeExecutor({"hang": hang}), tmp_path, timeout_seconds=0.1)
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert "Time limit" in result.error
    assert result.report.verdict == Verdict.INCONCLUSIVE


async def test_tool_errors_are_passed_back_to_the_model(tmp_path):
    def explode(_args):
        raise RuntimeError("backend down")

    tools = FakeExecutor(
        {"run_query": ("field `usr_id` not found; did you mean `user_id`?", True), "boom": explode}
    )
    model = ScriptedModelClient(
        [tool_call_response(("run_query", {}, "t1"), ("boom", {}, "t2")), submit()]
    )
    result = await run(model, tools, tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    first, second = model.requests[1].tool_results
    assert first.is_error and "did you mean `user_id`" in first.content
    assert second.is_error and "RuntimeError: backend down" in second.content


async def test_malformed_arguments_and_unknown_tools_get_error_results(tmp_path):
    tools = FakeExecutor()
    model = ScriptedModelClient(
        [
            tool_call_response(
                ToolUseBlock(id="m1", name="describe_dataset", parse_error="Expecting value"),
                ("nope", {"x": 1}, "u1"),
            ),
            submit(),
        ]
    )
    result = await run(model, tools, tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert tools.calls == []  # neither reached the executor
    bad_args, unknown = model.requests[1].tool_results
    assert (bad_args.tool_use_id, bad_args.is_error) == ("m1", True)
    assert "Expecting value" in bad_args.content
    assert (unknown.tool_use_id, unknown.is_error) == ("u1", True)
    assert "Unknown tool `nope`" in unknown.content and "describe_dataset" in unknown.content


async def test_max_tokens_cutoff_is_nudged_then_recovers(tmp_path):
    cut = text_response("The analysis so far shows", stop_reason="max_tokens")
    model = ScriptedModelClient([cut, submit()])
    result = await run(model, tmp_path=tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
    assert "cut off" in model.requests[1].text()


async def test_repeated_max_tokens_cutoffs_stop_the_run(tmp_path):
    cut = text_response("blah", stop_reason="max_tokens")
    model = ScriptedModelClient([cut, cut, cut, cut])
    result = await run(model, tmp_path=tmp_path)
    assert model.calls == 3
    assert result.terminated_reason == TerminatedReason.ERROR
    assert "cut off" in result.error
    assert result.report.verdict == Verdict.INCONCLUSIVE


async def test_empty_model_response_does_not_leave_an_empty_assistant_turn(tmp_path):
    model = ScriptedModelClient([ModelResponse(text="", stop_reason="end_turn"), submit()])
    await run(model, tmp_path=tmp_path)
    assistant = model.requests[1].messages[1]
    assert assistant.role == "assistant" and assistant.content


async def test_system_prompt_is_stable_across_runs(tmp_path):
    a = ScriptedModelClient([submit()])
    b = ScriptedModelClient([submit()])
    await run(a, tmp_path=tmp_path)
    await run(b, tmp_path=tmp_path)
    assert a.requests[0].system == b.requests[0].system
    assert ALERT not in a.requests[0].system


async def test_every_result_starts_with_a_sequential_ref_in_call_order(tmp_path):
    model = ScriptedModelClient(
        [
            tool_call_response(
                ("describe_dataset", {}, "toolu_a"), ("missing_tool", {}, "toolu_b")
            ),
            tool_call_response(("describe_dataset", {}, "toolu_c")),
            submit(),
        ]
    )
    result = await run(model, FakeExecutor(), tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    seen = [
        (r.tool_use_id, r.content.splitlines()[0])
        for request in model.requests[1:]
        for r in request.tool_results
    ]
    assert seen[:3] == [("toolu_a", "[ref c1]"), ("toolu_b", "[ref c2]"), ("toolu_c", "[ref c3]")]
    events = [e for e in read_log(tmp_path / "run.jsonl") if e["type"] == "tool_result"]
    assert [(e["tool_call_id"], e["ref"]) for e in events] == [
        ("toolu_a", "c1"),
        ("toolu_b", "c2"),
        ("toolu_c", "c3"),
        ("sub", "c4"),
    ]
    assert events[0]["content"] == "[ref c1]\nservices: checkout, payment"
    assert events[1]["is_error"] and events[1]["content"].startswith("[ref c2]\nUnknown tool")


def _sent_schema() -> dict:
    """The submit_report input schema as the model receives it."""
    from incident_agent.agent.tools import SubmitReportTool

    return SubmitReportTool().spec.input_schema


def _evidence_props() -> dict:
    return _sent_schema()["properties"]["evidence"]["items"]["properties"]


def test_evidence_schema_tells_the_model_to_cite_the_ref():
    text = _evidence_props()["tool_call_id"]["description"]
    assert "[ref cN]" in text and "c3" in text


def test_category_definitions_are_in_the_submit_report_schema():
    from incident_agent.agent.report import CATEGORY_DEFINITIONS, CauseCategory

    assert set(CATEGORY_DEFINITIONS) == set(CauseCategory)
    cause = _sent_schema()["properties"]["causes"]["items"]["properties"]
    text = cause["category"]["description"]
    for category, definition in CATEGORY_DEFINITIONS.items():
        assert f"- {category.value}: {definition}" in text


def test_cited_values_guidance_asks_for_one_verbatim_value_each():
    text = _evidence_props()["cited_values"]["description"]
    assert "ONE value" in text and "Good:" in text and "Bad:" in text
    assert "single value" in load_system_prompt()


async def test_loop_feeds_results_to_the_citation_check_and_the_model_can_fix_its_report(tmp_path):
    bad = [{"claim": "services", "tool_call_id": "c1", "cited_values": ["payment", "checkout 30"]}]
    good = [{"claim": "services", "tool_call_id": "c1", "cited_values": ["payment"]}]
    model = ScriptedModelClient(
        [
            tool_call_response(("describe_dataset", {}, "t1")),
            tool_call_response(("submit_report", report_args(evidence=bad), "s1")),
            tool_call_response(("submit_report", report_args(evidence=good), "s2")),
        ]
    )
    result = await run(model, FakeExecutor(), tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED and result.steps == 3
    rejection = model.requests[2].tool_results[0]
    assert rejection.is_error and "'checkout 30' does not appear in the result of `c1`" in (
        rejection.content
    )
    checks = [e for e in read_log(tmp_path / "run.jsonl") if e["type"] == "citation_check"]
    assert [(c["attempt"], c["ok"]) for c in checks] == [(1, False), (2, True)]


async def test_citation_check_can_be_switched_off(tmp_path):
    bad = [{"claim": "services", "tool_call_id": "c7", "cited_values": ["nope"]}]
    model = ScriptedModelClient([tool_call_response(("submit_report", report_args(evidence=bad)))])
    result = await run(model, FakeExecutor(), tmp_path, verify_citations=False)
    assert result.terminated_reason == TerminatedReason.SUBMITTED
