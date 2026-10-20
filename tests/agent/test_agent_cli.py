"""CLI: investigate wiring (with a scripted model) and report rendering. No network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agent_fakes import FakeExecutor, report_args
from typer.testing import CliRunner

import incident_agent.agent.investigator as investigator_module
from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.cli import app, render_result

runner = CliRunner()

SCENARIO_YAML = """
id: demo
title: Demo
difficulty: single
alert: Checkout is slow.
ground_truth:
  healthy: false
  causes:
    - {service: payment, category: dependency_failure, description: adyen timeouts}
capture:
  demo_version: "2.0"
  baseline_seconds: 1
  fault_seconds: 1
  sample_rate: 1
  span_count: 1
  captured_at: "2026-10-19T00:00:00Z"
"""


def test_help_lists_commands_without_importing_heavy_modules():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "investigate" in result.output and "serve-mcp" in result.output


def test_investigate_requires_a_known_scenario(tmp_path):
    result = runner.invoke(
        app, ["investigate", "--scenario", "nope", "--fixtures-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "Pass --scenario" in result.output


@pytest.mark.anyio
async def test_render_result_shows_report_stats_and_log_path(tmp_path):
    evidence = [{"claim": "payment error rate", "tool_call_id": "t1", "cited_values": ["30%"]}]
    model = ScriptedModelClient(
        [tool_call_response(("submit_report", report_args(evidence=evidence), "t1"))]
    )
    result = await investigate(
        "a",
        tools=FakeExecutor(),
        model=model,
        config=AgentConfig(verify_citations=False),  # this test cites a made-up call on purpose
        log_path=tmp_path / "x.jsonl",
        run_id="r",
    )
    out = render_result(result)
    assert "Verdict: root_cause_found   Confidence: high" in out
    assert "payment (dependency_failure): adyen calls time out" in out
    assert "(tool call t1)  [30%]" in out
    assert "Steps: 1  Tool calls: 1" in out
    assert f"Step log: {tmp_path / 'x.jsonl'}" in out


def test_investigate_command_runs_the_investigator(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "scenario.yaml").write_text(SCENARIO_YAML)
    seen = {}

    def fake_make(config, *, model_factory, fixtures_dir):
        async def run(scenario, attempt, log_path, run_id):
            seen.update(alert=scenario.alert, max_steps=config.max_steps, run_id=run_id)
            model = ScriptedModelClient(
                [tool_call_response(("submit_report", report_args(), "t1"))]
            )
            return await investigate(
                scenario.alert,
                tools=FakeExecutor(),
                model=model,
                config=config,
                log_path=log_path,
                run_id=run_id,
            )

        return run

    monkeypatch.setattr(investigator_module, "make_investigator", fake_make)
    monkeypatch.chdir(tmp_path)
    args = [
        "investigate",
        "--scenario",
        "demo",
        "--fixtures-dir",
        str(tmp_path),
        "--max-steps",
        "4",
    ]

    default = runner.invoke(app, args)
    assert default.exit_code == 0, default.output
    assert seen == {"alert": "Checkout is slow.", "max_steps": 4, "run_id": "adhoc"}
    match = re.search(r"Step log: (runs/adhoc/\d{8}-\d{6}-demo\.jsonl)", default.output)
    assert match and Path(match.group(1)).exists()

    custom = runner.invoke(app, [*args, "payments are failing"])
    assert custom.exit_code == 0 and seen["alert"] == "payments are failing"
