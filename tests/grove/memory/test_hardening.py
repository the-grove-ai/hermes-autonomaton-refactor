"""memory-operational-hardening-v1 — four operational edge-case fixes.

Fix 1: telemetry debounce (per-session batch, not per-turn).
Fix 2: rejection memory (recently-rejected fed to T1).
Fix 3: minimum complexity gate (trivial sessions skip T1).
Fix 4: staging-queue collision guard (pending supersessions annotated).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.memory.detector import (
    DETECTOR_SYSTEM_PROMPT,
    ContextPersistenceDetector,
    _session_worth_extracting,
)
from grove.memory.events import MemoryAccessed, MemoryCreated
from grove.memory.provider import create_memory_provider
from grove.memory.store import MemoryStore

_TS = "2026-06-01T00:00:00+00:00"


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


@pytest.fixture()
def detector(store, tmp_path):
    return ContextPersistenceDetector(store=store, base_dir=tmp_path)


def _seed(store, record_id, content, *, entity_type="DomainFact", confidence=0.9):
    store.append_event(MemoryCreated(
        event_id="evt_" + record_id, timestamp=_TS, record_id=record_id,
        entity_type=entity_type, content=content, confidence=confidence,
        dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.rebuild_index()


def _rich_transcript():
    """A transcript that clears the Fix 3 complexity gate (>=3 user turns)."""
    return [
        {"role": "user", "content": "First substantive message about the project."},
        {"role": "assistant", "content": "Reply one."},
        {"role": "user", "content": "Second message with more detail."},
        {"role": "assistant", "content": "Reply two."},
        {"role": "user", "content": "Third message confirming a preference."},
    ]


def _stage(path: Path, record: dict):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ── Fix 1: telemetry debounce ─────────────────────────────────────────────

def test_provider_writes_no_access_events_on_turn(store, tmp_path):
    _seed(store, "mem_a", "Fact A.")
    _seed(store, "mem_b", "Fact B.")
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    provider({"session_id": "s1", "intent_class": "conversation"})

    # No MemoryAccessed events during the live turn (debounced).
    assert not [e for e in store.read_events() if isinstance(e, MemoryAccessed)]


def test_flush_emits_one_event_per_record_across_turns(store, tmp_path):
    _seed(store, "mem_a", "Fact A.")
    _seed(store, "mem_b", "Fact B.")
    _seed(store, "mem_c", "Fact C.")
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])

    # Simulate a 10-turn session serving the same 3 records each turn.
    for _ in range(10):
        provider({"session_id": "s1", "intent_class": "conversation"})

    assert not [e for e in store.read_events() if isinstance(e, MemoryAccessed)]

    emitted = store.flush_access_events("s1")
    assert emitted == 3
    accesses = [e for e in store.read_events() if isinstance(e, MemoryAccessed)]
    assert len(accesses) == 3
    assert {a.record_id for a in accesses} == {"mem_a", "mem_b", "mem_c"}


def test_flush_is_idempotent(store):
    _seed(store, "mem_a", "Fact A.")
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    provider({"session_id": "s1", "intent_class": "conversation"})

    assert store.flush_access_events("s1") == 1
    assert store.flush_access_events("s1") == 0  # nothing left to flush
    accesses = [e for e in store.read_events() if isinstance(e, MemoryAccessed)]
    assert len(accesses) == 1


# ── Fix 2: rejection memory ───────────────────────────────────────────────

def _capture_call(detector):
    captured = {}

    def spy(filtered, index, dock, rejected):
        captured["filtered"] = filtered
        captured["index"] = index
        captured["dock"] = dock
        captured["rejected"] = rejected
        return json.dumps({"proposals": []})

    detector._call_detector = spy
    return captured


def test_recent_rejection_fed_to_t1(detector):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _stage(detector.proposals_path, {
        "session_id": "old", "status": "rejected", "timestamp": recent,
        "proposal": {"action": "create", "target_id": None, "dock_goal_ref": None,
                     "proposed_record": {"entity_type": "OperatorPreference",
                                         "content": "Operator prefers Python 3.10.",
                                         "confidence": 0.8, "justification": "x"}},
    })
    captured = _capture_call(detector)

    detector.detect_and_stage("sess-new", _rich_transcript(), [])

    contents = [r["content"] for r in captured["rejected"]]
    assert "Operator prefers Python 3.10." in contents


def test_old_rejection_excluded(detector):
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    _stage(detector.proposals_path, {
        "session_id": "old", "status": "rejected", "timestamp": old,
        "proposal": {"action": "create", "target_id": None, "dock_goal_ref": None,
                     "proposed_record": {"entity_type": "OperatorPreference",
                                         "content": "Stale rejected preference.",
                                         "confidence": 0.8, "justification": "x"}},
    })
    captured = _capture_call(detector)

    detector.detect_and_stage("sess-new", _rich_transcript(), [])

    assert captured["rejected"] == []


def test_system_prompt_has_rejection_rule():
    assert "REJECTION MEMORY" in DETECTOR_SYSTEM_PROMPT
    assert "recently_rejected_proposals" in DETECTOR_SYSTEM_PROMPT


# ── Fix 3: minimum complexity gate ────────────────────────────────────────

def test_gate_one_turn_no_tools_false():
    assert _session_worth_extracting(
        [{"role": "user", "content": "What time is it?"}]
    ) is False


def test_gate_two_turns_no_tools_false():
    assert _session_worth_extracting([
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]) is False


def test_gate_three_turns_true():
    assert _session_worth_extracting([
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
    ]) is True


def test_gate_one_turn_with_tools_true():
    assert _session_worth_extracting([
        {"role": "user", "content": "deploy the thing"},
        {"role": "assistant", "content": "ok",
         "tool_calls": [{"function": {"name": "deploy", "arguments": "{}"}}]},
    ]) is True


def test_trivial_session_skips_t1_and_writes_no_lock(detector):
    called = {"n": 0}

    def spy(*_a, **_k):
        called["n"] += 1
        return json.dumps({"proposals": []})

    detector._call_detector = spy
    staged = detector.detect_and_stage(
        "sess-trivial", [{"role": "user", "content": "hi"}], [],
    )
    assert staged == 0
    assert called["n"] == 0                       # T1 never called
    assert not detector.proposals_path.exists()   # no processing lock written


# ── Fix 4: staging-queue collision guard ──────────────────────────────────

def test_pending_supersession_annotated(detector, store):
    _seed(store, "mem_123", "Original head fact.")
    # a pending supersede targeting mem_123
    _stage(detector.proposals_path, {
        "session_id": "earlier", "status": "pending", "timestamp": _TS,
        "proposal": {"action": "supersede", "target_id": "mem_123",
                     "dock_goal_ref": None,
                     "proposed_record": {"entity_type": "DomainFact",
                                         "content": "Updated head.",
                                         "confidence": 0.9, "justification": "x"}},
    })
    captured = _capture_call(detector)

    detector.detect_and_stage("sess-new", _rich_transcript(), [])

    entry = next(r for r in captured["index"] if r["id"] == "mem_123")
    assert "[PENDING SUPERSESSION]" in entry["content"]


def test_no_pending_no_annotation(detector, store):
    _seed(store, "mem_123", "Original head fact.")
    captured = _capture_call(detector)

    detector.detect_and_stage("sess-new", _rich_transcript(), [])

    entry = next(r for r in captured["index"] if r["id"] == "mem_123")
    assert "[PENDING SUPERSESSION]" not in entry["content"]


def test_system_prompt_has_pending_mutations_rule():
    assert "PENDING MUTATIONS" in DETECTOR_SYSTEM_PROMPT
    assert "[PENDING SUPERSESSION]" in DETECTOR_SYSTEM_PROMPT
