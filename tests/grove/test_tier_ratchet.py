"""Sprint 47 — TierRatchet detection tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from grove.eval.tier_ratchet import (
    MIN_SAMPLE,
    propose_routing_adjustments,
)
from grove.intent_store import IntentRecord


_PATTERN = "f" * 64


def _r(
    *,
    intent_class: str = "conversation",
    complexity: str = "simple",
    confidence: float = 0.90,
    outcome: str = "success",
    tier: str = "T2",
    turn_id: Optional[str] = None,
    idx: int = 0,
) -> IntentRecord:
    return IntentRecord(
        timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
        session_id="s_t",
        turn_id=turn_id or f"t_{intent_class}_{idx}",
        user_message_stem="probe",
        pattern_hash=_PATTERN,
        intent_class=intent_class,
        register_class="casual",
        complexity_signal=complexity,
        confidence=confidence,
        outcome=outcome,
        tier_selected=tier,
    )


def _downward_qualifying(intent_class: str, n: int = MIN_SAMPLE):
    return [
        _r(intent_class=intent_class, complexity="simple",
           confidence=0.92, outcome="success", tier="T2", idx=i)
        for i in range(n)
    ]


def _upward_qualifying(intent_class: str, n: int = MIN_SAMPLE):
    # 2 successes + 3 corrections out of 5 = correction_rate 0.6 ≥ 0.30
    records = [
        _r(intent_class=intent_class, outcome="success", tier="T2", idx=0),
        _r(intent_class=intent_class, outcome="success", tier="T2", idx=1),
        _r(intent_class=intent_class, outcome="correction", tier="T2", idx=2),
        _r(intent_class=intent_class, outcome="correction", tier="T2", idx=3),
        _r(intent_class=intent_class, outcome="correction", tier="T2", idx=4),
    ]
    return records[:n]


# ── Insufficient sample (A1) ────────────────────────────────────────


class TestInsufficientSample:
    def test_empty_returns_no_proposal(self) -> None:
        assert propose_routing_adjustments([]) == []

    def test_below_min_sample_returns_no_proposal(self) -> None:
        records = _downward_qualifying("conversation", n=MIN_SAMPLE - 1)
        assert propose_routing_adjustments(records) == []

    def test_unknown_intent_is_skipped(self) -> None:
        records = [
            _r(intent_class="unknown", idx=i) for i in range(MIN_SAMPLE + 2)
        ]
        assert propose_routing_adjustments(records) == []


# ── Downward detection ───────────────────────────────────────────────


class TestDownwardProposal:
    def test_simple_high_confidence_proposes_downward(self) -> None:
        records = _downward_qualifying("conversation", n=MIN_SAMPLE)
        proposals = propose_routing_adjustments(records)
        assert len(proposals) == 1
        p = proposals[0]
        # Sprint 32 renamed the type from Sprint 47's "routing_update"
        # to the GRV-008 § II canonical "routing_adjustment".
        assert p.type == "routing_adjustment"
        assert p.payload == {"rule": "downward", "add_intents": ["conversation"]}
        assert len(p.evidence) == MIN_SAMPLE

    def test_low_confidence_blocks_downward(self) -> None:
        records = [
            _r(intent_class="conversation", complexity="simple",
               confidence=0.80, outcome="success", tier="T2", idx=i)
            for i in range(MIN_SAMPLE)
        ]
        assert propose_routing_adjustments(records) == []

    def test_low_simple_fraction_blocks_downward(self) -> None:
        # 1 simple + 4 moderate out of 5 = simple_fraction 0.20
        records = [
            _r(intent_class="conversation", complexity="simple",
               confidence=0.95, outcome="success", tier="T2", idx=0),
        ] + [
            _r(intent_class="conversation", complexity="moderate",
               confidence=0.95, outcome="success", tier="T2", idx=i)
            for i in range(1, MIN_SAMPLE)
        ]
        assert propose_routing_adjustments(records) == []

    def test_any_correction_blocks_downward(self) -> None:
        records = _downward_qualifying("conversation", n=MIN_SAMPLE)
        # Replace one with a correction.
        records[0] = _r(
            intent_class="conversation", complexity="simple",
            confidence=0.92, outcome="correction", tier="T2", idx=0,
        )
        assert propose_routing_adjustments(records) == []

    def test_low_success_rate_blocks_downward(self) -> None:
        # 3 success + 2 drop out of 5 = success 0.60, no correction.
        records = (
            _downward_qualifying("conversation", n=3)
            + [
                _r(intent_class="conversation", complexity="simple",
                   confidence=0.92, outcome="drop", tier="T2", idx=i)
                for i in range(3, MIN_SAMPLE)
            ]
        )
        assert propose_routing_adjustments(records) == []

    def test_majority_t1_blocks_downward(self) -> None:
        records = [
            _r(intent_class="conversation", complexity="simple",
               confidence=0.92, outcome="success", tier="T1", idx=i)
            for i in range(MIN_SAMPLE)
        ]
        assert propose_routing_adjustments(records) == []

    def test_already_in_downward_intents_blocks(self) -> None:
        records = _downward_qualifying("conversation", n=MIN_SAMPLE)
        current = {
            "downward": {
                "match": {"intents": ["conversation"]},
            }
        }
        assert propose_routing_adjustments(
            records, current_routing_rules=current,
        ) == []


# ── Upward detection ────────────────────────────────────────────────


class TestUpwardProposal:
    def test_high_correction_rate_proposes_upward(self) -> None:
        records = _upward_qualifying("debugging", n=MIN_SAMPLE)
        proposals = propose_routing_adjustments(records)
        assert len(proposals) == 1
        p = proposals[0]
        assert p.payload == {"rule": "upward", "add_intents": ["debugging"]}

    def test_low_correction_rate_blocks_upward(self) -> None:
        # 4 success + 1 correction = correction_rate 0.20 < 0.30
        records = (
            [_r(intent_class="debugging", outcome="success", idx=i) for i in range(4)]
            + [_r(intent_class="debugging", outcome="correction", idx=4)]
        )
        assert propose_routing_adjustments(records) == []

    def test_already_in_upward_intents_blocks(self) -> None:
        records = _upward_qualifying("debugging", n=MIN_SAMPLE)
        current = {
            "upward": {
                "match": {"intents": ["debugging"]},
            }
        }
        assert propose_routing_adjustments(
            records, current_routing_rules=current,
        ) == []


# ── Compound proposals ──────────────────────────────────────────────


class TestCompoundProposals:
    def test_one_downward_one_upward_returned(self) -> None:
        records = (
            _downward_qualifying("conversation", n=MIN_SAMPLE)
            + _upward_qualifying("debugging", n=MIN_SAMPLE)
        )
        proposals = propose_routing_adjustments(records)
        rules = sorted(p.payload["rule"] for p in proposals)
        assert rules == ["downward", "upward"]
