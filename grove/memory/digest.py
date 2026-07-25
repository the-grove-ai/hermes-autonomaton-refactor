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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from grove.memory.events import (
    MemoryCreated,
    MemoryDeprecated,
    MemoryGraduated,
    MemorySuperseded,
    new_event_id,
    new_record_id,
)
from grove.memory.dispositions import DISPOSED_AT_FIELD, TERMINAL_STATUSES
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
        """Natural-language one-liner for operator review (kaizen-voice).

        Operator-facing voice — NO internal schema (no ``[action]`` prefix, no
        ``EntityType:`` label, no raw float, no ``mem_`` id). Just the content,
        confidence as a percentage. A ``supersede`` reads as an update.

        Static — store-independent (reads only the proposal dict), so it can
        serve as a one-arg renderer in the unified RENDER_REGISTRY. Still
        callable as ``handler.summary_renderer(proposal)`` on an instance.
        """
        action = proposal.get("action", "create")
        if action == "deprecate":
            # Deprecation proposals carry target_id + reason + content (NOT a
            # proposed_record). Render the record in plain words — no schema,
            # no id. Falls back when content was not embedded at stage time.
            content = proposal.get("content")
            if not content:
                return "A memory record has decayed below threshold. Deprecate?"
            reason = proposal.get("reason", "")
            tail = f" — {reason}" if reason else ""
            return f"'{content}'{tail}. Deprecate?"
        if action == "graduate":
            # Graduation proposals carry target_id + content (NOT a
            # proposed_record), same as deprecate. Read content directly —
            # reaching for proposal["proposed_record"] here would KeyError.
            content = proposal.get("content")
            if not content:
                return "A stable memory record is ready to graduate to permanent knowledge. Graduate?"
            return f"'{content}' has proven stable. Graduate it to permanent knowledge?"
        rec = proposal["proposed_record"]
        try:
            pct = round(float(rec["confidence"]) * 100)
        except (TypeError, ValueError, KeyError):
            pct = 0
        body = f"{rec['content']} (Confidence: {pct}%)"
        if action == "supersede":
            return f"Updated understanding: {body}"
        return body

    def apply(self, proposal: Dict[str, Any]) -> bool:
        """Apply an approved proposal: mint the event, rebuild the index.

        Fail loud at the write boundary — an unknown action, or a supersede
        naming a record not in the index, raises BEFORE any append.
        """
        action = proposal.get("action")
        dock_goal_ref = proposal.get("dock_goal_ref")
        # dock-goal-ref-integrity-v1 M3 — belt behind the detector's parse-site
        # gate: no ref that is not a dock goal id reaches the event log. NOTE:
        # validity (the ref EXISTS in the dock — operator + machine goals) is a
        # DISTINCT predicate from push-relevance (the goal is active NOW,
        # run_agent.py:5718); do not unify them — a record validly ref'ing a
        # paused goal must survive here and merely not push there. Cold path:
        # one load_dock() per approval, and only when a ref is present.
        if dock_goal_ref is not None:
            from grove.dock import load_dock

            dock = load_dock()
            dock_ids = {g.id for g in dock.goals} if dock is not None else set()
            if dock_goal_ref not in dock_ids:
                logger.warning(
                    "[grove.memory.digest] proposal dock_goal_ref %r matches "
                    "no dock goal id; nulled before apply",
                    dock_goal_ref,
                )
                dock_goal_ref = None
        sources = proposal.get("sources") or []
        # GUARD P3-b — captured from the PRE-supersede projection (before the
        # common tail's rebuild flips the old record to "superseded"); drives
        # the stale-cellar-page reap after rebuild.
        superseded_graduated_at: Optional[str] = None

        if action == "create":
            rec = proposal["proposed_record"]
            event: Any = MemoryCreated(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=new_record_id(),
                entity_type=rec["entity_type"],
                content=rec["content"],
                confidence=float(rec["confidence"]),
                dock_goal_ref=dock_goal_ref,
                sources=sources,
                supersedes=None,
            )
        elif action == "supersede":
            rec = proposal["proposed_record"]
            target_id = proposal.get("target_id")
            if not target_id:
                raise ValueError("supersede proposal missing target_id")
            projected = self._store.projected_records()
            if target_id not in projected:
                raise ValueError(
                    f"supersede target {target_id!r} is not in the memory index; "
                    f"refusing to append a dangling supersede event"
                )
            # Capture BEFORE the rebuild (ruling d): if the superseded record was
            # graduated, its cellar page must be reaped after the index rebuild.
            superseded_graduated_at = projected[target_id].graduated_at
            event = MemorySuperseded(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=new_record_id(),
                entity_type=rec["entity_type"],
                content=rec["content"],
                confidence=float(rec["confidence"]),
                dock_goal_ref=dock_goal_ref,
                sources=sources,
                supersedes=target_id,
            )
        elif action == "deprecate":
            target_id = proposal.get("target_id")
            if not target_id:
                raise ValueError("deprecate proposal missing target_id")
            if target_id not in self._store.projected_records():
                raise ValueError(
                    f"deprecate target {target_id!r} is not in the memory index; "
                    f"refusing to append a dangling deprecate event"
                )
            reason = proposal.get("reason", "Confidence below deprecation threshold")
            event = MemoryDeprecated(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=target_id,
                reason=reason,
            )
        elif action == "graduate":
            target_id = proposal.get("target_id")
            if not target_id:
                raise ValueError("graduate proposal missing target_id")
            record = self._store.projected_records().get(target_id)
            if record is None:
                raise ValueError(
                    f"graduate target {target_id!r} is not in the memory index; "
                    f"refusing to append a dangling graduate event"
                )
            # Idempotency (fail loud): a record already graduated has a cellar
            # page and a MemoryGraduated event — re-minting would duplicate.
            if record.graduated_at is not None:
                raise ValueError(
                    f"graduate target {target_id!r} is already graduated "
                    f"(graduated_at={record.graduated_at!r}); refusing to re-graduate"
                )
            event = MemoryGraduated(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=target_id,
            )
        else:
            raise ValueError(f"unknown memory proposal action {action!r}")

        self._store.append_event(event)
        self._store.rebuild_index()

        # Graduation projects the now-graduated record into the wiki cellar. The
        # fold set graduated_at AND flipped status to "graduated" (K4 dual-serve
        # closure) — query() no longer serves it; the cellar page is its sole
        # surface. Lazy import — the memory subsystem reaches the wiki subsystem
        # ONLY here, never at module load (GUARD P4-a: grove.wiki imports
        # grove.memory under TYPE_CHECKING only, so no runtime cycle).
        if action == "graduate":
            from grove.wiki.pipeline import project_memory

            project_memory(self._store.projected_records()[event.record_id])
        elif action == "supersede" and superseded_graduated_at is not None:
            # The superseded record was graduated — reap its now-stale cellar
            # page (the new record will graduate on its own merits later). The
            # FTS index drops the orphaned row on its next mtime refresh, so no
            # explicit index delete is needed. Hash basis MUST match
            # _write_page (sha256("memory#"+id)[:_HASH_LEN]).
            import hashlib

            from grove.wiki.pipeline import (
                _HASH_LEN,
                _MEMORY_SOURCE_PREFIX,
                _MEMORY_SOURCE_TYPE,
                get_wiki_path,
            )

            short_hash = hashlib.sha256(
                (_MEMORY_SOURCE_PREFIX + target_id).encode("utf-8")
            ).hexdigest()[:_HASH_LEN]
            out_dir = get_wiki_path() / "pages" / _MEMORY_SOURCE_TYPE
            if out_dir.is_dir():
                for stale in out_dir.glob(f"*-{short_hash}.md"):
                    stale.unlink()

        return True


def _read_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        logger.info("[grove.memory.digest] proposals file does not exist: %s", path)
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
        proposer="memory_digest",  # proposal-proposer-attribution-v1 (#3)
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
    counts = {"approved": 0, "rejected": 0, "deferred": 0, "dismissed": 0}
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
            rec[DISPOSED_AT_FIELD] = _now_iso()  # R-20 anchor (disposition-gate-v1)
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
            rec[DISPOSED_AT_FIELD] = _now_iso()  # R-20 anchor (disposition-gate-v1)
            changed = True
            counts["rejected"] += 1
            _record_kaizen_disposition(
                _disposition_envelope(proposal, session_id),
                disposition="rejected",
                reason=proposal.get("proposed_record", {}).get("justification"),
                ledger_dir=ledger_dir,
            )
        elif decision == "dismiss":
            # crystallization-cadence-v1 (Gap 3) — SOFT dismiss. Flip to a
            # distinct "dismissed" status: the proposal loses push eligibility
            # (is_push_eligible / _pending read "pending" only) and stays in the
            # CLI backlog, but is NOT a rejection — _recently_rejected reads
            # status=="rejected" only, so the detector's rejection memory is
            # untouched and valid insight is not blinded. No kaizen rejection
            # disposition is recorded (that channel is for genuine rejections).
            rec["status"] = "dismissed"
            rec[DISPOSED_AT_FIELD] = _now_iso()  # R-20 anchor (disposition-gate-v1)
            changed = True
            counts["dismissed"] += 1
        else:
            counts["deferred"] += 1

    if changed:
        _rewrite(path, records)
    return counts


# ── disposition-gate-v1 P3 — the reset verb (R-19) ────────────────────────
def default_proposals_path() -> Path:
    """``~/.grove/memory_proposals.jsonl`` — the crystallization store."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "memory_proposals.jsonl"


@dataclass
class ResetResult:
    """The WRITE RESULT of a reset — the caller reports this, never the tap."""
    ok: bool
    status_before: Optional[str] = None
    message: str = ""


def reset_memory_proposal(
    proposal_id: str,
    *,
    proposals_path: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
) -> ResetResult:
    """Un-decide: flip a terminally-disposed memory proposal back to ``pending``
    so the producer and operator treat the subject as undecided. Nothing more —
    the original action is NOT re-run, and WHICH proposals are permitted does
    not change (disposition-gate-v1 P3, R-19; scope pinned by Andon F3/Task 3).

    Discipline:
      * Uses the EXISTING atomic rewriter (:func:`_rewrite`) — no second writer.
      * Clears ``disposed_at`` (R-28): a later re-disposition anchors on its OWN
        timestamp, never the stale one. digest/actions re-stamp on the flip.
      * Emits a ``kaizen_disposition`` ledger event (disposition="reset") so
        "did my reset land" is answerable without a filesystem read.
      * Returns the WRITE RESULT. A successful rewrite is ``ok=True`` even if the
        ledger append fails (the write is authority; the ledger is observability,
        logged loud) — the confirmation reports the write, never the tap.
    """
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_MEMORY_CONTEXT,
        compute_proposal_id,
    )
    from grove.flywheel_cli import _record_kaizen_disposition

    path = Path(proposals_path) if proposals_path else default_proposals_path()
    records = _read_records(path)
    for rec in records:
        if rec.get("status") not in TERMINAL_STATUSES:
            continue
        proposal = rec.get("proposal")
        if not isinstance(proposal, dict):
            continue
        session_id = rec.get("session_id", "")
        evidence = (session_id,) if session_id else ()
        pid = compute_proposal_id(
            type=PROPOSAL_TYPE_MEMORY_CONTEXT, payload=proposal, evidence=evidence,
        )
        if pid != proposal_id:
            continue
        before = rec.get("status")
        rec["status"] = "pending"
        rec.pop(DISPOSED_AT_FIELD, None)  # R-28 — no stale anchor into the next decision
        _rewrite(path, records)
        ledger_ok = True
        try:
            _record_kaizen_disposition(
                _disposition_envelope(proposal, session_id),
                disposition="reset",
                extra={"status_before": before},
                ledger_dir=ledger_dir,
            )
        except Exception as exc:  # noqa: BLE001 — write landed; ledger is observability
            ledger_ok = False
            logger.warning(
                "[grove.memory.digest] reset ledger event failed (non-fatal; "
                "the pending flip landed): %r", exc,
            )
        # R-30 — the confirmation is self-disclosing on partial success, in
        # plain phone-readable language. Both facts, no jargon: the flip landed
        # (ok=True), and if the ledger append failed the operator is told this
        # reset won't show in the disposition log.
        message = f"Reset to pending (was {before})."
        if not ledger_ok:
            message += (
                " The reset landed, but saving its ledger entry failed — so "
                "this reset won't show in the disposition log."
            )
        return ResetResult(ok=True, status_before=before, message=message)
    return ResetResult(
        ok=False,
        message=(
            "No disposed proposal matched this id — it may already be pending, "
            "already reset, or removed."
        ),
    )
