"""Compaction next to memory: the summary call must not disturb memory's prompt or ref ledger."""

from __future__ import annotations

import pytest
from agent_fakes import read_log, report_args
from context_helpers import run
from test_context_compaction import compact_config, compacting_script, tools

from incident_agent.agent.context import COMPACT_SYSTEM
from incident_agent.agent.memory import MemoryStore, enable_memory
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.report import TerminatedReason

pytestmark = pytest.mark.anyio


async def test_compaction_leaves_memory_refs_and_prompt_alone(tmp_path):
    note = {
        "content": "product-catalog p95 was about 25 seconds during the incident",
        "scope": "service",
        "scope_key": "product-catalog",
        "reason": "baseline for later comparisons",
        "source": "c2",  # a pre-compaction ref
    }
    script = compacting_script(then=[tool_call_response(("save_memory", note, "m1"))])
    script.append(tool_call_response(("submit_report", report_args(), "sub")))
    model = ScriptedModelClient(script)
    store = MemoryStore(tmp_path / "mem")
    memory_tools, wrapped = enable_memory(
        tools(), model, store, run_id="r1", investigation_id="inv-1"
    )
    result = await run(wrapped, memory_tools, tmp_path, context=compact_config())
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    assert model.requests[3].system == COMPACT_SYSTEM  # no memory prompt in the summary call
    saved = next(r for r in read_log(result.log_path) if r.get("tool_call_id") == "m1")
    assert not saved["is_error"] and "Saved note" in saved["content"]
    assert [n.source for n in store.all_notes()] == ["r1/t2"]
