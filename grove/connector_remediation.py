"""learning-loop-bridge-v1 (Strike 2) — connector remediation recording.

The dark path this closes (GATE-A Q7 / DARK Q7, connector half): Strike 1 made
the connector-failure Cancel verbose, but a successful ``Retry`` left no
record — the system saw the block and never saw the fix. This writes a
``correction`` IntentRecord at the verified-reconnect moment so the system
records both the block and the operator's remediation.

Correlation mechanism (the GATE-A A4 correction): the SPEC originally keyed
this off the ``pending_andon`` marker, but that marker is written only by the
YELLOW Sovereign-Prompt path and is keyed by ``session_id`` — it never names a
connector. The correct anchor is the connect-breaker itself
(``tools.mcp_tool.get_connect_failures``): the breaker is keyed by connector
name and IS the prior-failure state. The caller in ``run_agent`` captures
whether the breaker held a failure for the connector *before* the retry
cleared it, and only invokes this recorder on a verified reconnect that
followed a real prior failure — so there is no false positive on a normal
first-time connect and none on a failed re-connect.

The record is synthetic: the connector disposition short-circuits the turn
before classification and dispatch, so there is no live ``ClassificationResult``
to read. The classification fields are sentinel-filled the same way
``Dispatcher._write_intent_record`` fills an unclassified turn, with a
self-describing ``intent_class`` and ``confidence=1.0`` (a verified reconnect
is deterministic). Fail-loud: a write failure propagates to the caller, which
surfaces it with diagnostic context — telemetry that cannot record is a loud
failure, not a swallowed one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = ["record_connector_remediation"]

# Sentinels mirror the unclassified-turn fill in Dispatcher._write_intent_record
# so downstream consumers can identify these records and TierRatchet ignores
# them (their own intent_class, low sample, outcome="correction").
_PATTERN_HASH = "connector_remediation"
_INTENT_CLASS = "connector_remediation"
_REGISTER_CLASS = "unknown"
_COMPLEXITY_SIGNAL = "unknown"


def record_connector_remediation(
    *,
    session_id: str,
    turn_id: str,
    connector_name: str,
    store: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write a ``correction`` IntentRecord for a verified connector reconnect.

    Args:
        session_id: the agent's session id.
        turn_id: the current turn id (the connector turn short-circuits
            dispatch, so callers pass the dispatcher's ``_current_turn_id``
            or fall back to ``session_id`` — the honest "not a classified
            turn" marker).
        connector_name: the reconnected MCP server (e.g. ``"notion"``).
        store: the IntentStore to append to. Defaults to the process
            singleton; tests pass a tmp-path store.
        now: injectable UTC clock for testing.

    Returns the persisted record dict. Raises on write failure (fail loud).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if store is None:
        from grove.intent_store import get_store
        store = get_store()

    from grove.intent_store import IntentRecord, normalize_message_stem

    record = IntentRecord(
        timestamp=now.isoformat(),
        session_id=session_id,
        turn_id=turn_id,
        user_message_stem=normalize_message_stem(
            f"[connector remediation] retry {connector_name}"
        ),
        pattern_hash=_PATTERN_HASH,
        intent_class=_INTENT_CLASS,
        register_class=_REGISTER_CLASS,
        complexity_signal=_COMPLEXITY_SIGNAL,
        confidence=1.0,
        outcome="correction",
    )
    return store.append(record)
