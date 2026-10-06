"""Re-issuing a request — the one-time action behind two remedies.

A remedy that says "retry one tier up" or "open a clean session and ask
again there" is carried out by re-issuing the operator's original request:
the same words, on a different tier or in a fresh session. Only the gateway
can do that (it owns the chat's session and the message loop), so a remedy
ARMS a re-issue here and the gateway carries it out after the current turn.

Armed state is one small file per chat session under ``$GROVE_HOME`` and is
consumed exactly once. It carries no authority of its own: a re-issue is armed
only by an accepted remedy, by a signed standing rule, or by the ladder rule
(one turn, one tier up — see ``grove.andon``), and each is on the ledger
before this is written.

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
        # The turn that armed it, and every attempt at this request so far
        # (turn, tier, why it did not complete): the escalation's own trace.
        "turn_uid": action.get("turn_uid"),
        "attempts": list(action.get("attempts") or []),
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


def arm_tier(session_id: str, tier: str, attempts: Optional[list] = None,
             andon_id: Optional[str] = None) -> None:
    """Pin the NEXT turn of a session to a tier, once. Written by the gateway
    as it re-issues a request one tier up; consumed by the Dispatcher when it
    routes that turn (:func:`take_pin`). ``attempts`` are the earlier tries at
    this request, carried so the turn that finally answers can show them."""
    path = _dir() / ("tier-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "tier": tier, "attempts": list(attempts or []), "andon_id": andon_id,
    }), encoding="utf-8")


def take_pin(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Consume the tier pin for a session's next turn: ``{"tier", "attempts",
    "andon_id"}``, or None. Exactly once."""
    if not session_id:
        return None
    path = _dir() / ("tier-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        pin = None
    path.unlink()
    if not isinstance(pin, dict) or pin.get("tier") not in TIER_LADDER:
        return None
    return {"tier": pin["tier"], "attempts": list(pin.get("attempts") or []),
            "andon_id": pin.get("andon_id")}


def take_tier(session_id: Optional[str]) -> Optional[str]:
    pin = take_pin(session_id)
    return pin["tier"] if pin else None


def armed(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The re-issue armed for a session, WITHOUT consuming it."""
    if not session_id:
        return None
    path = _path(str(session_id))
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def mark_stopped(session_id: str, turn_uid: Optional[str], andon_id: Optional[str]) -> None:
    """Note that a turn's attempt was stopped with nothing left to retry on
    (the top of the ladder). Read only against the same turn; a later turn
    never matches it."""
    path = _dir() / ("stop-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"turn_uid": turn_uid, "andon_id": andon_id}),
                    encoding="utf-8")


def stopped(session_id: Optional[str], turn_uid: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stop noted for exactly this turn, or None."""
    if not session_id or not turn_uid:
        return None
    path = _dir() / ("stop-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get("turn_uid") == turn_uid else None


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
