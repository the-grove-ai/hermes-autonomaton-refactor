"""Kaizen's answer when a goal's work is asked for in a session that is not
clean.

A goal's session rule (``grove.decision_work.session_rule``) says whether its
sessions are isolated, what opens one, and what happens when the work is asked
for in a session that is not clean. That rule acts directly — no model and no
keg stand between it and what the system does — so it is in force only while
the operator's signature on exactly that rule stands, as a revocable standing
grant.

  * The rule is not signed (or changed since it was): propose it for
    signature. A standard-work change, in the portal.
  * The rule is signed and says ``open_clean_session``: the remedy is already
    authorized by that standing rule — the gateway opens a clean session and
    re-issues the request, and the action is recorded against the grant.
  * The rule is signed and says nothing about unclean sessions: watch. The
    operator declares ``on_unclean`` in the Dock and signs the changed rule.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from grove.andon import Answer, KIND_REMEDY, KIND_STANDARD_WORK


def propose_rule(work: Any, andon: Mapping[str, Any]) -> str:
    """Queue the goal's current session rule for the operator's signature.
    Returns the proposal id (the same id if it is already waiting)."""
    from grove import decision_work as dw
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_SESSION_RULE, RoutingProposal, append, compute_proposal_id,
    )

    cfg = work.config
    rule = dw.session_rule(cfg)
    digest = dw.session_rule_digest(cfg)
    payload = {"goal": cfg.goal_id, "digest": digest, "rule": rule}
    evidence = (f"session_rule:{cfg.goal_id}:{digest}",)
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_SESSION_RULE, payload=payload, evidence=evidence),
        type=PROPOSAL_TYPE_SESSION_RULE, payload=payload, evidence=evidence,
        eval_hash="sha256:" + hashlib.sha256(
            f"session_rule|{cfg.goal_id}|{digest}".encode("utf-8")).hexdigest(),
        created_at=datetime.now(timezone.utc).isoformat(),
        semantic_justification=describe(rule),
        proposer="kaizen",
        detail={"andon_id": andon.get("andon_id")},
    )
    append(proposal)      # idempotent: an identical rule already waiting is kept
    return proposal.proposal_id


def describe(rule: Mapping[str, Any]) -> str:
    parts = [f"Sessions for {rule.get('goal')}"]
    if rule.get("isolation"):
        parts.append("answer from the goal's declared sources only (no Cellar, no memory)")
    if rule.get("opens_on"):
        parts.append(
            f"open when a session's first message matches “{rule['opens_on']}” "
            f"(overlap ≥ {rule.get('match_threshold')})"
        )
    if rule.get("on_unclean") == "open_clean_session":
        parts.append(
            "and when the work is asked for in any other session, a clean "
            "session is opened and the request re-issued there"
        )
    return "; ".join(parts) + "."


def answer(andon: Mapping[str, Any], context: Any = None) -> Answer:
    from grove import decision_work as dw
    from grove.kaizen.answers import _work, watch, watch_unresolved

    work = _work(context, andon)
    if work is None or not work.config.isolated:
        return watch_unresolved(andon, context)
    cfg = work.config
    grant = dw.session_rule_grant(cfg)
    if grant is None:
        proposal_id = propose_rule(work, andon)
        return Answer(
            kind=KIND_STANDARD_WORK,
            summary=(
                f"The session rule for {cfg.goal_id} is not signed, so no "
                "session is isolated for it. Proposed the rule for your "
                "signature in the portal."
            ),
            artifact=proposal_id,
            detail={"rule": dw.session_rule(cfg)},
        )
    if cfg.on_unclean == dw.ON_UNCLEAN_OPEN_CLEAN:
        return Answer(
            kind=KIND_REMEDY,
            summary="Opening a clean session and re-issuing the request there.",
            artifact=grant.id,
            write_class="session_reset",
            detail={"standing_grant": grant.id, "authorized": "standing_rule",
                    "reissue": {"clean_session": True,
                                "request": (andon.get("details") or {}).get("request"),
                                "session_id": (andon.get("details") or {}).get("session_id"),
                                "andon_id": andon.get("andon_id"),
                                "authorized": grant.id}},
        )
    return watch(
        andon, signature={"class": "unclean_session", "goal": cfg.goal_id},
        description=(
            f"work for {cfg.goal_id} asked for in a session that is not clean; "
            "its signed session rule declares no action for that, so this work "
            "needs a new session started by hand"
        ),
        promote_after=3,
    )
