"""Telemetry backends and the `get_backend` factory.

Selection (arguments win over environment):
- `TELEMETRY_BACKEND`: `duckdb` (default, offline fixtures) or `honeycomb` (live; needs
  `HONEYCOMB_API_KEY`, optional `HONEYCOMB_DATASET`, `HONEYCOMB_API_URL`).
- `FIXTURES_DIR`: directory holding `<scenario-id>/events.jsonl.gz` (default: `./fixtures`, else
  the repository's `fixtures/`).
- `SCENARIO_ID`: scenario to load when none is passed.

The Honeycomb module is imported lazily so it is never loaded on the default path.
"""

import os
from pathlib import Path

from incident_agent.backends.base import (
    BackendError,
    BackendUnavailableError,
    FieldTypeError,
    InvalidQueryError,
    TelemetryBackend,
    UnknownFieldError,
    UnsupportedOperationError,
)

__all__ = [
    "BackendError",
    "BackendUnavailableError",
    "FieldTypeError",
    "InvalidQueryError",
    "TelemetryBackend",
    "UnknownFieldError",
    "UnsupportedOperationError",
    "get_backend",
]


def default_fixtures_dir() -> Path:
    env = os.environ.get("FIXTURES_DIR")
    if env:
        return Path(env)
    cwd = Path("fixtures")
    return cwd if cwd.is_dir() else Path(__file__).resolve().parents[3] / "fixtures"


def get_backend(
    scenario_id: str | None = None,
    *,
    kind: str | None = None,
    fixtures_dir: Path | str | None = None,
) -> TelemetryBackend:
    """Build the configured backend. `scenario_id` is required for duckdb, ignored for Honeycomb."""
    kind = (kind or os.environ.get("TELEMETRY_BACKEND") or "duckdb").strip().lower()
    if kind == "duckdb":
        from incident_agent.backends.duckdb_backend import DuckDBBackend

        scenario = scenario_id or os.environ.get("SCENARIO_ID")
        if not scenario:
            raise BackendError("duckdb backend needs a scenario id (argument or SCENARIO_ID)")
        return DuckDBBackend.from_fixture(fixtures_dir or default_fixtures_dir(), scenario)
    if kind == "honeycomb":
        from incident_agent.backends.honeycomb_backend import HoneycombBackend

        return HoneycombBackend.from_env()
    raise BackendError(f"unknown TELEMETRY_BACKEND `{kind}`; use `duckdb` or `honeycomb`")
