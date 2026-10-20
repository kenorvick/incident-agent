"""Memory ISOLATION eval: seed `evals/memory_seeds/isolation.yaml`, then recall across every scope,
key and keyword and assert nothing leaks. Deterministic (no model); the last test runs the same
checks through a scripted agent run and reads them back from its step log."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agent_fakes import FakeExecutor, report_args

from incident_agent.agent.loop import AgentConfig, investigate
from incident_agent.agent.memory import (
    MemoryStore,
    RecallTool,
    SaveMemoryTool,
    Scope,
    ToolCallLedger,
    enable_memory,
    load_seed_notes,
    seed_store,
)
from incident_agent.agent.models.base import ToolUseBlock
from incident_agent.agent.models.fake import ScriptedModelClient, tool_call_response
from incident_agent.agent.report import TerminatedReason
from incident_agent.backends import default_fixtures_dir
from incident_agent.evals.logs import parse_log
from incident_agent.evals.scenario import load_scenario
from incident_agent.evals.scoring import memory_score

pytestmark = pytest.mark.anyio

SEED = Path(__file__).resolve().parents[2] / "evals" / "memory_seeds" / "isolation.yaml"
INV = "healthy-baseline"  # the investigation the recalls run in
MARKER = re.compile(r"isomarker-\w+")

Key = tuple[Scope, str]


def markers_in(text: str) -> set[str]:
    """Markers in recalled text. An empty recall echoes the asker's own keywords back
    ("matching 'x'"), which is the query and not a returned note, so that part is skipped."""
    return set(MARKER.findall(re.sub(r"matching '[^']*'", "", text)))


def expected_by_key() -> dict[Key, set[str]]:
    """The markers each (scope, key) may return: its live notes' (not the expired one)."""
    out: dict[Key, set[str]] = {}
    for note in load_seed_notes(SEED):
        if note.age_days >= note.ttl_days:
            continue  # expired when seeded
        out.setdefault((note.scope, note.scope_key), set()).update(markers_in(note.content))
    return out


@pytest.fixture
def store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "mem")
    seed_store(store, SEED)
    return store


def recall_tool(store: MemoryStore, investigation: str = INV) -> RecallTool:
    return RecallTool(store, investigation_id=investigation)


# --- the seed file itself -----------------------------------------------------------------


def test_seed_file_has_one_unique_marker_per_note():
    notes = load_seed_notes(SEED)
    found = [markers_in(n.content) for n in notes]
    assert all(len(m) == 1 for m in found)
    assert len({next(iter(m)) for m in found}) == len(notes)
    # near-identical keys, a prefix, a case variant, a shared service/investigation key, and an
    # expired note are all present: that is what makes the grid below a real test
    keys = {(n.scope, n.scope_key) for n in notes}
    assert {(Scope.SERVICE, k) for k in ("payment", "payment-gateway", "Payment", "cart")} <= keys
    assert (Scope.SERVICE, INV) in keys and (Scope.INVESTIGATION, INV) in keys
    assert any(n.age_days >= n.ttl_days for n in notes)


# --- the store ----------------------------------------------------------------------------


def test_every_scope_and_key_returns_exactly_its_own_notes(store):
    expected = expected_by_key()
    keys = {k for k, _ in expected} | {"pay", "payment-", "PAYMENT", "unknown", "*", "", "current"}
    seeded_keys = {key for _, key in expected}
    for scope in Scope:
        for key in seeded_keys | keys:
            got = markers_in(" ".join(n.content for n in store.query(scope, key).notes))
            assert got == expected.get((scope, key), set()), (scope, key)


def test_keywords_narrow_within_a_key_and_never_reach_another(store):
    expected = expected_by_key()
    all_markers = set().union(*expected.values())
    words = [*all_markers, "isomarker", "belongs", "service", "investigation", "payment", "cart"]
    for (scope, key), own in expected.items():
        for word in words:
            got = markers_in(
                " ".join(n.content for n in store.query(scope, key, keywords=word).notes)
            )
            assert got <= own, (scope, key, word)
            if word in all_markers and word not in own:
                assert got == set(), f"{word!r} pulled {got} into {scope.value} {key!r}"
        # many keywords at once, including every foreign marker: still only this key's notes
        got = markers_in(
            " ".join(n.content for n in store.query(scope, key, keywords=list(all_markers)).notes)
        )
        assert got == own, (scope, key)


def test_expired_note_is_never_returned_but_is_stored(store):
    assert any("isomarker-expired" in n.content for n in store.all_notes())
    for scope in Scope:
        for key in {k for _, k in expected_by_key()}:
            for kw in (None, "isomarker-expired", "expired", "shipping"):
                notes = store.query(scope, key, keywords=kw).notes
                assert all("isomarker-expired" not in n.content for n in notes)
    live = store.query(Scope.SERVICE, "shipping").notes
    assert [markers_in(n.content) for n in live] == [{"isomarker-shipping"}]


# --- the tool the agent calls --------------------------------------------------------------


async def test_recall_tool_output_has_only_the_requested_notes(store):
    expected = expected_by_key()
    tool = recall_tool(store)
    for scope, key in expected:
        # an investigation key is reached as `current` (or its own id), never another's
        asked = "current" if scope is Scope.INVESTIGATION else key
        if scope is Scope.INVESTIGATION and key != INV:
            continue  # another investigation: covered below
        out, is_error = await tool.call({"scope": scope.value, "scope_key": asked})
        assert not is_error
        assert markers_in(out) == expected[(scope, key)], (scope, key)


async def test_investigation_scope_cannot_name_another_investigation(store):
    tool = recall_tool(store)
    for other in ("payment-failure-50pct", "hard-red-herring-cart-errors"):
        out, is_error = await tool.call({"scope": "investigation", "scope_key": other})
        assert is_error and markers_in(out) == set()
        assert "this investigation only" in out
    own, _ = await tool.call({"scope": "investigation", "scope_key": "current"})
    assert markers_in(own) == {"isomarker-invhealthy"}
    # the same investigation seen from another one gets that one's note
    other_run = recall_tool(store, "payment-failure-50pct")
    out, _ = await other_run.call({"scope": "investigation", "scope_key": "current"})
    assert markers_in(out) == {"isomarker-invpayfail"}
    # an investigation with no notes sees nothing, not the others'
    out, is_error = await recall_tool(store, "never-seeded").call(
        {"scope": "investigation", "scope_key": "current"}
    )
    assert not is_error and markers_in(out) == set() and "No unexpired memory notes" in out


async def test_service_and_investigation_with_the_same_key_stay_apart(store):
    tool = recall_tool(store)
    svc, _ = await tool.call({"scope": "service", "scope_key": INV})
    inv, _ = await tool.call({"scope": "investigation", "scope_key": "current"})
    assert markers_in(svc) == {"isomarker-svcsame"}
    assert markers_in(inv) == {"isomarker-invhealthy"}


async def test_malformed_or_wildcard_keys_return_nothing(store):
    tool = recall_tool(store)
    for key in ("payment OR 1=1", "../cart", "payment\ncart", "pay*", "*", "%", '{"$ne": ""}', ""):
        out, _ = await tool.call({"scope": "service", "scope_key": key})
        assert markers_in(out) == set(), key
    # prefixes and case variants are different keys, so they get their own notes and nothing else
    for key, own in (("pay", set()), ("payment-gateway", {"isomarker-gateway"})):
        out, is_error = await tool.call({"scope": "service", "scope_key": key})
        assert not is_error and markers_in(out) == own
    out, _ = await tool.call({"scope": "service", "scope_key": "payment", "keywords": "gateway"})
    assert markers_in(out) == set()  # `payment` has no note that mentions gateway


async def test_notes_saved_by_a_run_stay_with_their_scope(store):
    ledger = ToolCallLedger()
    ledger.stage([ToolUseBlock(id="t1", name="run_query", input={})])
    ledger.commit()
    save = SaveMemoryTool(store, ledger, run_id="run1", investigation_id=INV)
    for scope, key in (("service", "payment"), ("investigation", "current")):
        text, is_error = await save.call(
            {
                "content": "isomarker-written is a fact saved during this run",
                "scope": scope,
                "scope_key": key,
                "reason": "isolation check",
                "source": "t1",
            }
        )
        assert not is_error, text
    tool = recall_tool(store)

    def seen(out: str) -> set[str]:
        return markers_in(out)

    wrote_service, _ = await tool.call({"scope": "service", "scope_key": "payment"})
    assert "isomarker-written" in seen(wrote_service)
    for key in ("payment-gateway", "Payment", "cart", "checkout"):
        out, _ = await tool.call({"scope": "service", "scope_key": key})
        assert "isomarker-written" not in seen(out), key
    mine, _ = await tool.call({"scope": "investigation", "scope_key": "current"})
    assert "isomarker-written" in seen(mine)
    for inv in ("payment-failure-50pct", "never-seeded"):
        out, _ = await recall_tool(store, inv).call(
            {"scope": "investigation", "scope_key": "current"}
        )
        assert "isomarker-written" not in seen(out), inv


# --- end to end: a scripted run, checked from its step log ----------------------------------


async def test_scripted_run_recalls_only_what_its_scope_allows(store, tmp_path):
    recalls = [
        ({"scope": "service", "scope_key": "payment"}, {"isomarker-payment"}),
        (
            {"scope": "service", "scope_key": "payment-gateway", "keywords": "isomarker-cart"},
            set(),
        ),
        ({"scope": "investigation", "scope_key": "current"}, {"isomarker-invhealthy"}),
        ({"scope": "investigation", "scope_key": "payment-failure-50pct"}, set()),  # refused
        (
            {
                "scope": "service",
                "scope_key": "cart",
                "keywords": "isomarker-payment isomarker-cart",
            },
            {"isomarker-cart"},
        ),
        ({"scope": "service", "scope_key": "Payment"}, {"isomarker-capital"}),
    ]
    script = [
        tool_call_response(("recall", args, f"r{i}")) for i, (args, _) in enumerate(recalls)
    ] + [tool_call_response(("submit_report", report_args(), "sub"))]
    model = ScriptedModelClient(script)
    tools, wrapped = enable_memory(FakeExecutor(), model, store, run_id="r1", investigation_id=INV)
    result = await investigate(
        "storefront looks slow",
        tools=tools,
        model=wrapped,
        config=AgentConfig(max_steps=10),
        log_path=tmp_path / "run.jsonl",
        run_id="r1",
        scenario_id=INV,
    )
    assert result.terminated_reason == TerminatedReason.SUBMITTED

    log = parse_log(tmp_path / "run.jsonl")
    got = {r.tool_call_id: r for r in log.tool_results if r.name == "recall"}
    for i, (args, allowed) in enumerate(recalls):
        record = got[f"r{i}"]
        assert markers_in(record.content) == allowed, (args, record.content)
    assert got["r3"].is_error  # another investigation's key is refused, not served

    spec = load_scenario(default_fixtures_dir() / INV)
    score = memory_score(spec, result.report, log)
    assert score.recall_calls == len(recalls)
    assert score.memories_returned == [
        "iso-payment",
        "iso-inv-healthy",
        "iso-cart",
        "iso-payment-capitalized",
    ]
    assert set(score.recalled_services) == {"payment", "cart", "Payment"}
