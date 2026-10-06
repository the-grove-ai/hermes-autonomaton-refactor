"""Jidoka — the watcher. It flags; it never fixes.

One door for every detector. A detector that sees something in the feed calls
:func:`flag`, which writes two linked events to the Kaizen ledger:

  * ``jidoka_flag`` — what was seen: a tier-down pattern (repeated correct
    work) or an anomaly (a miss), which detector saw it and which Dock goal it
    relates to, or none.
  * ``andon_event`` — the cord, pulled by that flag. It carries the issue's
    details and its provenance (the turns and records it rests on). When the
    issue is a miss the line stops: ``stops_line`` is set and the caller halts
    the affected standard work. A tier-down pattern stops nothing.

Kaizen answers an andon event with a proposal that cites the event's id, so a
trace reads flag → andon event → proposal → signed (or feedback) → new
standard work as linked steps.

Pipeline stage: Telemetry. Jidoka reads what turns already recorded and
writes only its own two events. It observes feed WRITES — a detector runs when
a record is appended (:func:`observe_decision`), not on a timer and not from
inside the operator's confirmation.

Detectors behind this door today: reference agreement and correction anomaly
(both below, driven by a goal's declared decision work) and repetition (the T0
pattern scanner, ``grove.eval.pattern_compiler``). The other, older detectors
still file proposals through their own entry points.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from grove.keg import (
    FLAG_ANOMALY,
    FLAG_TIER_DOWN_PATTERN,
    FLAGS,
    LOOP_ANDON_EVENT,
    LOOP_JIDOKA_FLAG,
)

logger = logging.getLogger(__name__)

DETECTOR_REFERENCE_AGREEMENT = "reference_agreement"
DETECTOR_CORRECTION = "correction"
DETECTOR_REPETITION = "repetition"


def _ledger(ledger: Any = None) -> Any:
    if ledger is not None:
        return ledger
    from grove.kaizen_ledger import KaizenLedger
    return KaizenLedger(
        "jidoka-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )


def flag(
    kind: str,
    *,
    detector: str,
    goal: Optional[str],
    summary: str,
    evidence: Optional[List[Mapping[str, Any]]] = None,
    details: Optional[Mapping[str, Any]] = None,
    ledger: Any = None,
) -> Dict[str, Any]:
    """Record a flag and pull the andon cord. Returns the andon event.

    ``evidence`` is the provenance: one entry per turn or record the flag
    rests on (``turn_uid`` / ``turn_id`` / record ids). A write failure
    propagates — a watcher that cannot record what it saw has not watched."""
    if kind not in FLAGS:
        raise ValueError(f"a Jidoka flag must be one of {FLAGS}, got {kind!r}")
    led = _ledger(ledger)
    provenance = [dict(e) for e in (evidence or [])]
    flag_id = uuid.uuid4().hex
    led.record(
        LOOP_JIDOKA_FLAG,
        loop_step=LOOP_JIDOKA_FLAG,
        flag_id=flag_id,
        flag=kind,
        detector=detector,
        goal=goal,
        summary=summary,
        evidence_count=len(provenance),
    )
    andon = {
        "andon_id": uuid.uuid4().hex,
        "flag_id": flag_id,
        "flag": kind,
        "detector": detector,
        "goal": goal,
        "summary": summary,
        "stops_line": kind == FLAG_ANOMALY,
        "provenance": provenance,
        "details": dict(details or {}),
    }
    led.record(LOOP_ANDON_EVENT, loop_step=LOOP_ANDON_EVENT, **andon)
    logger.info(
        "[jidoka] %s flagged by %s (goal=%s): %s — andon %s%s",
        kind, detector, goal, summary, andon["andon_id"],
        " — line stopped" if andon["stops_line"] else "",
    )
    return andon


# ── detectors over a goal's decision feed ─────────────────────────────


def _goal_kegs(work: Any, statuses: tuple, *, store: Any = None) -> List[Any]:
    """This goal's keg entries in the current run's lineage, by status."""
    from grove import keg as keg_mod
    from grove.pattern_cache import PatternCacheStore

    run = work.log.current_run() or {}
    out = []
    for entry in (store or PatternCacheStore()).all():
        record = keg_mod.keg_record(entry).get("keg") or {}
        if record.get("dock_goal") != work.config.goal_id:
            continue
        if record.get("lineage") != run.get("run_id"):
            continue
        if entry.status in statuses:
            out.append(entry)
    return out


def observe_decision(
    work: Any,
    proposed: Mapping[str, Any],
    decided: Mapping[str, Any],
    *,
    ledger: Any = None,
    store: Any = None,
) -> List[Dict[str, Any]]:
    """Run the decision-feed detectors on one appended decision. Returns the
    andon events raised (possibly none), each with what was done about it:
    ``halted`` (the keg entries stopped) and ``kaizen`` (Kaizen's answer).

    Called by ``DecisionWork.decide`` as the record is appended, so a halt is
    in force before the operator's confirmation turn ends."""
    from grove.decision_work import DECISION_CONFIRM, DECISION_CORRECT
    from grove.pattern_cache import (
        PatternCacheStore, STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED,
    )

    store = store or PatternCacheStore()
    goal = work.config.goal_id
    events: List[Dict[str, Any]] = []
    keg_ref = proposed.get("keg")

    if decided.get("decision") == DECISION_CORRECT:
        provenance = [{
            "item_id": proposed.get("item_id"),
            "proposed_id": proposed.get("id"), "decided_id": decided.get("id"),
            "turn_uid": proposed.get("turn_uid"), "turn_id": proposed.get("turn_id"),
            "correction_turn_uid": decided.get("turn_uid"),
        }]
        by = (
            f"keg {keg_ref.get('name')} v{keg_ref.get('version')}" if keg_ref
            else f"{proposed.get('tier') or 'the interpreter'}"
        )
        andon = flag(
            FLAG_ANOMALY, detector=DETECTOR_CORRECTION, goal=goal,
            summary=(
                f"the operator corrected {proposed.get('item_id')} "
                f"({by}: {proposed.get('output')} → {decided.get('output')})"
            ),
            evidence=provenance,
            details={
                "item_id": proposed.get("item_id"),
                "inputs": proposed.get("inputs"),
                "served": proposed.get("output"),
                "corrected": decided.get("output"),
                "keg": keg_ref,
            },
            ledger=ledger,
        )
        andon["halted"] = []
        andon["kaizen"] = None
        if keg_ref and keg_ref.get("pattern_id"):
            # A miss stops the line: the keg halts. It stays signed — its grant
            # stands until the operator rules on Kaizen's fix — but it no
            # longer serves, so covered work goes back to the interpreter.
            entry = store.get(keg_ref["pattern_id"])
            if entry is not None and entry.status == STATUS_ACTIVE:
                store.set_status(entry.pattern_id, STATUS_HALTED)
                andon["halted"].append(entry.pattern_id)
            from grove.kaizen import standard_work
            andon["kaizen"] = standard_work.answer(work, andon, ledger=ledger, store=store)
        events.append(andon)
        return events

    if decided.get("decision") != DECISION_CONFIRM:
        return events
    rule = work.evidence()
    if not rule.get("met"):
        return events
    # A pattern already answered — a keg drafted, serving or halted for this
    # goal in this run — is not flagged again.
    if _goal_kegs(work, (STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED), store=store):
        return events
    andon = flag(
        FLAG_TIER_DOWN_PATTERN, detector=DETECTOR_REFERENCE_AGREEMENT, goal=goal,
        summary=(
            f"{rule['confirmations']} confirmed decisions matched the reference "
            f"table, with no correction against it"
        ),
        evidence=rule["evidence"],
        details={"rule": rule["rule"], "confirmations": rule["confirmations"]},
        ledger=ledger,
    )
    from grove.kaizen import standard_work
    andon["halted"] = []
    andon["kaizen"] = standard_work.answer(work, andon, ledger=ledger, store=store)
    events.append(andon)
    return events
