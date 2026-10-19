"""Command-line interface."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from incident_agent.agent.report import InvestigationResult

app = typer.Typer(help="Investigate microservice incidents with telemetry-querying agents.")


@app.command()
def investigate(
    alert: Annotated[
        str | None, typer.Argument(help="Alert text. Defaults to the scenario's own alert.")
    ] = None,
    scenario: Annotated[
        str, typer.Option("--scenario", "-s", help="Scenario id (a fixture).")
    ] = "",
    max_steps: Annotated[int | None, typer.Option(help="Model-call limit (default 15).")] = None,
    provider: Annotated[str | None, typer.Option(help="openrouter or anthropic.")] = None,
    model: Annotated[str | None, typer.Option(help="Model ID for the provider.")] = None,
    fixtures_dir: Annotated[Path | None, typer.Option(help="Fixtures directory.")] = None,
) -> None:
    """Investigate an incident in a recorded scenario and print the report."""
    import anyio

    from incident_agent.agent.investigator import make_investigator
    from incident_agent.agent.loop import AgentConfig
    from incident_agent.agent.models import ModelError, get_model_client
    from incident_agent.backends import default_fixtures_dir
    from incident_agent.evals.scenario import list_scenarios, load_scenario

    fixtures = fixtures_dir or default_fixtures_dir()
    if not scenario or not (fixtures / scenario / "scenario.yaml").exists():
        known = ", ".join(s.id for s in list_scenarios(fixtures)) or "none found"
        typer.echo(f"Pass --scenario <id>. Scenarios in {fixtures}: {known}", err=True)
        raise typer.Exit(code=2)
    spec = load_scenario(fixtures / scenario)
    if alert:
        spec = spec.model_copy(update={"alert": alert})

    config = AgentConfig(**({"max_steps": max_steps} if max_steps else {}))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = Path("runs") / "adhoc" / f"{stamp}-{scenario}.jsonl"
    investigator = make_investigator(
        config,
        model_factory=lambda: get_model_client(provider, model),
        fixtures_dir=fixtures,
    )
    typer.echo(f"Investigating {scenario}: {spec.alert}\n")
    try:
        result = anyio.run(investigator, spec, 1, log_path, "adhoc")
    except ModelError as exc:  # missing key, bad provider
        typer.echo(f"Model error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(render_result(result))
    if result.terminated_reason != "submitted":
        raise typer.Exit(code=1)


def render_result(result: "InvestigationResult") -> str:
    """A readable report plus run statistics."""
    report = result.report
    lines = [f"Verdict: {report.verdict.value}   Confidence: {report.confidence.value}"]
    for i, cause in enumerate(report.causes, 1):
        lines.append(f"Cause {i}: {cause.service} ({cause.category.value}): {cause.summary}")
    if report.evidence:
        lines.append("Evidence:")
        for ev in report.evidence:
            values = f"  [{', '.join(ev.cited_values)}]" if ev.cited_values else ""
            lines.append(f"  - {ev.claim}  (tool call {ev.tool_call_id}){values}")
    if report.uncertainties:
        lines.append("Uncertainties:")
        lines.extend(f"  - {u}" for u in report.uncertainties)
    lines.append("")
    lines.append(
        f"Ended: {result.terminated_reason.value}" + (f" ({result.error})" if result.error else "")
    )
    lines.append(
        f"Steps: {result.steps}  Tool calls: {result.tool_calls}  "
        f"Tokens: {result.input_tokens} in / {result.output_tokens} out  "
        f"Cost: ${result.cost_usd:.4f}  Time: {result.duration_ms / 1000:.1f}s"
    )
    lines.append(f"Step log: {result.log_path}")
    return "\n".join(lines)


@app.command()
def eval(
    scenario: Annotated[
        list[str] | None, typer.Option("--scenario", "-s", help="Only these scenario ids.")
    ] = None,
    difficulty: Annotated[
        list[str] | None, typer.Option(help="Only these difficulties (single, hard, ...).")
    ] = None,
    runs: Annotated[int, typer.Option(help="Runs per scenario.")] = 3,
    concurrency: Annotated[int, typer.Option(help="Investigations in flight at once.")] = 2,
    max_steps: Annotated[int | None, typer.Option(help="Model-call limit (default 15).")] = None,
    provider: Annotated[str | None, typer.Option(help="openrouter or anthropic.")] = None,
    model: Annotated[str | None, typer.Option(help="Model ID for the provider.")] = None,
    notes: Annotated[str, typer.Option(help="Free text recorded with the results.")] = "",
    run_id: Annotated[str | None, typer.Option(help="Run id (default: a timestamp).")] = None,
    resume: Annotated[bool, typer.Option(help="Continue an existing --run-id.")] = False,
    compare_to: Annotated[
        str | None, typer.Option(help="Earlier run id to show a before/after delta against.")
    ] = None,
    report: Annotated[bool, typer.Option(help="Append results to docs/eval-results.md.")] = True,
    fixtures_dir: Annotated[Path | None, typer.Option(help="Fixtures directory.")] = None,
) -> None:
    """Run the scenario eval suite over recorded fixtures and report the results."""
    import os

    import anyio

    from incident_agent.agent.investigator import make_investigator
    from incident_agent.agent.loop import AgentConfig
    from incident_agent.agent.models import ModelError, SpendLedger, get_model_client
    from incident_agent.backends import default_fixtures_dir
    from incident_agent.evals.report import append_entry, render_entry
    from incident_agent.evals.runner import load_eval_run, run_eval, select_scenarios
    from incident_agent.evals.scenario import list_scenarios

    fixtures = fixtures_dir or default_fixtures_dir()
    try:
        specs = select_scenarios(list_scenarios(fixtures), ids=scenario, difficulties=difficulty)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if not specs:
        typer.echo(f"No scenarios matched in {fixtures}.", err=True)
        raise typer.Exit(code=2)

    try:  # fail fast on a missing key or bad provider, before any run starts
        client = get_model_client(provider, model)
    except ModelError as exc:
        typer.echo(f"Model error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    model_id = getattr(client.inner, "model", model)
    baseline = load_eval_run(Path("runs") / compare_to) if compare_to else None

    ledger = SpendLedger()
    typer.echo(f"{len(specs)} scenarios x {runs} runs. {ledger.describe()}\n")
    config = AgentConfig(**({"max_steps": max_steps} if max_steps else {}))
    result = anyio.run(
        lambda: run_eval(
            specs,
            make_investigator(
                config,
                model_factory=lambda: get_model_client(provider, model, ledger=ledger),
                fixtures_dir=fixtures,
            ),
            runs_per_scenario=runs,
            concurrency=concurrency,
            run_id=run_id,
            config={"effort": os.environ.get("MODEL_EFFORT", "medium")},
            provider=provider or os.environ.get("MODEL_PROVIDER", "openrouter"),
            model_id=model_id,
            notes=notes,
            ledger=ledger,
            resume=resume,
        )
    )
    if report:
        entry = append_entry(result, compare_to=baseline)
        typer.echo(f"{entry}\nAppended to docs/eval-results.md")
    else:
        typer.echo(render_entry(result, compare_to=baseline))
    typer.echo(f"\nRun logs: runs/{result.run_id}/   {ledger.describe()}")


@app.command("serve-mcp")
def serve_mcp(
    scenario: Annotated[
        str | None, typer.Option("--scenario", "-s", help="Fixture scenario to serve.")
    ] = None,
) -> None:
    """Serve the telemetry MCP server over stdio."""
    from incident_agent.mcp_server.server import run_stdio

    run_stdio(scenario)
