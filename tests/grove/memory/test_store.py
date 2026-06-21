"""Phase 1 tests — memory-substrate-v1 MemoryEvent schema, event store, index.

The MemoryStore is event-sourced: the JSONL log at ``memory_records.jsonl``
is the source of truth, and ``memory_index.json`` is a derived projection.
R4 invariant: the projection is reconstructible from the log alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from grove.memory.events import (
    MemoryAccessed,
    MemoryCreated,
    MemoryDeprecated,
    MemorySuperseded,
)
from grove.memory.record import MemoryRecord
from grove.memory.store import MemoryStore


def _ts(offset_days: float = 0.0) -> str:
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


# 1. Event append + read round-trip (each event type)

def test_event_round_trip_all_types(store):
    events = [
        MemoryCreated(
            event_id="evt_aaaaaaaa",
            timestamp=_ts(),
            record_id="mem_11111111",
            entity_type="DomainFact",
            content="Take Flight Advisors uses Notion for project tracking.",
            confidence=0.9,
            dock_goal_ref=None,
            sources=[{"session_id": "s1", "turn_id": "t1"}],
            supersedes=None,
        ),
        MemorySuperseded(
            event_id="evt_bbbbbbbb",
            timestamp=_ts(1),
            record_id="mem_22222222",
            entity_type="ProjectState",
            content="VM prod HEAD is 06a520d04.",
            confidence=0.85,
            dock_goal_ref="grove-content-pipeline",
            sources=[{"session_id": "s2", "turn_id": "t2"}],
            supersedes="mem_11111111",
        ),
        MemoryDeprecated(
            event_id="evt_cccccccc",
            timestamp=_ts(2),
            record_id="mem_22222222",
            reason="superseded by newer head",
        ),
        MemoryAccessed(
            event_id="evt_dddddddd",
            timestamp=_ts(3),
            record_id="mem_11111111",
            session_id="s3",
            context="notion tracking",
        ),
    ]
    for ev in events:
        store.append_event(ev)

    read_back = list(store.read_events())
    assert read_back == events


# 2. Index rebuild from event log: create 3, supersede 1, deprecate 1, access 2

def test_index_rebuild_projects_expected_state(store):
    store.append_event(MemoryCreated(
        event_id="evt_c1", timestamp=_ts(), record_id="mem_a",
        entity_type="DomainFact", content="Fact A", confidence=0.9,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "1"}],
        supersedes=None,
    ))
    store.append_event(MemoryCreated(
        event_id="evt_c2", timestamp=_ts(), record_id="mem_b",
        entity_type="OperatorPreference", content="Pref B", confidence=0.8,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "2"}],
        supersedes=None,
    ))
    store.append_event(MemoryCreated(
        event_id="evt_c3", timestamp=_ts(), record_id="mem_c",
        entity_type="ProjectState", content="State C", confidence=0.7,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "3"}],
        supersedes=None,
    ))
    # supersede mem_a with mem_d
    store.append_event(MemorySuperseded(
        event_id="evt_s1", timestamp=_ts(1), record_id="mem_d",
        entity_type="DomainFact", content="Fact A v2", confidence=0.95,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "4"}],
        supersedes="mem_a",
    ))
    # deprecate mem_b
    store.append_event(MemoryDeprecated(
        event_id="evt_x1", timestamp=_ts(1), record_id="mem_b",
        reason="no longer true",
    ))
    # access mem_c twice
    store.append_event(MemoryAccessed(
        event_id="evt_a1", timestamp=_ts(2), record_id="mem_c",
        session_id="s", context="state",
    ))
    store.append_event(MemoryAccessed(
        event_id="evt_a2", timestamp=_ts(3), record_id="mem_c",
        session_id="s", context="state again",
    ))

    store.rebuild_index()
    idx = store.projected_records()

    assert idx["mem_a"].status == "superseded"
    assert idx["mem_d"].status == "active"
    assert idx["mem_d"].supersedes == "mem_a"
    assert idx["mem_b"].status == "deprecated"
    assert idx["mem_c"].status == "active"
    assert idx["mem_c"].access_count == 2
    assert idx["mem_c"].last_accessed == _ts(3)


# 3. R4 INVARIANT — delete index, rebuild, identical projected state

def test_r4_invariant_rebuild_from_log(store):
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_ts(), record_id="mem_a",
        entity_type="DomainFact", content="Fact", confidence=0.9,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "1"}],
        supersedes=None,
    ))
    store.append_event(MemorySuperseded(
        event_id="evt_2", timestamp=_ts(1), record_id="mem_b",
        entity_type="DomainFact", content="Fact v2", confidence=0.95,
        dock_goal_ref=None, sources=[{"session_id": "s", "turn_id": "2"}],
        supersedes="mem_a",
    ))
    store.append_event(MemoryAccessed(
        event_id="evt_3", timestamp=_ts(2), record_id="mem_b",
        session_id="s", context="fact",
    ))
    store.rebuild_index()
    before = store.projected_records()

    store.index_path.unlink()
    assert not store.index_path.exists()

    store.rebuild_index()
    after = store.projected_records()

    assert after == before


# 4. Entity-type decay: ProjectState decays, DomainFact doesn't

def test_entity_type_decay(store):
    store.append_event(MemoryCreated(
        event_id="evt_p", timestamp=_ts(), record_id="mem_ps",
        entity_type="ProjectState", content="State", confidence=0.8,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.append_event(MemoryCreated(
        event_id="evt_d", timestamp=_ts(), record_id="mem_df",
        entity_type="DomainFact", content="Fact", confidence=0.8,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.rebuild_index()

    # 10 days after creation
    store.apply_decay(now=datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc))
    idx = store.projected_records()

    assert idx["mem_ps"].confidence < 0.8       # ProjectState decayed
    assert idx["mem_df"].confidence == 0.8      # DomainFact unchanged
    # 0.95 ** 10 == 0.5987...
    assert idx["mem_ps"].confidence == pytest.approx(0.8 * (0.95 ** 10))


# 5. Dock-modulated decay: active dock_goal_ref skips decay

def test_dock_modulated_decay_suspends(store):
    store.append_event(MemoryCreated(
        event_id="evt_g", timestamp=_ts(), record_id="mem_g",
        entity_type="ProjectState", content="Goal state", confidence=0.8,
        dock_goal_ref="active-goal", sources=[], supersedes=None,
    ))
    store.append_event(MemoryCreated(
        event_id="evt_o", timestamp=_ts(), record_id="mem_o",
        entity_type="ProjectState", content="Other state", confidence=0.8,
        dock_goal_ref="inactive-goal", sources=[], supersedes=None,
    ))
    store.rebuild_index()

    store.apply_decay(
        active_dock_goals=["active-goal"],
        now=datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    idx = store.projected_records()

    assert idx["mem_g"].confidence == 0.8       # suspended, no decay
    assert idx["mem_o"].confidence < 0.8        # decayed normally


# 6. Query with keyword match + dock_goal_refs boost

def test_query_keyword_and_dock_boost(store):
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_ts(), record_id="mem_plain",
        entity_type="DomainFact", content="Notion is used for tracking.",
        confidence=0.9, dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.append_event(MemoryCreated(
        event_id="evt_2", timestamp=_ts(), record_id="mem_goal",
        entity_type="ProjectState", content="Notion tracking for the goal.",
        confidence=0.7, dock_goal_ref="my-goal", sources=[], supersedes=None,
    ))
    store.rebuild_index()

    results = store.query(keywords=["notion"], dock_goal_refs=["my-goal"])
    assert [r.id for r in results] == ["mem_goal", "mem_plain"]

    # No keyword hit on a third record → excluded from keyword-scored ranking
    no_match = store.query(keywords=["nonexistent-term-xyz"])
    assert no_match == []


# 7. Supersession: created then superseded → old record superseded

def test_supersession_marks_old(store):
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_ts(), record_id="mem_old",
        entity_type="DomainFact", content="Old", confidence=0.9,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.append_event(MemorySuperseded(
        event_id="evt_2", timestamp=_ts(1), record_id="mem_new",
        entity_type="DomainFact", content="New", confidence=0.95,
        dock_goal_ref=None, sources=[], supersedes="mem_old",
    ))
    store.rebuild_index()
    idx = store.projected_records()

    assert idx["mem_old"].status == "superseded"
    assert idx["mem_new"].status == "active"
    # superseded record no longer surfaces in queries
    active_ids = [r.id for r in store.query()]
    assert "mem_old" not in active_ids
    assert "mem_new" in active_ids


# 8. MemoryAccessed compilation: 3 access events → count=3, latest timestamp

def test_accessed_compilation(store):
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_ts(), record_id="mem_a",
        entity_type="DomainFact", content="Fact", confidence=0.9,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    for i, off in enumerate((1, 2, 3), start=1):
        store.append_event(MemoryAccessed(
            event_id=f"evt_acc{i}", timestamp=_ts(off), record_id="mem_a",
            session_id="s", context="ctx",
        ))
    store.rebuild_index()
    idx = store.projected_records()

    assert idx["mem_a"].access_count == 3
    assert idx["mem_a"].last_accessed == _ts(3)


# record_access path: appends event + bumps live index without full rebuild

def test_record_access_bumps_live_index(store):
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp=_ts(), record_id="mem_a",
        entity_type="DomainFact", content="Fact", confidence=0.9,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.rebuild_index()
    assert store.projected_records()["mem_a"].access_count == 0

    store.record_access("mem_a", session_id="s", context="kw")
    rec = store.projected_records()["mem_a"]
    assert rec.access_count == 1
    assert rec.last_accessed is not None

    # the access event landed in the log (survives rebuild)
    store.rebuild_index()
    assert store.projected_records()["mem_a"].access_count == 1


# ── turn-keyword-relevance-v1: require_keyword_match gate ─────────────────

def _seed_kw(store, rid, content, *, confidence=0.9, dock_goal_ref=None):
    store.append_event(MemoryCreated(
        event_id="evt_" + rid, timestamp=_ts(), record_id=rid,
        entity_type="DomainFact", content=content, confidence=confidence,
        dock_goal_ref=dock_goal_ref, sources=[], supersedes=None,
    ))
    store.rebuild_index()


def test_require_keyword_match_false_keeps_dock_records(store):
    _seed_kw(store, "mem_kw", "the deploy script details", confidence=0.6)
    _seed_kw(store, "mem_dock", "unrelated goal note", confidence=0.6,
             dock_goal_ref="g1")
    res = store.query(keywords=["deploy"], dock_goal_refs=["g1"],
                      require_keyword_match=False)
    ids = [r.id for r in res]
    assert "mem_kw" in ids and "mem_dock" in ids        # both survive (boost)
    # Dock boost (2.0) outranks a single keyword hit (1.0) — documented order.
    assert ids.index("mem_dock") < ids.index("mem_kw")


def test_require_keyword_match_true_narrows(store):
    # Regression: the original narrowing contract (default True).
    _seed_kw(store, "mem_kw", "the deploy script", confidence=0.9)
    _seed_kw(store, "mem_other", "totally different content", confidence=0.9)
    assert [r.id for r in store.query(keywords=["deploy"])] == ["mem_kw"]
    assert store.query(keywords=["nonexistent-term-xyz"]) == []


def test_blended_ranking(store):
    _seed_kw(store, "both", "deploy goal thing", confidence=0.5, dock_goal_ref="g1")
    _seed_kw(store, "kw", "deploy only thing", confidence=0.5)
    _seed_kw(store, "dock", "goal only thing", confidence=0.5, dock_goal_ref="g1")
    _seed_kw(store, "neither", "unrelated thing", confidence=0.5)
    res = store.query(keywords=["deploy"], dock_goal_refs=["g1"],
                      require_keyword_match=False)
    ids = [r.id for r in res]
    assert ids[0] == "both"                  # keyword + dock = highest
    assert ids[-1] == "neither"              # zero-hit, no boost = lowest
    assert ids.index("dock") < ids.index("kw")   # boost 2.0 > 1 keyword hit
