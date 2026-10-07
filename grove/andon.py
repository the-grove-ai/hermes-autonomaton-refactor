"""The andon handler — what happens, every time, when a detector pulls the cord.

Detectors call :func:`raise_andon` and return. They hold no reference to
Kaizen, carry no retry logic and have no fallback: everything after the cord
is pulled is THIS handler, the same for every detector, including for
Kaizen's own failures. That is what keeps the pipeline invariant under
abnormality — no detector can pull the cord and walk away.

This is the handler, not the spine. The spine is the five-stage pipeline;
this module is how two of its stages behave when something is abnormal:

  Telemetry  — the flag and the andon event are recorded (what fired, on what
               input, against which standard work, with what confidence).
  Approval   — the answer's surface is classified BEFORE the operator sees
               anything, and that decides the channel it travels on.

For every andon event, in order:

  1. record ``jidoka_flag`` (the detector's observation);
  2. stop the line if the issue is a miss — halt the standard work named;
  3. record ``andon_event`` (the issue, its provenance, what was stopped);
  4. route the event to Kaizen;
  5. if Kaizen cannot answer, that failure is itself an andon event, raised
     through this same function — no special path;
  6. record exactly one closing artifact, ``kaizen_answer``, chained to the
     flag and the event by their ledger hashes.

Kaizen's answer is one of three kinds (:data:`ANSWER_KINDS`). The kind fixes
the surface it may write and the channel it travels on:

  standard_work — a change to standard work. Scope-defining. Portal, signed.
  remedy        — an in-scope action. In-scope ONLY. Answered in conversation.
  watch         — no countermeasure yet; an inert record of what is being
                  watched and what would promote it. In-scope. Automatic.

The zone rule: green changes are approved in conversation; changes to
authority are signed in the portal. What makes a remedy green is its surface,
decided here and never by the answer's own say-so.

A remedy whose action would touch a scope-defining surface is not a remedy.
:func:`classify_surface` decides that before anything is offered, and such an
answer is refused as a remedy (:class:`ScopeViolation`) — it must come back
as a standard-work change.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from grove.keg import (
    FLAG_ANOMALY, FLAGS, LOOP_ANDON_EVENT, LOOP_JIDOKA_FLAG,
)

logger = logging.getLogger(__name__)

KIND_STANDARD_WORK = "standard_work"
KIND_REMEDY = "remedy"
KIND_WATCH = "watch"
ANSWER_KINDS = (KIND_STANDARD_WORK, KIND_REMEDY, KIND_WATCH)

SURFACE_SCOPE_DEFINING = "scope_defining"
SURFACE_IN_SCOPE = "in_scope"

CHANNEL_PORTAL = "portal"          # signed by the operator (Grant Token / demo stamp)
CHANNEL_CHAT = "chat_accept"       # approved in conversation; in-scope surfaces only
CHANNEL_AUTOMATIC = "automatic"    # an inert watch record; changes no behavior

LOOP_KAIZEN_ANSWER = "kaizen_answer"
DETECTOR_KAIZEN_FAILURE = "kaizen_failure"

# The actions a remedy may take, and the single in-scope surface each writes.
# A remedy naming any other write class, or any path the scope wall calls
# scope-defining, is refused. Adding a class here is a decision about what
# the system may do on an answer in conversation: keep the list short.
REMEDY_WRITE_CLASSES: Dict[str, str] = {
    "set_aside_item": "the goal's own decision log",
    "tier_escalation": "this turn's routing (one tier up, once)",
    "kaizen_tier_escalation": "Kaizen's own drafting tier (one tier up, once)",
    "session_reset": "this chat's session (a clean one, under a signed standing rule)",
    # Green because the SIGNED session rule lets the goal's vocabulary supply
    # phrases for verbs already in that rule. It adds a way to say something
    # the operator already authorized; it can never add a verb, change a
    # threshold or widen scope (checked again when it is applied).
    "vocabulary_alias": "the goal's learned vocabulary (a phrase for a signed verb)",
}

# The ladder rule. The router sends each turn to the cheapest tier that can
# handle it; when that tier does not complete the turn, the turn fails UPWARD,
# one tier, once per tier. Moving one turn up grants no authority and is always
# the safe direction, so it needs no operator accept: the remedy is carried out
# when it is answered, recorded against its event, and shown to the operator.
# Only these write classes may be authorized this way. Changing which tier a
# CLASS of work starts on is standard work, and is signed.
AUTHORIZED_STANDING_RULE = "standing_rule"
AUTHORIZED_LADDER_RULE = "ladder_rule"
LADDER_WRITE_CLASSES = frozenset({"tier_escalation"})

# How deep Kaizen's failures may nest before the handler closes the event with
# a watch — an answer that is built without a model and cannot fail to draft.
_MAX_DEPTH = 2


class ScopeViolation(Exception):
    """An answer offered on a channel its surface does not allow."""


class KaizenCouldNotAnswer(Exception):
    """Kaizen tried and has no valid answer. ``details`` says what it tried.
    Raised BY Kaizen; the handler turns it into the next andon event."""

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass
class Answer:
    """Kaizen's answer to one andon event."""
    kind: str
    summary: str
    # What the answer wrote or filed: a proposal id, a cache entry id, a
    # remedy's proposal id. This is the artifact that closes the event.
    artifact: Optional[str] = None
    # remedy only: the one-time action, and any filesystem paths it writes.
    write_class: Optional[str] = None
    write_targets: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)


def classify_surface(answer: Answer) -> str:
    """The surface an answer writes, decided from the answer itself.

    A standard-work change is scope-defining by definition. A watch is an
    inert in-scope record. A remedy is in-scope ONLY IF its write class is a
    declared one-time action AND none of the paths it names is scope-defining
    under the scope wall (``grove.utils.fs_utils.is_scope_defining`` — the
    same wall that governs every other write)."""
    if answer.kind == KIND_STANDARD_WORK:
        return SURFACE_SCOPE_DEFINING
    if answer.kind == KIND_WATCH:
        return SURFACE_IN_SCOPE
    if answer.write_class not in REMEDY_WRITE_CLASSES:
        return SURFACE_SCOPE_DEFINING
    from grove.utils.fs_utils import is_scope_defining
    for target in answer.write_targets:
        if is_scope_defining(str(target)):
            return SURFACE_SCOPE_DEFINING
    return SURFACE_IN_SCOPE


def channel_for(answer: Answer) -> str:
    """The one channel an answer may travel on. Raises :class:`ScopeViolation`
    for a remedy that would touch a scope-defining surface: that is a
    standard-work change and must be answered as one."""
    if answer.kind not in ANSWER_KINDS:
        raise ScopeViolation(f"unknown answer kind {answer.kind!r}")
    surface = classify_surface(answer)
    if answer.kind == KIND_STANDARD_WORK:
        return CHANNEL_PORTAL
    if answer.kind == KIND_WATCH:
        return CHANNEL_AUTOMATIC
    if surface != SURFACE_IN_SCOPE:
        raise ScopeViolation(
            f"remedy {answer.write_class!r} would write a scope-defining "
            f"surface; it must be proposed as a standard-work change"
        )
    if (answer.detail.get("authorized") == AUTHORIZED_LADDER_RULE
            and answer.write_class not in LADDER_WRITE_CLASSES):
        raise ScopeViolation(
            f"the ladder rule authorizes only {sorted(LADDER_WRITE_CLASSES)}, "
            f"not {answer.write_class!r}")
    return CHANNEL_CHAT


def assert_chat_acceptable(answer: Answer) -> None:
    """The guard on the chat-accept path: nothing accepted in chat may write
    a scope-defining surface. Called again at the moment a remedy is applied,
    not only when it is offered."""
    if answer.kind != KIND_REMEDY or classify_surface(answer) != SURFACE_IN_SCOPE:
        raise ScopeViolation(
            "only an in-scope remedy may be accepted in chat"
        )


def _bus(ledger: Any = None) -> Any:
    """The one bus: the Kaizen ledger of the session whose turn is running;
    outside any turn (a scan, a portal action), a dated ``andon-`` ledger."""
    if ledger is not None:
        return ledger
    from grove import turn_provenance
    from grove.kaizen_ledger import KaizenLedger

    session_id = (turn_provenance.current() or {}).get("session_id")
    if session_id:
        return KaizenLedger(str(session_id))
    return KaizenLedger(
        "andon-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )


def _stop_line(andon: Dict[str, Any]) -> List[str]:
    """Halt the standard work a miss names. It stays signed — its grant
    stands until the operator rules — but it no longer serves."""
    keg = (andon.get("details") or {}).get("keg") or {}
    pattern_id = keg.get("pattern_id")
    if not pattern_id:
        return []
    from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE, STATUS_HALTED

    store = PatternCacheStore()
    entry = store.get(pattern_id)
    if entry is None or entry.status != STATUS_ACTIVE:
        return []
    store.set_status(pattern_id, STATUS_HALTED)
    return [pattern_id]


def raise_andon(
    kind: str,
    *,
    detector: str,
    goal: Optional[str],
    summary: str,
    evidence: Optional[List[Mapping[str, Any]]] = None,
    details: Optional[Mapping[str, Any]] = None,
    observed_input: Any = None,
    matched_skill: Optional[str] = None,
    confidence: Optional[float] = None,
    context: Optional[Mapping[str, Any]] = None,
    ledger: Any = None,
    _originating: Optional[List[str]] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    """Pull the andon cord. Returns the andon event, with what was stopped
    (``halted``) and Kaizen's recorded answer (``answer``).

    ``context`` carries live handles Kaizen needs to draft (the goal's work
    object, a scanned candidate). It is never written to the ledger.

    A ledger write failure propagates: an abnormality that cannot be recorded
    has not been handled."""
    if kind not in FLAGS:
        raise ValueError(f"an andon must be one of {FLAGS}, got {kind!r}")
    bus = _bus(ledger)
    provenance = [dict(e) for e in (evidence or [])]

    flag = bus.record(
        LOOP_JIDOKA_FLAG,
        loop_step=LOOP_JIDOKA_FLAG,
        flag_id=uuid.uuid4().hex,
        flag=kind,
        detector_id=detector,
        goal=goal,
        summary=summary,
        observed_input=observed_input,
        matched_skill=matched_skill,
        confidence=confidence,
        evidence_count=len(provenance),
    )
    andon: Dict[str, Any] = {
        "andon_id": uuid.uuid4().hex,
        "flag_id": flag["flag_id"],
        "flag": kind,
        "detector": detector,
        "goal": goal,
        "summary": summary,
        "stops_line": kind == FLAG_ANOMALY,
        "provenance": provenance,
        "details": dict(details or {}),
        "originating": list(_originating or []),
    }
    andon["halted"] = _stop_line(andon) if andon["stops_line"] else []
    event = bus.record(LOOP_ANDON_EVENT, loop_step=LOOP_ANDON_EVENT, **andon)
    logger.info(
        "[andon] %s from %s (goal=%s): %s — andon %s%s",
        kind, detector, goal, summary, andon["andon_id"],
        f" — halted {andon['halted']}" if andon["halted"] else "",
    )

    # Route to Kaizen. Its failure is the next andon event, through this same
    # function; at the depth limit the event closes with a watch.
    from grove.kaizen import answers

    closes = [andon["andon_id"]] + list(andon["originating"])
    try:
        if _depth >= _MAX_DEPTH:
            answer = answers.watch_unresolved(andon, context)
        else:
            answer = answers.answer(andon, context)
        if not isinstance(answer, Answer):
            raise KaizenCouldNotAnswer(
                "Kaizen returned no answer", {"returned": type(answer).__name__})
        channel = channel_for(answer)
    except (KaizenCouldNotAnswer, ScopeViolation) as failure:
        failed = raise_andon(
            FLAG_ANOMALY,
            detector=DETECTOR_KAIZEN_FAILURE,
            goal=goal,
            summary=f"Kaizen could not answer andon {andon['andon_id'][:8]}: {failure}",
            evidence=provenance,
            details={
                "originating_andon_id": andon["andon_id"],
                "originating_detector": detector,
                "originating_details": andon["details"],
                "failure": type(failure).__name__,
                **getattr(failure, "details", {}),
            },
            matched_skill="kaizen",
            context=context,
            ledger=bus,
            _originating=closes,
            _depth=_depth + 1,
        )
        # The failure's answer closes this event too (it lists it in `closes`).
        andon["answer"] = failed["answer"]
        andon["escalated_to"] = failed["andon_id"]
        return andon

    surface = classify_surface(answer)
    closing = bus.record(
        LOOP_KAIZEN_ANSWER,
        loop_step=LOOP_KAIZEN_ANSWER,
        andon_id=andon["andon_id"],
        closes=closes,
        kind=answer.kind,
        surface_class=surface,
        channel=channel,
        write_class=answer.write_class,
        artifact=answer.artifact,
        summary=answer.summary,
        # What drafting this answer took: each tier tried and the tokens it
        # used. Present only when a model drafted it.
        **({"drafting": [
            {"tier": a.get("tier"), "refused": bool(a.get("refused")),
             **({"tokens": a["tokens"]} if a.get("tokens") else {})}
            for a in answer.detail["attempts"]]}
           if answer.kind == KIND_STANDARD_WORK and answer.detail.get("attempts") else {}),
        # Custody: this close commits to the flag and the event by their
        # ledger hashes, and names every turn the event rests on.
        source_chain=[flag["record_hash"], event["record_hash"]] + [
            str(p["turn_uid"]) for p in provenance if p.get("turn_uid")
        ],
    )
    authorized = answer.detail.get("authorized") if answer.kind == KIND_REMEDY else None
    if authorized in (AUTHORIZED_STANDING_RULE, AUTHORIZED_LADDER_RULE):
        # Already authorized — by a rule the operator signed, or by the ladder
        # rule — so it needs no further accept. Carried out here and recorded
        # against its authority; still only ever an in-scope, one-time action.
        assert_chat_acceptable(answer)
        from grove import reissue

        action = dict(answer.detail.get("reissue") or {})
        if action.get("session_id"):
            reissue.arm(action, session_id=action["session_id"])
            applied = {}
            if authorized == AUTHORIZED_LADDER_RULE:
                applied = {
                    "from_tier": action.get("from_tier"), "tier": action.get("tier"),
                    "summary": (f"escalated {action.get('from_tier')} → "
                                f"{action.get('tier')} (ladder rule)"),
                    "attempts": list(action.get("attempts") or []),
                }
            bus.record(
                "remedy_applied",
                loop_step="remedy_applied",
                andon_id=andon["andon_id"],
                goal=goal,
                write_class=answer.write_class,
                standing_grant=answer.detail.get("standing_grant"),
                surface_class=surface, channel=authorized,
                source_chain=[closing["record_hash"]],
                **applied,
            )
    stops = answer.detail.get("stops_attempt") if answer.kind == KIND_WATCH else None
    if stops and stops.get("session_id"):
        # The top of the ladder: nothing higher to try. The attempt is stopped
        # for the rest of its turn; no retry is armed.
        from grove import reissue
        reissue.mark_stopped(
            str(stops["session_id"]), stops.get("turn_uid"), andon["andon_id"])
    andon["answer"] = {
        "kind": answer.kind, "summary": answer.summary, "artifact": answer.artifact,
        "surface_class": surface, "channel": channel,
        "write_class": answer.write_class, "detail": dict(answer.detail),
        "record_hash": closing["record_hash"],
    }
    return andon
