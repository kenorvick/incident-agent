"""Step-log contract: event types, fields and flushing."""

from __future__ import annotations

import pytest
from agent_fakes import FakeExecutor, read_log, report_args

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.models.base import Usage
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.tracing import StepLogger, approx_tokens
from incident_agent.evals.logs import parse_log

pytestmark = pytest.mark.anyio


async def test_log_has_every_event_with_the_contract_fields(tmp_path):
    log_path = tmp_path / "nested" / "run.jsonl"
    model = ScriptedModelClient(
        [
            tool_call_response(
                ("describe_dataset", {"a": 1}, "t1"),
                text="Looking.",
                usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3),
                cost_usd=0.5,
            ),
            tool_call_response(("submit_report", report_args(), "t2")),
        ]
    )
    result = await investigate(
        "alert text",
        tools=FakeExecutor({"describe_dataset": "x" * 41}),
        model=model,
        config=AgentConfig(max_steps=7),
        log_path=log_path,
        run_id="run-9",
        scenario_id="sc",
    )
    events = read_log(log_path)
    assert [e["type"] for e in events] == [
        "run_start",
        "model_call",
        "tool_result",
        "model_call",
        "tool_result",
        "final",
        "run_end",
    ]
    assert all("ts" in e for e in events)

    start, call, tool, *_ = events
    assert start["run_id"] == "run-9" and start["scenario_id"] == "sc"
    assert start["alert"] == "alert text"
    assert (start["provider"], start["model_id"]) == ("fake", "fake-model")
    assert start["config"]["max_steps"] == 7

    assert call["step"] == 1
    assert call["input_tokens"] == 10 and call["output_tokens"] == 5
    assert call["cache_read_tokens"] == 3 and call["cache_write_tokens"] == 0
    assert call["cost_usd"] == 0.5 and call["stop_reason"] == "tool_use"
    assert "latency_ms" in call
    assert call["text"] == "Looking."
    assert call["tool_calls"] == [{"id": "t1", "name": "describe_dataset", "arguments": {"a": 1}}]

    assert tool["step"] == 1 and tool["tool_call_id"] == "t1" and tool["name"] == "describe_dataset"
    assert tool["arguments"] == {"a": 1} and tool["is_error"] is False
    assert tool["content"] == "x" * 41  # the full text given to the model
    assert tool["result_chars"] == 41 and tool["approx_tokens"] == approx_tokens("x" * 41) == 11
    assert tool["truncated_by_harness"] is False and "latency_ms" in tool

    # submit_report is logged like any other tool, with the harness's reply as its content
    assert events[4]["name"] == "submit_report" and "submitted" in events[4]["content"]

    final, end = events[-2:]
    assert final["terminated_reason"] == "submitted"
    assert final["report"] == result.report.model_dump(mode="json")
    assert end == {
        "ts": end["ts"],
        "type": "run_end",
        "steps": 2,
        "tool_calls": 2,
        "input_tokens": 13 + 100,
        "output_tokens": 5 + 20,
        "cost_usd": 0.5,
        "duration_ms": end["duration_ms"],
    }


async def test_log_is_flushed_while_the_run_is_in_progress(tmp_path):
    log_path = tmp_path / "run.jsonl"
    seen: list[list[str]] = []

    def peek(_args):
        seen.append([e["type"] for e in read_log(log_path)])  # as if the process crashed here
        return "ok"

    model = ScriptedModelClient(
        [
            tool_call_response(("peek", {}, "t1")),
            tool_call_response(("submit_report", report_args())),
        ]
    )
    await investigate(
        "a",
        tools=FakeExecutor({"peek": peek}),
        model=model,
        config=AgentConfig(),
        log_path=log_path,
        run_id="r",
    )
    assert seen == [["run_start", "model_call"]]


async def test_loop_log_is_readable_by_the_eval_log_parser(tmp_path):
    log_path = tmp_path / "run.jsonl"
    model = ScriptedModelClient(
        [
            tool_call_response(("describe_dataset", {}, "t1")),
            tool_call_response(("submit_report", report_args())),
        ]
    )
    await investigate(
        "a", tools=FakeExecutor(), model=model, config=AgentConfig(), log_path=log_path, run_id="r"
    )
    log = parse_log(log_path)
    assert log.skipped_lines == 0
    assert log.run_start.scenario_id == ""  # scenario_id was None -> null -> default
    assert len(log.model_calls) == 2 and len(log.tool_results) == 2
    assert log.final.terminated_reason == "submitted"
    assert log.run_end.steps == 2


def test_context_event_and_lines_are_valid_json(tmp_path):
    with StepLogger(tmp_path / "x.jsonl") as log:
        log.context_event("truncate", {"tool_call_id": "t1", "kept_chars": 100})
    (event,) = read_log(tmp_path / "x.jsonl")
    assert event["type"] == "context_event"
    assert event["kind"] == "truncate" and event["details"]["kept_chars"] == 100
