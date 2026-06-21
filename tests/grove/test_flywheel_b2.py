"""B2 flywheel-detect-v1 — wire the dark detector to the B1 spine.

Proofs:
  * C1 — stateless + stable cluster id; TierRatchet emits routing_adjustment
    carrying populated source_patterns; proposal_id unaffected (hash-excluded).
  * C2 — the runner queues proposals and a re-run over unchanged store state
    DEDUPS (same spike → one proposal, not two).
  * C3 — the scoped approve-time gate refuses a routing_adjustment with empty
    source_patterns, while a zone_promotion with empty source_patterns still
    approves (legacy producers unaffected).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)
from grove.eval.tier_ratchet import (
    compute_cluster_id,
    propose_routing_adjustments,
)
from grove.intent_store import IntentRecord, IntentStore


def _r(*, intent_class, outcome, idx, pattern="a" * 64):
    return IntentRecord(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        session_id=f"s{idx % 3}",
        turn_id=f"t_{intent_class}_{idx}",
        user_message_stem="probe",
        pattern_hash=pattern,
        intent_class=intent_class,
        register_class="technical",
        complexity_signal="moderate",
        confidence=0.9,
        outcome=outcome,
        tier_selected="T1",
    )


def _correction_spike(intent_class="date_arithmetic"):
    """5 records, 3 corrections (correction_rate 0.6 ≥ 0.30) → one upward."""
    return [
        _r(intent_class=intent_class, outcome="success", idx=0),
        _r(intent_class=intent_class, outcome="success", idx=1),
        _r(intent_class=intent_class, outcome="correction", idx=2),
        _r(intent_class=intent_class, outcome="correction", idx=3),
        _r(intent_class=intent_class, outcome="correction", idx=4),
    ]


# ── C1 — cluster id ──────────────────────────────────────────────────


def test_cluster_id_is_stateless_and_stable() -> None:
    a = compute_cluster_id("date_arithmetic", ("h1", "h2"))
    b = compute_cluster_id("date_arithmetic", ("h1", "h2"))
    assert a == b                              # reproducible, no persistence
    assert a.startswith("cluster:sha256:")
    # Different members → different id (self-describing).
    assert compute_cluster_id("date_arithmetic", ("h1", "h3")) != a
    assert compute_cluster_id("other", ("h1", "h2")) != a


def test_ratchet_emits_routing_adjustment_with_source_patterns() -> None:
    proposals = propose_routing_adjustments(
        _correction_spike(), current_routing_rules={},
    )
    assert len(proposals) == 1
    p = proposals[0]
    assert p.type == PROPOSAL_TYPE_ROUTING_ADJUSTMENT
    assert p.payload == {"rule": "ratchet_promoted_t3", "add_intents": ["date_arithmetic"]}
    # source_patterns is populated with exactly the cluster id for this signal.
    assert p.source_patterns == (
        compute_cluster_id("date_arithmetic", ("a" * 64,)),
    )


def test_source_patterns_does_not_affect_proposal_id() -> None:
    """Re-confirm B1's hash exclusion at the producer: the id is computed from
    type+payload+evidence only, so the populated source_patterns can't shift it."""
    p = propose_routing_adjustments(
        _correction_spike(), current_routing_rules={},
    )[0]
    assert p.source_patterns  # non-empty
    assert p.proposal_id == compute_proposal_id(
        type=p.type, payload=p.payload, evidence=p.evidence,
    )


# ── C2 — idempotent wiring ───────────────────────────────────────────


def test_detection_rerun_dedups_same_spike(tmp_path: Path) -> None:
    """Run detection twice on the SAME state → one proposal, not two."""
    queue = tmp_path / "proposals.jsonl"
    records = _correction_spike()
    for _ in range(2):
        for proposal in propose_routing_adjustments(records, current_routing_rules={}):
            append(proposal, path=queue)
    assert len(read_all(path=queue)) == 1


def test_run_tier_ratchet_scan_queues_then_dedups(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    store = IntentStore(store_path=tmp_path / "intent.jsonl")
    for rec in _correction_spike():
        store.append(rec)

    new1, dup1 = flywheel_cli.run_tier_ratchet_scan(
        store=store, current_routing_rules={}, queue_path=queue,
    )
    assert (new1, dup1) == (1, 0)
    # Second run over unchanged store state: dedups, does not stack.
    new2, dup2 = flywheel_cli.run_tier_ratchet_scan(
        store=store, current_routing_rules={}, queue_path=queue,
    )
    assert (new2, dup2) == (0, 1)
    queued = read_all(path=queue)
    assert len(queued) == 1
    assert queued[0].source_patterns  # the cluster rode through into the queue


# ── C3 — scoped approve-time gate ────────────────────────────────────


def _routing_proposal(*, source_patterns):
    payload = {"rule": "upward", "add_intents": ["date_arithmetic"]}
    evidence = ("t1", "t2")
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload=payload,
        evidence=evidence,
        eval_hash="",
        created_at="2026-06-01T00:00:00+00:00",
        source_patterns=source_patterns,
    )


def test_routing_adjustment_empty_cluster_refused(tmp_path: Path, capsys) -> None:
    queue = tmp_path / "proposals.jsonl"
    p = _routing_proposal(source_patterns=())
    append(p, path=queue)
    rc = flywheel_cli.cli_approve(
        p.proposal_id, queue_path=queue, machine_path=tmp_path / "m.yaml",
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "no source_patterns" in err
    # Non-destructive: the proposal is retained for the operator.
    assert len(read_all(path=queue)) == 1
    assert not (tmp_path / "m.yaml").exists()


def test_routing_adjustment_with_cluster_approves(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "m.yaml"
    p = _routing_proposal(source_patterns=("cluster:sha256:x",))
    append(p, path=queue)
    rc = flywheel_cli.cli_approve(
        p.proposal_id, queue_path=queue, machine_path=machine,
    )
    assert rc == 0
    assert machine.exists()
    assert read_all(path=queue) == []


def test_zone_promotion_empty_cluster_still_approves(tmp_path: Path, monkeypatch) -> None:
    """The gate is scoped to routing_adjustment ONLY — other types keep
    approving with empty source_patterns (no legacy retrofit)."""
    queue = tmp_path / "proposals.jsonl"
    payload = {
        "tool": "terminal",
        "pattern": r".*\.grove/skills/cal/.*",
        "zone": "green",
        "reason": "allow cal",
    }
    p = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ZONE_PROMOTION, payload=payload, evidence=("t1",),
        ),
        type=PROPOSAL_TYPE_ZONE_PROMOTION,
        payload=payload,
        evidence=("t1",),
        eval_hash="",
        created_at="2026-06-01T00:00:00+00:00",
        source_patterns=(),  # empty — must NOT block a zone_promotion
    )
    append(p, path=queue)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", lambda **kw: None)
    rc = flywheel_cli.cli_approve(p.proposal_id, queue_path=queue)
    assert rc == 0
    assert read_all(path=queue) == []
