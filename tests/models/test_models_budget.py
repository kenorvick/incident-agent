"""Spend guard: ledger persistence, cap enforcement, labels, summaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_agent.agent.models.base import (
    BudgetExceededError,
    Message,
    ModelConfigError,
    ModelResponse,
    Usage,
)
from incident_agent.agent.models.budget import (
    BudgetedModelClient,
    SpendLedger,
    cap_from_env,
    default_ledger_path,
    spend_label,
)
from incident_agent.agent.models.fake import ScriptedModelClient, text_response

pytestmark = pytest.mark.anyio


def priced(cost: float, model: str = "m1") -> ModelResponse:
    return text_response(
        "ok", cost_usd=cost, model=model, usage=Usage(input_tokens=10, output_tokens=5)
    )


async def call(client: BudgetedModelClient) -> ModelResponse:
    return await client.complete("sys", [Message.user("hi")], [], 100)


async def test_records_each_call_with_tokens_cost_and_label(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=10)
    inner = ScriptedModelClient([priced(0.25), priced(0.5, "m2")])
    client = BudgetedModelClient(inner, ledger, label="run-1")

    await call(client)
    await call(client)

    first, second = ledger.entries()
    assert (first.provider, first.model, first.cost_usd, first.label) == (
        "fake",
        "m1",
        0.25,
        "run-1",
    )
    assert (first.input_tokens, first.output_tokens) == (10, 5)
    assert first.cost_source == "estimated"
    assert first.ts.endswith("+00:00")
    assert second.model == "m2"
    assert ledger.total() == pytest.approx(0.75)
    assert ledger.remaining() == pytest.approx(9.25)


async def test_blocks_at_cap_without_calling_the_model(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=1.0)
    inner = ScriptedModelClient([priced(0.6), priced(0.6), priced(0.6)])
    client = BudgetedModelClient(inner, ledger)

    await call(client)  # 0.6
    await call(client)  # 1.2: crosses the cap, the call itself is allowed
    with pytest.raises(BudgetExceededError) as exc:
        await call(client)

    assert inner.calls == 2  # the third request never reached the model
    assert "SPEND_CAP_USD" in str(exc.value)
    assert "1.2" in str(exc.value)


async def test_cap_is_inclusive(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=0.5)
    inner = ScriptedModelClient([priced(0.5), priced(0.1)])
    client = BudgetedModelClient(inner, ledger)

    await call(client)  # spend == cap exactly
    with pytest.raises(BudgetExceededError):
        await call(client)


async def test_zero_cap_blocks_everything(tmp_path):
    inner = ScriptedModelClient([priced(0.1)])
    client = BudgetedModelClient(inner, SpendLedger(tmp_path / "l.jsonl", cap_usd=0))
    with pytest.raises(BudgetExceededError):
        await call(client)
    assert inner.calls == 0


async def test_failed_calls_are_not_charged(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=10)
    client = BudgetedModelClient(ScriptedModelClient([RuntimeError("boom")]), ledger)
    with pytest.raises(RuntimeError):
        await call(client)
    assert ledger.entries() == []


async def test_spend_survives_process_restart(tmp_path):
    path = tmp_path / "l.jsonl"
    first = BudgetedModelClient(ScriptedModelClient([priced(0.9)]), SpendLedger(path, cap_usd=1.0))
    await call(first)

    # A brand-new ledger and client (as after a restart) see the earlier spend.
    reborn = SpendLedger(path, cap_usd=1.0)
    assert reborn.total() == pytest.approx(0.9)
    second = BudgetedModelClient(ScriptedModelClient([priced(0.2), priced(0.2)]), reborn)
    await call(second)  # 1.1 now
    with pytest.raises(BudgetExceededError):
        await call(second)


async def test_two_clients_share_one_ledger_file(tmp_path):
    path = tmp_path / "l.jsonl"
    a = BudgetedModelClient(ScriptedModelClient([priced(0.6)]), SpendLedger(path, cap_usd=1.0))
    b = BudgetedModelClient(
        ScriptedModelClient([priced(0.6), priced(0.1)]), SpendLedger(path, cap_usd=1.0)
    )
    await call(a)
    await call(b)
    with pytest.raises(BudgetExceededError):
        await call(b)


async def test_spend_label_context_tags_entries(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=10)
    client = BudgetedModelClient(
        ScriptedModelClient([priced(0.1), priced(0.2), priced(0.3)]), ledger
    )

    with spend_label("eval-7/scenario-a/1"):
        await call(client)
    with spend_label("eval-7/scenario-b/1"):
        await call(client)
    await call(client)  # unlabelled

    assert [e.label for e in ledger.entries()] == [
        "eval-7/scenario-a/1",
        "eval-7/scenario-b/1",
        None,
    ]
    summary = ledger.summary("eval-7/")
    assert summary.calls == 2
    assert summary.spent_usd == pytest.approx(0.3)
    assert summary.remaining_usd == pytest.approx(10 - 0.6)  # remaining is against the global cap
    assert summary.by_label == {
        "eval-7/scenario-a/1": pytest.approx(0.1),
        "eval-7/scenario-b/1": pytest.approx(0.2),
    }
    assert ledger.summary().by_label["(unlabelled)"] == pytest.approx(0.3)


async def test_explicit_wrapper_label_wins_over_context(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=10)
    client = BudgetedModelClient(ScriptedModelClient([priced(0.1)]), ledger, label="explicit")
    with spend_label("ctx"):
        await call(client)
    assert ledger.entries()[0].label == "explicit"


def test_summary_text_for_the_eval_runner(tmp_path):
    ledger = SpendLedger(tmp_path / "l.jsonl", cap_usd=10)
    ledger.record(priced(2.5), "x")
    text = ledger.describe()
    assert (
        "$2.5000" in text and "$10.00" in text and "$7.5000 remaining" in text and "1 calls" in text
    )


def test_missing_file_means_zero_and_parent_dirs_are_created(tmp_path):
    ledger = SpendLedger(tmp_path / "deep" / "dir" / "l.jsonl", cap_usd=10)
    assert ledger.total() == 0
    ledger.check()
    ledger.record(priced(0.1))
    assert ledger.path.exists()


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = SpendLedger(path, cap_usd=10)
    ledger.record(priced(0.4))
    with path.open("a") as fh:
        fh.write('{"truncated": \n\n')
    ledger.record(priced(0.1))
    assert ledger.total() == pytest.approx(0.5)


def test_cap_from_env(monkeypatch):
    assert cap_from_env() == 10.0
    monkeypatch.setenv("SPEND_CAP_USD", "2.5")
    assert cap_from_env() == 2.5
    assert SpendLedger().cap_usd == 2.5
    monkeypatch.setenv("SPEND_CAP_USD", "")
    assert cap_from_env() == 10.0
    for bad in ("lots", "-1"):
        monkeypatch.setenv("SPEND_CAP_USD", bad)
        with pytest.raises(ModelConfigError):
            cap_from_env()


def test_default_ledger_path_is_repo_local_and_overridable(monkeypatch, tmp_path):
    monkeypatch.delenv("SPEND_LEDGER_PATH")
    monkeypatch.chdir(tmp_path)  # the cwd must not matter
    path = default_ledger_path()
    assert path.parts[-2:] == (".cache", "spend-ledger.jsonl")
    assert (path.parent.parent / "pyproject.toml").exists()
    monkeypatch.setenv("SPEND_LEDGER_PATH", str(tmp_path / "x.jsonl"))
    assert default_ledger_path() == Path(tmp_path / "x.jsonl")
