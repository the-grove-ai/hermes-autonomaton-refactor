"""Dispatcher-init orchestration for the memory substrate.

Thin, testable glue between the IntentStore (dormancy signal), the Dock
(active-goal context), and the Context Persistence Detector (extraction).
The Dispatcher calls these from ``__init__``; keeping the logic here keeps
the Dispatcher footprint to a guarded, loud-log delegation.

Dormancy is derived directly from the IntentStore feed (sessions with a
``pending`` record older than the TTL), NOT from ``sweep_stale_pending``'s
return value — the sweep returns a count, not session ids, and capturing
dormant sessions must happen BEFORE the sweep finalizes their pending
records (the overnight "laptop closed" case).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "dormant_session_ids",
    "run_memory_extraction",
    "load_active_dock_goal_dicts",
]

# Memory-dormancy TTL — distinct from the 60-min Implicit Success Sweep.
# A session idle ≥ this long is a candidate for memory extraction.
DEFAULT_DORMANCY_MINUTES = 30


def dormant_session_ids(
    intent_store: Any,
    *,
    minutes: int = DEFAULT_DORMANCY_MINUTES,
    now: Optional[datetime] = None,
) -> List[str]:
    """Session ids with a ``pending`` record older than ``minutes``.

    Reads the collapsed per-turn view (``latest_by_turn``) so a turn already
    finalized (or swept) is not treated as dormant. Returns distinct session
    ids in first-seen order. ``now`` is a test seam.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(minutes=minutes)).isoformat()
    ordered: List[str] = []
    seen = set()
    for record in intent_store.latest_by_turn():
        if record.outcome != "pending":
            continue
        if record.timestamp >= cutoff_iso:
            continue
        if record.session_id not in seen:
            seen.add(record.session_id)
            ordered.append(record.session_id)
    return ordered


def run_memory_extraction(
    *,
    detector: Any,
    store: Any,
    session_ids: List[str],
    transcript_loader: Callable[[str], List[Dict[str, Any]]],
    dock_goals: List[Dict[str, Any]],
) -> int:
    """Run the detector over each dormant session; return total staged.

    For each swept session, first flushes the batched MemoryAccessed
    telemetry (Fix 1 — one event per served record id, regardless of whether
    extraction then proceeds), then runs the detector. No internal error
    handling — the detector is fail-loud and the caller (Dispatcher init)
    wraps this in a single loud-log guard. The detector's own processing
    lock makes each session strictly one-shot.
    """
    staged_total = 0
    flushed_total = 0
    for session_id in session_ids:
        flushed_total += store.flush_access_events(session_id)
        transcript = transcript_loader(session_id)
        staged_total += detector.detect_and_stage(
            session_id, transcript, dock_goals,
        )
    if staged_total or flushed_total:
        logger.info(
            "[grove.memory] staged %d proposal(s), flushed %d access "
            "event(s) from %d dormant session(s)",
            staged_total, flushed_total, len(session_ids),
        )
    return staged_total


def _goal_to_dict(goal: Any) -> Dict[str, Any]:
    """Map a Dock ``Goal`` to the detector's goal-summary shape.

    The goal ``id`` is the slug the detector and provider key
    ``dock_goal_ref`` against.
    """
    return {
        "slug": goal.id,
        "name": goal.name,
        "status": goal.status,
        "vector": goal.vector,
    }


def load_active_dock_goal_dicts(dock: Any = None) -> List[Dict[str, Any]]:
    """Active Dock goals as ``{slug, name, status, vector}`` dicts.

    Returns ``[]`` when no Dock is installed. ``dock`` may be injected for
    tests; otherwise the runtime Dock is loaded.
    """
    from grove.dock import active_goals, load_dock

    if dock is None:
        dock = load_dock()
    if dock is None:
        return []
    return [_goal_to_dict(g) for g in active_goals(dock)]
