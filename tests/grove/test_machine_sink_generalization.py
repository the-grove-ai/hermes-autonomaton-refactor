"""machine-sink-generalization-v1 — generalized ratchet sinks + memory
enrichment.

Part A: tier ratchet emits ratchet_promoted_tX (not downward/upward); the
validation gate accepts the new sinks + legacy names, rejects unknown.
Part B: RoutingProposal carries an optional semantic_justification that is
excluded from proposal identity, populated from memory at generation time,
and rendered into the Kaizen offering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from grove.eval.proposal_queue import (
    RoutingProposal,
    append as queue_append,
    compute_proposal_id,
    read_all,
)
from grove.eval.tier_ratchet import (
    MIN_SAMPLE,
    SINK_DOWNWARD,
    SINK_UPWARD,
    propose_routing_adjustments,
)
from grove.intent_store import IntentRecord

_PATTERN = "a" * 64


def _r(*, intent_class="conversation", complexity="simple", confidence=0.92,
       outcome="success", tier="T2", stem="probe", idx=0) -> IntentRecord:
    return IntentRecord(
        timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
        session_id="s", turn_id=f"t_{intent_class}_{idx}", user_message_stem=stem,
        pattern_hash=_PATTERN, intent_class=intent_class, register_class="casual",
        complexity_signal=complexity, confidence=confidence, outcome=outcome,
        tier_selected=tier,
    )


def _downward(intent="conversation", n=MIN_SAMPLE):
    return [_r(intent_class=intent, idx=i) for i in range(n)]


def _upward(intent="debugging", n=MIN_SAMPLE):
    return (
        [_r(intent_class=intent, outcome="success", idx=0),
         _r(intent_class=intent, outcome="success", idx=1)]
        + [_r(intent_class=intent, outcome="correction", idx=i) for i in range(2, n)]
    )


class _FakeMemoryStore:
    def __init__(self, records=None, raises=False):
        self._records = records or []
        self._raises = raises

    def query(self, **kwargs):
        if self._raises:
            raise RuntimeError("memory store boom")
        return self._records


class _Rec:
    def __init__(self, content):
        self.content = content


# ── Part A: sink generalization ───────────────────────────────────────────

def test_downward_emits_ratchet_promoted_t1():
    proposals = propose_routing_adjustments(_downward())
    assert len(proposals) == 1
    assert proposals[0].payload["rule"] == "ratchet_promoted_t1"
    assert SINK_DOWNWARD == "ratchet_promoted_t1"


def test_upward_emits_ratchet_promoted_t3():
    proposals = propose_routing_adjustments(_upward())
    assert len(proposals) == 1
    assert proposals[0].payload["rule"] == "ratchet_promoted_t3"
    assert SINK_UPWARD == "ratchet_promoted_t3"


def test_validation_accepts_new_sinks():
    from grove.flywheel_cli import _validate_routing_rule
    for name in ("ratchet_promoted_t1", "ratchet_promoted_t2", "ratchet_promoted_t3"):
        _validate_routing_rule(name)  # must not raise


def test_validation_accepts_legacy_names():
    from grove.flywheel_cli import _validate_routing_rule
    _validate_routing_rule("downward")
    _validate_routing_rule("upward")


def test_validation_rejects_unknown():
    from grove.flywheel_cli import _validate_routing_rule
    with pytest.raises(ValueError):
        _validate_routing_rule("bogus_rule")


def test_dedup_intent_already_in_new_sink():
    current = {"ratchet_promoted_t1": {"match": {"intents": ["conversation"]}}}
    assert propose_routing_adjustments(
        _downward("conversation"), current_routing_rules=current,
    ) == []


def test_apply_path_writes_new_sink(tmp_path):
    from grove.flywheel_cli import _approve_routing_adjustment
    payload = {"rule": "ratchet_promoted_t2", "add_intents": ["analysis"]}
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=("t1",)),
        type="routing_adjustment", payload=payload, evidence=("t1",),
        eval_hash="", created_at="2026-06-01T00:00:00+00:00",
        source_patterns=("c1",),
    )
    machine = tmp_path / "routing.autonomaton.yaml"
    _approve_routing_adjustment(proposal, machine_path=machine)
    text = machine.read_text()
    assert "ratchet_promoted_t2" in text
    assert "analysis" in text


# ── Part B: memory-enriched justification ─────────────────────────────────

def test_justification_round_trips(tmp_path):
    payload = {"rule": "ratchet_promoted_t1", "add_intents": ["conversation"]}
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_adjustment", payload=payload, evidence=("t1",)),
        type="routing_adjustment", payload=payload, evidence=("t1",),
        eval_hash="", created_at="2026-06-01T00:00:00+00:00",
        source_patterns=(), semantic_justification="Context: uses Notion.",
    )
    qp = tmp_path / "proposals.jsonl"
    queue_append(proposal, path=qp)
    [restored] = read_all(path=qp)
    assert restored.semantic_justification == "Context: uses Notion."


def test_old_record_without_justification_deserializes(tmp_path):
    import json
    qp = tmp_path / "proposals.jsonl"
    # an old record: no semantic_justification key
    old = {"proposal_id": "sha256:x", "type": "routing_adjustment",
           "payload": {"rule": "downward", "add_intents": ["conversation"]},
           "evidence": ["t1"], "eval_hash": "", "created_at": "2026-01-01T00:00:00+00:00",
           "source_patterns": []}
    qp.write_text(json.dumps(old) + "\n")
    [restored] = read_all(path=qp)
    assert restored.semantic_justification == ""   # default


def test_justification_excluded_from_proposal_id():
    payload = {"rule": "ratchet_promoted_t1", "add_intents": ["conversation"]}
    evidence = ("t1", "t2")
    pid = compute_proposal_id(type="routing_adjustment", payload=payload, evidence=evidence)

    def _mk(justification):
        return RoutingProposal(
            proposal_id=pid, type="routing_adjustment", payload=payload,
            evidence=evidence, eval_hash="", created_at="2026-06-01T00:00:00+00:00",
            semantic_justification=justification,
        )

    a = _mk("Context: A")
    b = _mk("Context: completely different")
    assert a.proposal_id == b.proposal_id   # identity stable across justification


def test_rendering_includes_justification():
    from grove.flywheel_cli import _summary_routing_adjustment
    payload = {"rule": "ratchet_promoted_t1", "add_intents": ["conversation"]}
    proposal = RoutingProposal(
        proposal_id="sha256:x", type="routing_adjustment", payload=payload,
        evidence=("t1",), eval_hash="", created_at="2026-06-01T00:00:00+00:00",
        semantic_justification="Context: prefers CLI.",
    )
    out = _summary_routing_adjustment(proposal)
    assert "add conversation to routing.ratchet_promoted_t1" in out
    assert "(Context: prefers CLI.)" in out


def test_rendering_without_justification_no_parenthetical():
    from grove.flywheel_cli import _summary_routing_adjustment
    payload = {"rule": "ratchet_promoted_t1", "add_intents": ["conversation"]}
    proposal = RoutingProposal(
        proposal_id="sha256:x", type="routing_adjustment", payload=payload,
        evidence=("t1",), eval_hash="", created_at="2026-06-01T00:00:00+00:00",
    )
    out = _summary_routing_adjustment(proposal)
    assert out == "add conversation to routing.ratchet_promoted_t1"
    assert "(" not in out


def test_memory_enrichment_populates_justification():
    store = _FakeMemoryStore(records=[_Rec("Operator uses Python."),
                                      _Rec("Prefers local execution.")])
    proposals = propose_routing_adjustments(_downward(), memory_store=store)
    assert len(proposals) == 1
    j = proposals[0].semantic_justification
    assert "Context: Operator uses Python." in j
    assert "Also: Prefers local execution." in j


def test_memory_query_failure_does_not_block_generation():
    store = _FakeMemoryStore(raises=True)
    proposals = propose_routing_adjustments(_downward(), memory_store=store)
    assert len(proposals) == 1                       # still generated
    assert proposals[0].semantic_justification == ""  # enrichment empty
