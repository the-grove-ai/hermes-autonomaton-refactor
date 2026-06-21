"""Stage 01 Kaizen digest for memory_context proposals.

The detector stages proposals into ``memory_proposals.jsonl``; this module
is the approval-application layer the operator's decision drives:

* :class:`MemoryProposalHandler` — renders one proposal and applies an
  approved one (mints a :class:`MemoryCreated` / :class:`MemorySuperseded`
  event and rebuilds the index).
* :func:`run_digest` — walks the pending proposals, asks the injected
  ``decide`` callback per proposal, applies the decision, flips the record's
  status, and records a uniform ``kaizen_disposition`` ledger event via the
  existing ``grove.flywheel_cli._record_kaizen_disposition`` boundary.

``run_digest`` is surface-agnostic: the ``decide`` callback abstracts the
decision source (a TTY prompt, the conversational push surface, or a test
stub). Fail-loud write boundary: a ``supersede`` whose ``target_id`` is not
in the index raises BEFORE any event is appended (the Phase 1 commitment —
the immutable log is never poisoned with a dangling reference).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from grove.memory.events import (
    MemoryCreated,
    MemorySuperseded,
    new_event_id,
    new_record_id,
)
from grove.memory.store import MemoryStore

logger = logging.getLogger(__name__)

__all__ = ["MemoryProposalHandler", "run_digest"]

# decide(summary: str, proposal: dict) -> "approve" | "reject" | "defer"
DecideFn = Callable[[str, Dict[str, Any]], str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryProposalHandler:
    """Render and apply a single memory_context proposal."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @staticmethod
    def summary_renderer(proposal: Dict[str, Any]) -> str:
        """Human-readable one-liner for operator review.

        Static — store-independent (reads only the proposal dict), so it can
        serve as a one-arg renderer in the unified RENDER_REGISTRY. Still
        callable as ``handler.summary_renderer(proposal)`` on an instance.
        """
        rec = proposal["proposed_record"]
        action = proposal.get("action", "create")
        line = (
            f"[{action}] {rec['entity_type']}: {rec['content']} "
            f"(Confidence: {rec['confidence']})"
        )
        if action == "supersede" and proposal.get("target_id"):
            line += f" — supersedes {proposal['target_id']}"
        return line

    def apply(self, proposal: Dict[str, Any]) -> bool:
        """Apply an approved proposal: mint the event, rebuild the index.

        Fail loud at the write boundary — an unknown action, or a supersede
        naming a record not in the index, raises BEFORE any append.
        """
        action = proposal.get("action")
        rec = proposal["proposed_record"]
        entity_type = rec["entity_type"]
        content = rec["content"]
        confidence = float(rec["confidence"])
        dock_goal_ref = proposal.get("dock_goal_ref")
        sources = proposal.get("sources") or []

        if action == "create":
            event: Any = MemoryCreated(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=new_record_id(),
                entity_type=entity_type,
                content=content,
                confidence=confidence,
                dock_goal_ref=dock_goal_ref,
                sources=sources,
                supersedes=None,
            )
        elif action == "supersede":
            target_id = proposal.get("target_id")
            if not target_id:
                raise ValueError("supersede proposal missing target_id")
            if target_id not in self._store.projected_records():
                raise ValueError(
                    f"supersede target {target_id!r} is not in the memory index; "
                    f"refusing to append a dangling supersede event"
                )
            event = MemorySuperseded(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=new_record_id(),
                entity_type=entity_type,
                content=content,
                confidence=confidence,
                dock_goal_ref=dock_goal_ref,
                sources=sources,
                supersedes=target_id,
            )
        else:
            raise ValueError(f"unknown memory proposal action {action!r}")

        self._store.append_event(event)
        self._store.rebuild_index()
        return True


def _read_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("[grove.memory.digest] malformed proposals line: %r", exc)
    return records


def _rewrite(path: Path, records: List[Dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def _disposition_envelope(proposal: Dict[str, Any], session_id: str):
    """Wrap a memory proposal in a minimal RoutingProposal for the uniform
    ``_record_kaizen_disposition`` boundary (it reads only id / type /
    evidence-count)."""
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_MEMORY_CONTEXT,
        RoutingProposal,
        compute_proposal_id,
    )

    evidence = (session_id,) if session_id else ()
    pid = compute_proposal_id(
        type=PROPOSAL_TYPE_MEMORY_CONTEXT, payload=proposal, evidence=evidence,
    )
    return RoutingProposal(
        proposal_id=pid,
        type=PROPOSAL_TYPE_MEMORY_CONTEXT,
        payload=proposal,
        evidence=evidence,
        eval_hash="",
        created_at=_now_iso(),
    )


def run_digest(
    *,
    store: MemoryStore,
    proposals_path: Path,
    decide: DecideFn,
    ledger_dir: Any = None,
) -> Dict[str, int]:
    """Surface pending memory proposals and apply the operator's decisions.

    For each pending proposal: render it, ask ``decide``; on ``approve``
    mint the event and flip the record to ``approved``; on ``reject`` flip
    to ``rejected``; on ``defer`` leave it pending. A ``kaizen_disposition``
    ledger event is recorded for every approve (``applied``) and reject
    (``rejected``). Returns the per-disposition counts.
    """
    from grove.flywheel_cli import _record_kaizen_disposition

    path = Path(proposals_path)
    records = _read_records(path)
    handler = MemoryProposalHandler(store)
    counts = {"approved": 0, "rejected": 0, "deferred": 0}
    changed = False

    for rec in records:
        if rec.get("status") != "pending" or "proposal" not in rec:
            continue
        proposal = rec["proposal"]
        session_id = rec.get("session_id", "")
        summary = handler.summary_renderer(proposal)
        decision = decide(summary, proposal)

        if decision == "approve":
            applied = handler.apply(proposal)
            rec["status"] = "approved"
            changed = True
            counts["approved"] += 1
            _record_kaizen_disposition(
                _disposition_envelope(proposal, session_id),
                disposition="applied",
                applied_result={"applied": bool(applied)},
                ledger_dir=ledger_dir,
            )
        elif decision == "reject":
            rec["status"] = "rejected"
            changed = True
            counts["rejected"] += 1
            _record_kaizen_disposition(
                _disposition_envelope(proposal, session_id),
                disposition="rejected",
                reason=proposal.get("proposed_record", {}).get("justification"),
                ledger_dir=ledger_dir,
            )
        else:
            counts["deferred"] += 1

    if changed:
        _rewrite(path, records)
    return counts
