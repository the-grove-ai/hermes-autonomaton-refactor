"""artifact-continuation-v1 P2 — continuation dispatch entry + lineage threading.

Covers: the template-locked frame (exact-string pin), parent_artifact_ids
merge on artifact_written (parented / unparented / degrade-loud), the
one-shot stash consume (source pin), the store-then-deny Stage-04 handler
(row stored zone-labeled, payload carrier complete, deny returned, failure
still denies), the dispatch entry result shape, and confirm-time emission
via the core helper (full payload / pre-existing-shape defaults / failure
resilience / no-event-on-failed-dispatch).
"""

from __future__ import annotations

import json
import types

import pytest

from grove import continuation
from grove.artifact_identity import (
    artifact_id,
    canonical_artifact_path,
    emit_approved_artifact_written,
)
from grove.continuation import (
    CONTINUATION_FRAME,
    PendingStoreSovereignHandler,
    dispatch_continuation_turn,
)
from grove.dispatcher import Dispatcher
from grove.kaizen_ledger import KaizenLedger
from grove.utils import fs_utils


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(home / "wiki"))
    return home


class _FakeLedger:
    def __init__(self):
        self.events = []

    def record(self, event_type, **fields):
        self.events.append((event_type, fields))
        return {}


def _write_intent(path, call_id="c1"):
    return types.SimpleNamespace(
        tool_name="write_file", arguments={"path": path, "content": "x"},
        call_id=call_id,
    )


def _shell(monkeypatch):
    d = Dispatcher.__new__(Dispatcher)
    d._last_loaded_primary_slug = None
    d._current_turn_id = "sess#4"
    d._current_turn_classification = types.SimpleNamespace(
        intent_class="research"
    )
    d._current_turn_tool_invocations = []
    monkeypatch.setattr(d, "_fleet_governance", lambda: [], raising=False)
    monkeypatch.setattr(fs_utils, "is_write_allowed", lambda *a, **k: True)
    return d


# ── frame ────────────────────────────────────────────────────────────────────


def test_frame_constant_exact():
    assert CONTINUATION_FRAME == (
        "Operator continuation request over existing artifact(s).\n"
        "Context files (read each before acting):\n"
        "{artifact_paths}\n"
        "\n"
        "Operator instruction (verbatim):\n"
        "{instruction}"
    )


# ── lineage merge at the emission site ───────────────────────────────────────


def test_parented_turn_emits_parent_list(monkeypatch, tmp_path):
    d = _shell(monkeypatch)
    d._current_turn_parent_artifact_ids = ["a" * 16, "b" * 16]
    ledger = _FakeLedger()
    out = d._enforce_write_confinement(
        [_write_intent(str(tmp_path / "child.md"))], None, ledger,
    )
    assert out is None
    events = [f for t, f in ledger.events if t == "artifact_written"]
    assert len(events) == 1
    assert events[0]["parent_artifact_ids"] == ["a" * 16, "b" * 16]


def test_unparented_turn_emits_empty_list(monkeypatch, tmp_path):
    # Chat-path regression: no stash attribute at all → field present, [].
    d = _shell(monkeypatch)
    ledger = _FakeLedger()
    d._enforce_write_confinement(
        [_write_intent(str(tmp_path / "plain.md"))], None, ledger,
    )
    events = [f for t, f in ledger.events if t == "artifact_written"]
    assert events[0]["parent_artifact_ids"] == []


def test_malformed_stash_degrades_loud_event_still_files(
    monkeypatch, tmp_path, caplog
):
    class _Bad:
        def __iter__(self):
            raise RuntimeError("poisoned stash")

        def __bool__(self):
            return True

    d = _shell(monkeypatch)
    d._current_turn_parent_artifact_ids = _Bad()
    ledger = _FakeLedger()
    with caplog.at_level("WARNING"):
        out = d._enforce_write_confinement(
            [_write_intent(str(tmp_path / "x.md"))], None, ledger,
        )
    assert out is None
    events = [f for t, f in ledger.events if t == "artifact_written"]
    assert events[0]["parent_artifact_ids"] == []  # degraded, not dropped
    assert any(
        "parent lineage merge failed" in r.getMessage() for r in caplog.records
    )


def test_reset_block_consumes_one_shot_slot():
    """Source pin: turn setup initializes the per-turn stash from the
    one-shot ``_next_turn_parent_artifact_ids`` slot and clears the slot,
    in the SAME reset region as the P3 siblings."""
    import inspect

    import grove.dispatcher as dispatcher_mod

    src = inspect.getsource(dispatcher_mod)
    anchor = src.find("agent._artifact_links_notice = None")
    consume = src.find(
        "self._current_turn_parent_artifact_ids = list(", anchor
    )
    clear = src.find("self._next_turn_parent_artifact_ids = None", anchor)
    assert anchor != -1 and consume != -1 and clear != -1
    # substrate-citation-v1 P4 (cross-sprint mechanical correction) — the
    # per-turn reset region grew by the P3-sibling cellar-citation resets
    # (_artifact_links_rendered + _cellar_* stashes), which legitimately sit
    # between this anchor and the parent-slot consume/clear. Bounds relaxed from
    # 900/1200 to accommodate (current 1681/1818); the SEMANTIC guard — consume +
    # clear remain in the same reset region, not drifted into another method —
    # is preserved with headroom.
    assert 0 < consume - anchor < 2500
    assert 0 < clear - anchor < 2700


# ── store-then-deny handler ──────────────────────────────────────────────────


def _yellow_halt(path):
    return types.SimpleNamespace(
        intents=[_write_intent(path)],
        triggering_index=0,
        zone="yellow",
        pattern_key=None,
    )


def test_yellow_halt_stores_row_and_denies(grove_home, tmp_path):
    from grove.eval import proposal_queue as pq
    from grove.red_pending_store import get_red_pending_store

    target = tmp_path / "refined.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = PendingStoreSovereignHandler(
        parent_artifact_ids=["c" * 16]
    )
    fake_dispatcher = types.SimpleNamespace(
        _current_turn_id="portal_x#1",
        _last_loaded_primary_slug=None,
        _current_turn_classification=types.SimpleNamespace(
            intent_class="research"
        ),
    )
    handler.bind(fake_dispatcher)

    disposition = handler(_yellow_halt(str(target)))

    assert disposition == "deny"  # never four-choice, never allow
    assert len(handler.stored) == 1
    pid = handler.stored[0]["proposal_id"]
    assert handler.stored[0]["zone"] == "yellow"
    # Durable store row exists (claimable exactly once).
    entry = get_red_pending_store().pop(pid)
    assert entry is not None and entry.tool_name == "write_file"
    # Queue-row payload carries the full identity context.
    rows = [p for p in pq.read_all() if p.proposal_id.endswith(pid)]
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["zone"] == "yellow"
    assert payload["parent_artifact_ids"] == ["c" * 16]
    assert payload["turn_id"] == "portal_x#1"
    assert payload["active_primary_skill_slug"] is None
    assert payload["intent_class"] == "research"
    assert payload["tool"] == "write_file"


def test_store_failure_still_denies_loudly(grove_home, tmp_path, monkeypatch, caplog):
    import grove.red_pending_store as rps

    def _boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(rps, "get_red_pending_store", _boom)
    handler = PendingStoreSovereignHandler()
    with caplog.at_level("WARNING"):
        disposition = handler(_yellow_halt(str(tmp_path / "x.md")))
    assert disposition == "deny"  # fail-closed
    assert handler.stored == []
    assert any(
        "pending-store write failed" in r.getMessage() for r in caplog.records
    )


# ── dispatch entry ───────────────────────────────────────────────────────────


class _FakeAgent:
    def __init__(self):
        self.prompts = []

    def run_conversation(self, prompt):
        self.prompts.append(prompt)
        return {"final_response": "done."}


class _FakeDispatcher:
    last = None

    def __init__(self, *, sovereign_prompt_handler=None, agent_kwargs=None):
        _FakeDispatcher.last = self
        self.sovereign_prompt_handler = sovereign_prompt_handler
        self.agent_kwargs = agent_kwargs or {}
        self.agent = _FakeAgent()
        self._current_turn_id = (
            f"{self.agent_kwargs.get('session_id')}#1"
        )


def test_dispatch_entry_frame_and_result_shape(grove_home, tmp_path, monkeypatch):
    # Seed one ledger-known artifact to act as parent.
    parent = tmp_path / "parent.md"
    parent.write_text("p", encoding="utf-8")
    canonical = canonical_artifact_path(str(parent))
    pid = artifact_id(canonical)
    KaizenLedger("seed").record(
        "artifact_written", path=canonical, artifact_id=pid, turn_id="s#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
        parent_artifact_ids=[],
    )

    monkeypatch.setattr(continuation, "Dispatcher", _FakeDispatcher)
    monkeypatch.setattr(
        continuation, "_resolve_runtime_agent_kwargs",
        lambda: dict(model="m", api_key="k", base_url=None, provider="p"),
    )
    result = dispatch_continuation_turn("Sharpen the summary.", [pid])

    d = _FakeDispatcher.last
    # Template-locked frame, instruction verbatim, ledger-resolved path.
    assert d.agent.prompts == [CONTINUATION_FRAME.format(
        artifact_paths=canonical, instruction="Sharpen the summary.",
    )]
    # Origin marking per the cron precedent.
    assert d.agent_kwargs["platform"] == "portal"
    assert d.agent_kwargs["quiet_mode"] is True
    assert d.agent_kwargs["session_id"].startswith("portal_")
    # One-shot lineage slot set for the turn-setup consume.
    assert d._next_turn_parent_artifact_ids == [pid]
    # Handler bound to the dispatcher.
    assert d.sovereign_prompt_handler._dispatcher is d
    # Result shape.
    assert result["response_text"] == "done."
    assert result["turn_id"] == d._current_turn_id
    assert result["halted"] is False
    assert result["pending_items"] == []
    assert result["artifact_ids_written"] == []


def test_dispatch_entry_resolves_runtime_provider(grove_home, tmp_path, monkeypatch):
    """Live-prove regression: with no caller model override, the entry
    resolves model + provider per the cron precedent BEFORE construction
    (an empty model raises ProviderDetectionError in prod)."""
    parent = tmp_path / "p.md"
    parent.write_text("p", encoding="utf-8")
    canonical = canonical_artifact_path(str(parent))
    pid = artifact_id(canonical)
    KaizenLedger("seed2").record(
        "artifact_written", path=canonical, artifact_id=pid, turn_id="s#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
        parent_artifact_ids=[],
    )
    monkeypatch.setattr(continuation, "Dispatcher", _FakeDispatcher)
    monkeypatch.setattr(
        continuation, "_resolve_runtime_agent_kwargs",
        lambda: dict(model="m-x", api_key="k", base_url="b", provider="p"),
    )
    dispatch_continuation_turn("go", [pid])
    kw = _FakeDispatcher.last.agent_kwargs
    assert kw["model"] == "m-x" and kw["provider"] == "p"
    # Caller override wins outright (no resolution call needed).
    monkeypatch.setattr(
        continuation, "_resolve_runtime_agent_kwargs",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    dispatch_continuation_turn("go", [pid], agent_kwargs={"model": "override"})
    assert _FakeDispatcher.last.agent_kwargs["model"] == "override"


def test_dispatch_entry_unknown_parent_fails_loud(grove_home, monkeypatch):
    monkeypatch.setattr(continuation, "Dispatcher", _FakeDispatcher)
    with pytest.raises(LookupError, match="unknown artifact id"):
        dispatch_continuation_turn("x", ["f" * 16])


# ── confirm-time emission (core helper) ──────────────────────────────────────


_FULL_PAYLOAD = {
    "zone": "yellow",
    "parent_artifact_ids": ["d" * 16],
    "turn_id": "portalsess#3",
    "active_primary_skill_slug": None,
    "intent_class": "research",
    "tool": "write_file",
}


def test_approved_emission_full_payload(grove_home, tmp_path):
    target = str(tmp_path / "approved.md")
    events = emit_approved_artifact_written(
        "write_file", [target], dict(_FULL_PAYLOAD),
    )
    assert len(events) == 1
    ev = events[0]
    canonical = canonical_artifact_path(target)
    assert ev["path"] == canonical
    assert ev["artifact_id"] == artifact_id(canonical)
    assert ev["turn_id"] == "portalsess#3"       # ORIGINAL minting turn
    assert ev["parent_artifact_ids"] == ["d" * 16]
    assert ev["intent_class"] == "research"
    assert ev["tool"] == "write_file"
    # Filed under the minting turn's session ledger (turn_id prefix).
    persisted = KaizenLedger("portalsess").events_by_type("artifact_written")
    assert [e["artifact_id"] for e in persisted] == [ev["artifact_id"]]


def test_approved_emission_pre_existing_row_defaults(grove_home, tmp_path):
    # A pre-existing RED row has payload {"zone": "red"} only → honest defaults.
    events = emit_approved_artifact_written(
        "write_file", [str(tmp_path / "old.md")], {"zone": "red"},
    )
    assert len(events) == 1
    assert events[0]["turn_id"] is None
    assert events[0]["parent_artifact_ids"] == []
    assert events[0]["active_primary_skill_slug"] is None
    assert events[0]["intent_class"] is None


def test_approved_emission_failure_never_raises(grove_home, tmp_path, monkeypatch, caplog):
    from grove import kaizen_ledger as kl

    monkeypatch.setattr(
        kl.KaizenLedger, "record",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    with caplog.at_level("WARNING"):
        events = emit_approved_artifact_written(
            "write_file", [str(tmp_path / "x.md")], dict(_FULL_PAYLOAD),
        )
    assert events == []
    assert any(
        "approved-write EMISSION failed" in r.getMessage()
        for r in caplog.records
    )


def test_no_targets_no_events(grove_home):
    assert emit_approved_artifact_written("terminal", [], {}) == []


def test_confirm_emission_only_on_success_branch():
    """Structural pin (ruling 6, 'failed dispatch → no event'): the confirm
    handler invokes the core emission helper INSIDE the ``result.success``
    branch only — a failed dispatch can never file an identity event."""
    import inspect

    from grove.api import actions as actions_mod

    src = inspect.getsource(actions_mod.handle_red_proposal_confirm)
    success_idx = src.find('if result.get("success"):')
    emit_idx = src.find("emit_approved_artifact_written")
    fail_idx = src.find('reason = result.get("reason")')
    assert success_idx != -1 and emit_idx != -1 and fail_idx != -1
    assert success_idx < emit_idx < fail_idx  # inside the success branch


def test_approve_result_carries_write_targets(grove_home, tmp_path):
    # approve_red_proposal derives write_targets via the seam's extractor —
    # prove the returned targets for a stored write_file row.
    from grove.effect_signature import canonical_effect_signature
    from grove.red_pending_store import (
        PendingRedProposal,
        RedPendingStore,
        action_proposal_id,
        approve_red_proposal,
        prepare_execute_arguments,
    )

    # /tmp is in the confinement union and NOT under the file tool's
    # sensitive-path prefixes (macOS pytest tmp_path resolves to
    # /private/var/... which IS sensitive-walled — environmental).
    import os as _os
    from pathlib import Path as _Path

    target = _Path(f"/tmp/ac-p2-row-{_os.getpid()}.md")
    if target.exists():
        target.unlink()
    args = prepare_execute_arguments(
        "write_file", {"path": str(target), "content": "body"},
    )
    sig = canonical_effect_signature("write_file", args)
    pid = action_proposal_id(sig)
    store = RedPendingStore(db_path=grove_home / "red_pending_test.db")
    store.put(PendingRedProposal(
        proposal_id=pid, tool_name="write_file", arguments=args,
        effect_signature=sig, description="d", rationale="", created_at="t",
    ))
    try:
        result = approve_red_proposal(pid, store)
        assert result["success"] is True, result
        assert result["write_targets"] == [str(target)]
        assert target.read_text(encoding="utf-8") == "body"
    finally:
        if target.exists():
            target.unlink()
