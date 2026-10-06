"""Re-issuing a request — the one-time action behind two remedies.

A remedy that says "retry one tier up" or "open a clean session and ask
again there" is carried out by re-issuing the operator's original request:
the same words, on a different tier or in a fresh session. Only the gateway
can do that (it owns the chat's session and the message loop), so a remedy
ARMS a re-issue here and the gateway carries it out after the current turn.

Armed state is one small file per chat session under ``$GROVE_HOME`` and is
consumed exactly once. It carries no authority of its own: a re-issue is armed
only by an accepted remedy or by a signed standing rule, and both are on the
ledger before this is written.

Pipeline stage: Execution.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def _dir() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / ".reissue"


def _path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:128]
    return _dir() / f"{safe}.json"


def arm(action: Mapping[str, Any], *, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Arm one re-issue for a chat session (default: the session whose turn is
    running). ``action`` may carry ``clean_session`` and ``tier``."""
    if session_id is None:
        from grove import turn_provenance
        session_id = (turn_provenance.current() or {}).get("session_id")
    if not session_id:
        raise ValueError("a re-issue needs the chat session it belongs to")
    record = {
        "session_id": str(session_id),
        "clean_session": bool(action.get("clean_session")),
        "tier": action.get("tier"),
        "request": action.get("request"),
        "andon_id": action.get("andon_id"),
        "authorized": action.get("authorized"),
        "armed_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _path(str(session_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return record


TIER_LADDER = ("T1", "T2", "T3")


def next_tier(tier: Optional[str]) -> Optional[str]:
    """One tier up from ``tier``, or None at the top of the ladder."""
    if tier not in TIER_LADDER:
        return None
    index = TIER_LADDER.index(tier)
    return TIER_LADDER[index + 1] if index + 1 < len(TIER_LADDER) else None


def arm_tier(session_id: str, tier: str) -> None:
    """Pin the NEXT turn of a session to a tier, once. Written by the gateway
    as it re-issues a request one tier up; consumed by the Dispatcher when it
    routes that turn (:func:`take_tier`)."""
    path = _dir() / ("tier-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tier": tier}), encoding="utf-8")


def take_tier(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    path = _dir() / ("tier-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        tier = json.loads(path.read_text(encoding="utf-8")).get("tier")
    except ValueError:
        tier = None
    path.unlink()
    return tier if tier in TIER_LADDER else None


def take(session_id: str) -> Optional[Dict[str, Any]]:
    """Consume the armed re-issue for a session, if any. Exactly once."""
    path = _path(str(session_id))
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        record = None
    path.unlink()
    return record if isinstance(record, dict) else None
