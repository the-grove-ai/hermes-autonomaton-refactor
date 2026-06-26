"""MemoryStore — append-only event log + projected index.

Event-sourced, mirroring ``grove/intent_store.py``:

  * ``memory_records.jsonl`` — the append-only event log (source of truth).
  * ``memory_index.json``    — a derived projection cache of the ACTIVE
    records. Always reconstructible from the log (R4 invariant); deleting
    it and calling :meth:`rebuild_index` yields identical projected state.

The log holds :mod:`grove.memory.events`; the projection holds
:class:`~grove.memory.record.MemoryRecord`. Writes are guarded by an
in-process lock (single-line JSONL records rely on ``O_APPEND`` atomicity
for cross-process safety, same as the IntentStore).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from grove.memory.events import (
    MemoryAccessed,
    MemoryCreated,
    MemoryDeprecated,
    MemoryEvent,
    MemoryGraduated,
    MemorySuperseded,
    new_event_id,
)
from grove.memory.record import MemoryRecord, decay_rate_for

logger = logging.getLogger(__name__)

__all__ = ["MemoryStore"]


# Score boost applied to a record whose dock_goal_ref matches one of the
# operator's active Dock goals — goal-relevant memory ranks above general
# memory of comparable keyword strength.
_DOCK_GOAL_BOOST = 2.0

# Below this confidence a decayed record is NOT auto-deprecated — that is a
# Sprint B detector proposal, not a store-internal action.
_DEPRECATION_FLOOR = 0.2

# Serialization discriminator → event class. Stored as ``__type__`` on each
# JSONL line so the reader reconstructs the right frozen dataclass.
_EVENT_TYPES = {
    "MemoryCreated": MemoryCreated,
    "MemorySuperseded": MemorySuperseded,
    "MemoryDeprecated": MemoryDeprecated,
    "MemoryAccessed": MemoryAccessed,
    "MemoryGraduated": MemoryGraduated,
}


# memory-operational-hardening-v1 Fix 1 — telemetry debounce. The provider
# served-record IDs are collected per session here (module-level so they
# survive the per-call fresh-store factory) and flushed to exactly one
# MemoryAccessed event per unique record id when the session is swept. This
# replaces the per-turn record_access write (N+1 unbounded log growth).
_PENDING_ACCESS: Dict[str, set] = {}
_PENDING_ACCESS_LOCK = threading.Lock()
# Context label recorded on a batched access event (the section that served it).
_ACCESS_BATCH_CONTEXT = "accumulated_domain_memory"


def _reset_pending_access() -> None:
    """Clear the module-level pending-access registry (test isolation)."""
    with _PENDING_ACCESS_LOCK:
        _PENDING_ACCESS.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """Event-sourced memory store: JSONL log + projected active index."""

    def __init__(self, base_dir: Path) -> None:
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        self._log_path = base / "memory_records.jsonl"
        self._index_path = base / "memory_index.json"
        self._lock = threading.Lock()
        self._index: Dict[str, MemoryRecord] = {}
        self.rebuild_index()

    # ── paths ────────────────────────────────────────────────────────────

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def index_path(self) -> Path:
        return self._index_path

    # ── event log: write + read ──────────────────────────────────────────

    def append_event(self, event: MemoryEvent) -> dict:
        """Serialize ``event`` and append one JSON line to the log.

        Returns the persisted dict (carrying the ``__type__`` discriminator)
        so callers can forward it without re-serializing.
        """
        type_name = type(event).__name__
        if type_name not in _EVENT_TYPES:
            raise ValueError(
                f"unknown memory event type {type_name!r}; "
                f"expected one of {sorted(_EVENT_TYPES)}"
            )
        data = asdict(event)
        data["__type__"] = type_name
        line = json.dumps(data, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return data

    def read_events(self) -> Iterator[MemoryEvent]:
        """Stream every event in append order.

        Malformed or unknown-type lines are skipped at warning level — the
        immutable log is replayed, not rejected; a damaged line cannot be
        un-appended, so the runtime logs and continues rather than crashing.
        """
        if not self._log_path.exists():
            return
        with open(self._log_path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[grove.memory] malformed event line %d in %s: %r",
                        line_no, self._log_path, exc,
                    )
                    continue
                type_name = data.pop("__type__", None)
                cls = _EVENT_TYPES.get(type_name)
                if cls is None:
                    logger.warning(
                        "[grove.memory] unknown event type %r at line %d in %s",
                        type_name, line_no, self._log_path,
                    )
                    continue
                try:
                    yield cls(**data)
                except TypeError as exc:
                    logger.warning(
                        "[grove.memory] schema mismatch line %d in %s: %r",
                        line_no, self._log_path, exc,
                    )

    # ── projection ───────────────────────────────────────────────────────

    def rebuild_index(self) -> Dict[str, MemoryRecord]:
        """Fold the full event log into the projected index and cache it.

        Returns the projected records (all statuses). Writes the ACTIVE
        subset to ``memory_index.json``. This is the R4 reconstruction path.
        """
        records: Dict[str, MemoryRecord] = {}
        for ev in self.read_events():
            if isinstance(ev, MemoryCreated):
                records[ev.record_id] = self._record_from_event(ev)
            elif isinstance(ev, MemorySuperseded):
                old = records.get(ev.supersedes)
                if old is None:
                    logger.warning(
                        "[grove.memory] supersede %s names missing target %s",
                        ev.record_id, ev.supersedes,
                    )
                else:
                    old.status = "superseded"
                records[ev.record_id] = self._record_from_event(ev)
            elif isinstance(ev, MemoryDeprecated):
                rec = records.get(ev.record_id)
                if rec is None:
                    logger.warning(
                        "[grove.memory] deprecate names missing record %s",
                        ev.record_id,
                    )
                else:
                    rec.status = "deprecated"
            elif isinstance(ev, MemoryAccessed):
                rec = records.get(ev.record_id)
                if rec is None:
                    logger.warning(
                        "[grove.memory] access names missing record %s",
                        ev.record_id,
                    )
                else:
                    rec.access_count += 1
                    rec.last_accessed = ev.timestamp
            elif isinstance(ev, MemoryGraduated):
                rec = records.get(ev.record_id)
                if rec is None:
                    logger.warning(
                        "[grove.memory] graduate names missing record %s",
                        ev.record_id,
                    )
                else:
                    # Dual-serve invariant: record graduation, but DO NOT touch
                    # status — the record stays "active" and is still served
                    # via the query/JSONL path. Suppression is deferred to K4.
                    rec.graduated_at = ev.timestamp

        self._index = records
        self._save_index(records)
        return records

    def projected_records(self) -> Dict[str, MemoryRecord]:
        """The full in-memory projection (all statuses), keyed by record id."""
        return self._index

    @staticmethod
    def _record_from_event(ev) -> MemoryRecord:
        return MemoryRecord(
            id=ev.record_id,
            entity_type=ev.entity_type,
            content=ev.content,
            confidence=ev.confidence,
            dock_goal_ref=ev.dock_goal_ref,
            sources=list(ev.sources),
            status="active",
            supersedes=ev.supersedes,
            created_at=ev.timestamp,
            last_accessed=None,
            access_count=0,
            decay_rate=decay_rate_for(ev.entity_type),
        )

    # ── query ────────────────────────────────────────────────────────────

    def query(
        self,
        *,
        keywords: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        min_confidence: float = 0.0,
        dock_goal_refs: Optional[List[str]] = None,
        require_keyword_match: bool = True,
    ) -> List[MemoryRecord]:
        """Return ACTIVE records matching the predicates, ranked by score.

        Filter by ``entity_types`` and ``min_confidence``. Score by the
        count of ``keywords`` found (case-insensitive substring) in the
        content, with a fixed boost when ``dock_goal_ref`` is in
        ``dock_goal_refs``. Sort by score desc, then confidence desc.

        ``require_keyword_match`` (default True — the original narrowing
        contract): when keywords are given, records with zero keyword hits are
        excluded. Pass ``False`` (turn-keyword-relevance-v1) for keyword-as-
        boost: keyword hits ADD to score but zero-hit records (e.g. Dock-goal
        memory) survive and rank on their boost/confidence.
        """
        scored: List[tuple] = []
        for rec in self._index.values():
            if rec.status != "active":
                continue
            if entity_types is not None and rec.entity_type not in entity_types:
                continue
            if rec.confidence < min_confidence:
                continue

            score = 0.0
            if keywords:
                content_lower = rec.content.lower()
                hits = sum(1 for kw in keywords if kw.lower() in content_lower)
                if require_keyword_match and hits == 0:
                    continue
                score += hits
            if dock_goal_refs and rec.dock_goal_ref in dock_goal_refs:
                score += _DOCK_GOAL_BOOST
            scored.append((score, rec.confidence, rec))

        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [rec for _, _, rec in scored]

    # ── access telemetry ─────────────────────────────────────────────────

    def record_access(self, record_id: str, session_id: str, context: str) -> None:
        """Append a MemoryAccessed event and bump the live index in place.

        Does NOT rebuild from the log — the access count and last_accessed
        are updated on the in-memory record directly (system telemetry, no
        governance). Raises ``KeyError`` if ``record_id`` is unknown.
        """
        if record_id not in self._index:
            raise KeyError(f"record_access on unknown record_id {record_id!r}")
        event = MemoryAccessed(
            event_id=new_event_id(),
            timestamp=_now_iso(),
            record_id=record_id,
            session_id=session_id,
            context=context,
        )
        self.append_event(event)
        rec = self._index[record_id]
        rec.access_count += 1
        rec.last_accessed = event.timestamp

    def mark_accessed(self, session_id: str, record_id: str) -> None:
        """Record that ``record_id`` was served this session — NO event write.

        memory-operational-hardening-v1 Fix 1: collected into a module-level
        per-session set (deduped) and flushed once when the session is swept.
        Replaces the per-turn :meth:`record_access` write that grew the log
        N-per-turn for zero analytical value.
        """
        with _PENDING_ACCESS_LOCK:
            _PENDING_ACCESS.setdefault(session_id, set()).add(record_id)

    def flush_access_events(self, session_id: str) -> int:
        """Emit exactly one MemoryAccessed event per unique served record id.

        Drains the session's pending-access set, so a second call emits
        nothing (idempotent). Appends events directly to the log — it does
        not bump the in-memory index (the store is typically a fresh sweep
        instance) and never raises on a since-deprecated record (the access
        happened regardless). Returns the number of events emitted.
        """
        with _PENDING_ACCESS_LOCK:
            record_ids = _PENDING_ACCESS.pop(session_id, None)
        if not record_ids:
            return 0
        for record_id in sorted(record_ids):
            self.append_event(MemoryAccessed(
                event_id=new_event_id(),
                timestamp=_now_iso(),
                record_id=record_id,
                session_id=session_id,
                context=_ACCESS_BATCH_CONTEXT,
            ))
        return len(record_ids)

    # ── decay ────────────────────────────────────────────────────────────

    def apply_decay(
        self,
        active_dock_goals: Optional[List[str]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Apply entity-type decay to active records' confidence in place.

        A record whose ``dock_goal_ref`` is in ``active_dock_goals`` is
        SUSPENDED — no decay (the operator's declared priority keeps it
        warm). Otherwise confidence is multiplied by
        ``decay_rate ** days_since_last_accessed`` (falling back to days
        since creation when never accessed).

        A record dropping below the deprecation floor is left as-is — auto
        deprecation is a Sprint B detector proposal, not a store action.
        The ``now`` keyword is a test seam, mirroring
        :meth:`IntentStore.sweep_stale_pending`.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        active_goals = set(active_dock_goals or ())

        for rec in self._index.values():
            if rec.status != "active":
                continue
            if rec.dock_goal_ref is not None and rec.dock_goal_ref in active_goals:
                continue  # suspended by an active Dock goal
            if rec.decay_rate >= 1.0:
                continue  # no time decay for this entity type
            anchor = rec.last_accessed or rec.created_at
            days = (now - datetime.fromisoformat(anchor)).total_seconds() / 86400.0
            if days <= 0:
                continue
            rec.confidence = rec.confidence * (rec.decay_rate ** days)
            if rec.confidence < _DEPRECATION_FLOOR:
                logger.debug(
                    "[grove.memory] %s decayed below floor (%.3f) — "
                    "Sprint B detector territory, not auto-deprecating",
                    rec.id, rec.confidence,
                )

        self._save_index(self._index)

    # ── index cache I/O ──────────────────────────────────────────────────

    def _load_index(self) -> Dict[str, MemoryRecord]:
        """Read the active-record projection cache from ``memory_index.json``."""
        if not self._index_path.exists():
            return {}
        raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        return {rid: MemoryRecord(**fields) for rid, fields in raw.items()}

    def _save_index(self, records: Dict[str, MemoryRecord]) -> None:
        """Write the ACTIVE subset of ``records`` to ``memory_index.json``."""
        active = {
            rid: asdict(rec)
            for rid, rec in records.items()
            if rec.status == "active"
        }
        with self._lock:
            tmp = self._index_path.with_suffix(self._index_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(active, sort_keys=True, indent=2), encoding="utf-8"
            )
            tmp.replace(self._index_path)
