"""Context management: result cap, window exhaustion, clearing, and the unmanaged no-op."""

from __future__ import annotations

import pytest
from agent_fakes import FakeExecutor, read_log, report_args
from context_helpers import ALERT, assert_valid_history, call, pad, run, text_of
from pydantic import ValidationError
from typer.testing import CliRunner

from incident_agent.agent.context import (
    ContextConfig,
    cap_tool_result,
    clear_old_results,
    clear_stub,
    loggable,
)
from incident_agent.agent.loop import AgentConfig
from incident_agent.agent.models.base import Message, ToolResultBlock, ToolUseBlock, Usage
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.report import TerminatedReason, Verdict
from incident_agent.agent.tracing import approx_tokens
from incident_agent.cli import app

pytestmark = pytest.mark.anyio

STUB_TAIL = "(cleared to save context; full result still citable by ref)"


def submit(input_tokens: int = 100, ref: str = "c1", value: str = "30%"):
    evidence = [{"claim": "x", "tool_call_id": ref, "cited_values": [value]}]
    return tool_call_response(
        ("submit_report", report_args(evidence=evidence), "sub"),
        usage=Usage(input_tokens=input_tokens, output_tokens=20),
    )


def events(path, kind: str | None = None):
    found = [r for r in read_log(path) if r["type"] == "context_event"]
    return [r for r in found if kind in (None, r["kind"])]


# --- the cap -----------------------------------------------------------------------------


def test_cap_leaves_small_results_alone_and_disabled_cap_alone():
    assert cap_tool_result("small", 100) == ("small", False)
    big = "x" * 100_000
    assert cap_tool_result(big, None) == (big, False)


def test_cap_cuts_to_the_limit_and_says_what_was_cut_and_how_to_narrow():
    text = "\n".join(f"line {i} " + "y" * 40 for i in range(2000))
    capped, truncated = cap_tool_result(text, 500)
    assert truncated
    assert approx_tokens(capped) <= 500
    assert capped.startswith("line 0 ")
    assert f"of {len(text)} characters" in capped and "The rest was cut" in capped
    assert "Narrow the request" in capped
    head = capped.split("\n\n[Result truncated")[0]
    assert text.startswith(head) and head.splitlines()[-1].endswith("y" * 40)  # whole lines


async def test_oversized_result_is_truncated_in_history_and_logged(tmp_path):
    big = pad("run_query: huge", 6000)
    model = ScriptedModelClient([call("run_query", "t1", 500), submit()])
    tools = FakeExecutor({"run_query": big})
    result = await run(model, tools, tmp_path, context=ContextConfig(max_tool_result_tokens=1000))
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    shown = model.requests[1].tool_results[0].content
    assert shown.startswith("[ref c1]\nrun_query: huge")
    assert approx_tokens(shown) <= 1010 and "truncated by the harness" in shown
    log = read_log(result.log_path)
    tr = next(r for r in log if r["type"] == "tool_result")
    assert tr["truncated_by_harness"] is True and tr["content"] == shown  # what the model saw
    (event,) = events(result.log_path, "truncate")
    d = event["details"]
    assert (d["ref"], d["name"], d["limit_tokens"]) == ("c1", "run_query", 1000)
    assert d["original_tokens"] > 5000 > d["kept_tokens"]


async def test_cap_is_on_by_default_and_normal_results_are_untouched(tmp_path):
    assert AgentConfig().context.max_tool_result_tokens == 4000
    model = ScriptedModelClient([call("run_query", "t1", 500), submit()])
    result = await run(model, FakeExecutor({"run_query": pad("ok", 1500)}), tmp_path)
    assert not events(result.log_path)
    tr = next(r for r in read_log(result.log_path) if r["type"] == "tool_result")
    assert tr["truncated_by_harness"] is False


# --- config ------------------------------------------------------------------------------


def test_clear_and_compact_need_a_window():
    for mode in ("clear", "compact"):
        with pytest.raises(ValidationError, match="needs max_context_tokens"):
            ContextConfig(mode=mode)
    assert ContextConfig(mode="clear", max_context_tokens=9000).mode == "clear"


def test_run_start_config_is_flat_and_shows_the_context_settings():
    flat = AgentConfig(context=ContextConfig(mode="compact", max_context_tokens=9000)).log_dict()
    assert flat["context_mode"] == "compact" and flat["max_context_tokens"] == 9000
    assert flat["compact_threshold"] == 0.75 and "context" not in flat
    assert loggable(ContextConfig()) == {
        "max_tool_result_tokens": 4000,
        "max_context_tokens": None,
        "context_mode": "none",
    }


# --- window exhaustion without management ------------------------------------------------


async def test_window_exhaustion_without_management_ends_inconclusive(tmp_path):
    model = ScriptedModelClient([call("run_query", "t1", 2400), call("run_query", "t2", 2800)])
    tools = FakeExecutor({"run_query": pad("q", 300)})
    result = await run(model, tools, tmp_path, context=ContextConfig(max_context_tokens=3000))
    assert model.calls == 2  # the third request would not have fit, so it was never sent
    assert result.terminated_reason == TerminatedReason.BUDGET
    assert result.report.verdict == Verdict.INCONCLUSIVE
    assert "Context window exhausted" in (result.error or "")
    assert "max_context_tokens is 3000" in result.error
    assert any("Context window exhausted" in u for u in result.report.uncertainties)
    assert read_log(result.log_path)[-1]["type"] == "run_end"


async def test_a_run_that_fits_the_window_is_not_stopped(tmp_path):
    model = ScriptedModelClient([call("run_query", "t1", 500), submit(600)])
    result = await run(
        model,
        FakeExecutor({"run_query": pad("q error_rate 30%", 100)}),  # cited value must be present
        tmp_path,
        context=ContextConfig(max_context_tokens=5000),
    )
    assert result.terminated_reason == TerminatedReason.SUBMITTED


async def test_first_request_is_estimated_from_prefix_and_alert(tmp_path):
    model = ScriptedModelClient([submit()])
    result = await run(
        model, FakeExecutor(), tmp_path, context=ContextConfig(max_context_tokens=50)
    )
    assert model.calls == 0 and result.terminated_reason == TerminatedReason.BUDGET


# --- clearing ----------------------------------------------------------------------------


def clear_config(window: int = 3000, **kw):
    return ContextConfig(mode="clear", max_context_tokens=window, **kw)


def three_step_script():
    return [
        call("describe_dataset", "t1", 1000),
        call("run_query", "t2", 1500),
        call("get_trace", "t3", 1800),
        submit(2000, ref="c2", value="p95=25043"),
    ]


def three_step_tools():
    return FakeExecutor(
        {
            "describe_dataset": pad("Dataset: 16393 events | 15 services", 400),
            "run_query": pad("run_query: p95(duration_ms) by service.name", 400) + "\np95=25043",
            "get_trace": pad("get_trace 6de69b56: 10 spans, 2 error spans", 400),
        }
    )


async def test_clear_stubs_old_results_keeps_refs_headers_and_recent_turns(tmp_path):
    model = ScriptedModelClient(three_step_script())
    result = await run(model, three_step_tools(), tmp_path, context=clear_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    # Before call 4 the estimate (1800 + ~450) passes 60% of 3000: result c1 is cleared.
    history = model.requests[3].messages
    results = [b for m in history for b in m.content if isinstance(b, ToolResultBlock)]
    stub, kept_b, kept_c = (r.content for r in results)
    assert stub == (f"[ref c1] describe_dataset: Dataset: 16393 events | 15 services {STUB_TAIL}")
    assert kept_b.startswith("[ref c2]\nrun_query: p95") and "row 0000" in kept_b
    assert kept_c.startswith("[ref c3]\nget_trace 6de69b56") and "row 0000" in kept_c
    assert [r.tool_use_id for r in results] == ["t1", "t2", "t3"]  # nothing dropped
    for request in model.requests:
        assert_valid_history(request.messages)

    # Earlier requests were sent before anything was cleared.
    assert all("cleared" not in str(r.messages) for r in model.requests[:3])
    # The step log still holds the full original result, and the clear is logged.
    logged = {r["ref"]: r for r in read_log(result.log_path) if r["type"] == "tool_result"}
    assert "row 0000" in logged["c1"]["content"] and "cleared" not in logged["c1"]["content"]
    (event,) = events(result.log_path, "clear")
    d = event["details"]
    assert d["cleared_results"] == 1 and d["refs"] == ["c1"] and d["tokens_freed"] > 300
    assert d["estimate_after"] == d["estimate_before"] - d["tokens_freed"]
    assert d["window"] == 3000


async def test_clear_keeps_a_run_inside_a_window_that_stops_the_unmanaged_run(tmp_path):
    def script(reported_after_clear: int):
        # A real provider reports less input once old results are cleared.
        return [
            call("describe_dataset", "t1", 1000),
            call("run_query", "t2", 1500),
            call("get_trace", "t3", 1800),
            call("run_query", "t4", reported_after_clear),
            submit(reported_after_clear + 200, ref="c2", value="p95=25043"),
        ]

    unmanaged = await run(
        ScriptedModelClient(script(2800)),
        three_step_tools(),
        tmp_path / "a",
        context=ContextConfig(max_context_tokens=3000),
    )
    managed = await run(
        ScriptedModelClient(script(1700)),
        three_step_tools(),
        tmp_path / "b",
        context=clear_config(),
    )
    assert unmanaged.terminated_reason == TerminatedReason.BUDGET
    assert managed.terminated_reason == TerminatedReason.SUBMITTED


async def test_clear_does_nothing_below_the_threshold(tmp_path):
    model = ScriptedModelClient(three_step_script())
    result = await run(model, three_step_tools(), tmp_path, context=clear_config(window=100_000))
    assert not events(result.log_path)
    assert all("cleared" not in str(r.messages) for r in model.requests)


def test_clear_never_touches_submit_report_or_memory_results_or_the_current_turn():
    ok = ToolResultBlock(tool_use_id="s", content="[ref c1]\nInvalid report: fix `causes`.")
    big = ToolResultBlock(tool_use_id="a", content="[ref c2]\n" + pad("header", 300))
    mem = ToolResultBlock(tool_use_id="m", content="[ref c3]\n" + pad("note", 300))
    recent = ToolResultBlock(tool_use_id="r", content="[ref c4]\n" + pad("recent", 300))
    messages = [
        Message.user(ALERT),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="s", name="submit_report"),
                ToolUseBlock(id="a", name="run_query"),
                ToolUseBlock(id="m", name="recall"),
            ],
        ),
        Message.tool_results([ok, big, mem]),
        Message(role="assistant", content=[ToolUseBlock(id="r", name="run_query")]),
        Message.tool_results([recent]),
    ]
    refs, freed = clear_old_results(messages, keep_recent=1)
    assert refs == ["c2"] and freed > 200
    assert ok.content.endswith("fix `causes`.") and mem.content.startswith("[ref c3]\nnote")
    assert recent.content.startswith("[ref c4]\nrecent") and "cleared" not in recent.content
    assert big.content.startswith("[ref c2] run_query: header (cleared")
    assert clear_old_results(messages, keep_recent=1) == ([], 0)  # idempotent


def test_stub_is_not_used_when_it_would_not_save_anything():
    tiny = ToolResultBlock(tool_use_id="a", content="[ref c1]\nok")
    messages = [
        Message(role="assistant", content=[ToolUseBlock(id="a", name="run_query")]),
        Message.tool_results([tiny]),
        Message(role="assistant", content=[ToolUseBlock(id="b", name="run_query")]),
        Message.tool_results([ToolResultBlock(tool_use_id="b", content="[ref c2]\nok")]),
    ]
    assert clear_old_results(messages, keep_recent=1) == ([], 0)
    assert tiny.content == "[ref c1]\nok"


def test_stub_shortens_a_long_header_and_keeps_the_error_flag():
    stub = clear_stub("c9", "run_query", "[ref c9]\n" + "h" * 500 + "\nmore")
    assert stub.startswith("[ref c9] run_query: " + "h" * 100) and "..." in stub
    assert "\n" not in stub and stub.endswith(STUB_TAIL)
    err = ToolResultBlock(
        tool_use_id="a", content="[ref c1]\n" + pad("Unknown field", 300), is_error=True
    )
    messages = [
        Message(role="assistant", content=[ToolUseBlock(id="a", name="run_query")]),
        Message.tool_results([err]),
        Message(role="assistant", content=[ToolUseBlock(id="b", name="run_query")]),
        Message.tool_results([ToolResultBlock(tool_use_id="b", content="x")]),
    ]
    clear_old_results(messages, keep_recent=1)
    assert err.is_error and err.content.startswith("[ref c1] run_query: Unknown field")


async def test_clear_leaves_assistant_turns_and_their_reasoning_state_alone(tmp_path):
    script = three_step_script()
    for item in script[:3]:
        item.provider_data = {"fake": {"thinking_blocks": [f"opaque-{item.tool_calls[0].id}"]}}
    model = ScriptedModelClient(script)
    await run(model, three_step_tools(), tmp_path, context=clear_config())
    assistants = [m for m in model.requests[3].messages if m.role == "assistant"]
    assert [m.provider_data["fake"]["thinking_blocks"] for m in assistants] == [
        ["opaque-t1"],
        ["opaque-t2"],
        ["opaque-t3"],
    ]


async def test_system_prompt_and_tools_are_identical_whatever_the_mode(tmp_path):
    seen = []
    for mode, window in (("none", None), ("clear", 3000)):
        model = ScriptedModelClient(three_step_script())
        await run(
            model,
            three_step_tools(),
            tmp_path / mode,
            context=ContextConfig(mode=mode, max_context_tokens=window),
        )
        seen.append({(r.system, str(r.tools)) for r in model.requests})
    assert seen[0] == seen[1] and len(seen[0]) == 1


# --- no-op -------------------------------------------------------------------------------


async def test_no_management_leaves_history_and_log_as_an_unmanaged_loop_would(tmp_path):
    def script():
        return [call("describe_dataset", "t1", 500), call("run_query", "t2", 900), submit(1200)]

    def tools():
        return FakeExecutor(
            {"describe_dataset": pad("Dataset", 300), "run_query": pad("run_query: x", 300)}
        )

    default = ScriptedModelClient(script())
    a = await run(default, tools(), tmp_path / "a")
    capless = ScriptedModelClient(script())
    await run(capless, tools(), tmp_path / "b", context=ContextConfig(max_tool_result_tokens=None))

    assert [r.messages for r in default.requests] == [r.messages for r in capless.requests]
    final = default.requests[-1].messages
    assert [m.role for m in final] == ["user", "assistant", "user", "assistant", "user"]
    assert text_of(final[0]) == f"Alert:\n\n{ALERT}"
    assert final[2].content[0].content == "[ref c1]\n" + pad("Dataset", 300)
    assert final[4].content[0].content == "[ref c2]\n" + pad("run_query: x", 300)
    assert default.requests[0].tools == default.requests[-1].tools
    log = read_log(a.log_path)
    assert "context_event" not in [r["type"] for r in log]
    assert all("purpose" not in r for r in log if r["type"] == "model_call")


# --- CLI ---------------------------------------------------------------------------------


def test_cli_rejects_a_mode_without_a_window():
    result = CliRunner().invoke(
        app, ["investigate", "--scenario", "healthy-baseline", "--context-mode", "clear"]
    )
    assert result.exit_code == 2 and "needs max_context_tokens" in result.output


def test_cli_rejects_an_unknown_mode():
    result = CliRunner().invoke(
        app, ["investigate", "--scenario", "healthy-baseline", "--context-mode", "shrink"]
    )
    assert result.exit_code == 2


def test_cli_passes_context_flags_into_the_agent_config_and_eval_config(monkeypatch, tmp_path):
    import incident_agent.agent.investigator as investigator_module
    import incident_agent.agent.models as models_module
    import incident_agent.evals.runner as runner_module

    seen: dict = {}

    class FakeClient:
        class inner:  # noqa: N801
            model = "fake"

    def fake_make(config, **kwargs):
        seen["agent"] = config

    async def fake_run_eval(specs, investigator, **kwargs):
        seen["config"] = kwargs["config"]
        raise SystemExit(0)

    monkeypatch.setattr(investigator_module, "make_investigator", fake_make)
    monkeypatch.setattr(runner_module, "run_eval", fake_run_eval)
    monkeypatch.setattr(models_module, "get_model_client", lambda *a, **k: FakeClient())
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "-s",
            "healthy-baseline",
            "--context-mode",
            "compact",
            "--max-context-tokens",
            "12000",
            "--no-report",
            "--runs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    context = seen["agent"].context
    assert (context.mode, context.max_context_tokens) == ("compact", 12000)
    assert seen["config"]["context_mode"] == "compact"
    assert seen["config"]["max_context_tokens"] == 12000
