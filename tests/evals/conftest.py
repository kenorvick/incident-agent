"""Shared fixtures for eval tests. Nothing here touches the network or the real spend ledger."""

from __future__ import annotations

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(autouse=True)
def isolated_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    for name in ("SPEND_CAP_USD", "MODEL_PROVIDER", "MODEL_ID"):
        monkeypatch.delenv(name, raising=False)
    ledger = tmp_path_factory.mktemp("eval-ledger") / "spend-ledger.jsonl"
    monkeypatch.setenv("SPEND_LEDGER_PATH", str(ledger))
