"""Factory: provider selection, env handling, and the spend guard on every path."""

from __future__ import annotations

import json

import httpx
import pytest

from incident_agent.agent.models import (
    BudgetedModelClient,
    BudgetExceededError,
    Message,
    ModelConfigError,
    SpendLedger,
    get_model_client,
)
from incident_agent.agent.models.anthropic import AnthropicClient
from incident_agent.agent.models.openrouter import OpenRouterClient

pytestmark = pytest.mark.anyio

OK_BODY = {
    "id": "gen-1",
    "model": "anthropic/claude-sonnet-5.5",
    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.4},
}


def test_defaults_to_openrouter_sonnet(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    client = get_model_client(load_env=False)
    assert isinstance(client, BudgetedModelClient)
    assert isinstance(client.inner, OpenRouterClient)
    assert client.inner.model == "anthropic/claude-sonnet-5.5"
    assert client.inner.effort == "medium"


def test_model_id_and_effort_come_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("MODEL_ID", "anthropic/claude-haiku-5.5")
    monkeypatch.setenv("MODEL_EFFORT", "low")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    inner = get_model_client(load_env=False).inner
    assert (inner.model, inner.effort) == ("anthropic/claude-haiku-5.5", "low")

    monkeypatch.setenv("MODEL_EFFORT", "none")
    assert get_model_client(load_env=False).inner.effort is None


def test_blank_model_id_uses_the_default(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert get_model_client(load_env=False).inner.model == "anthropic/claude-sonnet-5.5"


def test_anthropic_provider(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "Anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = get_model_client(load_env=False)
    assert isinstance(client.inner, AnthropicClient)
    assert client.inner.model == "claude-sonnet-5-5"


def test_anthropic_rejects_an_openrouter_style_model_id(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_ID", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(ModelConfigError, match="claude-sonnet-5-5"):
        get_model_client(load_env=False)


@pytest.mark.parametrize(
    ("provider", "var"),
    [("openrouter", "OPENROUTER_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")],
)
def test_missing_key_names_the_variable_and_env_file(monkeypatch, provider, var):
    monkeypatch.setenv("MODEL_PROVIDER", provider)
    with pytest.raises(ModelConfigError) as exc:
        get_model_client(load_env=False)
    assert var in str(exc.value)
    assert ".env" in str(exc.value)


def test_unknown_provider_and_bad_effort(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    with pytest.raises(ModelConfigError, match="openrouter, anthropic"):
        get_model_client(load_env=False)
    monkeypatch.setenv("MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MODEL_EFFORT", "turbo")
    with pytest.raises(ModelConfigError, match="MODEL_EFFORT"):
        get_model_client(load_env=False)


def test_loads_dotenv_from_the_working_directory(monkeypatch, tmp_path):
    # setenv + delenv registers an undo, so load_dotenv's write is cleaned up afterwards.
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\nMODEL_ID=anthropic/x\n")
    monkeypatch.setenv("MODEL_ID", "x")
    monkeypatch.delenv("MODEL_ID")
    monkeypatch.chdir(tmp_path)

    client = get_model_client()
    assert client.inner.model == "anthropic/x"
    assert client.inner._api_key == "from-dotenv"


@pytest.mark.parametrize("provider", ["openrouter", "anthropic"])
def test_every_provider_path_is_wrapped_by_the_guard(monkeypatch, provider):
    monkeypatch.setenv("MODEL_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = get_model_client(load_env=False)
    assert type(client) is BudgetedModelClient
    assert isinstance(client.ledger, SpendLedger)


async def test_guard_blocks_before_any_http_when_cap_is_spent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=1.0)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=OK_BODY)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = get_model_client(load_env=False, ledger=ledger, http_client=http, label="t")

    for _ in range(3):  # 0.4 + 0.4 + 0.4 = 1.2: the third call crosses the cap
        await client.complete("s", [Message.user("hi")], [], 100)
    with pytest.raises(BudgetExceededError):
        await client.complete("s", [Message.user("hi")], [], 100)

    assert len(calls) == 3
    entries = ledger.entries()
    assert [e.cost_usd for e in entries] == [0.4, 0.4, 0.4]
    assert entries[0].cost_source == "provider"
    assert entries[0].label == "t"
    assert json.loads(calls[0].content)["max_tokens"] == 100
