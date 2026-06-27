"""crystallization-cadence-v1 — dedup (global), relevance gate, soft dismiss.

Post-K6 Bug 6: crystallization "Shop floor note" proposals spammed unrelated
turns (no relevance gate), re-surfaced verbatim across sessions (session-scoped
dedup reset), and a soft dismiss poisoned the detector's rejection memory.

These tests exercise the three fixes through the REAL helpers:
  Gap 1 — the GLOBAL ever-pushed ledger (cross-session dedup).
  Gap 2 — the deterministic intent->entity relevance gate (+ Dock override).
  Gap 3 — soft "dismissed" status, distinct from hard "rejected".
"""

from __future__ import annotations

import json

import pytest

from grove.memory.push_relevance import is_push_relevant, reset_relevance_cache
from grove.kaizen.renderable import MemoryProposalRenderable


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    # get_hermes_home() honors GROVE_HOME — isolate the global pushed ledger.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _mem_record(content="reportlab renders PDFs", entity_type="DomainFact",
                goal_ref=None, status="pending"):
    return {
        "session_id": "s", "status": status,
        "proposal": {
            "action": "create", "dock_goal_ref": goal_ref,
            "proposed_record": {
                "entity_type": entity_type, "content": content, "confidence": 0.9,
            },
        },
    }


def _bare_agent():
    import run_agent
    return object.__new__(run_agent.AIAgent)


# ── Gap 2 — the relevance map ────────────────────────────────────────────


class TestRelevanceMap:
    def setup_method(self):
        reset_relevance_cache()

    def test_unrelated_intent_suppressed(self):
        # The Bug 6 case: a reportlab/numpy DomainFact on a governance
        # (system_admin) turn. system_admin is intentionally absent from the
        # map → suppress all.
        assert is_push_relevant("system_admin", "DomainFact") is False

    def test_relevant_intent_allowed(self):
        assert is_push_relevant("research", "DomainFact") is True
        assert is_push_relevant("code_generation", "ArchitecturalRule") is True

    def test_unknown_intent_suppressed(self):
        assert is_push_relevant("unknown_intent", "DomainFact") is False
        assert is_push_relevant(None, "DomainFact") is False

    def test_missing_entity_suppressed(self):
        assert is_push_relevant("research", None) is False

    def test_dock_override_requires_all_four_conditions(self):
        # crystallization-cadence-v1.1: the override fires only when the turn
        # ENGAGES the proposal's goal — active + aligned (direct/indirect) +
        # engaged goal == goal_ref. All four must hold.
        assert is_push_relevant(
            "system_admin", "DomainFact", goal_ref="g1", active_goal_ids={"g1"},
            goal_alignment="direct", engaged_goal_id="g1",
        ) is True
        # (3) fails — orthogonal alignment (the smoke-test scheduling case).
        assert is_push_relevant(
            "system_admin", "DomainFact", goal_ref="g1", active_goal_ids={"g1"},
            goal_alignment="orthogonal", engaged_goal_id="g1",
        ) is False
        # (4) fails — the turn engages a DIFFERENT active goal.
        assert is_push_relevant(
            "system_admin", "DomainFact", goal_ref="g1", active_goal_ids={"g1", "g2"},
            goal_alignment="direct", engaged_goal_id="g2",
        ) is False
        # (2) fails — the goal is not active.
        assert is_push_relevant(
            "system_admin", "DomainFact", goal_ref="g1", active_goal_ids={"g2"},
            goal_alignment="direct", engaged_goal_id="g1",
        ) is False
        # The old v1 over-permissive call (active-only, no alignment) no longer
        # overrides — this is the bug that surfaced reportlab on every turn.
        assert is_push_relevant(
            "system_admin", "DomainFact", goal_ref="g1", active_goal_ids={"g1"},
        ) is False


# ── Gap 1 + Gap 2 — the agent push gate ──────────────────────────────────


class TestPushRelevanceOk:
    def setup_method(self):
        reset_relevance_cache()

    def test_routing_proposal_bypasses_memory_gate(self, grove_home):
        class _Routing:
            type = "routing"
            short_id = "r1"
        assert _bare_agent()._push_relevance_ok(_Routing(), None, set()) is True

    def test_memory_suppressed_on_unrelated_intent(self, grove_home):
        r = MemoryProposalRenderable(_mem_record(entity_type="DomainFact"))
        assert _bare_agent()._push_relevance_ok(r, "system_admin", set()) is False

    def test_memory_allowed_on_relevant_intent(self, grove_home):
        r = MemoryProposalRenderable(_mem_record(entity_type="DomainFact"))
        assert _bare_agent()._push_relevance_ok(r, "research", set()) is True

    def test_global_ledger_blocks_resurface(self, grove_home):
        from tools.flywheel_review_tool import _mark_pushed_memory_id
        r = MemoryProposalRenderable(_mem_record(entity_type="DomainFact"))
        agent = _bare_agent()
        assert agent._push_relevance_ok(r, "research", set()) is True
        # Once globally marked as pushed, it never auto-pushes again — even on
        # a relevant turn.
        _mark_pushed_memory_id(r.short_id)
        assert agent._push_relevance_ok(r, "research", set()) is False

    def test_umbrella_goal_suppressed_on_orthogonal_turn(self, grove_home):
        # crystallization-cadence-v1.1 regression: the exact smoke-test bug —
        # a ProjectState tagged to the always-active umbrella goal
        # (hermes-autonomaton) must NOT override the gate on an orthogonal
        # 'scheduling' turn ("what's on my calendar tomorrow?").
        r = MemoryProposalRenderable(
            _mem_record(entity_type="ProjectState", goal_ref="hermes-autonomaton"))
        agent = _bare_agent()
        assert agent._push_relevance_ok(
            r, "scheduling", {"hermes-autonomaton"},
            goal_alignment="orthogonal", engaged_goal_id="hermes-autonomaton",
        ) is False
        # ...but on a turn that DIRECTLY engages that goal, the override fires.
        assert agent._push_relevance_ok(
            r, "scheduling", {"hermes-autonomaton"},
            goal_alignment="direct", engaged_goal_id="hermes-autonomaton",
        ) is True


# ── Gap 1 — the global ever-pushed ledger (cross-session) ────────────────


class TestGlobalPushedLedger:
    def test_mark_and_read(self, grove_home):
        from tools.flywheel_review_tool import (
            _mark_pushed_memory_id, _read_pushed_memory_ids,
        )
        assert _read_pushed_memory_ids() == set()
        _mark_pushed_memory_id("abc123")
        assert "abc123" in _read_pushed_memory_ids()

    def test_survives_across_session_boundary(self, grove_home):
        # Unlike .push_cadence.json (session-keyed, resets on a new session),
        # the global ledger does NOT reset — this is the cross-session leak fix.
        from tools.flywheel_review_tool import (
            _mark_pushed_memory_id, _read_pushed_memory_ids, _read_push_cadence,
        )
        _mark_pushed_memory_id("xyz")
        assert _read_push_cadence("a-brand-new-session")["surfaced_ids"] == set()
        assert "xyz" in _read_pushed_memory_ids()


# ── SPEC test 1 — same content hash does not surface twice ───────────────


class TestContentHashDedup:
    def setup_method(self):
        reset_relevance_cache()

    def test_identical_content_same_short_id(self):
        a = MemoryProposalRenderable(_mem_record(content="same fact"))
        b = MemoryProposalRenderable(_mem_record(content="same fact"))
        assert a.short_id == b.short_id  # content-addressable (sha256)
        c = MemoryProposalRenderable(_mem_record(content="different fact"))
        assert c.short_id != a.short_id

    def test_same_content_not_surfaced_twice(self, grove_home):
        from tools.flywheel_review_tool import _mark_pushed_memory_id
        a = MemoryProposalRenderable(_mem_record(content="dup"))
        b = MemoryProposalRenderable(_mem_record(content="dup"))
        agent = _bare_agent()
        assert agent._push_relevance_ok(a, "research", set()) is True
        _mark_pushed_memory_id(a.short_id)  # surfaced once
        # The duplicate (identical content hash) is now suppressed.
        assert agent._push_relevance_ok(b, "research", set()) is False


# ── SPEC test 4 — the cooldown guard fires ───────────────────────────────


class TestCooldownFires:
    def test_cooldown_suppresses_next_n_turns(self, grove_home):
        import run_agent
        from tools.flywheel_review_tool import _read_push_cadence, _write_push_cadence
        n = run_agent.AIAgent._PUSH_COOLDOWN_TURNS
        s = "sess"
        _write_push_cadence(s, last_push_turn=1, surfaced_ids=set(),
                            surfaced_connectors=set())
        # turns 2..n are within the cooldown window → suppressed.
        for turn in range(2, 1 + n):
            last = _read_push_cadence(s)["last_push_turn"]
            assert last is not None and (turn - last) < n
        # turn 1+n is the first turn outside the window → may push again.
        assert not ((1 + n) - 1 < n)


# ── Gap 3 — soft dismiss vs hard reject ──────────────────────────────────


def _proposal(content="A fact.", entity_type="DomainFact"):
    return {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {
            "entity_type": entity_type, "content": content,
            "confidence": 0.9, "justification": "matters",
        },
    }


def _stage(base, proposal, status="pending"):
    rec = {"session_id": "s", "status": status,
           "timestamp": "2026-06-01T00:00:00+00:00", "proposal": proposal}
    with open(base / "memory_proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _records(base):
    text = (base / "memory_proposals.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


class TestSoftDismiss:
    def test_dismiss_sets_dismissed_status(self, tmp_path):
        from grove.memory.cli import cli_memory_dismiss, memory_proposal_short_id
        prop = _proposal()
        _stage(tmp_path, prop)
        rc = cli_memory_dismiss(memory_proposal_short_id(prop), base_dir=tmp_path)
        assert rc == 0
        recs = _records(tmp_path)
        assert recs[0]["status"] == "dismissed"

    def test_dismissed_excluded_from_pending(self, tmp_path):
        # Loses push eligibility — _pending is pending-only, so it never feeds
        # the push surface again.
        from grove.memory.cli import (
            cli_memory_dismiss, _pending, _base, memory_proposal_short_id,
        )
        prop = _proposal()
        _stage(tmp_path, prop)
        cli_memory_dismiss(memory_proposal_short_id(prop), base_dir=tmp_path)
        assert _pending(_base(tmp_path)) == []

    def test_dismiss_does_not_record_rejection(self, tmp_path):
        # The detector's _recently_rejected reads status=="rejected" only — a
        # soft dismiss must NOT appear as a rejection (don't blind extraction).
        from grove.memory.cli import cli_memory_dismiss, memory_proposal_short_id
        prop = _proposal()
        _stage(tmp_path, prop)
        cli_memory_dismiss(memory_proposal_short_id(prop), base_dir=tmp_path)
        assert all(r["status"] != "rejected" for r in _records(tmp_path))

    def test_reject_still_sets_rejected(self, tmp_path):
        # Regression: hard reject is unchanged — permanent, feeds rejection memory.
        from grove.memory.cli import cli_memory_reject, memory_proposal_short_id
        prop = _proposal()
        _stage(tmp_path, prop)
        rc = cli_memory_reject(memory_proposal_short_id(prop), base_dir=tmp_path)
        assert rc == 0
        assert _records(tmp_path)[0]["status"] == "rejected"

    def test_dismiss_proposal_tool_routes_to_memory(self, tmp_path, monkeypatch):
        # The tool resolves a bare id to the memory store and dismisses it.
        from grove.memory.cli import memory_proposal_short_id
        import grove.memory.cli as memory_cli
        monkeypatch.setattr(memory_cli, "_base", lambda _b: tmp_path)
        prop = _proposal()
        _stage(tmp_path, prop)
        from tools.flywheel_review_tool import dismiss_proposal
        out = json.loads(dismiss_proposal(memory_proposal_short_id(prop)))
        assert out["success"] is True
        assert out["kind"] == "memory"
        assert _records(tmp_path)[0]["status"] == "dismissed"
