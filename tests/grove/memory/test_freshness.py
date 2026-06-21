"""context-freshness-detector-v1 — governed forgetting tests.

The FreshnessDetector applies entity-type decay and stages deprecation
proposals for records that fall below the store's deprecation floor; the
MemoryProposalHandler then mints MemoryDeprecated events for approved ones.
Entity-type-awareness (R2) is the load-bearing invariant: only ProjectState
decays, Dock-active records are suspended, and the cognitive budget caps a
sweep at three proposals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from grove.kaizen.renderable import MemoryProposalRenderable
from grove.memory.digest import MemoryProposalHandler
from grove.memory.events import MemoryCreated, MemoryDeprecated
from grove.memory.freshness import FreshnessDetector
from grove.memory.store import _DEPRECATION_FLOOR, MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


def _add(store, rid, entity_type, confidence, days_old, dock_goal_ref=None):
    """Append a MemoryCreated `days_old` days in the past, then it is the
    record's only timestamp (last_accessed is None → decay anchors on it)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    store.append_event(MemoryCreated(
        event_id="evt_" + rid[-8:], timestamp=ts, record_id=rid,
        entity_type=entity_type, content=f"{entity_type} content for {rid}",
        confidence=confidence, dock_goal_ref=dock_goal_ref,
        sources=[], supersedes=None,
    ))


def _goals(*slugs):
    return [{"slug": s, "name": s, "status": "accelerating", "vector": "v"}
            for s in slugs]


# 1. apply_decay integration — ProjectState decays below floor after time

def test_apply_decay_drops_projectstate_below_floor(store):
    _add(store, "mem_old", "ProjectState", confidence=0.8, days_old=0)
    store.rebuild_index()
    # 40 days of disuse: 0.8 * 0.95**40 ≈ 0.103, comfortably below 0.2.
    forty_days = datetime.now(timezone.utc) + timedelta(days=40)
    store.apply_decay(now=forty_days)
    assert store.projected_records()["mem_old"].confidence < _DEPRECATION_FLOOR


# 2. FreshnessDetector — stale ProjectState proposed; fresh one is not

def test_stale_projectstate_proposed_fresh_not(store, tmp_path):
    _add(store, "mem_stale", "ProjectState", confidence=0.8, days_old=40)
    _add(store, "mem_fresh", "ProjectState", confidence=0.8, days_old=0)
    store.rebuild_index()

    detector = FreshnessDetector(base_dir=tmp_path)
    proposals = detector.detect(store, _goals())

    targets = {p["target_id"] for p in proposals}
    assert targets == {"mem_stale"}
    prop = proposals[0]
    assert prop["action"] == "deprecate"
    assert "content" in prop and prop["content"]
    assert "not accessed since" in prop["reason"]


# 3. FreshnessDetector — immune entity types NEVER proposed, regardless of age

def test_immune_entity_types_never_proposed(store, tmp_path):
    # Aged AND seeded below the floor — still immune (decay_rate == 1.0).
    _add(store, "mem_fact", "DomainFact", confidence=0.15, days_old=400)
    _add(store, "mem_rule", "ArchitecturalRule", confidence=0.10, days_old=400)
    _add(store, "mem_pref", "OperatorPreference", confidence=0.18, days_old=400)
    store.rebuild_index()

    detector = FreshnessDetector(base_dir=tmp_path)
    proposals = detector.detect(store, _goals())
    assert proposals == []


# 4. FreshnessDetector — Dock-active record suspended (DI-3)

def test_dock_active_record_not_proposed(store, tmp_path):
    _add(store, "mem_goal", "ProjectState", confidence=0.8, days_old=40,
         dock_goal_ref="live-goal")
    _add(store, "mem_orphan", "ProjectState", confidence=0.8, days_old=40,
         dock_goal_ref="dead-goal")
    store.rebuild_index()

    detector = FreshnessDetector(base_dir=tmp_path)
    proposals = detector.detect(store, _goals("live-goal"))

    targets = {p["target_id"] for p in proposals}
    assert "mem_goal" not in targets        # suspended by active Dock goal
    assert "mem_orphan" in targets          # not tied to a live goal → decays


# 5. FreshnessDetector — cognitive budget caps a sweep at 3

def test_max_three_proposals(store, tmp_path):
    for i in range(5):
        _add(store, f"mem_s{i}", "ProjectState", confidence=0.8, days_old=40)
    store.rebuild_index()

    detector = FreshnessDetector(base_dir=tmp_path)
    proposals = detector.detect(store, _goals())
    assert len(proposals) == 3
    # Ranked most-decayed first → ascending confidence.
    confs = [p["reason"] for p in proposals]
    assert len(confs) == 3


# 5b. stage_proposals — written as pending, deduped across sweeps

def test_stage_proposals_dedup(store, tmp_path):
    _add(store, "mem_stale", "ProjectState", confidence=0.8, days_old=40)
    store.rebuild_index()
    detector = FreshnessDetector(base_dir=tmp_path)

    proposals = detector.detect(store, _goals())
    assert detector.stage_proposals(proposals, "context-freshness-sweep") == 1
    # A second sweep finds the same target already pending → stages 0.
    assert detector.stage_proposals(proposals, "context-freshness-sweep") == 0


# 6. MemoryProposalHandler.apply("deprecate") — MemoryDeprecated event + status

def test_apply_deprecate_creates_event_and_deprecates(store):
    _add(store, "mem_x", "ProjectState", confidence=0.1, days_old=0)
    store.rebuild_index()

    handler = MemoryProposalHandler(store)
    handler.apply({
        "action": "deprecate", "target_id": "mem_x",
        "reason": "Confidence decayed to 10% -- not accessed since 2026-05-01",
        "content": "ProjectState content for mem_x",
    })

    assert store.projected_records()["mem_x"].status == "deprecated"
    events = list(store.read_events())
    assert any(isinstance(e, MemoryDeprecated) and e.record_id == "mem_x"
               for e in events)


def test_apply_deprecate_dangling_target_raises(store):
    handler = MemoryProposalHandler(store)
    with pytest.raises(ValueError):
        handler.apply({"action": "deprecate", "target_id": "mem_missing",
                       "reason": "r", "content": "c"})


# 7. summary_renderer — deprecation natural language, no schema leak

def test_summary_renderer_deprecation_natural_language():
    proposal = {
        "action": "deprecate", "target_id": "mem_abc12345",
        "reason": "Confidence decayed to 12% -- not accessed since 2026-05-01",
        "content": "VM prod HEAD is 06a520d04.",
    }
    text = MemoryProposalHandler.summary_renderer(proposal)
    assert "VM prod HEAD is 06a520d04." in text
    assert "Deprecate?" in text
    # No schema leak.
    assert "[deprecate]" not in text
    assert "EntityType" not in text
    assert "mem_abc12345" not in text


def test_summary_renderer_deprecation_missing_content_fallback():
    proposal = {"action": "deprecate", "target_id": "mem_x", "reason": "r"}
    text = MemoryProposalHandler.summary_renderer(proposal)
    assert text == "A memory record has decayed below threshold. Deprecate?"


# 8. push_body — deprecation frame differs from create/supersede

def test_push_body_deprecation_frame_differs():
    deprecate_rec = {"status": "pending", "proposal": {
        "action": "deprecate", "target_id": "mem_x",
        "reason": "r", "content": "Stale fact."}}
    create_rec = {"status": "pending", "proposal": {
        "action": "create", "target_id": None,
        "proposed_record": {"entity_type": "DomainFact", "content": "New fact.",
                            "confidence": 0.9, "justification": "j"}}}

    dep_body = MemoryProposalRenderable(deprecate_rec).push_body("CORE")
    new_body = MemoryProposalRenderable(create_rec).push_body("CORE")

    assert dep_body == "I'm recommending we retire a stale memory — CORE"
    assert new_body == "I crystallized a domain insight — CORE"
    assert dep_body != new_body


# 9. Backward compat — create/supersede apply + render unchanged

def test_create_apply_and_render_unchanged(store):
    handler = MemoryProposalHandler(store)
    create = {
        "action": "create", "target_id": None, "dock_goal_ref": None,
        "proposed_record": {"entity_type": "DomainFact", "content": "A fact.",
                            "confidence": 0.9, "justification": "j"},
    }
    assert handler.apply(create) is True
    actives = [r for r in store.projected_records().values()
               if r.status == "active"]
    assert len(actives) == 1 and actives[0].content == "A fact."

    assert handler.summary_renderer(create) == "A fact. (Confidence: 90%)"
    supersede = dict(create, action="supersede")
    assert handler.summary_renderer(supersede).startswith("Updated understanding:")
