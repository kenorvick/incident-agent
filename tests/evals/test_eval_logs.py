"""Step-log parsing is tolerant: unknown types, extra fields, nulls, truncated lines."""

from __future__ import annotations

import json

import pytest
from synthetic import good_evidence, make_report, make_result, standard_log

from incident_agent.evals.logs import (
    ModelCall,
    ToolResult,
    UnknownRecord,
    parse_log,
    parse_log_lines,
)


def test_parses_a_full_log(tmp_path):
    lb, a, b = standard_log()
    lb.finish(make_report())
    log = parse_log(lb.write(tmp_path / "1.jsonl"))
    assert log.run_start.scenario_id == "checkout-latency"
    assert log.run_start.config["max_steps"] == 20
    assert len(log.model_calls) == 2 and isinstance(log.model_calls[0], ModelCall)
    assert log.model_calls[0].tool_calls[0].id == a
    assert log.model_calls[0].total_input_tokens == 1000 - 200 + 150 + 50
    assert [r.tool_call_id for r in log.tool_results] == [a, b]
    assert log.final.terminated_reason == "submitted"
    assert log.run_end.steps == 2
    assert log.tool_results_by_id()[a][0].name == "get_service_overview"
    assert log.wall_clock_ms() == pytest.approx(60100, abs=1)
    assert log.skipped_lines == 0 and not log.missing


def test_tolerates_unknown_types_extra_fields_nulls_and_garbage():
    lines = [
        json.dumps({"ts": "2026-10-19T21:30:00Z", "type": "brand_new_event", "x": 1}),
        json.dumps(
            {
                "type": "tool_result",
                "tool_call_id": "t1",
                "name": "q",
                "content": {"a": 1},
                "is_error": None,
                "future_field": "kept",
                "latency_ms": None,
            }
        ),
        json.dumps({"type": "model_call", "tool_calls": None, "cost_usd": None, "step": "oops"}),
        json.dumps(["not", "an", "object"]),
        '{"type": "run_end", "steps": 3',  # truncated by a crash
        "",
    ]
    log = parse_log_lines(lines)
    assert log.skipped_lines == 2
    assert isinstance(log.records[0], UnknownRecord) and log.records[0].type == "brand_new_event"
    tr = log.tool_results[0]
    assert isinstance(tr, ToolResult)
    assert tr.is_error is False and tr.content == '{"a": 1}'
    assert tr.model_extra["future_field"] == "kept"
    # a known type that fails validation degrades to UnknownRecord instead of raising
    assert isinstance(log.records[2], UnknownRecord)
    assert log.run_end is None


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_log(tmp_path / "nope.jsonl")
    log = parse_log(tmp_path / "nope.jsonl", missing_ok=True)
    assert log.missing and log.records == []


def test_result_fixture_roundtrip(tmp_path):
    lb, a, b = standard_log()
    report = make_report(evidence=good_evidence(a, b))
    lb.finish(report)
    path = lb.write(tmp_path / "1.jsonl")
    assert make_result(report, path).log_path == path


def test_refs_are_parsed_and_stripped_from_the_result_text(tmp_path):
    lb, a, b = standard_log(refs=True)
    lb.finish(make_report())
    log = parse_log(lb.write(tmp_path / "1.jsonl"))
    first = log.tool_results[0]
    assert (first.ref, first.tool_call_id) == ("c1", a)
    assert first.content.startswith("[ref c1]\n") and not first.result_text.startswith("[ref")
    by = log.tool_results_by_cited_id()
    assert by["c2"] == by[b] and by["c1"][0].name == "get_service_overview"


def test_logs_without_refs_resolve_by_id_only(tmp_path):
    lb, a, b = standard_log()
    lb.finish(make_report())
    log = parse_log(lb.write(tmp_path / "1.jsonl"))
    assert log.tool_results[0].ref == ""
    assert log.tool_results[0].result_text == log.tool_results[0].content
    assert set(log.tool_results_by_cited_id()) == {a, b}
