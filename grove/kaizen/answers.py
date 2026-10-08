"""Kaizen's answers — one for every andon event, no exceptions.

The andon handler (``grove.andon``) routes each event here. Kaizen returns
exactly one :class:`~grove.andon.Answer`:

  standard_work — a drafted, backtested change to standard work, filed for
                  the operator's signature in the portal;
  remedy        — an in-scope action, filed for the operator's answer in
                  conversation (green changes are approved in conversation;
                  changes to authority are signed in the portal);
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


# What a watch remembers across a restart of its count: when the count
# restarted, who has been asked, and when the operator took the answer back.
_CARRIED = ("since", "asked", "revoked_at")


def _digest(signature: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(signature), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]


from grove.pattern_cache import watch_id, watch_record  # noqa: E402  (read helpers)


def _save_watch(store: Any, watch_id: str, record: Mapping[str, Any]) -> None:
    from grove.pattern_cache import CompiledPattern, STATUS_WATCHING

    store.upsert(CompiledPattern(
        pattern_id=watch_id, t0_key=watch_id,
        intent_class="watch", cacheable_type="watch",
        cached_response=None, compiled_invocation=None,
        evidence_hash="sha256:" + watch_id.rsplit(":", 1)[-1], status=STATUS_WATCHING,
        created_at=record["first_seen"],
        promotion_evidence=json.dumps({"watch": dict(record)}, sort_keys=True),
    ))


def note_watch(watch_id: str, *, store: Any = None, **changes: Any) -> None:
    """Add to what a watch remembers, leaving its count and status alone."""
    from grove.pattern_cache import PatternCacheStore

    store = store or PatternCacheStore()
    entry = store.get(watch_id)
    if entry is None:
        return
    record = {**watch_record(watch_id, store=store), **changes}
    status = entry.status
    _save_watch(store, watch_id, record)
    store.set_status(watch_id, status)


def rewatch(
    watch_id: str, *, detector: str, goal: Any, signature: Mapping[str, Any],
    store: Any = None, **changes: Any,
) -> None:
    """Start a watch's count again from nothing: what it counted was answered
    (declined, or taken back) and only what happens next counts now."""
    from grove.pattern_cache import PatternCacheStore

    store = store or PatternCacheStore()
    prior = watch_record(watch_id, store=store)
    record = {
        "detector": detector, "goal": goal, "signature": dict(signature),
        "description": prior.get("description") or "",
        "trigger": prior.get("trigger") or {}, "seen": 0,
        "andon_ids": [], "first_seen": prior.get("first_seen") or _now(),
        "last_seen": prior.get("last_seen") or _now(),
        **{k: prior[k] for k in _CARRIED if k in prior},
        **changes,
    }
    _save_watch(store, watch_id, record)


def watch(
    andon: Mapping[str, Any],
    *,
    signature: Mapping[str, Any],
    description: str,
    promote_after: int,
    promote: Optional[Callable[[Dict[str, Any]], Answer]] = None,
    store: Any = None,
    seen: Optional[int] = None,
) -> Answer:
    """Record (or re-observe) a watch: an inert cache entry that carries the
    condition which would promote it. Seeing the same condition again counts
    toward that condition; reaching it calls ``promote`` and returns ITS
    answer instead — the ratchet turning on repeated evidence.

    ``seen`` is the count when the detector counted it from the records
    itself; otherwise each observation adds one."""
    from grove.pattern_cache import (
        CompiledPattern, PatternCacheStore, STATUS_SUPERSEDED, STATUS_WATCHING,
    )

    store = store or PatternCacheStore()
    digest = _digest(signature)
    watch_id = f"watch:{andon.get('goal') or 'none'}:{andon.get('detector')}:{digest}"
    entry = store.get(watch_id)
    record: Dict[str, Any] = {}
    carried: Dict[str, Any] = {}
    if entry is not None:
        prior = json.loads(entry.promotion_evidence or "{}").get("watch") or {}
        carried = {k: prior[k] for k in _CARRIED if k in prior}
        if entry.status == STATUS_WATCHING:
            record = prior
    seen = int(seen) if seen is not None else int(record.get("seen") or 0) + 1
    record = {
        **carried,
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
            {"attempts": out.get("attempts"), "reason": "draft_failed",
             # Kept so the operator can be shown the case and asked for the rule.
             "miss": {k: (andon.get("details") or {}).get(k)
                      for k in ("item_id", "inputs", "served", "corrected")}},
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
                f"{record['seen']} times. Proposed keg v{out.get('version')} with "
                f"that change, for your signature. " + replay_summary(out)
            ),
            artifact=out.get("proposal_id"), detail=out,
        )

    return watch(
        andon, signature=signature,
        description=(
            f"the operator correcting {key!r} to {details.get('corrected')}; "
            f"{needed} matching corrections promote it to a proposed change"
        ),
        promote_after=needed,
        promote=_promote if work is not None and work.config.keg is not None else None,
    )


# Abnormalities that mean "this tier did not complete the turn": the answer it
# gave is not a valid one, or it answered without doing the work at all. Each
# is answered by the ladder rule — the same request, one tier up.
LADDER_REASONS = frozenset({
    "output_not_in_domain", "undeclared_output", "reply_without_tool",
    "reply_without_record",
    # The tier gave no answer inside the goal's declared time budget.
    "call_over_budget",
})
ESCALATING_MESSAGE = "That attempt didn't complete; retrying with a stronger model."
OVER_BUDGET_MESSAGE = "No answer inside the time budget; retrying one tier up."
NOT_COMPLETED_MESSAGE = (
    "This request couldn't be completed: every tier was tried and none "
    "finished it. Nothing was recorded."
)


def _fail_upward(andon: Mapping[str, Any]) -> Answer:
    """The ladder rule. Re-issue the same request one tier up — automatically,
    one attempt per tier. At the top there is nothing higher: stop, and watch."""
    from grove import reissue
    from grove.andon import AUTHORIZED_LADDER_RULE

    details = andon.get("details") or {}
    tier, reason = details.get("tier"), details.get("reason")
    attempts = list(details.get("attempts") or []) + [{
        "turn_uid": details.get("turn_uid"), "tier": tier, "reason": reason,
        "andon_id": andon.get("andon_id"),
    }]
    up = reissue.next_tier(tier)
    if up is None or not details.get("session_id") or not details.get("request"):
        # The top of the ladder (or a turn that cannot be re-issued): no
        # further retries. Watch for it repeating.
        stop = watch(
            andon, signature={"class": reason, "goal": andon.get("goal")},
            description=(
                f"a request no tier completed (last tried: {tier or 'unknown'}; "
                f"{reason})"
            ),
            promote_after=_DEFAULT_PROMOTE_AFTER,
        )
        if stop.kind == KIND_WATCH:
            stop.summary = f"{NOT_COMPLETED_MESSAGE} {stop.summary}"
            stop.detail = {**stop.detail, "stops_attempt": {
                "session_id": details.get("session_id"),
                "turn_uid": details.get("turn_uid"), "attempts": attempts}}
        return stop
    return Answer(
        kind=KIND_REMEDY,
        summary=OVER_BUDGET_MESSAGE if reason == "call_over_budget" else ESCALATING_MESSAGE,
        artifact=f"ladder:{andon.get('andon_id')}",
        write_class="tier_escalation",
        detail={"authorized": AUTHORIZED_LADDER_RULE,
                "reissue": {"goal": andon.get("goal"), "from_tier": tier, "tier": up,
                            "request": details.get("request"),
                            "session_id": details.get("session_id"),
                            "turn_uid": details.get("turn_uid"),
                            "andon_id": andon.get("andon_id"),
                            "authorized": AUTHORIZED_LADDER_RULE,
                            "attempts": attempts}},
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
    if reason in LADDER_REASONS:
        return _fail_upward(andon)
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
    if (
        details.get("reason") in ("draft_failed", "condition_refused")
        and work is not None and work.config.keg
    ):
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


def _operator_feedback(andon: Mapping[str, Any], context: Any) -> Answer:
    """The operator sent a draft back with a reason. Redraft with it: the
    same drafting and the same checks, now also bound by what they said. The
    stopped line stays stopped until a redraft is signed."""
    return _standard_work(andon, context)


def _confirmed_key(andon: Mapping[str, Any], context: Any) -> Answer:
    """The operator kept confirming a model's answer for a key the reference
    table does not list. Propose that answer as a rule of the goal's keg —
    drafted, backtested and signed in the portal like any other change to
    standard work. Their own confirmations are the evidence."""
    from grove.kaizen import standard_work

    details = andon.get("details") or {}
    work = _work(context, andon)
    key, output = details.get("key"), details.get("output")
    if work is None or work.config.keg is None or key is None or not output:
        return watch_unresolved(andon, context)
    out = standard_work.propose_direct_rule(
        work, andon, key=key, corrected=output, how="confirmed")
    if out.get("status") == "proposed":
        more = out.get("rules_added", 1) > 1
        return Answer(
            kind=KIND_STANDARD_WORK,
            summary=(
                f"You confirmed {key!r} the same way {details.get('confirmations')} times, "
                f"with no revision. Proposed keg v{out.get('version')} with that change"
                + (f" (one card, {out['rules_added']} changes)" if more else "")
                + ", for your signature. " + replay_summary(out)
            ),
            artifact=out.get("proposal_id"), detail=out,
        )
    return watch(
        andon, signature={"key": key, "status": out.get("status")},
        description=str(out.get("detail") or out.get("status")),
        promote_after=_UNREADABLE_PROMOTE_AFTER,
    )


def _key_alias(andon: Mapping[str, Any], context: Any) -> Answer:
    """A new key is an existing key under another name, on the goal's declared
    evidence. Propose a rule that answers it the same way — with the identity
    match and the confirmation stated — for the operator's signature in the
    portal. Never applied by the system, whatever the confidence."""
    from grove.kaizen import standard_work

    details = andon.get("details") or {}
    work = _work(context, andon)
    key, same_as, output = details.get("key"), details.get("same_as"), details.get("output")
    if work is None or work.config.keg is None or not (key and same_as and output):
        return watch_unresolved(andon, context)
    grounds = []
    for found in details.get("identity") or []:
        if found.get("kind") == "same":
            grounds.append(f"same {str(found.get('input')).replace('_', ' ')} "
                           f"({found.get('value')})")
        else:
            grounds.append(f"the item's {str(found.get('input')).replace('_', ' ')} "
                           f"names {same_as!r}")
    n = int(details.get("confirmations") or 0)
    because = (f"the same as {same_as!r}. Identity: " + "; ".join(grounds)
               + f". You confirmed it {n} time{'' if n == 1 else 's'}")
    out = standard_work.propose_direct_rule(
        work, andon, key=key, corrected=output, how="confirmed", because=because)
    if out.get("status") == "proposed":
        return Answer(
            kind=KIND_STANDARD_WORK,
            summary=(
                f"{key!r} looks like {same_as!r} under another name ("
                + "; ".join(grounds) + f"), and you confirmed the same answer. Proposed "
                f"v{out.get('version')} answering it the same way, for your signature. "
                + replay_summary(out)
            ),
            artifact=out.get("proposal_id"), detail={**out, "alias_of": same_as},
        )
    return watch(
        andon, signature={"key": key, "same_as": same_as, "status": out.get("status")},
        description=str(out.get("detail") or out.get("status")),
        promote_after=_UNREADABLE_PROMOTE_AFTER,
    )


def _phrase_reading(andon: Mapping[str, Any], context: Any) -> Answer:
    """A model read the operator's phrase as a verb the work session already
    has. Below the declared threshold: watch. At it: ask the operator, in
    conversation, whether the phrase should mean that from now on — a filed
    remedy, in scope because the signed session rule lets the goal's
    vocabulary supply phrases for that verb."""
    from grove import adaptation

    details = andon.get("details") or {}
    work = _work(context, andon)
    verb, phrase = details.get("verb"), details.get("phrase")
    needed = int(details.get("threshold") or _DEFAULT_PROMOTE_AFTER)
    signature = {"pattern": details.get("pattern"), "verb": verb, "phrase": phrase}
    if work is None:
        return watch_unresolved(andon, context)
    waiting = adaptation.pending(work.config.goal_id, verb, phrase)
    if waiting:
        # Already asked. Remind once in a session that has not seen the card.
        adaptation.offer(work.config, waiting[0], details.get("session_id"))
        return Answer(
            kind=KIND_WATCH,
            summary=(f"Already asked whether “{phrase}” should mean {verb}; "
                     f"waiting for the operator's answer."),
            artifact=watch_id(andon.get("goal"), andon.get("detector"), signature),
        )

    # Jidoka counted every reading no revision has undone. Readings from
    # before the operator last answered about this phrase (not now, or taking
    # it back) were already answered, and do not count toward asking again.
    since = watch_record(
        watch_id(andon.get("goal"), andon.get("detector"), signature)).get("since")
    counted = [p for p in (andon.get("provenance") or [])
               if not since or str(p.get("at") or "") > str(since)]

    def _promote(record: Dict[str, Any]) -> Optional[Answer]:
        if verb not in adaptation.signed_verbs(work.config):
            return None      # no signed rule lets the vocabulary supply it: keep watching
        return adaptation.propose(work, andon, counted)

    return watch(
        andon, signature=signature,
        description=(
            f"“{phrase}” read as {verb} by a model; {needed} readings with no "
            f"revision promote it to a question for the operator"
        ),
        promote_after=needed, promote=_promote, seen=len(counted),
    )


_ANSWERS: Dict[str, Callable[[Mapping[str, Any], Any], Answer]] = {
    "operator_feedback": _operator_feedback,
    "reference_agreement": _standard_work,
    "correction": _correction,
    "turn_check": _turn_check,
    "repetition": _repetition,
    "kaizen_failure": _kaizen_failure,
    "phrase_reading": _phrase_reading,
    "confirmed_key": _confirmed_key,
    "key_alias": _key_alias,
}


def answer(andon: Mapping[str, Any], context: Any = None) -> Answer:
    """Kaizen's answer to one andon event. An event from a detector Kaizen has
    no specific answer for still gets one: a watch."""
    handler = _ANSWERS.get(str(andon.get("detector")), watch_unresolved)
    return handler(andon, context)
