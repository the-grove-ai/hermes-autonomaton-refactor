"""Phase 3 tests — Dispatcher-init orchestration glue.

Covers the dormancy derivation (from the IntentStore feed), the extraction
driver (with a fake detector — no network), and the Dock active-goal
mapping (A6). The live Dispatcher.__init__ wiring is exercised in Phase 5
e2e; here the glue is unit-tested in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from grove.intent_store import IntentRecord, IntentStore
from grove.memory.lifecycle import (
    dormant_session_ids,
    load_active_dock_goal_dicts,
    run_memory_extraction,
)


def _intent(session_id, turn_id, timestamp, outcome="pending"):
    return IntentRecord(
        timestamp=timestamp, session_id=session_id, turn_id=turn_id,
        user_message_stem="x", pattern_hash="h", intent_class="conversation",
        register_class="casual", complexity_signal="simple", confidence=0.9,
        outcome=outcome,
    )


def test_dormant_session_ids_filters_by_ttl(tmp_path):
    store = IntentStore(store_path=tmp_path / "intent.jsonl")
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=45)).isoformat()      # dormant
    recent = (now - timedelta(minutes=5)).isoformat()    # active
    store.append(_intent("dormant-1", "t1", old))
    store.append(_intent("recent-1", "t2", recent))
    store.append(_intent("done-1", "t3", old, outcome="success"))

    result = dormant_session_ids(store, minutes=30, now=now)
    assert result == ["dormant-1"]


def test_dormant_session_ids_dedupes(tmp_path):
    store = IntentStore(store_path=tmp_path / "intent.jsonl")
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=45)).isoformat()
    store.append(_intent("s1", "t1", old))
    store.append(_intent("s1", "t2", old))
    store.append(_intent("s2", "t3", old))

    result = dormant_session_ids(store, minutes=30, now=now)
    assert result == ["s1", "s2"]


def test_run_memory_extraction_drives_detector():
    calls = []

    class FakeDetector:
        def detect_and_stage(self, session_id, transcript, dock_goals):
            calls.append((session_id, transcript, dock_goals))
            return 2

    transcripts = {"s1": [{"role": "user", "content": "a"}],
                   "s2": [{"role": "user", "content": "b"}]}
    total = run_memory_extraction(
        detector=FakeDetector(),
        session_ids=["s1", "s2"],
        transcript_loader=lambda sid: transcripts[sid],
        dock_goals=[{"slug": "g", "name": "G", "status": "accelerating"}],
    )
    assert total == 4
    assert [c[0] for c in calls] == ["s1", "s2"]
    assert calls[0][1] == [{"role": "user", "content": "a"}]


def test_load_active_dock_goal_dicts_maps_active_only():
    # Build a Dock with one active and one dormant goal (A6).
    from grove.dock import Dock, Goal

    def goal(gid, status, vector="strategic"):
        return Goal(
            id=gid, name=gid.title(), vector=vector, status=status,
            definition_of_done="done", context_sources=(), keywords=(),
            unlocked_skills=(), root=Path("/tmp"),
        )

    dock = Dock(
        goals=(goal("alpha", "accelerating"), goal("beta", "parked")),
        context_char_budget=5000, root=Path("/tmp"),
    )
    result = load_active_dock_goal_dicts(dock=dock)
    assert result == [
        {"slug": "alpha", "name": "Alpha", "status": "accelerating",
         "vector": "strategic"},
    ]


def test_load_active_dock_goal_dicts_no_dock_returns_empty():
    # Isolated GROVE_HOME (conftest) has no dock.yaml → load_dock() is None.
    assert load_active_dock_goal_dicts(dock=None) == []
