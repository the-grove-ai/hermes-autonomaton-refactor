"""kaizen-exploration-proposals-v1 — exploration_nudge suppression tombstones.

Own-namespace store (R-A / F-4 collision analysis): a rejected "try model X
interactively?" nudge must NOT re-surface for that slug, and must NEVER share
the ``binding_tombstones.json`` namespace (whose key is skill/model — a rejected
nudge could otherwise suppress a legitimate model_binding proposal on the same
skill/model pair, or vice versa). Keyed on the catalog ``slug`` alone, beside
``proposals.jsonl`` in ``~/.grove`` so a ``git reset --hard`` deploy cannot wipe
a dismissal and resurrect the nudge (the admission_friction tombstone precedent).

The zero-arm producer (``run_exploration_scan``, added in a later phase) reads
:func:`_suppressed` to subtract tombstoned slugs from the candidate set. This
module ships the store first so the reject_callback has a durable home in
Phase 2, before the producer exists.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.eval.proposal_queue import default_queue_path

logger = logging.getLogger(__name__)

_TOMBSTONE_FILENAME = "exploration_tombstones.json"


def default_tombstone_path() -> Path:
    """``~/.grove/exploration_tombstones.json`` — beside the proposal queue,
    OUTSIDE the repo tree, so a deploy git-reset cannot wipe a dismissal and
    resurrect the nudge (admission_friction precedent)."""
    return default_queue_path().with_name(_TOMBSTONE_FILENAME)


def _load_tombstones(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or default_tombstone_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Fail LOUD but never crash a rejection or a scan on one bad store file —
        # an unreadable store suppresses nothing (nudges may re-surface; the
        # operator re-rejects) rather than suppressing everything.
        logger.warning(
            "[exploration_scan] tombstone store unreadable at %s (%s) — treating "
            "as empty; dismissed nudges may re-surface until repaired.", p, exc,
        )
        return []
    entries = data.get("tombstones") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _write_tombstones(
    entries: List[Dict[str, Any]], path: Optional[Path] = None
) -> None:
    p = path or default_tombstone_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tombstones": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def record_tombstone(
    proposal: Any, *, path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Reject-disposition hook — called by the exploration_nudge handler row's
    ``reject_callback`` BEFORE queue removal. Keys on the catalog ``slug`` alone.
    Returns the entry, or None when the payload carries no slug (logged)."""
    payload = getattr(proposal, "payload", None) or {}
    slug = payload.get("slug")
    if not slug:
        logger.warning(
            "[exploration_scan] rejected exploration_nudge %s carries no slug — "
            "no tombstone written",
            getattr(proposal, "proposal_id", "?"),
        )
        return None
    entry = {
        "slug": slug,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "proposal_id": getattr(proposal, "proposal_id", None),
    }
    entries = _load_tombstones(path)
    entries.append(entry)
    _write_tombstones(entries, path)
    logger.info("[exploration_scan] tombstone recorded: %s", slug)
    return entry


def _suppressed(tombstones: List[Dict[str, Any]], slug: str) -> bool:
    """A slug is suppressed iff any tombstone names it."""
    return any(t.get("slug") == slug for t in tombstones)
