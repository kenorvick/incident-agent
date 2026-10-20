"""Whole pipeline offline: scripted model -> agent loop -> in-process MCP -> real fixture ->
step log -> scoring -> report, and the CLI on top. Zero model spend, no network.

The scripted investigator reads the real tool results out of the recorded requests, so the
evidence it cites is copied from what the tools actually returned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from incident_agent.agent.investigator import make_investigator
from incident_agent.agent.loop import AgentConfig
from incident_agent.agent.models import BudgetedModelClient, SpendLedger
from incident_agent.agent.models.fake import (
    RecordedRequest,
    ScriptedModelClient,
    ScriptItem,
    tool_call_response,
)
from incident_agent.backends import default_fixtures_dir
from incident_agent.cli import app
from incident_agent.evals.logs import parse_log
from incident_agent.evals.report import render_entry
from incident_agent.evals.runner import load_eval_run, run_eval
from incident_agent.evals.scenario import load_scenario

SCENARIO_ID = "payment-failure-50pct"
FIXTURE = default_fixtures_dir() / SCENARIO_ID

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason=f"fixture {SCENARIO_ID} not captured")

runner = CliRunner()


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SPEND_CAP_USD", "MODEL_PROVIDER", "MODEL_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPEND_LEDGER_PATH", str(tmp_path / "ledger" / "spend-ledger.jsonl"))


# --- the scripted investigator ------------------------------------------------------------


class ScriptedInvestigator:
    """Behaves like a competent agent: describe, rank services by error rate, drill into the
    worst one's errors, then submit a report citing ids and values from the real results."""

    def __init__(self, *, fabricate: bool = False, fix_after_rejection: bool = False) -> None:
        self.fabricate = fabricate
        self.fix_after_rejection = fix_after_rejection
        self.ids: dict[str, str] = {}  # step name -> ref (`c1`, ...) the harness shows
        self.worst = ""
        self.worst_rate = ""
        self.error_count = ""
        self.separator = ""

    def __call__(self, request: RecordedRequest) -> ScriptItem:
        step = request.index
        if step == 0:
            return tool_call_response(("describe_dataset", {}), text="Looking at the data.")
        result = request.tool_results[0]
        if step == 4 and self.fix_after_rejection:  # the citation check rejected step 3's report
            assert result.is_error and "99.9%" in result.content, result.content
            assert "does not appear" in result.content
            self.fabricate = False
            return tool_call_response(("submit_report", self.report()))
        assert not result.is_error, result.content
        if step == 1:
            self.ids["describe"] = "c1"
            assert "services (events):" in result.content
            return tool_call_response(
                (
                    "run_query",
                    {
                        "group_by": ["service.name"],
                        "calculations": [{"op": "ERROR_RATE"}, {"op": "COUNT"}],
                        "order_by": "error_rate",
                        "limit": 5,
                    },
                )
            )
        if step == 2:
            self.ids["query"] = "c2"
            # first data row under the header: `<service>  <error_rate>  <count>`
            row = re.search(r"^service\.name .*\n(\S+)\s+(\S+)\s+\d+$", result.content, re.M)
            self.worst, self.worst_rate = row.group(1), row.group(2)
            return tool_call_response(
                (
                    "find_outliers",
                    {
                        "selection": [
                            {"field": "error", "op": "=", "value": True},
                            {"field": "service.name", "op": "=", "value": self.worst},
                        ]
                    },
                )
            )
        if step == 3:
            self.ids["outliers"] = "c3"
            self.error_count = re.search(r"-> (\d+) events", result.content).group(1)
            if m := re.search(r"^\s+(\S+=\S+)\s*$", result.content, re.MULTILINE):
                self.separator = m.group(1)  # e.g. a "perfect separator" like error.type=Error
            return tool_call_response(("submit_report", self.report()))
        raise AssertionError(f"unexpected extra model call #{step}")

    def report(self) -> dict:
        rate = "99.9%" if self.fabricate else self.worst_rate
        evidence = [
            {
                "claim": f"{self.worst} has the highest error rate",
                "tool_call_id": self.ids["query"],
                "cited_values": [self.worst, rate],
            },
            {
                "claim": f"{self.error_count} failing {self.worst} events",
                "tool_call_id": self.ids["outliers"],
                "cited_values": [self.error_count, *([self.separator] if self.separator else [])],
            },
        ]
        return {
            "verdict": "root_cause_found",
            "causes": [
                {
                    "service": self.worst,
                    "category": "error_rate",
                    "summary": f"The {self.worst} service is failing a large share of requests.",
                }
            ],
            "evidence": evidence,
            "confidence": "high",
            "uncertainties": [],
        }


def never_submits(request: RecordedRequest) -> ScriptItem:
    return tool_call_response(("describe_dataset", {}))


def _factory(script):
    return lambda: ScriptedModelClient(script() if callable(script) else script)


def _spec():
    return load_scenario(FIXTURE)


# --- library-level end to end ---------------------------------------------------------------


@pytest.mark.anyio
async def test_scripted_investigator_passes_eval_end_to_end(tmp_path):
    runs_dir = tmp_path / "runs"
    investigator = make_investigator(
        AgentConfig(),
        model_factory=_factory(ScriptedInvestigator),
        fixtures_dir=default_fixtures_dir(),
    )
    run = await run_eval(
        [_spec()],
        investigator,
        runs_per_scenario=2,
        concurrency=2,
        run_id="e2e",
        runs_dir=runs_dir,
        provider="fake",
        model_id="fake-model",
    )

    assert run.finished and not run.partial and run.not_completed == []
    assert len(run.records) == 2
    for rec in run.records:
        assert rec.status == "completed", rec.error
        assert rec.result.terminated_reason == "submitted"
        assert rec.result.steps == 4 and rec.result.tool_calls == 4  # incl. submit_report
        s = rec.score
        assert s.service_correct and s.category_correct
        assert s.fabrication_free and s.fabricated_values == [] and s.invalid_tool_call_ids == []
        assert s.evidence_total == 2 and s.evidence_grounded == 2
        assert s.passed, s.failure_reasons
        assert s.tool_call_sequence == [
            "describe_dataset",
            "run_query",
            "find_outliers",
            "submit_report",
        ]
        assert s.errored_tool_calls == 0
        assert s.steps == 4

    # artifacts on disk parse and agree
    summary = load_eval_run(runs_dir / "e2e")
    assert summary.finished and len(summary.records) == 2
    assert summary.provider == "fake" and summary.model_id == "fake-model"
    assert summary.agent_config["max_steps"] == 15  # taken from the run_start log
    for n in (1, 2):
        log_file = runs_dir / "e2e" / SCENARIO_ID / f"{n}.jsonl"
        log = parse_log(log_file)
        assert log.skipped_lines == 0 and not log.missing
        assert log.run_start and log.run_start.scenario_id == SCENARIO_ID
        assert log.run_end and log.run_end.tool_calls == 4
        assert len(log.model_calls) == 4 and len(log.tool_results) == 4
        assert (runs_dir / "e2e" / SCENARIO_ID / f"{n}.result.json").exists()
        # raw lines carry the fields the scorer and contracts rely on
        records = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert [r["type"] for r in records][0] == "run_start"
        assert [r["type"] for r in records][-2:] == ["final", "run_end"]
        tool_results = [r for r in records if r["type"] == "tool_result"]
        assert all(r["content"] and r["result_chars"] == len(r["content"]) for r in tool_results)
    assert (runs_dir / "e2e" / "scenarios.json").exists()

    entry = render_entry(summary)
    assert SCENARIO_ID in entry
    assert "e2e" in entry and "fake-model" in entry
    assert "None: every scored run passed." in entry


@pytest.mark.anyio
async def test_cited_values_really_come_from_tool_results(tmp_path):
    """Guard the test itself: the report cites what the fixture returned, not constants."""
    scripted = ScriptedInvestigator()
    client = ScriptedModelClient(scripted)
    investigator = make_investigator(
        model_factory=lambda: client, fixtures_dir=default_fixtures_dir()
    )
    result = await investigator(_spec(), 1, tmp_path / "1.jsonl", "r")
    assert result.terminated_reason == "submitted"
    assert scripted.worst == "payment"
    assert re.fullmatch(r"\d+(\.\d+)?%", scripted.worst_rate)
    assert scripted.error_count.isdigit() and int(scripted.error_count) > 0
    # the final request the model saw contained all the cited text
    seen = "\n".join(b.content for r in client.requests for b in r.tool_results)
    assert scripted.worst_rate in seen and f"-> {scripted.error_count} events" in seen
    assert client.requests[0].tool_names.count("submit_report") == 1


@pytest.mark.anyio
async def test_fabricated_number_is_scored_as_fabrication(tmp_path):
    investigator = make_investigator(
        AgentConfig(verify_citations=False),  # without the guardrail the eval sees the fabrication
        model_factory=_factory(lambda: ScriptedInvestigator(fabricate=True)),
        fixtures_dir=default_fixtures_dir(),
    )
    run = await run_eval(
        [_spec()], investigator, runs_per_scenario=1, run_id="fab", runs_dir=tmp_path / "runs"
    )
    (rec,) = run.records
    s = rec.score
    assert rec.result.terminated_reason == "submitted"
    assert s.service_correct and s.category_correct  # the diagnosis itself was right
    assert not s.fabrication_free and not s.passed
    assert [(f.value, f.reason) for f in s.fabricated_values] == [("99.9%", "not_in_result")]
    assert any("fabricated" in r for r in s.failure_reasons)
    assert "fabricated" in render_entry(run)


@pytest.mark.anyio
async def test_citation_check_rejects_a_fabricated_number_and_the_fix_scores_clean(tmp_path):
    investigator = make_investigator(
        model_factory=_factory(
            lambda: ScriptedInvestigator(fabricate=True, fix_after_rejection=True)
        ),
        fixtures_dir=default_fixtures_dir(),
    )
    run = await run_eval(
        [_spec()], investigator, runs_per_scenario=1, run_id="chk", runs_dir=tmp_path / "runs"
    )
    (rec,) = run.records
    s = rec.score
    assert rec.result.terminated_reason == "submitted" and rec.result.steps == 5
    assert s.passed and s.fabrication_free
    assert s.citation_rejections == 1 and not s.citation_forced
    log = parse_log(tmp_path / "runs" / "chk" / rec.log_path)
    assert [(c.attempt, c.ok, c.forced) for c in log.citation_checks] == [
        (1, False, False),
        (2, True, False),
    ]
    assert log.citation_checks[0].problems[0]["value"] == "99.9%"


@pytest.mark.anyio
async def test_step_limit_gives_an_inconclusive_report(tmp_path):
    investigator = make_investigator(
        AgentConfig(max_steps=3),
        model_factory=_factory(lambda: never_submits),
        fixtures_dir=default_fixtures_dir(),
    )
    run = await run_eval(
        [_spec()], investigator, runs_per_scenario=1, run_id="limit", runs_dir=tmp_path / "runs"
    )
    (rec,) = run.records
    assert rec.status == "completed"  # a limit is a result, not a crash
    assert rec.result.terminated_reason == "max_steps"
    assert rec.result.steps == 3 and "Step limit" in rec.result.error
    report = rec.result.report
    assert report.verdict == "inconclusive" and report.causes == []
    assert not rec.score.passed and not rec.score.service_correct
    assert rec.score.terminated_reason == "max_steps"
    log = parse_log(tmp_path / "runs" / "limit" / SCENARIO_ID / "1.jsonl")
    assert log.final and log.final.terminated_reason == "max_steps"
    assert "inconclusive" in json.dumps(log.final.report)
    assert SCENARIO_ID in render_entry(run)


# --- CLI end to end -------------------------------------------------------------------------


@pytest.fixture
def scripted_cli(monkeypatch, tmp_path):
    """Make every CLI model lookup return a budgeted scripted client (no key, no network)."""
    created: list[ScriptedModelClient] = []
    own_ledger = SpendLedger(tmp_path / "own-ledger.jsonl", cap_usd=5.0)

    def fake_get_model_client(provider=None, model_id=None, *, ledger=None, label=None, **_):
        inner = ScriptedModelClient(ScriptedInvestigator())
        created.append(inner)
        return BudgetedModelClient(inner, ledger or own_ledger, label)

    monkeypatch.setattr("incident_agent.agent.models.get_model_client", fake_get_model_client)
    monkeypatch.setattr("incident_agent.agent.investigator.get_model_client", fake_get_model_client)
    return created


def test_cli_eval_end_to_end(scripted_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # anything written relative to the cwd stays in tmp
    runs_dir = tmp_path / "myruns"
    result = runner.invoke(
        app,
        [
            "eval",
            "--scenario",
            SCENARIO_ID,
            "--runs",
            "2",
            "--concurrency",
            "2",
            "--no-report",
            "--run-id",
            "cli1",
            "--runs-dir",
            str(runs_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert SCENARIO_ID in result.output and "cli1" in result.output
    assert str(runs_dir / "cli1") in result.output
    summary = load_eval_run(runs_dir / "cli1")
    assert [r.score.passed for r in summary.records] == [True, True]
    assert summary.model_id == "fake-model"
    assert summary.ledger_cost_usd == 0.0
    ledger = SpendLedger(tmp_path / "ledger" / "spend-ledger.jsonl")
    labels = {e.label for e in ledger.entries()}
    assert labels == {f"cli1/{SCENARIO_ID}/1", f"cli1/{SCENARIO_ID}/2"}
    assert not (tmp_path / "docs").exists() and not (tmp_path / "runs").exists()

    again = runner.invoke(
        app,
        ["eval", "-s", SCENARIO_ID, "--runs", "1", "--no-report", "--runs-dir", str(runs_dir),
         "--run-id", "cli2", "--compare-to", "cli1"],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    assert "cli1" in again.output


def test_cli_eval_appends_report_relative_to_cwd(scripted_cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the results log is docs/eval-results.md under the cwd
    result = runner.invoke(app, ["eval", "-s", SCENARIO_ID, "--runs", "1", "--run-id", "rep"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "docs" / "eval-results.md").read_text()
    assert "<!-- eval-run: rep -->" in text and SCENARIO_ID in text
    assert (tmp_path / "runs" / "rep" / "summary.json").exists()


def test_cli_investigate_end_to_end(scripted_cli, tmp_path):
    runs_dir = tmp_path / "myruns"
    result = runner.invoke(
        app, ["investigate", "--scenario", SCENARIO_ID, "--runs-dir", str(runs_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "Verdict: root_cause_found" in result.output
    assert "payment (error_rate)" in result.output
    assert "Ended: submitted" in result.output
    (log_file,) = (runs_dir / "adhoc").glob(f"*-{SCENARIO_ID}.jsonl")
    assert str(log_file) in result.output
    log = parse_log(log_file)
    assert log.run_start.alert.startswith("[HIGH] Checkout success rate")
    assert log.final.terminated_reason == "submitted"


def test_cli_investigate_exits_nonzero_when_limit_hit(scripted_cli, tmp_path, monkeypatch):
    def get(provider=None, model_id=None, *, ledger=None, label=None, **_):
        return BudgetedModelClient(
            ScriptedModelClient(never_submits), ledger or SpendLedger(tmp_path / "l.jsonl", 5.0)
        )

    monkeypatch.setattr("incident_agent.agent.models.get_model_client", get)
    result = runner.invoke(
        app,
        ["investigate", "-s", SCENARIO_ID, "--max-steps", "2", "--runs-dir", str(tmp_path / "r")],
    )
    assert result.exit_code == 1
    assert "Verdict: inconclusive" in result.output and "Ended: max_steps" in result.output


def test_cli_help_commands():
    for command in ("investigate", "eval", "serve-mcp"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, (command, result.output)
        assert "Usage" in result.output


def test_cli_eval_without_api_key_fails_fast(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # a real get_model_client would load a .env found from the cwd; do not let it
    import incident_agent.agent.models as models

    real = models.get_model_client
    monkeypatch.setattr(
        models, "get_model_client", lambda *a, **kw: real(*a, **{**kw, "load_env": False})
    )
    result = runner.invoke(app, ["eval", "--no-report", "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code == 2
    assert "OPENROUTER_API_KEY is not set" in result.output
    assert not (tmp_path / "runs").exists()


@pytest.mark.anyio
async def test_serve_mcp_command_speaks_stdio():
    """The real `serve-mcp` subprocess serves the fixture over the stdio transport."""
    import shutil
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    exe = shutil.which("incident-agent", path=str(Path(sys.executable).parent))
    if exe is None:
        pytest.skip("incident-agent script not installed")
    params = StdioServerParameters(command=exe, args=["serve-mcp", "--scenario", SCENARIO_ID])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"describe_dataset", "run_query", "find_outliers"} <= names
        result = await session.call_tool("describe_dataset", {})
        assert "services (events):" in result.content[0].text
