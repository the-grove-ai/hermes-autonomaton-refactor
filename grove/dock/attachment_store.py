"""goal-spine-v1 P2 — the attachment authority (mint / detach / reader).

Attachment is a durable, event-sourced fact: an ``artifact_goal_attached``
ledger event minted ONLY by :func:`mint_attachment`, ONLY with an approving
``proposal_id``. Reversal is a second event (``artifact_goal_detached``, the
MemoryDeprecated idiom) — latest-wins by timestamp at read time, so
mint → detach → re-mint re-attaches. Nothing is ever rewritten.

"Store" per the ``grove/memory/store.py`` event-sourced shape: append-only
events, read-time projection. The projection (read-time collapse) is the
AUTHORITY on what is attached; the writers' pre-mint read-guards are
best-effort bloat control only — no reader depends on them.

SANCTIONED-WRITER CONVENTION (H2, ratified): there is no registry,
decorator, or governance seam that enforces writer uniqueness — being
"sanctioned" is a convention (one narrow function, single entry point,
refusal-first validation, files its own audit event carrying proposal_id).
That gap is real and owned by privilege-broker-v1, not this sprint.

Audit-floor INVERSION (deliberate, vs the capability_registry binding
writer): there, the file mutation lands first and the audit event is a
trailing leg with an error-log floor. HERE the ledger event IS the
mutation — a ``record()`` failure RAISES, never floors, because flooring
would silently claim an attachment that never happened.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# The artifact-id shape (grove.artifact_identity canon: sha256(canonical
# path)[:16]). Local compile — the api-route module's _ID_RE is private to a
# module that drags aiohttp into the import path.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_ATTACH_SESSION_PREFIX = "attachment-"


class AttachmentWriteError(RuntimeError):
    """A mint/detach refusal — malformed or unresolvable input, fail loud."""


# ── writers ─────────────────────────────────────────────────────────────────


def _writer_ledger(ledger_dir: Optional[Path]):
    """One component-filer ledger handle per write (the cli-<utc> sentinel
    session precedent, capability_registry._file_binding_mutation_event)."""
    from grove.kaizen_ledger import KaizenLedger

    session_id = _ATTACH_SESSION_PREFIX + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    return KaizenLedger(session_id=session_id, ledger_dir=ledger_dir)


def _require_nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttachmentWriteError(
            f"{name} must be a non-empty string; got {value!r}"
        )
    return value.strip()


def _artifact_known(artifact_id: str, ledger_dir: Optional[Path]) -> bool:
    """Does any artifact_written event record this id? Tolerant scan (the
    _scan_ledger_index idiom) — used for write-strict input validation."""
    from grove.kaizen_ledger import default_ledger_dir

    base = Path(ledger_dir) if ledger_dir is not None else default_ledger_dir()
    if not base.is_dir():
        return False
    for path in sorted(base.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        event.get("event_type") == "artifact_written"
                        and event.get("artifact_id") == artifact_id
                    ):
                        return True
        except OSError:
            continue
    return False


def _excerpt_cap() -> int:
    """The config-valued excerpt bound (goal_attachment.excerpt_cap_chars).
    Lazy import: the loader lives with the detector's config block; this
    module stays import-light and cycle-free."""
    from grove.dock.attachment import load_goal_attachment_config

    return int(load_goal_attachment_config()["excerpt_cap_chars"])


def mint_attachment(
    artifact_id: str,
    goal_id: str,
    *,
    proposal_id: str,
    rationale: str,
    excerpt: str,
    ledger_dir: Optional[Path] = None,
    dock: Optional[Any] = None,
    excerpt_cap: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Mint an attachment — the SOLE sanctioned artifact_goal_attached writer.

    Contract (P2):

    * ``proposal_id`` is REQUIRED. No proposal_id, no attachment — approval
      is the only mint path.
    * Write-strict, refusal-first: malformed ids, an artifact_id no
      artifact_written event records, a goal_id not in the Dock, or an
      empty rationale/excerpt raise :class:`AttachmentWriteError` BEFORE
      any append. Never a partial write.
    * The excerpt is bounded by the config value
      (``goal_attachment.excerpt_cap_chars``); truncation is VISIBLE in the
      stored event (``excerpt_truncated`` + ``excerpt_full_chars``), never
      silent.
    * Idempotent (H4 ruling): a pair already attached in the projection is
      a no-op returning ``None`` — no duplicate row, no error. This
      pre-mint read-guard is BEST-EFFORT bloat control only; the read-time
      collapse is the authority and no reader depends on this guard.
    * The event is the mutation: a ``record()`` failure RAISES (never the
      binding-writer error floor).

    Returns the persisted event dict, or ``None`` on the idempotent no-op.
    """
    artifact_id = _require_nonempty_str(artifact_id, "artifact_id")
    goal_id = _require_nonempty_str(goal_id, "goal_id")
    proposal_id = _require_nonempty_str(proposal_id, "proposal_id")
    rationale = _require_nonempty_str(rationale, "rationale")
    excerpt = _require_nonempty_str(excerpt, "excerpt")

    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise AttachmentWriteError(
            f"artifact_id {artifact_id!r} is not a 16-hex artifact id"
        )
    if not _artifact_known(artifact_id, ledger_dir):
        raise AttachmentWriteError(
            f"artifact_id {artifact_id!r} has no artifact_written event in "
            f"the ledger — refusing to attach an unrecorded artifact"
        )

    if dock is None:
        from grove.dock import load_dock

        dock = load_dock()
    if dock is None:
        raise AttachmentWriteError(
            "Dock not installed (no dock.yaml) — cannot resolve goal_id "
            f"{goal_id!r}; refusing to attach to an unresolvable goal"
        )
    known_goals = {g.id for g in dock.goals}
    if goal_id not in known_goals:
        raise AttachmentWriteError(
            f"goal_id {goal_id!r} is not a Dock goal (known: "
            f"{sorted(known_goals)}) — refusing to attach to an "
            f"unresolvable goal"
        )

    # Best-effort idempotence guard (bloat control ONLY — the read-time
    # collapse below is the authority; no reader depends on this check).
    if (artifact_id, goal_id) in attached_pairs(ledger_dir=ledger_dir):
        logger.info(
            "[attachment_store] (%s, %s) already attached — no-op",
            artifact_id, goal_id,
        )
        return None

    cap = int(excerpt_cap) if excerpt_cap is not None else _excerpt_cap()
    if cap <= 0:
        raise AttachmentWriteError(
            f"excerpt_cap_chars must be positive; got {cap!r}"
        )
    full_chars = len(excerpt)
    truncated = full_chars > cap
    stored_excerpt = excerpt[:cap]

    # The event IS the mutation — record() raising propagates (deliberate;
    # see module docstring).
    return _writer_ledger(ledger_dir).record(
        "artifact_goal_attached",
        artifact_id=artifact_id,
        goal_id=goal_id,
        proposal_id=proposal_id,
        rationale=rationale,
        excerpt=stored_excerpt,
        excerpt_truncated=truncated,
        excerpt_full_chars=full_chars,
    )


def detach_attachment(
    artifact_id: str,
    goal_id: str,
    *,
    reason: str,
    ledger_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Detach a previously-minted attachment (the MemoryDeprecated idiom).

    Requires an operator-originated ``reason`` — NOT a proposal_id (H5
    ruling). Deliberately does NOT resolve the goal against the Dock: a
    pair attached to a since-pruned ``auto-*`` staging goal must remain
    detachable.

    Idempotent: detaching a pair that is not currently attached is a no-op
    returning ``None`` (best-effort guard, same non-authority status as the
    mint guard). Files its own audit event; a ``record()`` failure RAISES.
    """
    artifact_id = _require_nonempty_str(artifact_id, "artifact_id")
    goal_id = _require_nonempty_str(goal_id, "goal_id")
    reason = _require_nonempty_str(reason, "reason")

    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise AttachmentWriteError(
            f"artifact_id {artifact_id!r} is not a 16-hex artifact id"
        )

    if (artifact_id, goal_id) not in attached_pairs(ledger_dir=ledger_dir):
        logger.info(
            "[attachment_store] (%s, %s) is not attached — detach no-op",
            artifact_id, goal_id,
        )
        return None

    return _writer_ledger(ledger_dir).record(
        "artifact_goal_detached",
        artifact_id=artifact_id,
        goal_id=goal_id,
        reason=reason,
    )


# ── reader (read-time collapse — THE authority; R-9 tolerant) ───────────────


def _scan_attachment_events(ledger_dir: Optional[Path] = None) -> List[dict]:
    """Every attach/detach event across all session ledgers, in file/line
    order (the _scan_artifact_events dir-glob precedent — tolerant per-line
    parse; a malformed line or unreadable file never aborts the scan)."""
    from grove.kaizen_ledger import default_ledger_dir

    base = Path(ledger_dir) if ledger_dir is not None else default_ledger_dir()
    events: List[dict] = []
    if not base.is_dir():
        return events
    for path in sorted(base.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event_type") in (
                        "artifact_goal_attached",
                        "artifact_goal_detached",
                    ):
                        events.append(event)
        except OSError:
            continue
    return events


def attached_pairs(
    ledger_dir: Optional[Path] = None,
    *,
    live_goal_ids: Optional[Set[str]] = None,
) -> Dict[Tuple[str, str], dict]:
    """The attachment projection: (artifact_id, goal_id) → latest mint event.

    Ledger-derived and read-resilient (the _lineage_for idiom): an event
    with a malformed artifact_id/goal_id contributes nothing; a detached
    pair is excluded; it never raises. Latest-wins by ``timestamp`` (stable
    sort, scan order breaks exact ties) — mint → detach → re-mint
    re-attaches (H5 ruling).

    ``live_goal_ids`` (R-9): when given, a pair whose goal is no longer in
    the set — e.g. a pruned ``auto-*`` staging goal — contributes nothing.
    ``None`` skips the filter (the pure-ledger view).
    """
    latest: Dict[Tuple[str, str], dict] = {}
    events = _scan_attachment_events(ledger_dir)
    events.sort(
        key=lambda e: e.get("timestamp") if isinstance(e.get("timestamp"), str) else ""
    )  # stable — scan order preserved on exact-tie timestamps
    for event in events:
        aid = event.get("artifact_id")
        gid = event.get("goal_id")
        if not (isinstance(aid, str) and aid and isinstance(gid, str) and gid):
            continue  # malformed event — no contribution, never an error
        latest[(aid, gid)] = event
    attached = {
        pair: event
        for pair, event in latest.items()
        if event.get("event_type") == "artifact_goal_attached"
    }
    if live_goal_ids is not None:
        attached = {
            (aid, gid): event
            for (aid, gid), event in attached.items()
            if gid in live_goal_ids  # pruned goal — contributes nothing (R-9)
        }
    return attached


def attachments_for_artifact(
    artifact_id: str,
    ledger_dir: Optional[Path] = None,
    *,
    live_goal_ids: Optional[Set[str]] = None,
) -> List[dict]:
    """Live attachment events for one artifact (insertion-ordered)."""
    return [
        event
        for (aid, _gid), event in attached_pairs(
            ledger_dir=ledger_dir, live_goal_ids=live_goal_ids
        ).items()
        if aid == artifact_id
    ]


def attachments_for_goal(
    goal_id: str,
    ledger_dir: Optional[Path] = None,
) -> List[dict]:
    """Live attachment events for one goal (the P4 view's read)."""
    return [
        event
        for (_aid, gid), event in attached_pairs(ledger_dir=ledger_dir).items()
        if gid == goal_id
    ]


def attached_artifact_ids(
    ledger_dir: Optional[Path] = None,
    *,
    live_goal_ids: Optional[Set[str]] = None,
) -> Set[str]:
    """Artifact ids with at least one live attachment — the detector's
    exclusion-seam read."""
    return {
        aid
        for (aid, _gid) in attached_pairs(
            ledger_dir=ledger_dir, live_goal_ids=live_goal_ids
        )
    }
