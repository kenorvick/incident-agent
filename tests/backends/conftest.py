from pathlib import Path

import pytest
from telemetry_rows import make_rows, write_events

from incident_agent.backends.duckdb_backend import DuckDBBackend


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("fixtures")
    write_events(root / "tiny" / "events.jsonl.gz", make_rows())
    return root


@pytest.fixture(scope="session")
def backend(fixtures_dir) -> DuckDBBackend:
    return DuckDBBackend.from_fixture(fixtures_dir, "tiny")
