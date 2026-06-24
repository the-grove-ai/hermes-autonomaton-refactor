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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "IntentRecord",
    "IntentStore",
    "VALID_OUTCOMES",
    "finalize_record",
    "get_store",
    "normalize_message_stem",
]


_STEM_CHARS = 100


def normalize_message_stem(message: str, max_chars: int = _STEM_CHARS) -> str:
    """Return the first ``max_chars`` of ``message`` for human-readable
    cross-reference against the classifier's ``pattern_hash``.

    The hash itself normalizes (lowercase + whitespace-collapsed); this
    stem preserves the operator's original casing and spacing so a
    ledger reader can recognize their own request at a glance.
    """
    return message[:max_chars]


def finalize_record(
    record: "IntentRecord", *, outcome: str, timestamp: str,
) -> "IntentRecord":
    """Construct a finalization record carrying the same ``turn_id``.

    The provisional-write pattern appends rather than mutates — this
    helper produces the second record so the original ``pending`` plus
    the finalization collapse to a single effective state under
    :meth:`IntentStore.latest_by_turn`. All other fields copy through
    unchanged so the finalized view carries the same telemetry as the
    pending record.
    """
    return replace(record, outcome=outcome, timestamp=timestamp)


# Closed set of valid outcome states. Sprint 28 Phase 4 implements the
# provisional-write pattern: ``pending`` is the initial state at turn-end;
# ``success`` / ``correction`` finalize at the start of the next turn;
# ``drop`` / ``error`` are terminal states the Dispatcher writes directly
# when the turn cannot reach a normal end.
#
# GRV-010 C2a/C2d — ``governance_terminated`` is the terminal outcome the
# Dispatcher writes when a STRUCTURAL governed denial ends the turn via
# TerminalGovernanceHalt: a RED-sovereign / deny_hard / quarantined-.andon
# refusal (C2a), a mid-execution GovernanceError, or a tier-unavailable halt
# (C2d). It is distinct from ``error`` (a fault) — the halt is the governance
# layer working as designed, and the Flywheel must SEE these structural stops
# to learn from them. Without this entry the write is rejected and the stop is
# invisible to the loop (the C2d-2 telemetry gap this closes).
VALID_OUTCOMES: frozenset[str] = frozenset({
    "pending",
    "success",
    "drop",
    "error",
    "correction",
    "governance_terminated",
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
    # seam5-intent-stickiness-v1 — tool names OFFERED (the resolved per-turn
    # surface), distinct from tools_yielded (those actually called). The
    # carry-forward heuristic reads the previous turn's offered surface so a
    # tool stays available across a terse continuation reply. Same tuple/JSON
    # coercion as tools_yielded so the frozen record stays hashable.
    tools_offered: Tuple[str, ...] = field(default_factory=tuple)
    api_calls: int = 0
    duration_ms: float = 0.0
    final_response_chars: Optional[int] = None

    # Sprint 30 — count of EscalationRequests this turn produced.
    # Zero for the vast majority of turns. Sprint 28's TierRatchet
    # consumer reads this to detect tier-pressure patterns ("intent_class
    # X escalates 40% of the time → consider raising default tier for X").
    escalation_count: int = 0

    # Sprint 48 — T0 pattern-compiler evidence (GATE-A decision 3). Captured
    # on the FinalResponse write going forward; the compiler mines these from
    # history (no live re-execution). Auto-purged after the pattern_cache
    # ``within_days`` window via :meth:`IntentStore.purge_expired_content`;
    # legacy records carry None and are ignored by the compiler.
    #   response_content — the actual response text (for STATIC patterns),
    #     capped to keep the append-only store sane.
    #   tool_invocation  — JSON string ``{"tool": str, "args": {...}}`` for a
    #     single-tool turn (for EXECUTABLE patterns). Stored as a string, not a
    #     dict, so the record stays frozen-hashable.
    response_content: Optional[str] = None
    tool_invocation: Optional[str] = None


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
        data["tools_offered"] = list(data["tools_offered"])
        line = json.dumps(data, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return data

    def purge_expired_content(self, within_days: int) -> int:
        """Null ``response_content`` + ``tool_invocation`` on records older
        than ``within_days`` (Sprint 48 / GATE-A decision 3 retention policy).

        The compiler only needs this evidence inside the promotion window;
        beyond it the captured response text and tool args are purged for
        privacy/retention. Rewrites the JSONL in place only when something is
        actually purged. Returns the count of records cleared. Best-effort: a
        missing store is a no-op; a write failure logs and leaves the file
        untouched.
        """
        if within_days < 0 or not self._path.exists():
            return 0
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=within_days)
        ).isoformat()
        purged = 0
        with self._lock:
            try:
                raw_lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            out: List[str] = []
            for raw in raw_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    out.append(raw)
                    continue
                expired = data.get("timestamp", "") < cutoff
                has_content = (
                    data.get("response_content") is not None
                    or data.get("tool_invocation") is not None
                )
                if expired and has_content:
                    data["response_content"] = None
                    data["tool_invocation"] = None
                    purged += 1
                out.append(json.dumps(data, sort_keys=True, default=str))
            if purged:
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                try:
                    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                    tmp.replace(self._path)
                except OSError as exc:
                    logger.warning(
                        "[grove.intent_store] content purge write failed: %r", exc,
                    )
                    return 0
        return purged

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
                if isinstance(data.get("tools_offered"), list):
                    data["tools_offered"] = tuple(data["tools_offered"])
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

    def sweep_stale_pending(
        self,
        *,
        older_than_minutes: int = 60,
        now: Optional[datetime] = None,
    ) -> int:
        """Implicit Success Sweep — finalize stale ``pending`` records as success.

        Sprint 28 policy: if the operator closes their laptop or walks
        away, ~99% of the time the task is complete. Session abandonment
        is treated as success. A ``pending`` record older than
        ``older_than_minutes`` belongs to a session that has plausibly
        ended without an explicit finalization (process crashed, laptop
        closed, gateway restarted); finalizing it as success keeps the
        feed honest about turns that actually happened without leaving
        an indefinite ``pending`` overhang.

        The threshold defaults to 60 minutes — large enough that a
        legitimate in-flight turn (typically seconds) cannot be swept
        by a concurrently-initializing Dispatcher in the same process.

        Operates against ``latest_by_turn`` so records already
        finalized via Phase 4 or a previous sweep are not re-swept.

        Args:
            older_than_minutes: pending records with timestamp older
                than ``now - older_than_minutes`` are finalized.
            now: explicit "now" for tests. Defaults to ``datetime.now(timezone.utc)``.

        Returns:
            Count of records finalized this call.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff_iso = (now - timedelta(minutes=older_than_minutes)).isoformat()
        now_iso = now.isoformat()
        swept = 0
        for record in list(self.latest_by_turn()):
            if record.outcome != "pending":
                continue
            if record.timestamp >= cutoff_iso:
                continue
            self.append(finalize_record(
                record, outcome="success", timestamp=now_iso,
            ))
            swept += 1
        return swept

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
