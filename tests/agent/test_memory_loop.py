"""Memory wired into the loop with a scripted model: opt-in only, byte-identical when off."""

from __future__ import annotations

import pytest
from agent_fakes import FakeExecutor, read_log, report_args

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.memory import MemoryStore, Scope, enable_memory
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.prompts import load_memory_prompt, load_system_prompt
from incident_agent.agent.report import TerminatedReason

pytestmark = pytest.mark.anyio

SAVE = {
    "content": "checkout normally has an error rate of about 0.3%",
    "scope": "service",
    "scope_key": "checkout",
    "reason": "baseline for future false-alarm checks",
}


def submit(call_id="sub"):
    return tool_call_response(("submit_report", report_args(), call_id))


async def run(model, tools, tmp_path):
    return await investigate(
        "checkout latency is high",
        tools=tools,
        model=model,
        config=AgentConfig(max_steps=10),
        log_path=tmp_path / "run.jsonl",
        run_id="r1",
        scenario_id="inv-1",
    )


async def test_memory_off_changes_nothing(tmp_path):
    model = ScriptedModelClient([tool_call_response(("describe_dataset", {}, "t1")), submit()])
    result = await run(model, FakeExecutor(), tmp_path)

    assert result.terminated_reason == TerminatedReason.SUBMITTED
    for request in model.requests:
        assert request.system == load_system_prompt()  # exactly the memory-free prompt
        assert request.tool_names == ["describe_dataset", "submit_report"]
    assert "memory" not in load_system_prompt().lower()


async def test_memory_on_adds_tools_and_prompt_section(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    model = ScriptedModelClient([submit()])
    tools, wrapped = enable_memory(
        FakeExecutor(), model, store, run_id="r1", investigation_id="inv-1"
    )
    await run(wrapped, tools, tmp_path)

    (request,) = model.requests
    assert request.system == load_system_prompt() + "\n\n" + load_memory_prompt()
    assert request.system.startswith(load_system_prompt())  # the cached prefix is unchanged
    assert request.tool_names == ["describe_dataset", "recall", "save_memory", "submit_report"]
    assert "hints" in load_memory_prompt() and "verify" in load_memory_prompt().lower()


async def test_memory_round_trip_in_loop_with_logged_results(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    model = ScriptedModelClient(
        [
            tool_call_response(("describe_dataset", {}, "t1")),
            tool_call_response(("save_memory", {**SAVE, "source": "t1"}, "m1")),
            tool_call_response(("recall", {"scope": "service", "scope_key": "checkout"}, "m2")),
            submit(),
        ]
    )
    tools, wrapped = enable_memory(
        FakeExecutor(), model, store, run_id="r1", investigation_id="inv-1"
    )
    result = await run(wrapped, tools, tmp_path)
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    (note,) = store.query(Scope.SERVICE, "checkout").notes
    assert note.source == "r1/t1" and note.written_by == "r1"

    events = {e["tool_call_id"]: e for e in read_log(tmp_path / "run.jsonl") if "tool_call_id" in e}
    assert not events["m1"]["is_error"] and "Saved note" in events["m1"]["content"]
    assert note.content in events["m2"]["content"]  # memory ops show up as ordinary tool_results
    assert "source: r1/t1" in events["m2"]["content"]


async def test_source_ref_in_loop_matches_the_ref_the_model_saw(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    model = ScriptedModelClient(
        [
            tool_call_response(("describe_dataset", {}, "toolu_01")),
            tool_call_response(("save_memory", {**SAVE, "source": "c1"}, "m1")),
            submit(),
        ]
    )
    tools, wrapped = enable_memory(
        FakeExecutor(), model, store, run_id="r1", investigation_id="inv-1"
    )
    await run(wrapped, tools, tmp_path)

    first = model.requests[1].tool_results[0]
    assert first.content.startswith("[ref c1]\n")  # what the model was told to cite
    (note,) = store.all_notes()
    assert note.source == "r1/toolu_01"


async def test_fabricated_and_same_turn_sources_are_rejected_in_loop(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    model = ScriptedModelClient(
        [
            # t1 and the save are in one batch: the model cannot have seen t1's result yet
            tool_call_response(
                ("describe_dataset", {}, "t1"), ("save_memory", {**SAVE, "source": "t1"}, "m0")
            ),
            tool_call_response(("save_memory", {**SAVE, "source": "toolu_invented"}, "m1")),
            tool_call_response(("save_memory", {**SAVE, "source": "t1"}, "m2")),  # now valid
            submit(),
        ]
    )
    tools, wrapped = enable_memory(
        FakeExecutor(), model, store, run_id="r1", investigation_id="inv-1"
    )
    await run(wrapped, tools, tmp_path)

    results = {
        e["tool_call_id"]: e for e in read_log(tmp_path / "run.jsonl") if e["type"] == "tool_result"
    }
    assert results["m0"]["is_error"] and results["m1"]["is_error"]
    assert "Recent refs" in results["m1"]["content"]
    assert not results["m2"]["is_error"]
    assert len(store.all_notes()) == 1


async def test_loop_log_still_records_provider_and_model(tmp_path):
    model = ScriptedModelClient([submit()])
    tools, wrapped = enable_memory(
        FakeExecutor(), model, MemoryStore(tmp_path / "mem"), run_id="r1", investigation_id="i"
    )
    await run(wrapped, tools, tmp_path)
    start = read_log(tmp_path / "run.jsonl")[0]
    assert (start["provider"], start["model_id"]) == ("fake", "fake-model")
