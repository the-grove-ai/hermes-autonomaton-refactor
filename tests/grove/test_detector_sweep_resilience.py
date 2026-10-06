"""detector-sweep-resilience-v1 P1 — shared producer guard + propagation.

Pins (shape copied from tests/grove/test_goal_attachment_proposal.py's
isolation-guard section): per-producer containment through
``_run_guarded_producer`` (raise contained, one registered
``producer_failure`` event, producer name as DATA), the pause-skip
semantic (paused → ZERO invocation, no event), cascade isolation
(detector 2 raising leaves detectors 3–5 + session compaction invoked,
exactly one event), the R-4 sweep-level emit at the call-site guard
(prologue/structural failure files the SWEEP name as data — gate ruling
b), both session-compaction emit sites (skip-and-continue semantics
preserved), and the inert P1 pause seam (``_paused_producers`` returns
the empty set).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from grove.dispatcher import (
    Dispatcher,
    _file_producer_failure,
    _paused_producers,
    _run_guarded_producer,
)
from grove.kaizen_ledger import default_ledger_dir


def _producer_failures():
    events = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_type"] == "producer_failure":
                events.append(event)
    return events


# ── shared guard helper (R-5) ───────────────────────────────────────────────


def test_guard_contains_raise_and_files_event():
    def _boom():
        raise RuntimeError("producer exploded")

    # Must NOT propagate — isolation contract.
    _run_guarded_producer("x_detector", _boom)

    events = _producer_failures()
    assert len(events) == 1
    assert events[0]["producer"] == "x_detector"
    assert "producer exploded" in events[0]["error"]


def test_guard_invokes_producer_on_success():
    calls = []
    _run_guarded_producer("x_detector", lambda: calls.append(1))
    assert calls == [1]
    assert _producer_failures() == []


def test_guard_pause_skips_invocation_entirely():
    calls = []
    _run_guarded_producer(
        "x_detector", lambda: calls.append(1),
        paused=frozenset({"x_detector"}),
    )
    # ZERO invocation, no event — the pause is a skip, not a soft-fail.
    assert calls == []
    assert _producer_failures() == []


def test_paused_producers_empty_when_no_pause_state():
    # P2: the seam delegates to the real reader; with no pause file on disk
    # (hermetic GROVE_HOME) the sweep behavior is identical to P1's inert
    # stub — absent file → empty set.
    assert _paused_producers() == frozenset()


def test_emit_primitive_files_minimal_uniform_payload():
    _file_producer_failure("session_compaction", RuntimeError("boom"))
    events = _producer_failures()
    assert len(events) == 1
    # Gate ruling (a): minimal-uniform payload — producer + error only,
    # beside the ledger's own reserved keys. (2026-10-06: the ledger's
    # reserved keys now include the provenance chain's two — prev_hash and
    # record_hash — on every event; the PAYLOAD is unchanged.)
    assert set(events[0]) == {
        "event_type", "session_id", "timestamp", "prev_hash", "record_hash",
        "producer", "error",
    }


# ── sweep integration (cascade / pause / compaction) ────────────────────────


class _NoOpDetector:
    def __init__(self, *a, **k):
        pass

    def detect(self, *a, **k):
        return []

    def stage_proposals(self, *a, **k):
        return None


class _FakeSession:
    def __init__(self, transcript=None, raise_on_read=False):
        self._transcript = transcript if transcript is not None else []
        self._raise = raise_on_read

    def get_messages_as_conversation(self, _sid):
        if self._raise:
            raise RuntimeError("transcript read boom")
        return self._transcript


class _FakeIntentStore:
    def filter(self, session_id=None):
        return []


def _shell(session=None):
    disp = Dispatcher.__new__(Dispatcher)
    disp.session = session or _FakeSession()
    disp._intent_store = _FakeIntentStore()
    return disp


def _stub_sweep(monkeypatch, tmp_path, calls):
    """Stub every sweep collaborator at its source module (mirrors
    test_wiki_session_compactor._stub_sweep_collaborators), recording
    producer invocations into ``calls``."""

    def _recorder_cls(tag):
        class _Rec(_NoOpDetector):
            def detect(self, *a, **k):
                calls.append(tag)
                return []

            def stage_proposals(self, *a, **k):
                calls.append(tag)
                return None

        return _Rec

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: str(tmp_path)
    )
    monkeypatch.setattr("grove.memory.store.MemoryStore", _NoOpDetector)
    monkeypatch.setattr(
        "grove.memory.detector.ContextPersistenceDetector", _NoOpDetector
    )
    monkeypatch.setattr(
        "grove.memory.lifecycle.load_active_dock_goal_dicts", lambda: []
    )
    monkeypatch.setattr(
        "grove.memory.lifecycle.run_memory_extraction",
        lambda **_k: calls.append("context_persistence"),
    )
    monkeypatch.setattr(
        "grove.memory.freshness.FreshnessDetector",
        _recorder_cls("freshness"),
    )
    monkeypatch.setattr(
        "grove.memory.graduation.GraduationDetector",
        _recorder_cls("graduation"),
    )
    monkeypatch.setattr(
        "grove.eval.consolidation_ratchet.ConsolidationRatchet",
        _recorder_cls("consolidation"),
    )
    monkeypatch.setattr(
        "grove.dock.detector.DockMutationDetector",
        _recorder_cls("dock_mutation"),
    )
    monkeypatch.setattr(
        "grove.wiki.session_compactor.compact_session",
        lambda sid, filtered, store, source_mtime, *, wiki_root=None: (
            calls.append("compaction") or None
        ),
    )


def test_cascade_isolation_detector_raise_leaves_siblings_running(
    monkeypatch, tmp_path
):
    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)

    class _BoomFreshness:
        def __init__(self, *a, **k):
            raise RuntimeError("freshness exploded")

    monkeypatch.setattr(
        "grove.memory.freshness.FreshnessDetector", _BoomFreshness
    )

    _shell()._extract_memory_from_dormant_sessions(["sess-1"])

    # Detector 2 raised; 1 ran before it, 3, 4, 5 + compaction after it.
    assert "context_persistence" in calls
    assert "graduation" in calls
    assert "consolidation" in calls
    assert "dock_mutation" in calls
    assert "compaction" in calls
    events = _producer_failures()
    assert len(events) == 1
    assert events[0]["producer"] == "freshness_detector"
    assert "freshness exploded" in events[0]["error"]


def test_pause_seam_skips_only_the_paused_producer(monkeypatch, tmp_path):
    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        "grove.dispatcher._paused_producers",
        lambda: frozenset({"dock_mutation_detector"}),
    )

    _shell()._extract_memory_from_dormant_sessions(["sess-1"])

    assert "dock_mutation" not in calls
    for tag in ("context_persistence", "freshness", "graduation",
                "consolidation", "compaction"):
        assert tag in calls
    assert _producer_failures() == []


def test_compaction_per_session_emit_preserves_skip_and_continue(
    monkeypatch, tmp_path
):
    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)

    session = _FakeSession(raise_on_read=True)
    _shell(session)._extract_memory_from_dormant_sessions(["s1", "s2"])

    # Both sessions attempted (skip-and-continue preserved), one event each.
    events = _producer_failures()
    assert len(events) == 2
    assert {e["producer"] for e in events} == {"session_compaction"}
    assert all("transcript read boom" in e["error"] for e in events)


def test_compaction_subsystem_emit(monkeypatch, tmp_path):
    calls: list = []
    _stub_sweep(monkeypatch, tmp_path, calls)

    class _ExplodingSlice(list):
        def __getitem__(self, item):
            raise RuntimeError("subsystem boom")

    _shell()._extract_memory_from_dormant_sessions(
        _ExplodingSlice(["s1"])
    )

    events = _producer_failures()
    assert len(events) == 1
    assert events[0]["producer"] == "session_compaction"
    assert "subsystem boom" in events[0]["error"]


# ── R-4 sweep-level floor at the call-site guard ────────────────────────────


def test_sweep_floor_files_sweep_name_on_structural_failure(
    monkeypatch, tmp_path
):
    # Gate ruling (b): a raise that ESCAPES the per-producer guards
    # (prologue/structural failure) is a SWEEP failure — filed with the
    # sweep name as data at the R-4 call-site guard, never attributed to
    # a single producer.
    from grove.intent_store import IntentStore

    monkeypatch.setattr(
        "grove.memory.lifecycle.dormant_session_ids",
        lambda store, minutes=30: ["sess-x"],
    )

    def _prologue_boom(self, session_ids):
        raise RuntimeError("prologue boom")

    monkeypatch.setattr(
        Dispatcher, "_extract_memory_from_dormant_sessions", _prologue_boom
    )
    # Neutralize the sibling goal-attachment sweep (same dormancy gate).
    monkeypatch.setattr(
        "grove.dock.attachment.run_goal_attachment_sweep", lambda: None
    )

    store = IntentStore(store_path=tmp_path / "records.jsonl")
    # Construction must survive the contained sweep failure.
    Dispatcher(intent_store=store, session_db=SimpleNamespace())

    events = _producer_failures()
    assert len(events) == 1
    assert events[0]["producer"] == "memory_extraction_sweep"
    assert "prologue boom" in events[0]["error"]


# ── init-sweeps-background-v1 — inline (default) vs background ───────────────


def _stub_three_sweeps(monkeypatch, order, gate=None):
    monkeypatch.setattr(
        "grove.memory.lifecycle.dormant_session_ids",
        lambda store, minutes=30: ["sess-x"],
    )

    def _memory(self, sids):
        if gate is not None:
            gate.wait(timeout=10)
        order.append("memory_sweep")

    monkeypatch.setattr(
        Dispatcher, "_extract_memory_from_dormant_sessions", _memory
    )
    monkeypatch.setattr(
        "grove.dock.attachment.run_goal_attachment_sweep",
        lambda: order.append("goal_attachment"),
    )
    monkeypatch.setattr(
        "grove.eval.producer_recurrence.build_producer_recurrence_proposals",
        lambda: order.append("recurrence"),
    )


def _join_init_sweeps():
    import threading

    for t in threading.enumerate():
        if t.name == "grove-init-sweeps":
            t.join(timeout=10)
            assert not t.is_alive()


def test_init_sweeps_default_is_inline(monkeypatch, tmp_path):
    from grove.intent_store import IntentStore
    import grove.dispatcher as disp

    assert disp._init_sweeps_mode() == "inline"   # no config file → inline
    order: list = []
    _stub_three_sweeps(monkeypatch, order)
    Dispatcher(
        intent_store=IntentStore(store_path=tmp_path / "records.jsonl"),
        session_db=SimpleNamespace(),
    )
    # Inline: all three have run, in order, by the time construction returns.
    assert order == ["memory_sweep", "goal_attachment", "recurrence"]


def test_init_sweeps_background_does_not_block_construction(
    monkeypatch, tmp_path
):
    import threading

    from grove.intent_store import IntentStore
    import grove.dispatcher as disp

    monkeypatch.setattr(disp, "_init_sweeps_mode", lambda: "background")
    order: list = []
    gate = threading.Event()   # holds the first sweep until we release it
    _stub_three_sweeps(monkeypatch, order, gate=gate)

    Dispatcher(
        intent_store=IntentStore(store_path=tmp_path / "records.jsonl"),
        session_db=SimpleNamespace(),
    )
    # Construction returned while the sweep is still held — the turn is free.
    assert order == []
    gate.set()
    _join_init_sweeps()
    # Same work, same load-bearing order, just later.
    assert order == ["memory_sweep", "goal_attachment", "recurrence"]


def test_init_sweeps_background_failure_is_contained_and_filed(
    monkeypatch, tmp_path
):
    from grove.intent_store import IntentStore
    import grove.dispatcher as disp

    monkeypatch.setattr(disp, "_init_sweeps_mode", lambda: "background")
    monkeypatch.setattr(
        "grove.memory.lifecycle.dormant_session_ids",
        lambda store, minutes=30: ["sess-x"],
    )
    monkeypatch.setattr(
        Dispatcher, "_extract_memory_from_dormant_sessions",
        lambda self, sids: None,
    )

    def _structural_boom(self):
        raise RuntimeError("background boom")

    # An UNGUARDED raise inside the sweep closure (structural) must not kill
    # the thread silently — it is logged and filed under the sweep's name.
    monkeypatch.setattr(Dispatcher, "_run_goal_attachment_sweep", _structural_boom)

    Dispatcher(
        intent_store=IntentStore(store_path=tmp_path / "records.jsonl"),
        session_db=SimpleNamespace(),
    )
    _join_init_sweeps()
    events = [e for e in _producer_failures() if e["producer"] == "init_sweeps"]
    assert len(events) == 1 and "background boom" in events[0]["error"]


def test_init_sweeps_mode_config(monkeypatch, tmp_path):
    import grove.dispatcher as disp

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("init_sweeps:\n  mode: background\n")
    assert disp._init_sweeps_mode() == "background"
    cfg.write_text("admission_friction:\n  window_days: 30\n")
    assert disp._init_sweeps_mode() == "inline"       # absent block → inline
    cfg.write_text("init_sweeps:\n  mode: sometimes\n")
    with pytest.raises(ValueError, match="init_sweeps.mode"):
        disp._init_sweeps_mode()
