"""Detectors over a goal's decision feed.

Run as each decision record is appended (``DecisionWork.decide``) — Jidoka
observes feed WRITES, so a miss stops the line before the operator's
confirmation turn ends.

  reference agreement — tier-down: enough confirmed decisions equal the
      reference table's value, with no correction against it (the goal's
      declared evidence rule).
  correction — anomaly: the operator changed an answer. When a keg produced
      that answer the event names the keg, and the handler halts it.
  confirmed key — tier-down: for a key the reference table does not list,
      enough model-decided items were confirmed by the operator with the same
      answer and none was revised (the goal's declared ``confirmed_key`` rule).
  phrase reading — tier-down: a model read the operator's whole message as a
      verb the work session already has, and the count of such readings with
      no revision after is taken from the decision log. Only for a goal that
      declares the pattern and has adaptation switched on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from grove.andon import raise_andon
from grove.keg import FLAG_ANOMALY, FLAG_TIER_DOWN_PATTERN

DETECTOR_REFERENCE_AGREEMENT = "reference_agreement"
DETECTOR_CORRECTION = "correction"
DETECTOR_PHRASE_READING = "phrase_reading"
DETECTOR_CONFIRMED_KEY = "confirmed_key"
DETECTOR_KEY_ALIAS = "key_alias"


def _plain(output: Any) -> str:
    """An output as words ("tag finance"), not as a data structure."""
    if isinstance(output, Mapping):
        return ", ".join(f"{k} {v}" for k, v in output.items())
    return str(output)


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
                f"({by}: {_plain(proposed.get('output'))} → "
                f"{_plain(decided.get('output'))})"
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
                # Why, in the operator's words, when they said.
                "operator_said": decided.get("operator_said"),
            },
            observed_input={"served": proposed.get("output"),
                            "corrected": decided.get("output")},
            matched_skill=(keg_ref or {}).get("pattern_id"),
            context=context,
        )]

    if decided.get("decision") != DECISION_CONFIRM:
        return []
    events = _phrase_readings(work, decided, context)
    events += _confirmed_key(work, proposed, context)
    events += _key_alias(work, proposed, context)
    rule = work.evidence()
    if not rule.get("met") or _answered_already(work):
        return events
    return events + [raise_andon(
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


def _phrase_readings(
    work: Any, decided: Mapping[str, Any], context: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """The operator's words on this confirmation were not a phrase the work
    session knows, so a model read them. Count how often that exact phrase
    has been read this way, with no revision after, and flag it. Counting is
    all Jidoka does here; what the count becomes is Kaizen's answer."""
    cfg = work.config
    if not cfg.adaptation.enabled or not decided.get("operator_said"):
        return []
    from grove import adaptation
    from grove.decision_work import _phrase

    said = _phrase(decided.get("operator_said"))
    pattern = adaptation.alias_pattern(cfg, "confirm")
    if pattern is None or not pattern.propose or adaptation.refusal(cfg, "confirm", said):
        return []
    # Readings from before the operator last answered about this phrase were
    # already answered; the count Jidoka reports is the one that is still open.
    found = adaptation.readings(
        work, pattern, said, since=adaptation.since(cfg, pattern, said))
    if not found:
        return []
    return [raise_andon(
        FLAG_TIER_DOWN_PATTERN, detector=DETECTOR_PHRASE_READING, goal=cfg.goal_id,
        summary=(
            f"a model read “{said}” as {pattern.verb} {len(found)} time(s), "
            f"with no revision after"
        ),
        evidence=[{
            "item_id": r.get("item_id"), "decided_id": r.get("id"),
            "turn_uid": r.get("turn_uid"), "turn_id": r.get("turn_id"),
            "at": r.get("ts"),
        } for r in found],
        details={
            "pattern": pattern.id, "verb": pattern.verb, "phrase": said,
            "threshold": pattern.threshold, "readings": len(found),
            "session_id": decided.get("session_id"),
        },
        observed_input={"phrase": said, "read_as": pattern.verb},
        context=context,
    )]


def _confirmed_key(
    work: Any, proposed: Mapping[str, Any], context: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """The operator just confirmed a model's answer for a key the reference
    table does not list. When the goal's declared count of such
    confirmations is reached — same key, same answer, none revised — flag
    it. What the count becomes is Kaizen's answer."""
    cfg = work.config
    if cfg.reference is None or proposed.get("keg"):
        return []
    key = (proposed.get("inputs") or {}).get(cfg.reference.key_input)
    found = work.confirmed_key_evidence(key)
    if not found["met"]:
        return []
    return [raise_andon(
        FLAG_TIER_DOWN_PATTERN, detector=DETECTOR_CONFIRMED_KEY, goal=cfg.goal_id,
        summary=(
            f"{found['confirmations']} confirmed decisions gave {key!r} the same answer "
            f"({_plain(found['output'])}), with no revision"
        ),
        evidence=found["evidence"],
        details={"key": key, "output": found["output"],
                 "confirmations": found["confirmations"], "threshold": found["threshold"]},
        observed_input={"key": key, "confirmations": found["confirmations"]},
        context=context,
    )]


def _key_alias(
    work: Any, proposed: Mapping[str, Any], context: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """The operator just confirmed a model's answer for a key no rule covers.
    When the goal's declared alias evidence holds — the confirmations, an
    identity match with exactly one key the keg already answers, and the same
    answer — flag it. Nothing is applied: the answer is a proposal to sign."""
    cfg = work.config
    if cfg.reference is None or proposed.get("keg"):
        return []
    key = (proposed.get("inputs") or {}).get(cfg.reference.key_input)
    found = work.alias_evidence(key)
    if not found["met"]:
        return []
    return [raise_andon(
        FLAG_TIER_DOWN_PATTERN, detector=DETECTOR_KEY_ALIAS, goal=cfg.goal_id,
        summary=(
            f"{key!r} was confirmed as {_plain(found['output'])}, the answer the keg "
            f"gives {found['same_as']!r}, and is identified with it"
        ),
        evidence=found["evidence"],
        details={"key": key, "same_as": found["same_as"], "identity": found["identity"],
                 "output": found["output"], "confirmations": found["confirmations"]},
        observed_input={"key": key, "same_as": found["same_as"]},
        context=context,
    )]
