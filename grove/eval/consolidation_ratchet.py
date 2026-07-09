"""consolidation-ratchet-v1 — Stage 2 of the routing policy pipeline.

Stage 1 (``grove.eval.tier_ratchet``, shipped) reads the intent feed, detects
SHORT-term stability (n=5), and proposes adding an intent to a machine sink
rule (``ratchet_promoted_t1/t2/t3``) in ``routing.autonomaton.yaml``.

Stage 2 (this module) reads those machine sink entries and cross-references the
intent feed for LONG-term stability — a much higher bar (n≥20, ≥90% success,
ZERO governance Andons). A qualifying intent is proposed for graduation: its
routing becomes permanent operator policy in ``routing.config.yaml``. The
approval handler performs the two-file atomic write (see
``grove.flywheel_cli._approve_consolidation``).

Phase 1 is single-intent only: one sink intent → one named operator rule.
Clustering (Phase 3) and de-consolidation (Phase 2) are out of scope.

Stability is read from the intent feed (``intent_records.jsonl``), NOT the
machine sink — the sink carries only the graduated intent names; the per-turn
success / Andon signal lives on ``IntentRecord`` (GATE-A A1 disposition).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

from grove.eval.tier_ratchet import _aggregate_by_intent
from grove.intent_store import IntentStore

logger = logging.getLogger(__name__)

__all__ = ["ConsolidationRatchet", "SINK_TIER"]


# The machine sink rule names and the tier each one graduates to. The suffix
# IS the target tier — ratchet_promoted_t2 graduates an intent to T2 policy.
SINK_TIER: Dict[str, str] = {
    "ratchet_promoted_t1": "T1",
    "ratchet_promoted_t2": "T2",
    "ratchet_promoted_t3": "T3",
}

# The governance-Andon outcome (intent_store VALID_OUTCOMES): a structural
# governed denial. Long-term stability requires ZERO of these.
_ANDON_OUTCOME = "governance_terminated"


class ConsolidationRatchet:
    """Stage 2 detector: machine sink → permanent operator policy proposals."""

    # Long-term stability bar — deliberately far above TierRatchet's n=5.
    LONG_TERM_MIN_SAMPLE = 20
    MIN_SUCCESS_RATE = 0.90

    def detect(
        self,
        machine_config_path: Path,
        intent_store_path: Path,
    ) -> List[Dict[str, Any]]:
        """Propose graduation for sink intents that are stable long-term.

        1. Read ``routing.autonomaton.yaml`` — collect intents in the
           ``ratchet_promoted_t1/t2/t3`` sink rules.
        2. For each, compute long-term stats from the intent feed:
           n ≥ 20, success_rate ≥ 0.90, zero ``governance_terminated``.
        3. Return one consolidation proposal per qualifying intent.

        An intent NOT present in any machine sink is never proposed — Stage 1
        must graduate it first (that is the two-stage contract).
        """
        sink_intents = self._read_sink_intents(machine_config_path)
        if not sink_intents:
            return []

        records = list(IntentStore(intent_store_path).latest_by_turn())
        stats = _aggregate_by_intent(records)
        andons = self._andon_counts(records)

        proposals: List[Dict[str, Any]] = []
        for intent_class, sink_name in sorted(sink_intents.items()):
            s = stats.get(intent_class)
            if s is None:
                continue
            if s["n"] < self.LONG_TERM_MIN_SAMPLE:
                continue
            if s["success_rate"] < self.MIN_SUCCESS_RATE:
                continue
            if andons.get(intent_class, 0) > 0:
                continue
            proposals.append({
                "action": "consolidate",
                "intent_class": intent_class,
                "target_tier": SINK_TIER[sink_name],
                "source_sink": sink_name,
                "stats": {
                    "n": s["n"],
                    "success_rate": round(s["success_rate"], 4),
                    "andons": 0,
                },
            })
        return proposals

    def stage_proposals(
        self, proposals: List[Dict[str, Any]], session_id: str
    ) -> int:
        """Append each proposal to the routing proposal queue (proposals.jsonl).

        Wraps each dict in a ``RoutingProposal`` of type
        ``consolidation_proposal``. The id is computed from the STABLE identity
        (intent_class + target_tier + source_sink), excluding the volatile
        ``stats`` block, so a re-run with more evidence dedups instead of
        stacking. ``session_id`` is accepted for interface parity with the
        other stagers; the routing queue record carries no session field.
        Returns the number actually appended (duplicates dedup to no-op).
        """
        from grove.eval.proposal_queue import (
            PROPOSAL_TYPE_CONSOLIDATION,
            RoutingProposal,
            _now_iso,
            append,
            compute_proposal_id,
        )

        staged = 0
        for proposal in proposals:
            identity = {
                "intent_class": proposal["intent_class"],
                "target_tier": proposal["target_tier"],
                "source_sink": proposal["source_sink"],
            }
            record = RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_CONSOLIDATION,
                    payload=identity,
                    evidence=(),
                ),
                type=PROPOSAL_TYPE_CONSOLIDATION,
                payload=proposal,
                evidence=(),
                eval_hash="",
                created_at=_now_iso(),
                proposer="consolidation_ratchet",  # proposal-proposer-attribution-v1 (#10)
            )
            if append(record):
                staged += 1
        return staged

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _read_sink_intents(machine_config_path: Path) -> Dict[str, str]:
        """Map each sink-listed intent_class → its sink rule name.

        A missing or empty machine file is a clean empty result (a fresh
        install has graduated nothing yet). First sink wins if an intent
        somehow appears in two (it should not)."""
        path = Path(machine_config_path)
        if not path.exists():
            return {}
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        rules = ((raw.get("routing") or {}).get("routing_rules")) or {}
        out: Dict[str, str] = {}
        for sink_name in SINK_TIER:
            rule = rules.get(sink_name)
            if not isinstance(rule, dict):
                continue
            intents = (rule.get("match") or {}).get("intents") or []
            if not isinstance(intents, list):
                continue
            for intent_class in intents:
                if isinstance(intent_class, str) and intent_class not in out:
                    out[intent_class] = sink_name
        return out

    @staticmethod
    def _andon_counts(records: List[Any]) -> Dict[str, int]:
        """Per-intent count of ``governance_terminated`` outcomes — the Andon
        signal ``_aggregate_by_intent`` does not surface."""
        counts: Dict[str, int] = {}
        for record in records:
            if record.outcome == _ANDON_OUTCOME and record.intent_class:
                counts[record.intent_class] = counts.get(record.intent_class, 0) + 1
        return counts
