"""kaizen-proposal-surface-unification-v1 — one renderer, one push, one voice.

Covers: the KaizenRenderable protocol + memory adapter, the render-only
registry, compose_offering across both types, _PUSH_PRIORITY ordering, the
merged push surface (eligibility + dedup + priority), review_proposals'
single code path, and the removal of the old _append_memory_offer.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home

from grove.eval.proposal_queue import (
    RoutingProposal,
    append as queue_append,
    compute_proposal_id,
)
from grove.kaizen.renderable import KaizenRenderable, MemoryProposalRenderable
from grove.memory.cli import memory_proposal_short_id

_TS = "2026-06-01T00:00:00+00:00"


def _routing(intent="code_generation", created_at=None):
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    payload = {"rule": "ratchet_promoted_t1", "add_intents": [intent]}
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=("t1",)),
        type="routing_adjustment", payload=payload, evidence=("t1",),
        eval_hash="", created_at=created_at, source_patterns=("c1",),
    )


def _memory_record(content="Operator prefers the CLI.", confidence=0.9,
                   status="pending"):
    proposal = {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "OperatorPreference", "content": content,
                            "confidence": confidence, "justification": "j"},
    }
    return {"session_id": "s", "status": status, "timestamp": _TS, "proposal": proposal}


def _stage_routing(**kw):
    p = _routing(**kw)
    queue_append(p)
    return p


def _stage_memory(**kw):
    rec = _memory_record(**kw)
    path = Path(get_hermes_home()) / "memory_proposals.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return memory_proposal_short_id(rec["proposal"])


def _agent():
    # crystallization-cadence-v1: a REAL AIAgent instance (was a
    # SimpleNamespace) so the relevance-gate method (_push_relevance_ok) and the
    # _PUSH_COOLDOWN_TURNS class attribute resolve on the unbound push call.
    import run_agent
    a = object.__new__(run_agent.AIAgent)
    a.session_start = datetime.now() - timedelta(hours=1)
    a.session_id = ""
    a._user_turn_count = 0
    a._surfaced_proposal_ids = set()
    # portal-links P1 (a5139bf29): the push path resolves the portal base URL
    # from self._runtime_ctx.config (run_agent.py _config_load_or →
    # _runtime_ctx.config, run_agent.py:2775-2778). A fixture without it makes
    # compose_offering's resolve_portal_base_url branch AttributeError and the
    # whole push get swallowed (run_agent.py:5629-5630). An empty config dict is
    # the minimal real shape — resolve_portal_base_url(config={}) yields the
    # loopback portal base.
    a._runtime_ctx = types.SimpleNamespace(config={})
    return a


@pytest.fixture(autouse=True)
def _relevant_intent(monkeypatch):
    # crystallization-cadence-v1 (Gap 2): memory pushes are now relevance-gated
    # by intent_class. The fixtures here stage OperatorPreference memories, so
    # default the turn's classification to a relevant intent ('conversation')
    # — tests that need a DIFFERENT/irrelevant intent override this locally.
    from grove import providers
    monkeypatch.setattr(
        providers, "_last_classification",
        types.SimpleNamespace(intent_class="conversation"), raising=False,
    )


# 1. Protocol conformance

def test_routing_and_memory_satisfy_protocol():
    assert isinstance(_routing(), KaizenRenderable)
    assert isinstance(MemoryProposalRenderable(_memory_record()), KaizenRenderable)


# 2. Memory adapter shape

def test_memory_adapter_shape():
    rec = _memory_record(content="X.")
    r = MemoryProposalRenderable(rec)
    assert r.type == "memory_context"
    assert r.short_id == memory_proposal_short_id(rec["proposal"])
    assert r.is_push_eligible(None) is True              # pending → eligible
    assert MemoryProposalRenderable(
        _memory_record(status="rejected")).is_push_eligible(None) is False


# 3. Render registry

def test_render_registry_has_memory_and_routing():
    from grove.flywheel_cli import get_renderer
    assert callable(get_renderer("memory_context"))
    assert callable(get_renderer("routing_adjustment"))
    with pytest.raises(ValueError):
        get_renderer("nonexistent_type")


# 4. compose_offering with the memory adapter — Kaizen-voiced, no CLI

def test_compose_offering_memory_adapter():
    from grove.flywheel_cli import compose_offering
    rec = _memory_record(content="Take Flight uses Notion.")
    note = compose_offering(MemoryProposalRenderable(rec), is_push=True)
    assert "Take Flight uses Notion." in note
    assert "`" not in note and "flywheel" not in note
    assert "approve" in note.lower() and "dismiss" in note.lower()


# 5. compose_offering with routing — unchanged

def test_compose_offering_routing_unchanged():
    from grove import flywheel_cli
    p = _routing()
    assert flywheel_cli.compose_offering(p, is_push=False) == \
        flywheel_cli._summary_routing_adjustment(p)
    push = flywheel_cli.compose_offering(p, is_push=True)
    assert push.startswith(flywheel_cli._OFFERING_PUSH_PREFIX)
    assert "code_generation" in push


# 6. _PUSH_PRIORITY ordering

def test_push_priority_memory_above_routing():
    from grove.flywheel_cli import _PUSH_PRIORITY
    assert _PUSH_PRIORITY["memory_context"] == 1
    assert _PUSH_PRIORITY["routing_adjustment"] == 2
    assert _PUSH_PRIORITY["memory_context"] < _PUSH_PRIORITY["routing_adjustment"]


# 7. Merged push — higher priority (memory) surfaces first

def test_merged_push_memory_before_routing():
    """portal-reader-contract-fix-v1 + a5139bf29: memory proposals now carry
    requires_portal_review, so compose_offering emits the compact portal-link
    NOTIFICATION (rendering.py:567-572) rather than inline memory content. The
    chosen memory proposal (priority 1) still wins over routing — proven by the
    portal-link push appearing while routing content ('code_generation') does
    not surface this turn."""
    from run_agent import AIAgent
    _stage_routing()                 # eligible (created now)
    _stage_memory(content="Memory wins.")
    agent = _agent()
    out = AIAgent._append_pending_offer(agent, "Answer.")
    assert "New proposals await your review" in out   # memory (pri 1) → portal link
    assert "/portal#fragments/proposals/pending" in out
    assert "code_generation" not in out           # routing not surfaced this turn


# 8. Merged push — per-type eligibility

def test_merged_push_routing_eligibility_enforced():
    from run_agent import AIAgent
    # routing created BEFORE session_start → ineligible; no memory
    _stage_routing(created_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    out = AIAgent._append_pending_offer(_agent(), "Answer.")
    assert out == "Answer."                        # nothing eligible


def test_merged_push_memory_eligible_when_relevant():
    # crystallization-cadence-v1: prior-session memory is eligible despite no
    # session window — WHEN relevant to the turn's intent (OperatorPreference
    # on a 'conversation' turn, set by the autouse fixture).
    """portal-reader-contract-fix-v1 + a5139bf29: the eligible memory push now
    surfaces as the compact portal-link notification (rendering.py:567-572), not
    inline content — the assertion checks the push appears, preserving intent."""
    from run_agent import AIAgent
    _stage_memory(content="Prior-session memory.")
    out = AIAgent._append_pending_offer(_agent(), "Answer.")
    assert "New proposals await your review" in out
    assert "/portal#fragments/proposals/pending" in out


def test_merged_push_memory_suppressed_when_irrelevant(monkeypatch):
    # crystallization-cadence-v1 (Gap 2): the SAME memory is suppressed on an
    # unrelated intent (system_admin is absent from the relevance map) — this
    # is the reportlab/numpy-on-a-governance-turn fix.
    from grove import providers
    from run_agent import AIAgent
    monkeypatch.setattr(
        providers, "_last_classification",
        types.SimpleNamespace(intent_class="system_admin"), raising=False,
    )
    _stage_memory(content="Prior-session memory.")
    out = AIAgent._append_pending_offer(_agent(), "Answer.")
    assert out == "Answer."                        # suppressed, not surfaced


# 9. Merged push — shown-set dedup across types

def test_merged_push_dedup():
    """portal-reader-contract-fix-v1 + a5139bf29: the first push surfaces as the
    compact portal-link notification (rendering.py:567-572); dedup still holds —
    the second turn re-surfaces nothing (shown-set), returning the bare answer."""
    from run_agent import AIAgent
    _stage_memory(content="Only one.")
    agent = _agent()
    first = AIAgent._append_pending_offer(agent, "A.")
    assert "New proposals await your review" in first
    second = AIAgent._append_pending_offer(agent, "B.")
    assert second == "B."                          # already shown


# 10. Merged push — nothing pending

def test_merged_push_nothing_pending():
    from run_agent import AIAgent
    assert AIAgent._append_pending_offer(_agent(), "Answer.") == "Answer."


# 11 & 12. review_proposals — one path, both types

def test_review_unified_both_types():
    """ccaaf3ae4 (kaizen-no-id-display): review_proposals now returns
    out['proposals'] as ordinal-tagged DICTS {'ordinal', 'display', 'id'}
    (flywheel_review_tool.py:385-392), not display strings. Both types are still
    surfaced/reviewable through the one path — asserted against the 'display'
    (type-tagged render) and 'id' (short id) fields of each dict."""
    from tools.flywheel_review_tool import review_proposals
    _stage_routing()
    short = _stage_memory(content="A learned fact.")
    out = json.loads(review_proposals())
    assert out["pending_count"] == 2
    blob = " ".join(f'{p["display"]} {p["id"]}' for p in out["proposals"])
    assert "code_generation" in blob              # routing
    assert "A learned fact." in blob              # memory
    assert short in blob
    assert "[memory_context]" in blob and "[routing_adjustment]" in blob


def test_review_none():
    from tools.flywheel_review_tool import review_proposals
    out = json.loads(review_proposals())
    assert out["pending_count"] == 0
    assert "No pending proposals" in out.get("message", "")


# 13. The old separate memory push method is gone

def test_append_memory_offer_removed():
    from run_agent import AIAgent
    assert not hasattr(AIAgent, "_append_memory_offer")
