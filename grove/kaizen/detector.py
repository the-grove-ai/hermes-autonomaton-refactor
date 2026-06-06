"""Grove Kaizen — Intent Pattern Detector (Sprint 28 Phase 5 read-only).

Draft 1.4 Commitment 5.3: the DETECT stage of the six-stage Skill Flywheel
(OBSERVE → DETECT → PROPOSE → APPROVE → EXECUTE → REFINE).

Sprint 28 Phase 5 wires the detector to read the feed-first
:mod:`grove.intent_store` and surface recurring intent patterns within a
lookback window. The detector READs and AGGREGATES; it does not yet
PROPOSE skill candidates from the patterns it finds — that act-stage
work waits for a future sprint with operator-facing UX for the
proposals. The interface contract this sprint locks in is the
structured list-of-dicts that downstream stages will consume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


class IntentPatternDetector:
    """Scans recent intent records for repeated patterns.

    Sprint 28 Phase 5: the stub now reads. Construct with an explicit
    :class:`grove.intent_store.IntentStore` for tests; production
    callers get the module singleton via ``get_store()`` by passing
    ``None`` (the default).

    The Skill Flywheel's full DETECT-stage semantics (propose skill
    candidates from observed patterns) remains future work; this class
    now exposes the data layer those proposals will draw from.
    """

    def __init__(self, store: Optional["object"] = None) -> None:
        if store is None:
            from grove.intent_store import get_store
            store = get_store()
        self._store = store

    def detect(
        self, window_days: int = 14, threshold: int = 3,
    ) -> List[dict]:
        """Return recurring intent patterns from the store. READ-only.

        Aggregates intent records (post-Phase-4 collapse view: latest
        outcome per turn) by ``pattern_hash`` within the last
        ``window_days``. Patterns that recur at least ``threshold`` times
        surface as result entries. Each entry shape:

        ``{
            "pattern_hash": <sha256 hex>,
            "intent_class": <one of the Sprint 12 taxonomy>,
            "count": <int — total turns matching the pattern>,
            "session_count": <int — distinct session_ids>,
            "last_seen": <ISO 8601 UTC timestamp>,
        }``

        Sorted by ``count`` descending, then ``pattern_hash`` ascending
        for stable iteration. Empty list when no patterns meet the
        threshold or the store is empty — never raises on missing data.
        """
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat()
        groups: dict[str, dict] = {}
        for record in self._store.latest_by_turn():
            if record.timestamp < cutoff_iso:
                continue
            ph = record.pattern_hash
            if ph not in groups:
                groups[ph] = {
                    "pattern_hash": ph,
                    "intent_class": record.intent_class,
                    "count": 0,
                    "sessions": set(),
                    "last_seen": record.timestamp,
                }
            g = groups[ph]
            g["count"] += 1
            g["sessions"].add(record.session_id)
            if record.timestamp > g["last_seen"]:
                g["last_seen"] = record.timestamp
        results: List[dict] = []
        for g in groups.values():
            if g["count"] < threshold:
                continue
            results.append({
                "pattern_hash": g["pattern_hash"],
                "intent_class": g["intent_class"],
                "count": g["count"],
                "session_count": len(g["sessions"]),
                "last_seen": g["last_seen"],
            })
        results.sort(key=lambda r: (-r["count"], r["pattern_hash"]))
        return results

    # ── Sprint 63 — skill-synthesis candidate detection (PROPOSE stage) ──

    def detect_skill_candidates(
        self,
        *,
        n: int = 3,
        m: int = 2,
        window_days: int = 30,
        session_db: Optional["object"] = None,
    ) -> List[dict]:
        """Recurring multi-tool sequences eligible for skill synthesis. READ-only.

        Groups latest-by-turn records by their ``tools_yielded`` sequence
        within the last ``window_days``. A sequence qualifies when it:

          * recurs at least ``n`` times (default 3),
          * across at least ``m`` distinct sessions (default 2),
          * uses at least 2 distinct tools, and
          * carries NO ``correction`` outcome on any occurrence — a single
            correction marks the pattern unreliable and disqualifies it.

        For each qualifying sequence the full operator prompt is recovered from
        the session DB by stem-match (Sprint 63 Ruling 1: IntentRecord stores
        only a 100-char ``user_message_stem``; the full text lives in the
        session DB ``messages`` table, keyed by ``session_id``). The recovered
        prompts give synthesis the semantic context the tool sequence alone
        cannot supply. A sequence whose prompts cannot be recovered at all is
        dropped — no prompt, no synthesis (the missing-context Andon).

        ``session_db`` is any object exposing ``get_messages(session_id)``
        (production: the live ``hermes_state.SessionDB``). ``None`` lazily
        constructs the default-path SessionDB.

        Each entry shape::

            {
              "tool_sequence": (str, ...),
              "count": int,
              "session_count": int,
              "intent_class": str,
              "last_seen": <ISO 8601 UTC>,
              "evidence_turns": [turn_id, ...],
              "prompts": [full operator prompt, ...],
            }

        Sorted by ``count`` descending, then ``tool_sequence``. Empty list when
        nothing qualifies. Never raises on missing data.
        """
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat()
        groups: dict[tuple, dict] = {}
        for record in self._store.latest_by_turn():
            if record.timestamp < cutoff_iso:
                continue
            seq = tuple(record.tools_yielded or ())
            # A skill worth synthesizing composes at least two distinct tools;
            # single-tool or no-tool turns are out of scope (SPEC §2).
            if len(set(seq)) < 2:
                continue
            g = groups.get(seq)
            if g is None:
                g = groups[seq] = {
                    "tool_sequence": seq,
                    "count": 0,
                    "sessions": set(),
                    "intent_class": record.intent_class,
                    "last_seen": record.timestamp,
                    "has_correction": False,
                    "members": [],
                }
            if record.outcome == "correction":
                g["has_correction"] = True
            g["count"] += 1
            g["sessions"].add(record.session_id)
            if record.timestamp > g["last_seen"]:
                g["last_seen"] = record.timestamp
            g["members"].append(
                (record.session_id, record.turn_id, record.user_message_stem)
            )

        candidates: List[dict] = []
        db = session_db
        for g in groups.values():
            if g["has_correction"]:
                continue
            if g["count"] < n:
                continue
            if len(g["sessions"]) < m:
                continue
            if db is None:
                db = self._default_session_db()
            if db is None:
                # No session DB available — cannot recover prompts, so cannot
                # synthesize. Skip loudly rather than synthesize blind.
                logger.warning(
                    "[kaizen.detector] skill candidate %s skipped: no session "
                    "DB to recover operator prompts.", g["tool_sequence"],
                )
                continue
            prompts: List[str] = []
            evidence: List[str] = []
            seen_prompts: set = set()
            for session_id, turn_id, stem in g["members"]:
                prompt = self._recover_prompt(db, session_id, stem)
                if prompt and prompt not in seen_prompts:
                    seen_prompts.add(prompt)
                    prompts.append(prompt)
                evidence.append(turn_id)
            if not prompts:
                logger.warning(
                    "[kaizen.detector] skill candidate %s skipped: no operator "
                    "prompt recoverable from session DB (missing context).",
                    g["tool_sequence"],
                )
                continue
            candidates.append({
                "tool_sequence": g["tool_sequence"],
                "count": g["count"],
                "session_count": len(g["sessions"]),
                "intent_class": g["intent_class"],
                "last_seen": g["last_seen"],
                "evidence_turns": evidence,
                "prompts": prompts,
            })
        candidates.sort(key=lambda c: (-c["count"], c["tool_sequence"]))
        return candidates

    @staticmethod
    def _recover_prompt(
        session_db: "object", session_id: str, stem: str,
    ) -> Optional[str]:
        """Recover the full operator prompt for a turn by stem-match.

        Sprint 63 Ruling 1: IntentRecord carries only ``user_message_stem``
        (first 100 chars). The full message text lives in the session DB.
        Load the session's messages and return the first ``user`` message
        whose normalized stem equals ``stem``. Returns None when the session
        has no matching user message or the DB read fails — the caller treats
        None as "context unavailable".
        """
        from grove.intent_store import normalize_message_stem

        try:
            messages = session_db.get_messages(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[kaizen.detector] session DB read failed for %s: %r",
                session_id, exc,
            )
            return None
        for msg in messages or ():
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            if normalize_message_stem(content) == stem:
                return content
        return None

    @staticmethod
    def _default_session_db() -> Optional["object"]:
        """Lazily construct the default-path SessionDB; None on failure."""
        try:
            from hermes_state import SessionDB
            return SessionDB()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[kaizen.detector] could not open default session DB: %r", exc,
            )
            return None
