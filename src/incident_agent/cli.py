"""Command-line interface."""

import typer

app = typer.Typer(help="Investigate microservice incidents with telemetry-querying agents.")


def _not_implemented() -> None:
    typer.echo("not implemented yet")
    raise typer.Exit(code=1)


@app.command()
def investigate() -> None:
    """Investigate an incident."""
    _not_implemented()


@app.command()
def eval() -> None:
    """Run the scenario eval suite."""
    _not_implemented()


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Serve the telemetry MCP server."""
    _not_implemented()
