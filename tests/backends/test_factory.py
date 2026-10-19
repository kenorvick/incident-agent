import subprocess
import sys

import pytest

from incident_agent.backends import BackendError, BackendUnavailableError, get_backend
from incident_agent.backends.duckdb_backend import DuckDBBackend


def test_default_is_duckdb(monkeypatch, fixtures_dir):
    monkeypatch.delenv("TELEMETRY_BACKEND", raising=False)
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures_dir))
    backend = get_backend("tiny")
    assert isinstance(backend, DuckDBBackend) and backend.schema().row_count == 44


def test_scenario_and_dir_from_environment(monkeypatch, fixtures_dir):
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures_dir))
    monkeypatch.setenv("SCENARIO_ID", "tiny")
    assert get_backend().schema().row_count == 44


def test_explicit_arguments_win(monkeypatch, fixtures_dir):
    monkeypatch.setenv("TELEMETRY_BACKEND", "honeycomb")
    backend = get_backend("tiny", kind="duckdb", fixtures_dir=fixtures_dir)
    assert isinstance(backend, DuckDBBackend)


def test_missing_scenario_and_unknown_kind(monkeypatch, fixtures_dir):
    monkeypatch.delenv("SCENARIO_ID", raising=False)
    with pytest.raises(BackendError, match="scenario"):
        get_backend(fixtures_dir=fixtures_dir)
    with pytest.raises(FileNotFoundError, match="available scenarios"):
        get_backend("nope", fixtures_dir=fixtures_dir)
    with pytest.raises(BackendError, match="unknown TELEMETRY_BACKEND"):
        get_backend(kind="postgres")


def test_honeycomb_selected_by_env_needs_key(monkeypatch):
    monkeypatch.setenv("TELEMETRY_BACKEND", "honeycomb")
    monkeypatch.delenv("HONEYCOMB_API_KEY", raising=False)
    with pytest.raises(BackendUnavailableError):
        get_backend()


def test_honeycomb_is_not_imported_on_the_default_path(fixtures_dir):
    code = (
        "import sys\n"
        "from incident_agent.backends import get_backend\n"
        f"get_backend('tiny', fixtures_dir={str(fixtures_dir)!r}).schema()\n"
        "assert 'incident_agent.backends.honeycomb_backend' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
