"""Sprint 47 — TierRatchet, the first Flywheel proposal generator.

Reads the Sprint 28 intent feed, identifies tier-mismatch patterns,
and emits ``RoutingProposal``s the operator review pipeline gates
through the Sprint 46 hero suite before queuing.

The detector is precision-first per GATE-A: tight thresholds that
prefer false negatives (no proposal when one is plausible) over
false positives (a proposal that passes the gate but degrades
behavior). The operator broadens the thresholds in a follow-up
sprint if they want more proposal velocity.

Detection rules (v0.1):

* Downward — propose adding an intent to ``routing_rules.downward.
  match.intents`` when, for that intent class:

    n ≥ MIN_SAMPLE (5)
    avg_confidence ≥ 0.85
    simple_frac ≥ 0.80
    success_rate ≥ 0.90
    correction_rate == 0
    tier_distribution skewed to T2 (>= 50% T2)
    intent NOT already present in current downward intents

* Upward — propose adding an intent to ``routing_rules.upward.
  match.intents`` when, for that intent class:

    n ≥ MIN_SAMPLE (5)
    correction_rate ≥ 0.30
    intent NOT already present in current upward intents

Both detectors return at most one proposal per direction per call —
the operator reviews one routing change at a time. Future sprints
may emit batched proposals once approval flow supports compound
review.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    RoutingProposal,
    _now_iso,
    compute_proposal_id,
)
from grove.intent_store import IntentRecord

logger = logging.getLogger(__name__)


__all__ = [
    "MIN_SAMPLE",
    "MIN_AVG_CONFIDENCE_DOWNWARD",
    "MIN_SIMPLE_FRACTION_DOWNWARD",
    "MIN_SUCCESS_RATE_DOWNWARD",
    "MIN_CORRECTION_RATE_UPWARD",
    "SINK_DOWNWARD",
    "SINK_UPWARD",
    "compute_cluster_id",
    "propose_routing_adjustments",
]


# Detection thresholds — operator-approved at GATE-A. Precision-first.
MIN_SAMPLE = 5
MIN_AVG_CONFIDENCE_DOWNWARD = 0.85
MIN_SIMPLE_FRACTION_DOWNWARD = 0.80
MIN_SUCCESS_RATE_DOWNWARD = 0.90
MIN_CORRECTION_RATE_UPWARD = 0.30

# machine-sink-generalization-v1 — generalized machine-sink rule names.
# The detection stays a direction (cheaper / escalate); the target tier is
# implied by the detector's preconditions: downward fires only on a T2-skewed
# simple/high-success cluster (route it cheaper → T1); upward fires on a
# correction-heavy cluster (escalate → T3). The operator's routing.config.yaml
# declares these sink rules' target_tier; the machine file set-unions the
# intents in. (ratchet_promoted_t2 is reserved for a future detector — the
# current two directions do not target T2.)
SINK_DOWNWARD = "ratchet_promoted_t1"   # was "downward"
SINK_UPWARD = "ratchet_promoted_t3"     # was "upward"


def _build_justification(
    memory_store: Any,
    intent_class: str,
    message_stems: Tuple[str, ...],
) -> str:
    """Memory-enriched rationale for a routing proposal (Gemini amendment).

    Detection stays purely statistical; this only enriches the RENDERING.
    Queries the active memory index for operator context related to the
    cluster. Additive — ANY failure (no store, empty index, query error)
    returns "" and the proposal still generates. This is the one sanctioned
    silent degradation: an un-enriched proposal is still a valid proposal.
    """
    if memory_store is None:
        return ""
    try:
        keywords = list(message_stems[:3]) if message_stems else [intent_class]
        records = memory_store.query(
            keywords=keywords,
            entity_types=["DomainFact", "OperatorPreference"],
            min_confidence=0.5,
        )
        if not records:
            return ""
        justification = f"Context: {records[0].content}"
        if len(records) > 1:
            justification += f" Also: {records[1].content}"
        return justification
    except Exception as exc:  # noqa: BLE001 — enrichment never blocks generation
        logger.debug("[tier_ratchet] memory enrichment failed: %r", exc)
        return ""


def compute_cluster_id(
    intent_class: str, member_pattern_hashes: Tuple[str, ...],
) -> str:
    """Stateless, content-addressable id for a detected pattern cluster (B2).

    Fork 4 — stateless, NO persistence. A SHA-256 over the ``intent_class``
    plus the cluster's DISTINCT member ``pattern_hash`` set in sorted order.
    The id is self-describing and reproducible: two detection runs over the
    SAME cluster members compute the SAME id. Because the same cluster also
    carries the same ``evidence_turn_ids`` (and ``compute_proposal_id`` folds
    type+payload+evidence — never ``source_patterns``), the resulting
    ``proposal_id`` is also stable → a re-run dedups in the queue instead of
    stacking. The id changes ONLY when the cluster's member-pattern set changes
    (a genuinely different signal).

    ``member_pattern_hashes`` must already be the sorted distinct set; callers
    build it in :func:`_aggregate_by_intent`.
    """
    seed = intent_class + "|" + ",".join(member_pattern_hashes)
    return "cluster:sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _aggregate_by_intent(
    records: Iterable[IntentRecord],
) -> Dict[str, Dict[str, Any]]:
    """Group records by intent_class and compute per-class statistics.

    Returns a mapping ``intent_class → {n, success_rate, correction_rate,
    avg_confidence, simple_frac, tier_distribution, evidence_turn_ids,
    member_pattern_hashes}``. Intents with ``intent_class == "unknown"`` are
    skipped — they carry no routing-rule placement decision.
    """
    by_intent: Dict[str, List[IntentRecord]] = defaultdict(list)
    for record in records:
        if not record.intent_class or record.intent_class == "unknown":
            continue
        by_intent[record.intent_class].append(record)

    out: Dict[str, Dict[str, Any]] = {}
    for intent_class, recs in by_intent.items():
        n = len(recs)
        if n == 0:
            continue
        success = sum(1 for r in recs if r.outcome == "success")
        correction = sum(1 for r in recs if r.outcome == "correction")
        simple = sum(1 for r in recs if r.complexity_signal == "simple")
        confs = [r.confidence for r in recs if r.confidence is not None]
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        tier_dist: Dict[str, int] = defaultdict(int)
        for r in recs:
            if r.tier_selected:
                tier_dist[r.tier_selected] += 1
        out[intent_class] = {
            "n": n,
            "success_rate": success / n,
            "correction_rate": correction / n,
            "avg_confidence": avg_conf,
            "simple_fraction": simple / n,
            "tier_distribution": dict(tier_dist),
            "evidence_turn_ids": tuple(sorted(r.turn_id for r in recs)),
            "member_pattern_hashes": tuple(sorted({r.pattern_hash for r in recs})),
            # machine-sink-generalization-v1 — recent operator message stems
            # (newest first), the keyword seed for memory enrichment.
            "message_stems": tuple(
                r.user_message_stem
                for r in sorted(recs, key=lambda r: r.timestamp, reverse=True)
                if r.user_message_stem
            ),
        }
    return out


def _intent_already_listed(
    intent_class: str,
    *,
    current_routing_rules: Dict[str, Any],
    rule_name: str,
) -> bool:
    """Check whether ``intent_class`` already appears in
    ``routing_rules.<rule_name>.match.intents``.

    Defensive about shape: a malformed or absent block reads as
    "intent not listed" so the detector proposes; the gate catches
    the apply-time mistake if the shape was actually broken.
    """
    rule = (current_routing_rules or {}).get(rule_name) or {}
    match = rule.get("match") or {}
    intents = match.get("intents") or []
    if isinstance(intents, list):
        return intent_class in intents
    return False


def _maybe_downward(
    intent_class: str,
    stats: Dict[str, Any],
    current_routing_rules: Dict[str, Any],
    memory_store: Any = None,
) -> Optional[RoutingProposal]:
    """Apply the downward detection rules. Return one proposal or None."""
    if stats["n"] < MIN_SAMPLE:
        return None
    if stats["avg_confidence"] < MIN_AVG_CONFIDENCE_DOWNWARD:
        return None
    if stats["simple_fraction"] < MIN_SIMPLE_FRACTION_DOWNWARD:
        return None
    if stats["success_rate"] < MIN_SUCCESS_RATE_DOWNWARD:
        return None
    if stats["correction_rate"] > 0.0:
        return None
    tier_dist = stats["tier_distribution"]
    total = sum(tier_dist.values()) or 1
    if tier_dist.get("T2", 0) / total < 0.5:
        return None
    if _intent_already_listed(
        intent_class,
        current_routing_rules=current_routing_rules,
        rule_name=SINK_DOWNWARD,
    ):
        return None

    payload: Dict[str, Any] = {
        "rule": SINK_DOWNWARD,
        "add_intents": [intent_class],
    }
    evidence = stats["evidence_turn_ids"]
    cluster_id = compute_cluster_id(
        intent_class, stats["member_pattern_hashes"],
    )
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload=payload,
        evidence=evidence,
        eval_hash="",  # set by gate_proposal after the suite passes
        created_at=_now_iso(),
        source_patterns=(cluster_id,),
        semantic_justification=_build_justification(
            memory_store, intent_class, stats.get("message_stems", ()),
        ),
    )


def _maybe_upward(
    intent_class: str,
    stats: Dict[str, Any],
    current_routing_rules: Dict[str, Any],
    memory_store: Any = None,
) -> Optional[RoutingProposal]:
    """Apply the upward detection rules. Return one proposal or None."""
    if stats["n"] < MIN_SAMPLE:
        return None
    if stats["correction_rate"] < MIN_CORRECTION_RATE_UPWARD:
        return None
    if _intent_already_listed(
        intent_class,
        current_routing_rules=current_routing_rules,
        rule_name=SINK_UPWARD,
    ):
        return None

    payload: Dict[str, Any] = {
        "rule": SINK_UPWARD,
        "add_intents": [intent_class],
    }
    evidence = stats["evidence_turn_ids"]
    cluster_id = compute_cluster_id(
        intent_class, stats["member_pattern_hashes"],
    )
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload=payload,
        evidence=evidence,
        eval_hash="",
        created_at=_now_iso(),
        source_patterns=(cluster_id,),
        semantic_justification=_build_justification(
            memory_store, intent_class, stats.get("message_stems", ()),
        ),
    )


def propose_routing_adjustments(
    records: Iterable[IntentRecord],
    *,
    current_routing_rules: Optional[Dict[str, Any]] = None,
    memory_store: Any = None,
) -> List[RoutingProposal]:
    """Inspect the intent feed and emit routing proposals.

    ``current_routing_rules`` is the ``routing.routing_rules`` block
    from the merged operator+machine config; the detector uses it to
    avoid proposing intents already present in a list. Missing /
    None means the detector treats every relevant intent as a fresh
    addition.

    Returns at most one downward and one upward proposal per call.
    Empty list when no class meets a threshold — the A1
    "insufficient store" condition is intrinsic to the rules, not a
    special case.
    """
    stats = _aggregate_by_intent(records)
    current = current_routing_rules or {}

    downward: List[Tuple[str, RoutingProposal]] = []
    upward: List[Tuple[str, RoutingProposal]] = []
    for intent_class, intent_stats in stats.items():
        d = _maybe_downward(intent_class, intent_stats, current, memory_store)
        if d is not None:
            downward.append((intent_class, d))
        u = _maybe_upward(intent_class, intent_stats, current, memory_store)
        if u is not None:
            upward.append((intent_class, u))

    proposals: List[RoutingProposal] = []
    if downward:
        downward.sort(key=lambda t: t[0])
        proposals.append(downward[0][1])
    if upward:
        upward.sort(key=lambda t: t[0])
        proposals.append(upward[0][1])
    return proposals
