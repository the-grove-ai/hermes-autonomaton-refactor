"""Tests for grove.intent_store — Sprint 28 intent-capture-v1 Phase 1.

Covers the IntentRecord schema, IntentStore append + read round-trip,
outcome enforcement, filter predicates, the provisional-write collapse
semantics (latest_by_turn), and the singleton accessor.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grove import intent_store as _intent_store_mod
from grove.intent_store import (
    VALID_OUTCOMES,
    IntentRecord,
    IntentStore,
    get_store,
)


# ── Test helpers ──────────────────────────────────────────────────────────


def _ts(iso: str) -> str:
    """Helper: produce an ISO 8601 UTC timestamp from a short suffix."""
    return f"2026-05-27T00:00:{iso}+00:00"


def _record(
    *,
    timestamp: str = "2026-05-27T00:00:00+00:00",
    session_id: str = "sess-1",
    turn_id: str = "sess-1#0",
    user_message_stem: str = "hello world",
    pattern_hash: str = "abc123",
    intent_class: str = "conversation",
    register_class: str = "casual",
    complexity_signal: str = "simple",
    confidence: float = 0.9,
    outcome: str = "pending",
    **overrides,
) -> IntentRecord:
    base = dict(
        timestamp=timestamp,
        session_id=session_id,
        turn_id=turn_id,
        user_message_stem=user_message_stem,
        pattern_hash=pattern_hash,
        intent_class=intent_class,
        register_class=register_class,
        complexity_signal=complexity_signal,
        confidence=confidence,
        outcome=outcome,
    )
    base.update(overrides)
    return IntentRecord(**base)


# ── IntentRecord schema ───────────────────────────────────────────────────


class TestIntentRecordSchema:
    def test_required_fields_construct_record(self):
        r = _record()
        assert r.timestamp == "2026-05-27T00:00:00+00:00"
        assert r.session_id == "sess-1"
        assert r.turn_id == "sess-1#0"
        assert r.outcome == "pending"

    def test_record_is_frozen(self):
        r = _record()
        with pytest.raises((AttributeError, Exception)):
            r.outcome = "success"

    def test_optional_fields_default_correctly(self):
        # Phase 2's goal_alignment defaults None so Phase 1 records
        # written before the classifier extension ships round-trip.
        r = _record()
        assert r.goal_alignment is None
        assert r.tier_selected is None
        assert r.model_used is None
        assert r.tools_yielded == ()
        assert r.api_calls == 0
        assert r.duration_ms == 0.0
        assert r.final_response_chars is None

    def test_record_with_all_optional_fields(self):
        r = _record(
            goal_alignment="direct",
            tier_selected="T2",
            model_used="claude-sonnet-4-6",
            tools_yielded=("read_file", "web_search"),
            api_calls=3,
            duration_ms=1234.5,
            final_response_chars=512,
        )
        assert r.goal_alignment == "direct"
        assert r.tier_selected == "T2"
        assert r.tools_yielded == ("read_file", "web_search")
        assert r.api_calls == 3


# ── IntentStore append + read ─────────────────────────────────────────────


class TestIntentStoreAppendAndRead:
    def test_append_persists_and_returns_dict(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        r = _record()
        data = store.append(r)
        assert data["session_id"] == "sess-1"
        assert data["outcome"] == "pending"
        # File contains one line of JSON for the record.
        contents = (tmp_path / "store.jsonl").read_text(encoding="utf-8")
        assert contents.count("\n") == 1
        parsed = json.loads(contents.strip())
        assert parsed["session_id"] == "sess-1"

    def test_append_tools_yielded_serializes_as_list(self, tmp_path: Path):
        # The in-memory tuple coerces to a JSON list (tuples aren't a
        # JSON type) so the file is portable.
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(tools_yielded=("a", "b", "c")))
        line = (tmp_path / "store.jsonl").read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        assert parsed["tools_yielded"] == ["a", "b", "c"]

    def test_records_iterates_what_was_written(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id="t1", confidence=0.5))
        store.append(_record(turn_id="t2", confidence=0.7))
        store.append(_record(turn_id="t3", confidence=0.9))
        seen = list(store.records())
        assert [r.turn_id for r in seen] == ["t1", "t2", "t3"]
        assert [r.confidence for r in seen] == [0.5, 0.7, 0.9]

    def test_records_round_trip_coerces_tools_yielded_to_tuple(
        self, tmp_path: Path,
    ):
        # JSON lists become tuples on read so the frozen dataclass
        # invariants hold (no mutation through the alias path).
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(tools_yielded=("read_file",)))
        loaded = next(iter(store.records()))
        assert loaded.tools_yielded == ("read_file",)
        assert isinstance(loaded.tools_yielded, tuple)

    def test_records_returns_empty_when_file_missing(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "absent.jsonl")
        assert list(store.records()) == []

    def test_records_skips_malformed_lines(self, tmp_path: Path):
        path = tmp_path / "store.jsonl"
        store = IntentStore(store_path=path)
        store.append(_record(turn_id="good-1"))
        # Inject a junk line between two valid records (simulating a
        # corrupted append or partial flush from another process).
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        store.append(_record(turn_id="good-2"))
        seen = list(store.records())
        assert [r.turn_id for r in seen] == ["good-1", "good-2"]

    def test_records_skips_schema_mismatched_lines(self, tmp_path: Path):
        # A line that's valid JSON but missing required fields gets
        # debug-logged and dropped — the runtime prefers partial reads.
        path = tmp_path / "store.jsonl"
        store = IntentStore(store_path=path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"only": "garbage"}) + "\n")
        store.append(_record(turn_id="real-record"))
        seen = list(store.records())
        assert len(seen) == 1
        assert seen[0].turn_id == "real-record"


# ── Outcome enforcement ───────────────────────────────────────────────────


class TestOutcomeEnforcement:
    def test_valid_outcomes_set_contents(self):
        """R-T3: closed set includes governance_terminated (e8d592c11, GRV-010 C3)."""
        # The closed set is part of the Phase 4 provisional-write
        # contract; lock it down so an accidental edit elsewhere
        # surfaces in test failure rather than a runtime ValueError.
        # + awaiting_operator (retrieval-ambient-class-v1 P5 — the Sprint 67
        # deferral outcome the store rejected until now; the GATE-A defect).
        assert VALID_OUTCOMES == frozenset({
            "pending", "success", "drop", "error", "correction",
            "governance_terminated", "awaiting_operator",
        })

    @pytest.mark.parametrize("outcome", sorted(VALID_OUTCOMES))
    def test_append_accepts_each_valid_outcome(
        self, tmp_path: Path, outcome: str,
    ):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id=f"t-{outcome}", outcome=outcome))

    def test_append_rejects_unknown_outcome(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        with pytest.raises(ValueError, match="unknown outcome"):
            store.append(_record(outcome="ratified"))


# ── Provisional-write collapse semantics ──────────────────────────────────


class TestLatestByTurn:
    """The Phase 4 provisional-write pattern: a ``pending`` record at
    turn-end is later joined by a finalization with the same turn_id and
    a terminal outcome. latest_by_turn collapses the pair into the
    effective per-turn state, picking the latest timestamp.
    """

    def test_collapses_pending_then_success_to_success(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(
            turn_id="t1", timestamp=_ts("00"), outcome="pending",
        ))
        store.append(_record(
            turn_id="t1", timestamp=_ts("05"), outcome="success",
        ))
        latest = list(store.latest_by_turn())
        assert len(latest) == 1
        assert latest[0].outcome == "success"

    def test_collapses_pending_then_correction_to_correction(
        self, tmp_path: Path,
    ):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(
            turn_id="t1", timestamp=_ts("00"), outcome="pending",
        ))
        store.append(_record(
            turn_id="t1", timestamp=_ts("05"), outcome="correction",
        ))
        latest = list(store.latest_by_turn())
        assert [r.outcome for r in latest] == ["correction"]

    def test_pending_alone_stays_pending(self, tmp_path: Path):
        # Process died between turn-end and finalization. The record
        # surfaces as still-pending so downstream consumers can decide
        # whether to treat it as unresolved or assume success.
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id="t1", outcome="pending"))
        latest = list(store.latest_by_turn())
        assert [r.outcome for r in latest] == ["pending"]

    def test_drop_and_error_are_terminal_no_finalization_needed(
        self, tmp_path: Path,
    ):
        # ``drop`` / ``error`` are written directly at turn-termination
        # paths the Dispatcher reaches when there is no normal end. The
        # collapse view shows them unchanged.
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id="t-drop", outcome="drop"))
        store.append(_record(turn_id="t-err", outcome="error"))
        latest = {r.turn_id: r.outcome for r in store.latest_by_turn()}
        assert latest == {"t-drop": "drop", "t-err": "error"}

    def test_multiple_turns_remain_independent(self, tmp_path: Path):
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id="t1", timestamp=_ts("00"), outcome="pending"))
        store.append(_record(turn_id="t2", timestamp=_ts("01"), outcome="pending"))
        store.append(_record(turn_id="t1", timestamp=_ts("02"), outcome="success"))
        store.append(_record(turn_id="t2", timestamp=_ts("03"), outcome="correction"))
        latest = {r.turn_id: r.outcome for r in store.latest_by_turn()}
        assert latest == {"t1": "success", "t2": "correction"}


# ── Filter predicates ─────────────────────────────────────────────────────


class TestFilter:
    @pytest.fixture
    def seeded_store(self, tmp_path: Path) -> IntentStore:
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(
            session_id="A", turn_id="A#0", timestamp=_ts("00"),
            intent_class="code_generation", pattern_hash="ph-code",
            outcome="success",
        ))
        store.append(_record(
            session_id="A", turn_id="A#1", timestamp=_ts("01"),
            intent_class="debugging", pattern_hash="ph-debug",
            outcome="success",
        ))
        store.append(_record(
            session_id="B", turn_id="B#0", timestamp=_ts("02"),
            intent_class="code_generation", pattern_hash="ph-code",
            outcome="pending",
        ))
        return store

    def test_filter_by_session_id(self, seeded_store: IntentStore):
        out = seeded_store.filter(session_id="A")
        assert {r.turn_id for r in out} == {"A#0", "A#1"}

    def test_filter_by_intent_class(self, seeded_store: IntentStore):
        out = seeded_store.filter(intent_class="code_generation")
        assert {r.turn_id for r in out} == {"A#0", "B#0"}

    def test_filter_by_pattern_hash(self, seeded_store: IntentStore):
        out = seeded_store.filter(pattern_hash="ph-debug")
        assert [r.turn_id for r in out] == ["A#1"]

    def test_filter_by_outcome(self, seeded_store: IntentStore):
        out = seeded_store.filter(outcome="pending")
        assert [r.turn_id for r in out] == ["B#0"]

    def test_filter_by_since(self, seeded_store: IntentStore):
        out = seeded_store.filter(since=_ts("01"))
        assert {r.turn_id for r in out} == {"A#1", "B#0"}

    def test_filter_combines_predicates_with_and(self, seeded_store: IntentStore):
        out = seeded_store.filter(
            session_id="A", intent_class="code_generation",
        )
        assert [r.turn_id for r in out] == ["A#0"]

    def test_filter_collapse_by_turn_uses_latest(self, tmp_path: Path):
        # When the provisional-write pair lives in the store, collapse
        # mode filters against the effective state, not the raw pending.
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        store.append(_record(turn_id="t1", timestamp=_ts("00"), outcome="pending"))
        store.append(_record(turn_id="t1", timestamp=_ts("05"), outcome="success"))
        raw_pending = store.filter(outcome="pending")
        collapsed_pending = store.filter(
            outcome="pending", collapse_by_turn=True,
        )
        assert len(raw_pending) == 1
        assert collapsed_pending == []  # pending was finalized to success


# ── Concurrent writes ─────────────────────────────────────────────────────


class TestConcurrentWrites:
    def test_threaded_appends_all_persist(self, tmp_path: Path):
        # The in-process lock + POSIX O_APPEND atomicity must produce
        # exactly N lines for N parallel append() calls — no torn lines,
        # no lost writes, no interleaved JSON.
        store = IntentStore(store_path=tmp_path / "store.jsonl")
        N = 50

        def write(i: int) -> None:
            store.append(_record(turn_id=f"t{i:03d}"))

        threads = [threading.Thread(target=write, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        seen = list(store.records())
        assert len(seen) == N
        assert {r.turn_id for r in seen} == {f"t{i:03d}" for i in range(N)}


# ── Singleton accessor ───────────────────────────────────────────────────


class TestSingletonAccessor:
    def test_get_store_returns_same_instance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        # Force a fresh singleton by clearing the module global, then
        # confirm two get_store calls return the same instance.
        monkeypatch.setattr(_intent_store_mod, "_default_store", None)
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        first = get_store()
        second = get_store()
        assert first is second

    def test_get_store_respects_monkeypatched_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        # Tests inject their own store via the module global; the
        # accessor returns the injected instance unchanged.
        injected = IntentStore(store_path=tmp_path / "injected.jsonl")
        monkeypatch.setattr(_intent_store_mod, "_default_store", injected)
        assert get_store() is injected


class TestSubstrateCitationFields:
    """substrate-citation-v1 P3 — compounding-curve telemetry fields, all
    read-time-tolerant (Optional, default at parse)."""

    def test_new_fields_round_trip(self, tmp_path):
        store = IntentStore(store_path=tmp_path / "r.jsonl")
        store.append(_record(
            cellar_retrieval_hits=3,
            cellar_citations_rendered=2,
            cellar_retrieval_config_sig="floor=none;k=5;budget=1500",
        ))
        loaded = next(iter(store.records()))
        assert loaded.cellar_retrieval_hits == 3
        assert loaded.cellar_citations_rendered == 2
        assert loaded.cellar_retrieval_config_sig == "floor=none;k=5;budget=1500"

    def test_defaults_when_unset(self):
        r = _record()
        assert r.cellar_retrieval_hits == 0
        assert r.cellar_citations_rendered == 0
        assert r.cellar_retrieval_config_sig is None

    def test_historical_record_missing_keys_parses_with_defaults(self, tmp_path):
        # A legacy JSONL line written before these fields existed: the read
        # path (IntentRecord(**data)) must default the missing keys, never crash.
        import json

        store = IntentStore(store_path=tmp_path / "legacy.jsonl")
        legacy = {
            "timestamp": "2026-05-01T00:00:00+00:00", "session_id": "s",
            "turn_id": "s#0", "user_message_stem": "old", "pattern_hash": "h",
            "intent_class": "conversation", "register_class": "casual",
            "complexity_signal": "simple", "confidence": 0.5, "outcome": "success",
        }
        store.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        rec = next(iter(store.records()))
        assert rec.cellar_retrieval_hits == 0
        assert rec.cellar_citations_rendered == 0
        assert rec.cellar_retrieval_config_sig is None
