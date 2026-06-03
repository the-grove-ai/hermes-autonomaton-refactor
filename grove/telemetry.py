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


def log_pattern_cache_event(
    *,
    event_type: str,
    pattern_id: Optional[str] = None,
    t0_key: Optional[str] = None,
    intent_class: Optional[str] = None,
    cacheable_type: Optional[str] = None,
    response_time_ms: Optional[float] = None,
    correction_turn_id: Optional[str] = None,
) -> dict[str, Any]:
    """Emit a T0 pattern-cache event and return the event dict (Sprint 49 D5).

    ``event_type`` is one of:

    * ``t0_cache_hit`` — a query resolved from the compiled cache with no
      model call. Carries ``pattern_id`` / ``t0_key`` / ``intent_class`` /
      ``cacheable_type`` / ``response_time_ms``.
    * ``t0_cache_miss`` — a query did not match any active pattern; logged
      with ``t0_key`` only so future pattern identification can mine the
      misses without storing the message.
    * ``pattern_drift_detected`` — a served pattern was corrected on the next
      turn; carries ``pattern_id`` and the ``correction_turn_id``.

    Fields not relevant to the given event type are left ``None``. Cumulative
    savings is NOT computed here — the ``flywheel patterns stats`` command
    derives it from the served hit_count so the hot path logs only the raw
    signal (the Ratchet pattern: surface the event, derive downstream)."""
    event: dict[str, Any] = {
        "event_type": event_type,
        "pattern_id": pattern_id,
        "t0_key": t0_key,
        "intent_class": intent_class,
        "cacheable_type": cacheable_type,
        "response_time_ms": response_time_ms,
        "correction_turn_id": correction_turn_id,
        "timestamp": utc_now_iso(),
    }
    logger.info("%s %s", event_type, json.dumps(event, sort_keys=True))
    return event
