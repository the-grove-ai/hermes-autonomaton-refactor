"""Grove telemetry — structured event logging for sovereignty decisions.

Per Sprint 05 design D6: each sovereignty decision (promote / reject / revoke)
emits a structured ``sovereignty_decision`` event. v0.1 logs as JSON via the
standard ``logging`` module under the ``grove.telemetry`` logger. A future
sprint migrates the event store to SQL rows on the stages table.

The event schema is fixed in the design doc and treated as a public contract;
downstream tooling (the Kaizen recommender in Sprint 06b, dashboards later)
consumes these events directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from grove.skills import utc_now_iso

logger = logging.getLogger("grove.telemetry")


def log_sovereignty_decision(
    *,
    action: str,
    skill_name: str,
    skill_hash: str = "",
    scan_verdict: str = "unknown",
    operator: str = "unknown",
    source_path: Optional[str] = None,
    dest_path: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Emit a ``sovereignty_decision`` event and return the event dict.

    The return value lets callers chain (e.g. CLI renderers report what was
    logged) without re-constructing the dict.
    """
    event: dict[str, Any] = {
        "event_type": "sovereignty_decision",
        "action": action,
        "skill_name": skill_name,
        "skill_hash": skill_hash,
        "scan_verdict": scan_verdict,
        "operator": operator,
        "reason": reason,
        "timestamp": utc_now_iso(),
        "source_path": source_path,
        "dest_path": dest_path,
    }
    logger.info("sovereignty_decision %s", json.dumps(event, sort_keys=True))
    return event


def log_routing_decision(
    *,
    tier: str,
    reason: str,
    model: Optional[str],
    action: Optional[str] = None,
    zone: Optional[str] = None,
    confidence: Optional[float] = None,
    pattern_cache_hit: bool = False,
    intent_class: Optional[str] = None,
    pattern_hash: Optional[str] = None,
    register_class: Optional[str] = None,
    complexity_signal: Optional[str] = None,
) -> dict[str, Any]:
    """Emit a ``routing_decision`` event and return the event dict.

    One event per route() call in the live pipeline (Sprint 11 D9),
    enriched with the T-telemetry classification fields (Sprint 12 D10)
    when a classification is available. ``action`` and ``zone`` are null
    for v0.1 construction-time routing; the classification fields are
    null for a vanilla install or a failed classification.
    """
    event: dict[str, Any] = {
        "event_type": "routing_decision",
        "tier": tier,
        "reason": reason,
        "action": action,
        "zone": zone,
        "confidence": confidence,
        "pattern_cache_hit": pattern_cache_hit,
        "intent_class": intent_class,
        "pattern_hash": pattern_hash,
        "register_class": register_class,
        "complexity_signal": complexity_signal,
        "model": model,
        "timestamp": utc_now_iso(),
    }
    logger.info("routing_decision %s", json.dumps(event, sort_keys=True))
    return event


def log_ratchet_candidate(
    *,
    tier: str,
    model: Optional[str],
    action: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Emit a ``ratchet_candidate`` event and return the event dict.

    A routing decision that landed on a premium tier (T2/T3). v0.1 logs
    the raw signal only — no pattern matching, no downgrade. Kaizen's
    Ratchet (v0.2 functional) consumes these to propose tier downgrades.
    """
    event: dict[str, Any] = {
        "event_type": "ratchet_candidate",
        "tier": tier,
        "model": model,
        "action": action,
        "reason": reason,
        "timestamp": utc_now_iso(),
    }
    logger.info("ratchet_candidate %s", json.dumps(event, sort_keys=True))
    return event


def log_retrieval(
    *,
    sources: list[str],
    content_types: list[str],
    scores: list[float],
) -> dict[str, Any]:
    """Emit a ``retrieval`` event and return the event dict.

    One event per turn whose cellar retrieval produced a
    ``<cellar_context>`` block (Sprint 13 D8). It records *what* was
    retrieved — source paths, content types, relevance scores — and
    never the retrieved content itself: the cellar's contents stay local
    and out of the telemetry log.
    """
    event: dict[str, Any] = {
        "event_type": "retrieval",
        "result_count": len(sources),
        "sources": list(sources),
        "content_types": list(content_types),
        "scores": list(scores),
        "timestamp": utc_now_iso(),
    }
    logger.info("retrieval %s", json.dumps(event, sort_keys=True))
    return event
