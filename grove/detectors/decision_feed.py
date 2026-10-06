"""Detectors over a goal's decision feed.

Run as each decision record is appended (``DecisionWork.decide``) — Jidoka
observes feed WRITES, so a miss stops the line before the operator's
confirmation turn ends.

  reference agreement — tier-down: enough confirmed decisions equal the
      reference table's value, with no correction against it (the goal's
      declared evidence rule).
  correction — anomaly: the operator changed an answer. When a keg produced
      that answer the event names the keg, and the handler halts it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from grove.andon import raise_andon
from grove.keg import FLAG_ANOMALY, FLAG_TIER_DOWN_PATTERN

DETECTOR_REFERENCE_AGREEMENT = "reference_agreement"
DETECTOR_CORRECTION = "correction"


def _answered_already(work: Any) -> bool:
    """Whether this goal's pattern is already answered in the current run: a
    keg drafted, serving or halted. A pattern is flagged once, not on every
    further confirmation."""
    from grove import keg as keg_mod
    from grove.pattern_cache import (
        PatternCacheStore, STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED,
    )

    run = work.log.current_run() or {}
    for entry in PatternCacheStore().all():
        record = keg_mod.keg_record(entry).get("keg") or {}
        if (
            record.get("dock_goal") == work.config.goal_id
            and record.get("lineage") == run.get("run_id")
            and entry.status in (STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED)
        ):
            return True
    return False


def observe(
    work: Any, proposed: Mapping[str, Any], decided: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Run the decision-feed detectors on one appended decision. Returns the
    andon events raised (possibly none), each as the handler returned it."""
    from grove.decision_work import DECISION_CONFIRM, DECISION_CORRECT

    goal = work.config.goal_id
    keg_ref = proposed.get("keg")
    context = {"work": work}

    if decided.get("decision") == DECISION_CORRECT:
        by = (
            f"keg {keg_ref.get('name')} v{keg_ref.get('version')}" if keg_ref
            else f"{proposed.get('tier') or 'the interpreter'}"
        )
        return [raise_andon(
            FLAG_ANOMALY, detector=DETECTOR_CORRECTION, goal=goal,
            summary=(
                f"the operator corrected {proposed.get('item_id')} "
                f"({by}: {proposed.get('output')} → {decided.get('output')})"
            ),
            evidence=[{
                "item_id": proposed.get("item_id"),
                "proposed_id": proposed.get("id"), "decided_id": decided.get("id"),
                "turn_uid": proposed.get("turn_uid"), "turn_id": proposed.get("turn_id"),
                "correction_turn_uid": decided.get("turn_uid"),
            }],
            details={
                "item_id": proposed.get("item_id"),
                "inputs": proposed.get("inputs"),
                "served": proposed.get("output"),
                "corrected": decided.get("output"),
                "keg": keg_ref,
            },
            observed_input={"served": proposed.get("output"),
                            "corrected": decided.get("output")},
            matched_skill=(keg_ref or {}).get("pattern_id"),
            context=context,
        )]

    if decided.get("decision") != DECISION_CONFIRM:
        return []
    rule = work.evidence()
    if not rule.get("met") or _answered_already(work):
        return []
    return [raise_andon(
        FLAG_TIER_DOWN_PATTERN, detector=DETECTOR_REFERENCE_AGREEMENT, goal=goal,
        summary=(
            f"{rule['confirmations']} confirmed decisions matched the reference "
            f"table, with no correction against it"
        ),
        evidence=rule["evidence"],
        details={"rule": rule["rule"], "confirmations": rule["confirmations"]},
        observed_input={"confirmations": rule["confirmations"]},
        context=context,
    )]
