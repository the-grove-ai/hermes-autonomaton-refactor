"""Path-B revision-feedback store (suggest-revision-verb-v1).

Row_id-keyed JSON under ``~/.grove/forge/.feedback/<row_id>.json`` — sovereign,
and OUTSIDE ``pending_review/`` so it survives ``_archive_forge_slug`` (the draft
dir is archived; the guidance persists). The suggest_revision route (P2) writes
it; the host-side resolver (P3) reads it to fold a revision_directive into the
worker payload so the re-draft carries the operator's guidance.

ACCUMULATE-with-history: each operator revision APPENDS to ``history`` and
increments ``count`` — the worker sees the full chronological guidance, and the
N-breaker (P4) reads ``count``. Atomic ``tmp -> os.rename`` write (never a torn
read). ``revision_note`` is stored RAW; callers HTML-escape only at render.

Schema::

    {"history": [{"ts": <iso8601>, "revision_note": <raw str>}, ...],
     "count": <int>, "terminal_skip": false, "written_at": <iso8601>}

The ``terminal_skip`` setter and the TTL-GC land in P4 — not here (no dead code).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

from grove.fleet.staging import _atomic_write_bytes


def _store_dir() -> Path:
    return Path(get_hermes_home()) / "forge" / ".feedback"


def _entry_path(row_id: str) -> Path:
    """The store file for *row_id*. row_id is a Notion page id (uuid-ish); a
    basename-guard neutralizes any path separator so a crafted id can never escape
    the store dir."""
    safe = row_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return _store_dir() / f"{safe}.json"


def read(row_id: str) -> Optional[Dict[str, Any]]:
    """The store entry for *row_id*, or ``None`` when absent.

    A present-but-unreadable entry raises (``json.JSONDecodeError`` / ``OSError``)
    — the P3 resolver must FAIL LOUD, never draft feedback-blind on a corrupt
    entry. Callers decide how to surface it; this never swallows.
    """
    path = _entry_path(row_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write(row_id: str, revision_note: str) -> Dict[str, Any]:
    """ACCUMULATE the operator's revision guidance for *row_id* and return the
    persisted entry.

    Appends ``{ts, revision_note}`` to ``history``, increments ``count``, stamps
    ``written_at``; creates the entry if absent. ``revision_note`` is stored RAW.
    Atomic ``tmp -> os.rename`` (via ``_atomic_write_bytes``). Fail loud on an
    empty key — a store file under an empty row_id is never written.
    """
    if not row_id:
        raise ValueError("feedback_store.write: refusing to write under an empty row_id")
    now = datetime.now(timezone.utc).isoformat()
    entry = read(row_id) or {"history": [], "count": 0, "terminal_skip": False}
    entry.setdefault("history", []).append({"ts": now, "revision_note": revision_note})
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["written_at"] = now
    entry.setdefault("terminal_skip", False)
    _store_dir().mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(
        _entry_path(row_id),
        json.dumps(entry, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return entry
