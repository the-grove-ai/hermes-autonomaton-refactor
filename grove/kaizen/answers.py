"""Kaizen's answers — one for every andon event, no exceptions.

The andon handler (``grove.andon``) routes each event here. Kaizen returns
exactly one :class:`~grove.andon.Answer`:

  standard_work — a drafted, backtested change to standard work, filed for
                  the operator's signature in the portal;
  remedy        — a one-time, in-scope action for this turn, filed for a chat
                  accept (never a permanent change);
  watch         — "no countermeasure yet": an inert record of what is being
                  watched and what would promote it.

Kaizen proposes; it never commits and it never says only "I can't". When it
truly has no valid draft it raises :class:`~grove.andon.KaizenCouldNotAnswer`
and the handler raises THAT as the next andon event — through the same
handler, back to here — so even Kaizen's failure ends in one of the three.

Nothing here knows a domain. Which answer fits which event is a property of
the event (its detector and its reason), and the drafting itself lives beside
the standard work it drafts (``grove.kaizen.standard_work``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from grove.andon import (
    Answer, KIND_REMEDY, KIND_STANDARD_WORK, KIND_WATCH, KaizenCouldNotAnswer,
)

logger = logging.getLogger(__name__)

# How many times a watched condition must be seen before Kaizen promotes it
# to a proposal, when the event's own goal declares nothing else.
_DEFAULT_PROMOTE_AFTER = 2
_UNREADABLE_PROMOTE_AFTER = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _work(context: Optional[Mapping[str, Any]], andon: Mapping[str, Any]) -> Any:
    """The goal's decision work: the live object a detector passed, else
    loaded from the Dock by the event's goal. None when the event has none."""
    work = (context or {}).get("work")
    if work is not None:
        return work
    goal = andon.get("goal")
    if not goal:
        return None
    try:
        from grove.decision_work import DecisionWork, config_for_goal
        return DecisionWork(config_for_goal(str(goal)))
    except ValueError:
        return None


def replay_summary(out: Mapping[str, Any]) -> str:
    """Kaizen's one-line account of a backtest: three outcomes, each counted
    on its own, with the cases standard work leaves to the interpreter named."""
    line = (
        f"Replayed {out.get('replayed')} on history: {out.get('unchanged')} "
        f"unchanged, {out.get('would_change')} would change, "
        f"{out.get('not_covered')} not covered"
    )
    named = list(dict.fromkeys(out.get("not_covered_cases") or []))
    if named:
        line += f" ({'; '.join(named)})"
    return line + "."


# ── watch ─────────────────────────────────────────────────────────────


def watch(
    andon: Mapping[str, Any],
    *,
    signature: Mapping[str, Any],
    description: str,
    promote_after: int,
    promote: Optional[Callable[[Dict[str, Any]], Answer]] = None,
    store: Any = None,
) -> Answer:
    """Record (or re-observe) a watch: an inert cache entry that carries the
    condition which would promote it. Seeing the same condition again counts
    toward that condition; reaching it calls ``promote`` and returns ITS
    answer instead — the ratchet turning on repeated evidence."""
    from grove.pattern_cache import (
        CompiledPattern, PatternCacheStore, STATUS_SUPERSEDED, STATUS_WATCHING,
    )

    store = store or PatternCacheStore()
    digest = hashlib.sha256(
        json.dumps(dict(signature), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    watch_id = f"watch:{andon.get('goal') or 'none'}:{andon.get('detector')}:{digest}"
    entry = store.get(watch_id)
    record: Dict[str, Any] = {}
    if entry is not None and entry.status == STATUS_WATCHING:
        record = json.loads(entry.promotion_evidence or "{}").get("watch") or {}
    seen = int(record.get("seen") or 0) + 1
    record = {
        "detector": andon.get("detector"),
        "goal": andon.get("goal"),
        "signature": dict(signature),
        "description": description,
        "trigger": {"seen": int(promote_after)},
        "seen": seen,
        "andon_ids": list(record.get("andon_ids") or []) + [andon.get("andon_id")],
        "first_seen": record.get("first_seen") or _now(),
        "last_seen": _now(),
    }
    store.upsert(CompiledPattern(
        pattern_id=watch_id, t0_key=watch_id,
        intent_class="watch", cacheable_type="watch",
        cached_response=None, compiled_invocation=None,
        evidence_hash="sha256:" + digest, status=STATUS_WATCHING,
        created_at=record["first_seen"],
        promotion_evidence=json.dumps({"watch": record}, sort_keys=True),
    ))
    if promote is not None and seen >= int(promote_after):
        promoted = promote(record)
        if promoted is not None and promoted.kind != KIND_WATCH:
            store.set_status(watch_id, STATUS_SUPERSEDED)   # it has done its job
            promoted.detail = {**promoted.detail, "promoted_from": watch_id}
            return promoted
        # Nothing valid to propose yet: keep watching, and say so.
    return Answer(
        kind=KIND_WATCH,
        summary=(
            f"No countermeasure yet. Watching: {description} "
            f"(seen {seen} of {promote_after})."
        ),
        artifact=watch_id,
        detail={"watch": record},
    )


def watch_unresolved(andon: Mapping[str, Any], context: Any = None) -> Answer:
    """The answer that cannot fail to draft: a plain watch on this class of
    event. The handler uses it when Kaizen's own failures have nested as deep
    as they may, so the loop always closes."""
    return watch(
        andon,
        signature={"class": andon.get("detector"),
                   "reason": (andon.get("details") or {}).get("failure")
                   or (andon.get("details") or {}).get("reason")},
        description=f"repeated {andon.get('detector')} events Kaizen could not answer",
        promote_after=_UNREADABLE_PROMOTE_AFTER,
    )


# ── remedy ────────────────────────────────────────────────────────────


def file_remedy(
    andon: Mapping[str, Any], *, write_class: str, summary: str,
    action: Mapping[str, Any], write_targets: Optional[list] = None,
) -> Answer:
    """File a one-time action for the operator's chat accept. The handler
    classifies its surface before anything is offered; the same check runs
    again when it is applied."""
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_REMEDY, RoutingProposal, append, compute_proposal_id,
    )

    payload = {
        "andon_id": andon.get("andon_id"), "goal": andon.get("goal"),
        "write_class": write_class, "action": dict(action),
        "write_targets": list(write_targets or []),
    }
    evidence = tuple(
        str(p.get("turn_id") or p.get("turn_uid") or p.get("item_id") or "")
        for p in (andon.get("provenance") or [])
    ) or (str(andon.get("andon_id")),)
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_REMEDY, payload=payload, evidence=evidence),
        type=PROPOSAL_TYPE_REMEDY, payload=payload, evidence=evidence,
        eval_hash="sha256:" + hashlib.sha256(
            f"remedy|{andon.get('andon_id')}".encode("utf-8")).hexdigest(),
        created_at=_now(), semantic_justification=summary, proposer="kaizen",
    )
    append(proposal)
    return Answer(
        kind=KIND_REMEDY, summary=summary, artifact=proposal.proposal_id,
        write_class=write_class, write_targets=list(write_targets or []),
        detail={"action": dict(action)},
    )


# ── the answers, by detector ──────────────────────────────────────────


def _standard_work(andon: Mapping[str, Any], context: Any) -> Answer:
    """Draft and propose the keg, or keg revision, that answers the event."""
    from grove.kaizen import standard_work

    work = _work(context, andon)
    if work is None:
        return watch_unresolved(andon, context)
    out = standard_work.answer(work, andon)
    status = out.get("status")
    if status == "proposed":
        return Answer(
            kind=KIND_STANDARD_WORK,
            summary=(
                f"Proposed v{out.get('version')} for the operator's signature. "
                + replay_summary(out)
            ),
            artifact=out.get("proposal_id"), detail=out,
        )
    if status == "draft_failed":
        raise KaizenCouldNotAnswer(
            "no tier produced a revision that passed its checks",
            {"attempts": out.get("attempts"), "reason": "draft_failed"},
        )
    # Nothing to propose (already drafted, no keg declared, replay conflicts
    # with a confirmed case): say so as a watch, with the reason kept.
    return watch(
        andon, signature={"class": "no_proposal", "status": status},
        description=f"{out.get('detail') or status}",
        promote_after=_UNREADABLE_PROMOTE_AFTER,
    )


def _correction(andon: Mapping[str, Any], context: Any) -> Answer:
    details = andon.get("details") or {}
    if details.get("keg"):
        return _standard_work(andon, context)      # a keg missed: revise it
    work = _work(context, andon)
    key = None
    if work is not None and work.config.reference is not None:
        key = (details.get("inputs") or {}).get(work.config.reference.key_input)
    signature = {"key": key, "corrected": details.get("corrected")}
    # Teaching standard work a NEW answer takes the same repeated, confirmed
    # evidence as any other rule: the goal's own evidence threshold.
    needed = _DEFAULT_PROMOTE_AFTER
    if work is not None and work.config.evidence is not None:
        needed = work.config.evidence.threshold

    def _promote(record: Dict[str, Any]) -> Optional[Answer]:
        from grove.kaizen import standard_work

        out = standard_work.propose_direct_rule(
            work, andon, key=key, corrected=details.get("corrected"))
        if out.get("status") != "proposed":
            return None
        return Answer(
            kind=KIND_STANDARD_WORK,
            summary=(
                f"You corrected {key!r} to {details.get('corrected')} "
                f"{record['seen']} times. Proposed v{out.get('version')} with "
                f"that as a rule, for your signature. " + replay_summary(out)
            ),
            artifact=out.get("proposal_id"), detail=out,
        )

    return watch(
        andon, signature=signature,
        description=(
            f"the operator correcting {key!r} to {details.get('corrected')}; "
            f"{needed} matching corrections promote it to a proposed rule"
        ),
        promote_after=needed,
        promote=_promote if work is not None and work.config.keg is not None else None,
    )


def _turn_check(andon: Mapping[str, Any], context: Any) -> Answer:
    details = andon.get("details") or {}
    reason = details.get("reason")
    if reason == "item_unreadable":
        return file_remedy(
            andon, write_class="set_aside_item",
            summary=(
                f"Set {details.get('item_id') or 'this item'} aside for manual "
                "handling and move on to the next one."
            ),
            action={"goal": andon.get("goal"), "item_id": details.get("item_id"),
                    "reason": details.get("message") or reason},
        )
    if reason in ("output_not_in_domain", "undeclared_output"):
        from grove import reissue

        up = reissue.next_tier(details.get("tier"))
        if up is None:
            # Already at the top of the ladder (or at T0): nothing higher to
            # fail upward to. Watch for it repeating.
            return watch(
                andon, signature={"class": reason, "goal": andon.get("goal")},
                description=(
                    f"an invalid answer at {details.get('tier') or 'an unknown tier'} "
                    "with no higher tier to retry on"
                ),
                promote_after=_DEFAULT_PROMOTE_AFTER,
            )
        return file_remedy(
            andon, write_class="tier_escalation",
            summary=f"Retry this request one tier up, at {up}.",
            action={"goal": andon.get("goal"), "from_tier": details.get("tier"),
                    "tier": up, "request": details.get("request"),
                    "session_id": details.get("session_id"),
                    "andon_id": andon.get("andon_id")},
        )
    if reason in ("session_not_isolated", "contaminated_turn", "no_provenance"):
        from grove.kaizen import session_rule
        return session_rule.answer(andon, context)
    return watch_unresolved(andon, context)


def _repetition(andon: Mapping[str, Any], context: Any) -> Answer:
    propose = (context or {}).get("propose")
    if propose is None:
        return watch_unresolved(andon, context)
    proposal_id = propose(andon)
    if not proposal_id:
        return watch(
            andon, signature={"t0_key": (andon.get("details") or {}).get("t0_key")},
            description="a repeated request whose proposal is already waiting",
            promote_after=_UNREADABLE_PROMOTE_AFTER,
        )
    return Answer(
        kind=KIND_STANDARD_WORK,
        summary="Proposed retiring this repeated request to the T0 cache.",
        artifact=proposal_id,
    )


def _kaizen_failure(andon: Mapping[str, Any], context: Any) -> Answer:
    """Kaizen's own failure, answered like any other event. When a revision
    could not be drafted at any tier, ask the operator to write the condition
    — in the portal, where what they write is backtested and signed like any
    other standard-work change."""
    details = andon.get("details") or {}
    work = _work(context, andon)
    if details.get("reason") == "draft_failed" and work is not None and work.config.keg:
        from grove.kaizen import standard_work
        proposal_id = standard_work.request_operator_condition(work, andon)
        if proposal_id:
            return Answer(
                kind=KIND_STANDARD_WORK,
                summary=(
                    "No tier could draft the rule. Asked the operator to write "
                    "the condition in the portal; it will be backtested and "
                    "signed like any other change."
                ),
                artifact=proposal_id,
                detail={"attempts": details.get("attempts")},
            )
    return watch_unresolved(andon, context)


_ANSWERS: Dict[str, Callable[[Mapping[str, Any], Any], Answer]] = {
    "reference_agreement": _standard_work,
    "correction": _correction,
    "turn_check": _turn_check,
    "repetition": _repetition,
    "kaizen_failure": _kaizen_failure,
}


def answer(andon: Mapping[str, Any], context: Any = None) -> Answer:
    """Kaizen's answer to one andon event. An event from a detector Kaizen has
    no specific answer for still gets one: a watch."""
    handler = _ANSWERS.get(str(andon.get("detector")), watch_unresolved)
    return handler(andon, context)
