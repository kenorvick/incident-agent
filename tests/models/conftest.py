"""Shared fixtures for model-layer tests. No test here may touch the network."""

from __future__ import annotations

import pytest

from incident_agent.agent.models.retry import RetryPolicy

_ENV_VARS = (
    "MODEL_PROVIDER",
    "MODEL_ID",
    "MODEL_EFFORT",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "SPEND_CAP_USD",
    "SPEND_LEDGER_PATH",
)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep the developer's real environment out of every test, and never touch the real ledger."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    ledger = tmp_path_factory.mktemp("ledger") / "spend-ledger.jsonl"
    monkeypatch.setenv("SPEND_LEDGER_PATH", str(ledger))


class Sleeper:
    """Injectable no-op sleep that records the requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def policy() -> RetryPolicy:
    """Deterministic backoff: jitter fixed at zero, so delays are 0.5, 1, 2, 4 ..."""
    return RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=30.0, rng=lambda: 0.0)
