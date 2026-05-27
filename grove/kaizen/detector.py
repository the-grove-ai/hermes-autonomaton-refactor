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
