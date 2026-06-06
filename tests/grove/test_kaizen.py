"""Tests for the grove.kaizen package.

Originally Sprint 06b — verified the package exports and the three
Flywheel stubs raised NotImplementedError. Sprint 28 Phase 5 wires
the stubs to read the intent store and return structured results;
the NotImplementedError contract is replaced by the read-only data
contract documented in each stub.

Curator-copy interface tests stay unchanged — the Sprint 06b
out-of-scope guard for ``agent.curator`` is still in force.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from grove.intent_store import IntentRecord, IntentStore


# ── package exports ──────────────────────────────────────────────────────


def test_package_exports_three_stub_classes() -> None:
    from grove import kaizen
    assert hasattr(kaizen, "IntentPatternDetector")
    assert hasattr(kaizen, "TierRatchet")
    assert hasattr(kaizen, "UsageRefiner")
    # Sprint 63 added the synthesizer's PROPOSE-stage orchestrator.
    assert set(kaizen.__all__) == {
        "IntentPatternDetector", "TierRatchet", "UsageRefiner",
        "run_synthesis_pass",
    }


# ── Phase 5 read-only behavior — shared helpers ──────────────────────────


def _record(
    *,
    session_id: str = "s",
    turn_id: str = "s#1",
    pattern_hash: str = "ph-a",
    intent_class: str = "code_generation",
    confidence: float = 0.9,
    tier_selected: str = "T2",
    tools_yielded: tuple = (),
    outcome: str = "success",
    timestamp: str | None = None,
) -> IntentRecord:
    return IntentRecord(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        session_id=session_id,
        turn_id=turn_id,
        user_message_stem="m",
        pattern_hash=pattern_hash,
        intent_class=intent_class,
        register_class="technical",
        complexity_signal="moderate",
        confidence=confidence,
        outcome=outcome,
        tier_selected=tier_selected,
        tools_yielded=tools_yielded,
    )


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


# ── IntentPatternDetector ────────────────────────────────────────────────


class TestIntentPatternDetector:
    """Sprint 28 Phase 5 read-only contract — returns recurring patterns
    from the store, never raises NotImplementedError."""

    def test_construction_defaults_to_singleton_store(self):
        # No explicit store → the constructor pulls the module
        # singleton via get_store(). The per-test GROVE_HOME isolation
        # plus tests/conftest._reset_module_state ensure the singleton
        # is fresh per test.
        from grove.kaizen import IntentPatternDetector
        detector = IntentPatternDetector()
        assert detector._store is not None

    def test_empty_store_returns_empty_list(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        assert IntentPatternDetector(tmp_store).detect() == []

    def test_returns_only_patterns_above_threshold(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        # Two patterns: "ph-a" appears 4 times, "ph-b" appears 2 times.
        # Default threshold=3 surfaces ph-a only.
        for i in range(4):
            tmp_store.append(_record(
                turn_id=f"s#{i}", pattern_hash="ph-a",
            ))
        for i in range(4, 6):
            tmp_store.append(_record(
                turn_id=f"s#{i}", pattern_hash="ph-b",
            ))
        results = IntentPatternDetector(tmp_store).detect()
        assert [r["pattern_hash"] for r in results] == ["ph-a"]
        assert results[0]["count"] == 4

    def test_threshold_argument_lowers_the_floor(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        for i in range(2):
            tmp_store.append(_record(
                turn_id=f"s#{i}", pattern_hash="ph-rare",
            ))
        # threshold=1 surfaces every observed pattern.
        results = IntentPatternDetector(tmp_store).detect(threshold=1)
        assert [r["pattern_hash"] for r in results] == ["ph-rare"]
        assert results[0]["count"] == 2

    def test_window_days_excludes_old_records(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        for i in range(5):
            tmp_store.append(_record(
                turn_id=f"old#{i}", pattern_hash="ph-old", timestamp=old_ts,
            ))
        # 14-day default window excludes the old records.
        assert IntentPatternDetector(tmp_store).detect() == []

    def test_session_count_distinct_per_pattern(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        # 3 records, 2 sessions, all same pattern → count=3, sessions=2.
        for i, sess in enumerate(["sA", "sA", "sB"]):
            tmp_store.append(_record(
                session_id=sess, turn_id=f"{sess}#{i}",
                pattern_hash="ph-shared",
            ))
        result = IntentPatternDetector(tmp_store).detect(threshold=2)[0]
        assert result["count"] == 3
        assert result["session_count"] == 2

    def test_results_sorted_by_count_descending(self, tmp_store):
        from grove.kaizen import IntentPatternDetector
        for i in range(3):
            tmp_store.append(_record(
                turn_id=f"low#{i}", pattern_hash="ph-low",
            ))
        for i in range(5):
            tmp_store.append(_record(
                turn_id=f"hi#{i}", pattern_hash="ph-hi",
            ))
        results = IntentPatternDetector(tmp_store).detect()
        assert [r["pattern_hash"] for r in results] == ["ph-hi", "ph-low"]

    def test_uses_latest_by_turn_collapse_view(self, tmp_store):
        # Phase 4 leaves both a pending and a success record per turn.
        # The detector must collapse so each turn counts ONCE, not twice.
        from grove.kaizen import IntentPatternDetector
        # Provisional pair for 3 turns, same pattern.
        for i in range(3):
            base = _record(turn_id=f"s#{i}", pattern_hash="ph-x", outcome="pending")
            tmp_store.append(base)
            # Finalization with a later timestamp.
            tmp_store.append(_record(
                turn_id=f"s#{i}", pattern_hash="ph-x", outcome="success",
                timestamp=(
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat(),
            ))
        # 3 turns total → count=3 (not 6 — would be 6 if we counted raw records).
        result = IntentPatternDetector(tmp_store).detect()[0]
        assert result["count"] == 3


# ── TierRatchet ──────────────────────────────────────────────────────────


class TestTierRatchet:
    """Sprint 28 Phase 5 read-only contract — returns per-tier usage
    analysis, never raises NotImplementedError."""

    def test_construction_defaults_to_singleton_store(self):
        from grove.kaizen import TierRatchet
        assert TierRatchet()._store is not None

    def test_empty_store_returns_empty_dict(self, tmp_store):
        from grove.kaizen import TierRatchet
        assert TierRatchet(tmp_store).ratchet() == {}

    def test_groups_by_tier_with_counts_and_avg_confidence(self, tmp_store):
        from grove.kaizen import TierRatchet
        tmp_store.append(_record(turn_id="t1", tier_selected="T1", confidence=0.8))
        tmp_store.append(_record(turn_id="t2", tier_selected="T1", confidence=0.6))
        tmp_store.append(_record(turn_id="t3", tier_selected="T3", confidence=0.95))
        out = TierRatchet(tmp_store).ratchet()
        assert out["T1"]["count"] == 2
        assert out["T1"]["avg_confidence"] == 0.7
        assert out["T3"]["count"] == 1
        assert out["T3"]["avg_confidence"] == 0.95

    def test_intent_classes_per_tier_are_sorted_unique(self, tmp_store):
        from grove.kaizen import TierRatchet
        tmp_store.append(_record(
            turn_id="t1", tier_selected="T2", intent_class="code_generation",
        ))
        tmp_store.append(_record(
            turn_id="t2", tier_selected="T2", intent_class="debugging",
        ))
        tmp_store.append(_record(
            turn_id="t3", tier_selected="T2", intent_class="code_generation",
        ))
        out = TierRatchet(tmp_store).ratchet()
        assert out["T2"]["intent_classes"] == ["code_generation", "debugging"]

    def test_unknown_tier_buckets_records_with_no_tier(self, tmp_store):
        # Vanilla-install records have tier_selected=None. Phase 5
        # buckets them under "unknown" so they're queryable rather
        # than dropped.
        from grove.kaizen import TierRatchet
        tmp_store.append(_record(turn_id="t1", tier_selected=None))
        out = TierRatchet(tmp_store).ratchet()
        assert "unknown" in out
        assert out["unknown"]["count"] == 1


# ── UsageRefiner ─────────────────────────────────────────────────────────


class TestUsageRefiner:
    """Sprint 28 Phase 5 read-only contract — returns tool-usage
    frequency analysis, never raises NotImplementedError."""

    def test_construction_defaults_to_singleton_store(self):
        from grove.kaizen import UsageRefiner
        assert UsageRefiner()._store is not None

    def test_empty_store_returns_empty_list(self, tmp_store):
        from grove.kaizen import UsageRefiner
        assert UsageRefiner(tmp_store).refine() == []

    def test_aggregates_tool_frequency_across_turns(self, tmp_store):
        from grove.kaizen import UsageRefiner
        tmp_store.append(_record(
            turn_id="t1", tools_yielded=("read_file", "web_search"),
        ))
        tmp_store.append(_record(
            turn_id="t2", tools_yielded=("read_file",),
        ))
        tmp_store.append(_record(
            turn_id="t3", tools_yielded=("read_file", "search_files"),
        ))
        results = UsageRefiner(tmp_store).refine()
        by_tool = {r["tool"]: r for r in results}
        assert by_tool["read_file"]["frequency"] == 3
        assert by_tool["web_search"]["frequency"] == 1
        assert by_tool["search_files"]["frequency"] == 1

    def test_session_count_distinct_per_tool(self, tmp_store):
        from grove.kaizen import UsageRefiner
        tmp_store.append(_record(
            session_id="sA", turn_id="sA#1", tools_yielded=("read_file",),
        ))
        tmp_store.append(_record(
            session_id="sB", turn_id="sB#1", tools_yielded=("read_file",),
        ))
        tmp_store.append(_record(
            session_id="sA", turn_id="sA#2", tools_yielded=("read_file",),
        ))
        result = next(
            r for r in UsageRefiner(tmp_store).refine()
            if r["tool"] == "read_file"
        )
        assert result["frequency"] == 3
        assert result["session_count"] == 2

    def test_results_sorted_by_frequency_descending(self, tmp_store):
        from grove.kaizen import UsageRefiner
        tmp_store.append(_record(turn_id="t1", tools_yielded=("rare",)))
        tmp_store.append(_record(turn_id="t2", tools_yielded=("common",)))
        tmp_store.append(_record(turn_id="t3", tools_yielded=("common",)))
        tmp_store.append(_record(turn_id="t4", tools_yielded=("common",)))
        results = UsageRefiner(tmp_store).refine()
        assert [r["tool"] for r in results] == ["common", "rare"]

    def test_text_only_turns_contribute_nothing(self, tmp_store):
        # A turn with no tools_yielded (FinalResponse without tool calls)
        # produces no refiner entries — only the populated tools surface.
        from grove.kaizen import UsageRefiner
        tmp_store.append(_record(turn_id="t1", tools_yielded=()))
        tmp_store.append(_record(turn_id="t2", tools_yielded=("read_file",)))
        results = UsageRefiner(tmp_store).refine()
        assert [r["tool"] for r in results] == ["read_file"]


# ── Curator copy (Sprint 06b out-of-scope guard, unchanged) ──────────────


_CURATOR_PUBLIC_API = (
    "load_state",
    "save_state",
    "set_paused",
    "is_paused",
    "is_enabled",
    "get_interval_hours",
    "get_min_idle_hours",
    "get_stale_after_days",
    "get_archive_after_days",
    "should_run_now",
    "apply_automatic_transitions",
    "DEFAULT_INTERVAL_HOURS",
    "CURATOR_REVIEW_PROMPT",
)


def test_kaizen_curator_importable() -> None:
    from grove.kaizen import curator as kaizen_curator
    assert kaizen_curator is not None


def test_kaizen_curator_matches_agent_curator_interface() -> None:
    """The Kaizen-namespace curator exposes the same public surface as the
    canonical agent.curator — it is a verbatim copy."""
    from grove.kaizen import curator as kaizen_curator
    from agent import curator as agent_curator
    for symbol in _CURATOR_PUBLIC_API:
        assert hasattr(kaizen_curator, symbol), f"grove.kaizen.curator missing {symbol}"
        assert hasattr(agent_curator, symbol), f"agent.curator missing {symbol}"


def test_agent_curator_still_importable() -> None:
    """Sprint 06b out-of-scope guard: agent/curator.py is unmodified and
    remains the canonical implementation for existing consumers."""
    from agent import curator as agent_curator
    assert callable(agent_curator.load_state)
    assert callable(agent_curator.should_run_now)
