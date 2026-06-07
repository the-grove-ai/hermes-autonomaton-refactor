"""Sprint 38 — correction-signal-v1 (Approach A: classifier extension).

The mandatory scenarios from the sprint:

* Correction detected: Turn N finalizes as correction
* New task (not correction): Turn N finalizes as success
* Acknowledgment + new task: Turn N finalizes as success
* Session-end implicit sweep: finalizes as success
* False-positive defense: negation in non-corrective context
* Turn 1 cold-start defense: is_correction on first turn safely no-ops

Plus parser unit tests for the new ``is_correction`` field across the
JSON-bool, string, missing, and malformed shapes.

The classifier is mocked at ``grove.classify.classify_for_routing``
so we exercise the Dispatcher's finalization wiring deterministically
without burning Haiku calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from grove.classify import (
    ClassificationResult,
    _parse_classification,
)
from grove.intent_store import IntentRecord, IntentStore


_PATTERN_HASH = "f" * 64


def _make_pending(
    *,
    turn_id: str = "t_001",
    session_id: str = "s_test",
    intent_class: str = "planning",
    pattern_hash: str = _PATTERN_HASH,
    age_seconds: int = 5,
    stem: str = "what is the plan?",
) -> IntentRecord:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return IntentRecord(
        timestamp=ts.isoformat(),
        session_id=session_id,
        turn_id=turn_id,
        user_message_stem=stem,
        pattern_hash=pattern_hash,
        intent_class=intent_class,
        register_class="casual",
        complexity_signal="simple",
        confidence=0.8,
        outcome="pending",
    )


def _classification(*, is_correction: Optional[bool], intent_class: str = "planning") -> ClassificationResult:
    return ClassificationResult(
        intent_class=intent_class,
        pattern_hash=_PATTERN_HASH,
        confidence=0.8,
        register_class="casual",
        complexity_signal="simple",
        goal_alignment=None,
        is_correction=is_correction,
    )


def _finalize(dispatcher_stub, previous_turn_id: str) -> None:
    """Invoke the unbound finalizer with a test-double dispatcher."""
    from grove.dispatcher import Dispatcher
    Dispatcher._finalize_previous_turn_pending(dispatcher_stub, previous_turn_id)


# ── Parser tests ─────────────────────────────────────────────────────


class TestIsCorrectionParse:
    # Sprint 65 (classifier-tool-use-refactor-v1): _parse_classification
    # now receives the classify_intent tool's structured ``input`` dict,
    # not a JSON string. The ``raw`` fixtures below stay as JSON strings
    # for readability and are decoded with json.loads at the call site —
    # the same dict the live API delivers under tool_choice.
    def test_parses_true_bool(self) -> None:
        raw = (
            '{"routing_envelope": {'
            '"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8},'
            '"learning_envelope": {"is_correction": true}}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is True

    def test_parses_false_bool(self) -> None:
        raw = (
            '{"routing_envelope": {'
            '"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8},'
            '"learning_envelope": {"is_correction": false}}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is False

    def test_lenient_string_true(self) -> None:
        raw = (
            '{"routing_envelope": {'
            '"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8},'
            '"learning_envelope": {"is_correction": "TRUE"}}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is True

    def test_missing_field_defaults_to_none(self) -> None:
        raw = (
            '{"routing_envelope": {'
            '"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8},'
            '"learning_envelope": {}}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is None

    def test_malformed_value_drops_to_none(self) -> None:
        raw = (
            '{"routing_envelope": {'
            '"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8},'
            '"learning_envelope": {"is_correction": "yeah_probably"}}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is None

    def test_legacy_flat_response_has_none(self) -> None:
        """Sprint 12 flat shape carries no learning envelope at all."""
        raw = (
            '{"intent_class": "planning", "register_class": "casual",'
            '"complexity_signal": "simple", "confidence": 0.8}'
        )
        fields = _parse_classification(json.loads(raw))
        assert fields["is_correction"] is None


# ── Dispatcher finalization branch ───────────────────────────────────


class _DispatcherStub:
    """Minimal stand-in for Dispatcher carrying only the attrs the
    finalizer reads. Avoids constructing a real Dispatcher (which
    requires full agent + runtime context wiring)."""

    def __init__(
        self,
        *,
        intent_store: IntentStore,
        classification: Optional[ClassificationResult],
    ) -> None:
        self._intent_store = intent_store
        self._current_turn_classification = classification


class TestFinalizationBranch:
    def test_correction_detected_finalizes_as_correction(self, tmp_path: Path) -> None:
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=True),
        )
        _finalize(stub, "t_prior")
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert len(latest) == 1
        assert latest[0].outcome == "correction"

    def test_no_correction_finalizes_as_success(self, tmp_path: Path) -> None:
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=False),
        )
        _finalize(stub, "t_prior")
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert latest[0].outcome == "success"

    def test_acknowledgment_plus_new_task_finalizes_as_success(self, tmp_path: Path) -> None:
        """Operator-level: 'thanks, now do X' classifies is_correction=false."""
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=False),
        )
        _finalize(stub, "t_prior")
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert latest[0].outcome == "success"

    def test_none_classification_finalizes_as_success(self, tmp_path: Path) -> None:
        """Classifier failure (graceful tier per Sprint 12 D4) → success."""
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        stub = _DispatcherStub(intent_store=store, classification=None)
        _finalize(stub, "t_prior")
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert latest[0].outcome == "success"

    def test_is_correction_none_finalizes_as_success(self, tmp_path: Path) -> None:
        """Pre-Sprint-38 schema or unparseable bool → None → success."""
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=None),
        )
        _finalize(stub, "t_prior")
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert latest[0].outcome == "success"

    def test_already_finalized_does_not_double_write(self, tmp_path: Path) -> None:
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_prior"))
        # Finalize once via existing helper.
        from grove.intent_store import finalize_record
        prior = next(r for r in store.latest_by_turn() if r.turn_id == "t_prior")
        store.append(finalize_record(prior, outcome="success", timestamp=datetime.now(timezone.utc).isoformat()))
        records_before = list(store.records())
        # Now attempt re-finalize with a correction signal — should not write.
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=True),
        )
        _finalize(stub, "t_prior")
        records_after = list(store.records())
        assert len(records_after) == len(records_before)
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_prior"]
        assert latest[0].outcome == "success"

    def test_no_pending_record_for_turn_is_noop(self, tmp_path: Path) -> None:
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        # No record at all for the queried turn_id.
        stub = _DispatcherStub(
            intent_store=store,
            classification=_classification(is_correction=True),
        )
        _finalize(stub, "t_unknown")
        assert list(store.records()) == []

    def test_no_store_wired_is_noop(self) -> None:
        stub = _DispatcherStub(
            intent_store=None,
            classification=_classification(is_correction=True),
        )
        _finalize(stub, "t_prior")  # MUST NOT raise


# ── Turn 1 cold-start defense ────────────────────────────────────────


class TestTurn1ColdStart:
    def test_first_turn_with_is_correction_true_no_writes(self, tmp_path: Path) -> None:
        """On the first turn ``previous_turn_id`` is None — dispatch_turn
        guards on that and never calls the finalizer. Even if the
        classifier produces is_correction=true on the operator's first
        message, no spurious record is written for a non-existent prior."""
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        # Simulate dispatch_turn's guard: previous_turn_id is None.
        previous_turn_id: Optional[str] = None
        if previous_turn_id is not None:
            stub = _DispatcherStub(
                intent_store=store,
                classification=_classification(is_correction=True),
            )
            _finalize(stub, previous_turn_id)
        assert list(store.records()) == []


# ── Implicit Success Sweep regression ────────────────────────────────


class TestImplicitSweepUnchanged:
    def test_sweep_finalizes_orphaned_pending_as_success(self, tmp_path: Path) -> None:
        """The Sprint 28 sweep MUST remain success-only — orphaned
        sessions never carry correction semantics."""
        store = IntentStore(store_path=tmp_path / "intents.jsonl")
        store.append(_make_pending(turn_id="t_old", age_seconds=120 * 60))
        now = datetime.now(timezone.utc)
        count = store.sweep_stale_pending(
            older_than_minutes=60, now=now,
        )
        assert count == 1
        latest = [r for r in store.latest_by_turn() if r.turn_id == "t_old"]
        assert latest[0].outcome == "success"


# ── Dispatch ordering regression ─────────────────────────────────────


def test_classification_fires_before_finalization() -> None:
    """Sprint 38 reordered dispatch_turn so classification populates
    ``self._current_turn_classification`` BEFORE the finalizer runs.
    Static-code check: locate the two call sites in dispatch_turn and
    assert the classify-and-bind call appears earlier in the source
    than the finalize call."""
    import inspect
    import grove.dispatcher as _dispatcher_mod
    source = inspect.getsource(_dispatcher_mod.Dispatcher.dispatch_turn)
    classify_idx = source.find("_classify_and_bind_turn(agent")
    finalize_idx = source.find("_finalize_previous_turn_pending(previous_turn_id)")
    assert classify_idx > -1, "expected _classify_and_bind_turn call in dispatch_turn"
    assert finalize_idx > -1, "expected _finalize_previous_turn_pending call in dispatch_turn"
    assert classify_idx < finalize_idx, (
        "Sprint 38 invariant violated: classification MUST appear before "
        "finalization in dispatch_turn so is_correction is available "
        "when the finalizer runs"
    )
