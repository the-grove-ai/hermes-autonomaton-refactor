"""artifact-continuation-v1 P2 — the continuation dispatch entry (core).

One governed turn over existing artifact(s): a system-constructed,
template-locked prompt frame names the ledger-resolved artifact path(s) as
context and carries the operator's instruction VERBATIM — the model authors
no frame text, and the instruction is the only free field (the cron
prompt-job precedent). The turn runs through the full five-stage pipeline
with zero new zone machinery.

Origin class (ANDON ruling, Option A): store-then-deny. Yellow halts store
the triggering ToolIntent into the EXISTING durable pending store (+ the
agent-reachable queue row, whose payload carries the minting turn's identity
context — the 1e/1f carrier) and the turn continues on a denial observation.
RED halts never reach the Stage-04 handler (the §VI fork routes them to
``_resolve_red_halt``); because this handler is not the fail-closed deny
handler, the surface reads operator-REACHABLE and RED store-pends through
the existing dispatcher path unchanged. Resolution side: untouched — rows
render and resolve on the existing pending fragment flow.

This module is a CORE capability; HTTP wiring is a later phase. No HTTP
types appear here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from grove.dispatcher import Dispatcher

logger = logging.getLogger(__name__)

# Template-locked (exact-string pinned by test). {artifact_paths} is the
# system-derived newline-joined ledger-resolved path list; {instruction} is
# the operator's text VERBATIM. Nothing else is interpolated.
CONTINUATION_FRAME = (
    "Operator continuation request over existing artifact(s).\n"
    "Context files (read each before acting):\n"
    "{artifact_paths}\n"
    "\n"
    "Operator instruction (verbatim):\n"
    "{instruction}"
)


class PendingStoreSovereignHandler:
    """Stage-04 store-then-deny handler for continuation turns (Option A).

    On a Yellow halt: store the triggering ToolIntent as a pending row in the
    durable store, bridge an agent-reachable queue row whose payload carries
    {zone, parent_artifact_ids, turn_id, active_primary_skill_slug,
    intent_class, tool} (the confirm-time emission carrier), then return
    ``"deny"`` so the turn continues on a denial observation. Store failure
    is LOUD and the deny stands (fail-closed — never a silent allow).

    ``bind()`` attaches the owning Dispatcher after construction so the
    handler can read the minting turn's identity state at halt time.
    """

    def __init__(self, parent_artifact_ids: Optional[List[str]] = None):
        self.parent_artifact_ids = list(parent_artifact_ids or [])
        self.stored: List[Dict[str, Any]] = []
        self._dispatcher: Any = None

    def bind(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def __call__(self, halt: Any) -> str:
        try:
            self._store(halt)
        except Exception as exc:  # noqa: BLE001 — deny stands regardless
            logger.warning(
                "[continuation] pending-store write failed (the fail-closed "
                "deny stands; the action is NOT pending and NOT executed): %r",
                exc,
            )
        return "deny"

    def _store(self, halt: Any) -> None:
        from grove.effect_signature import canonical_effect_signature
        from grove.eval import proposal_queue as _pq
        from grove.red_pending_store import (
            RED_PENDING_PROPOSAL_TYPE,
            PendingRedProposal,
            action_proposal_id,
            describe_red_action,
            get_red_pending_store,
            prepare_execute_arguments,
        )

        trig = halt.intents[halt.triggering_index]
        tool = getattr(trig, "tool_name", "") or ""
        args = dict(getattr(trig, "arguments", None) or {})
        exec_args = prepare_execute_arguments(tool, args)
        sig = canonical_effect_signature(tool, exec_args)
        pid = action_proposal_id(sig)
        pattern_key = getattr(halt, "pattern_key", None)
        zone = getattr(halt, "zone", None) or "yellow"
        created_at = datetime.now(timezone.utc).isoformat()
        d = self._dispatcher

        description, is_opaque = describe_red_action(tool, args, pattern_key)
        entry = PendingRedProposal(
            proposal_id=pid,
            tool_name=tool,
            arguments=exec_args,
            effect_signature=sig,
            description=description,
            rationale="",
            created_at=created_at,
            is_opaque=is_opaque,
            pattern_key=pattern_key,
            zone=zone,
        )
        get_red_pending_store().put(entry)

        # The 1e/1f carrier — minting-turn identity context on the queue row
        # (ids and labels only; never argument values).
        _pq.append(
            _pq.RoutingProposal(
                proposal_id=f"{RED_PENDING_PROPOSAL_TYPE}:{pid}",
                type=RED_PENDING_PROPOSAL_TYPE,
                payload={
                    "zone": zone,
                    "parent_artifact_ids": list(self.parent_artifact_ids),
                    "turn_id": getattr(d, "_current_turn_id", None),
                    "active_primary_skill_slug": getattr(
                        d, "_last_loaded_primary_slug", None
                    ),
                    "intent_class": getattr(
                        getattr(d, "_current_turn_classification", None),
                        "intent_class", None,
                    ),
                    "tool": tool,
                },
                evidence=(),
                eval_hash=pid,
                created_at=created_at,
                proposer="governance",
            )
        )
        self.stored.append({"proposal_id": pid, "tool": tool, "zone": zone})


def dispatch_continuation_turn(
    instruction_text: str,
    artifact_ids: Optional[List[str]] = None,
    *,
    agent_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run ONE governed continuation turn over the named artifacts.

    ``artifact_ids`` must already be validated by the caller (the verb
    surface); an id the ledger cannot resolve fails LOUD here — never a
    silently smaller context list. Returns::

        {turn_id, response_text, halted, pending_items, artifact_ids_written}

    ``pending_items`` are the Yellow rows the store-then-deny handler filed
    this turn; ``artifact_ids_written`` are this turn's artifact_written ids
    read back from the turn's own session ledger (the sole id authority).
    """
    from grove.api.artifacts import _scan_ledger_index

    parent_ids = list(artifact_ids or [])
    index = _scan_ledger_index()
    paths: List[str] = []
    for aid in parent_ids:
        recorded = index.get(aid)
        if recorded is None:
            raise LookupError(
                f"unknown artifact id {aid!r} — the caller must validate ids "
                "against the ledger before dispatch (GATE-B cond. 4)."
            )
        paths.append(recorded)

    prompt = CONTINUATION_FRAME.format(
        artifact_paths="\n".join(paths) if paths else "(none)",
        instruction=instruction_text,
    )

    session_id = "portal_{}".format(
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    )
    handler = PendingStoreSovereignHandler(parent_artifact_ids=parent_ids)
    dispatcher = Dispatcher(
        sovereign_prompt_handler=handler,
        agent_kwargs=dict(
            platform="portal",
            session_id=session_id,
            quiet_mode=True,
            **(agent_kwargs or {}),
        ),
    )
    handler.bind(dispatcher)
    # One-shot lineage slot — the per-turn reset consumes it at turn setup.
    dispatcher._next_turn_parent_artifact_ids = list(parent_ids)

    agent = dispatcher.agent
    result = agent.run_conversation(prompt) or {}
    turn_id = getattr(dispatcher, "_current_turn_id", None)

    # This turn's written artifact ids, read back from the session's own
    # ledger (authoritative; the answer-decoration stash is consumed by the
    # response hook and cannot be re-read here).
    written: List[str] = []
    try:
        from grove.kaizen_ledger import KaizenLedger

        for event in KaizenLedger(session_id).events_by_type(
            "artifact_written"
        ):
            if event.get("turn_id") == turn_id and event.get("artifact_id"):
                written.append(event["artifact_id"])
    except Exception as exc:  # noqa: BLE001 — read-side resilience
        logger.warning(
            "[continuation] written-artifact readback failed (result field "
            "degrades to []; the ledger events themselves stand): %r", exc,
        )

    return {
        "turn_id": turn_id,
        "response_text": result.get("final_response") or "",
        "halted": bool(handler.stored),
        "pending_items": list(handler.stored),
        "artifact_ids_written": written,
    }
