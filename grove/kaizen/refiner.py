"""Grove Kaizen — Usage Refiner (Sprint 28 Phase 5 read-only).

Draft 1.4 Commitment 5.3: the REFINE stage of the six-stage Skill Flywheel
(OBSERVE → DETECT → PROPOSE → APPROVE → EXECUTE → REFINE).

Sprint 28 Phase 5 wires the refiner to read the feed-first
:mod:`grove.intent_store` and surface tool-usage frequency. The refiner
READs and AGGREGATES; it does not yet propose content refinements to
existing skills — the act-stage REFINE work (review skills against
observed usage, propose missing-step / stale-command edits) waits for
a future sprint with skill-content-aware analysis.
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class UsageRefiner:
    """Aggregates tool-usage observations from the intent feed.

    Sprint 28 Phase 5: the stub now reads. Construct with an explicit
    :class:`grove.intent_store.IntentStore` for tests; production
    callers get the module singleton via ``get_store()`` by passing
    ``None`` (the default).

    The Skill Flywheel's full REFINE-stage semantics (propose content
    edits to existing skills against observed usage) remains future
    work; this class now exposes the data layer those proposals will
    draw from.
    """

    def __init__(self, store: Optional["object"] = None) -> None:
        if store is None:
            from grove.intent_store import get_store
            store = get_store()
        self._store = store

    def refine(self) -> List[dict]:
        """Return tool-usage frequency from the intent store. READ-only.

        Aggregates ``tools_yielded`` across intent records (post-Phase-4
        collapse view: latest outcome per turn). Each unique tool name
        surfaces with its frequency, distinct session count, and most
        recent observation timestamp:

        ``{
            "tool": <tool name>,
            "frequency": <int — total turns that yielded this tool>,
            "session_count": <int — distinct session_ids>,
            "last_used": <ISO 8601 UTC timestamp>,
        }``

        Sorted by ``frequency`` descending, then ``tool`` ascending for
        stable iteration. Empty list when no records have tools_yielded
        populated (text-only turns / unclassified turns).
        """
        groups: dict[str, dict] = {}
        for record in self._store.latest_by_turn():
            for tool in record.tools_yielded:
                if not isinstance(tool, str) or not tool:
                    continue
                if tool not in groups:
                    groups[tool] = {
                        "tool": tool,
                        "frequency": 0,
                        "sessions": set(),
                        "last_used": record.timestamp,
                    }
                g = groups[tool]
                g["frequency"] += 1
                g["sessions"].add(record.session_id)
                if record.timestamp > g["last_used"]:
                    g["last_used"] = record.timestamp
        results: List[dict] = []
        for g in groups.values():
            results.append({
                "tool": g["tool"],
                "frequency": g["frequency"],
                "session_count": len(g["sessions"]),
                "last_used": g["last_used"],
            })
        results.sort(key=lambda r: (-r["frequency"], r["tool"]))
        return results
