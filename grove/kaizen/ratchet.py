"""Grove Kaizen — Tier Ratchet (Sprint 28 Phase 5 read-only).

Draft 1.4 Commitment 5.3: Kaizen's tier-management arm. The ratchet
promotes and demotes skills across the four Cognitive Router tiers
(Tier 0 Pattern Cache, Tier 1 Cheap Cognition, Tier 2 Premium Cognition,
Tier 3 Apex Cognition) based on observed usage.

Sprint 28 Phase 5 wires the ratchet to read the feed-first
:mod:`grove.intent_store` and surface per-tier usage analysis. The
ratchet READs and AGGREGATES; it does not yet APPLY tier moves — the
act-stage work waits for a future sprint with operator-facing UX for
ratchet decisions and a write path back into the routing config.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TierRatchet:
    """Promotes/demotes skills across the four Cognitive Router tiers.

    Sprint 28 Phase 5: the stub now reads. Construct with an explicit
    :class:`grove.intent_store.IntentStore` for tests; production
    callers get the module singleton via ``get_store()`` by passing
    ``None`` (the default).

    The Skill Flywheel's full ratchet semantics (apply tier
    promotion/demotion against the routing config) remains future
    work; this class now exposes the data layer those decisions will
    draw from.
    """

    def __init__(self, store: Optional["object"] = None) -> None:
        if store is None:
            from grove.intent_store import get_store
            store = get_store()
        self._store = store

    def ratchet(self) -> dict:
        """Return per-tier usage analysis from the intent store. READ-only.

        Aggregates intent records (post-Phase-4 collapse view) by
        ``tier_selected``. Records with no tier (vanilla install / no
        Cognitive Router) bucket under the key ``"unknown"``. Returns:

        ``{
            <tier_name>: {
                "intent_classes": [<sorted unique list>],
                "count": <int — total turns at this tier>,
                "avg_confidence": <float, rounded to 3 places>,
            },
            ...
        }``

        Empty dict when the store has no records. Never raises on
        missing data — Sprint 12's graceful-tier classification can
        produce records with ``confidence=0.0`` and
        ``tier_selected=None`` that bucket correctly under ``"unknown"``.
        """
        groups: dict[str, dict] = {}
        for record in self._store.latest_by_turn():
            tier = record.tier_selected or "unknown"
            if tier not in groups:
                groups[tier] = {
                    "intent_classes": set(),
                    "count": 0,
                    "_confidence_sum": 0.0,
                }
            g = groups[tier]
            g["intent_classes"].add(record.intent_class)
            g["count"] += 1
            g["_confidence_sum"] += float(record.confidence or 0.0)
        out: dict[str, dict] = {}
        for tier, g in groups.items():
            avg = g["_confidence_sum"] / g["count"] if g["count"] else 0.0
            out[tier] = {
                "intent_classes": sorted(g["intent_classes"]),
                "count": g["count"],
                "avg_confidence": round(avg, 3),
            }
        return out
