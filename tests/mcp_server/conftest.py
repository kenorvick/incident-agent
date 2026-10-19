"""Fixtures for the MCP tool tests: a tiny hand-written dataset and a ~18k-span synthetic one."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backends"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_synthetic import make_big_rows  # noqa: E402
from telemetry_rows import make_rows, write_events  # noqa: E402

from incident_agent.backends.duckdb_backend import DuckDBBackend  # noqa: E402
from incident_agent.mcp_server.tools import TOOLS, execute  # noqa: E402

_TOOLS = {t.name: t for t in TOOLS}


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mcp_fixtures")
    write_events(root / "tiny" / "events.jsonl.gz", make_rows())
    write_events(root / "big" / "events.jsonl.gz", make_big_rows())
    return root


@pytest.fixture(scope="session")
def tiny(fixtures_dir) -> DuckDBBackend:
    return DuckDBBackend.from_fixture(fixtures_dir, "tiny")


@pytest.fixture(scope="session")
def big(fixtures_dir) -> DuckDBBackend:
    return DuckDBBackend.from_fixture(fixtures_dir, "big")


@pytest.fixture
def call():
    """`call(backend, "run_query", group_by=["service.name"]) -> (text, is_error)`."""

    def _call(backend, name: str, **arguments):
        return execute(backend, _TOOLS[name], arguments)

    return _call
