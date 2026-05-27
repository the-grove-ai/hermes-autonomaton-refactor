"""Grove Intent Store — Sprint 28 intent-capture-v1, the feed-first layer.

Every operator interaction the Autonomaton handles writes an IntentRecord
to this store. The store is the persistent feed three downstream consumers
read from:

  1. Skill Flywheel (detector / ratchet / refiner stubs) — cross-session
     pattern recognition for skill promotion and tier ratcheting.
  2. Cognitive Router — learns which tier handles which intent_class
     reliably over time.
  3. Compositional context (deferred to a future sprint) — feeds soul,
     memory, goals, RAG, register based on observed intent patterns.

GRV-001 Principle: the Autonomaton gets smarter, cheaper, and more capable
with repeated usage. The Intent Store is the foundation that promise sits
on. A grove-autonomaton that costs the same and knows the same on day 100
as on day 1 has failed its design.

Storage: a single append-only JSON Lines file at
``~/.grove/intent_records.jsonl``. Cross-session by design — the Flywheel
needs to recognize that the operator asks similar things across many
sessions. Single-file layout pays a small lock-contention price for
straightforward cross-session queries.

Phase 4 provisional-write pattern: the Dispatcher writes a record with
``outcome="pending"`` at turn-end; a finalization record with the same
``turn_id`` and a terminal outcome (``success`` / ``correction``) appends
at the start of the next turn. ``latest_by_turn`` collapses the pair into
the effective state per turn.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "IntentRecord",
    "IntentStore",
    "VALID_OUTCOMES",
    "get_store",
]


# Closed set of valid outcome states. Sprint 28 Phase 4 implements the
# provisional-write pattern: ``pending`` is the initial state at turn-end;
# ``success`` / ``correction`` finalize at the start of the next turn;
# ``drop`` / ``error`` are terminal states the Dispatcher writes directly
# when the turn cannot reach a normal end.
VALID_OUTCOMES: frozenset[str] = frozenset({
    "pending",
    "success",
    "drop",
    "error",
    "correction",
})


@dataclass(frozen=True)
class IntentRecord:
    """One operator interaction's intent record.

    Frozen dataclass so records are hashable and never mutated after
    construction. The provisional-write pattern doesn't mutate records;
    it appends a fresh record with the same ``turn_id`` and a different
    outcome, and ``IntentStore.latest_by_turn`` collapses by turn_id.

    Required fields capture the irreducible per-turn surface: when, who
    (session), what (intent classification), and the initial outcome.
    Optional fields hold downstream telemetry that the Dispatcher fills
    when it has the information (tier routing, tools yielded, response
    length).
    """

    timestamp: str                              # ISO 8601 UTC
    session_id: str
    turn_id: str                                # session-unique monotonic id
    user_message_stem: str                      # first 100 chars normalized
    pattern_hash: str                           # SHA-256 from ClassificationResult
    intent_class: str
    register_class: str
    complexity_signal: str
    confidence: float
    outcome: str                                # see VALID_OUTCOMES

    # Phase 2 addition: goal_alignment from the extended Haiku classifier.
    # Optional so Phase 1 records (written before Phase 2 ships) and any
    # path that bypasses classification round-trip cleanly.
    goal_alignment: Optional[str] = None

    # Routing telemetry — None when no Cognitive Router configured
    # (vanilla install) or when the record is a pure finalization.
    tier_selected: Optional[str] = None
    model_used: Optional[str] = None

    # Per-turn dispatch telemetry. Tuples not lists so the dataclass stays
    # hashable; JSON round-trip coerces lists back to tuples on read.
    tools_yielded: Tuple[str, ...] = field(default_factory=tuple)
    api_calls: int = 0
    duration_ms: float = 0.0
    final_response_chars: Optional[int] = None


class IntentStore:
    """Append-only JSON Lines store for IntentRecords.

    One file, shared across sessions. Thread-safe writes via an in-process
    lock; cross-process / cross-instance safety relies on POSIX
    ``O_APPEND`` atomicity for short records — JSON Lines records are one
    line each, well within the atomic write boundary on macOS and Linux.

    Tests pass an explicit ``store_path``; production callers use
    :func:`get_store` to acquire the module-level singleton bound to
    ``~/.grove/intent_records.jsonl``.
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        if store_path is None:
            from hermes_constants import get_hermes_home
            store_path = Path(get_hermes_home()) / "intent_records.jsonl"
        self._path = Path(store_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: IntentRecord) -> dict:
        """Append one record to the store; return the persisted dict.

        Raises ValueError if ``record.outcome`` is not in
        :data:`VALID_OUTCOMES` — the closed set is enforced at write
        time so an unknown outcome value cannot accumulate silently in
        the feed (Architectural Prime Directive: fail loud).

        Returns the serialized payload as a dict so callers wanting to
        forward the same data to a logger or telemetry sink avoid a
        re-serialization round-trip.
        """
        if record.outcome not in VALID_OUTCOMES:
            raise ValueError(
                f"unknown outcome {record.outcome!r}; "
                f"expected one of {sorted(VALID_OUTCOMES)}"
            )
        data = asdict(record)
        # Coerce tuple → list for stable JSON; reads coerce back.
        data["tools_yielded"] = list(data["tools_yielded"])
        line = json.dumps(data, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return data

    def records(self) -> Iterator[IntentRecord]:
        """Stream every record in append order.

        Malformed lines (corrupted writes, partial flushes) are skipped
        at debug-log level — the runtime prefers a partial read over
        crashing on a damaged entry. Schema-mismatched lines (older
        records missing newer fields, or future fields this version
        doesn't know) are also skipped at debug.
        """
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "[grove.intent_store] malformed record line %d "
                        "in %s: %r", line_no, self._path, exc,
                    )
                    continue
                if isinstance(data.get("tools_yielded"), list):
                    data["tools_yielded"] = tuple(data["tools_yielded"])
                try:
                    yield IntentRecord(**data)
                except (TypeError, ValueError) as exc:
                    logger.debug(
                        "[grove.intent_store] schema mismatch line %d "
                        "in %s: %r", line_no, self._path, exc,
                    )

    def latest_by_turn(self) -> Iterator[IntentRecord]:
        """Yield the latest record per ``turn_id``.

        Phase 4 provisional-write pattern: a ``pending`` record at
        turn-end is later joined by a finalization record sharing the
        same ``turn_id`` and a terminal outcome. The latest-timestamp
        record for each turn is the effective state — this iterator
        materializes that view.

        Yields in arbitrary order; callers that need chronological
        order sort the result by ``timestamp``.
        """
        latest: dict[str, IntentRecord] = {}
        for record in self.records():
            existing = latest.get(record.turn_id)
            if existing is None or record.timestamp > existing.timestamp:
                latest[record.turn_id] = record
        yield from latest.values()

    def filter(
        self,
        *,
        session_id: Optional[str] = None,
        intent_class: Optional[str] = None,
        pattern_hash: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[str] = None,
        collapse_by_turn: bool = False,
    ) -> List[IntentRecord]:
        """Return records matching every supplied predicate.

        Predicates are AND-combined; ``None`` means "any". ``since`` is
        an inclusive ISO 8601 lower bound compared lexicographically
        (matches the timestamp format the store writes). Set
        ``collapse_by_turn=True`` to filter against the provisional-write
        collapsed view (one record per turn_id, latest wins).
        """
        source = self.latest_by_turn() if collapse_by_turn else self.records()
        out: List[IntentRecord] = []
        for record in source:
            if session_id is not None and record.session_id != session_id:
                continue
            if intent_class is not None and record.intent_class != intent_class:
                continue
            if pattern_hash is not None and record.pattern_hash != pattern_hash:
                continue
            if outcome is not None and record.outcome != outcome:
                continue
            if since is not None and record.timestamp < since:
                continue
            out.append(record)
        return out


# ── Module-level singleton accessor ──────────────────────────────────────


_default_store: Optional[IntentStore] = None


def get_store() -> IntentStore:
    """Return the module-level default store, constructing on first call.

    Production callers (the Dispatcher, the Flywheel stubs) acquire the
    store through this accessor so they share a single instance bound
    to ``~/.grove/intent_records.jsonl``. Tests monkeypatch
    ``_default_store`` with a tmp-path instance to isolate the file
    they write to from any other test or runtime state.
    """
    global _default_store
    if _default_store is None:
        _default_store = IntentStore()
    return _default_store
