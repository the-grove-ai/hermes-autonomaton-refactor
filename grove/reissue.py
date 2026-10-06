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
        # A work session's own re-issues: the goal whose next item is being
        # presented, or a request to leave the goal's session (a pause) so
        # the operator's message is answered outside it.
        "goal": action.get("goal"),
        "advance": bool(action.get("advance")),
        "leave_goal": bool(action.get("leave_goal")),
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


def absorb(session_id: str, text: str) -> None:
    """Keep a message the gateway absorbed while the next item was already on
    its way ("next", "ok"): no reply, no interrupt, no second card. Read by
    the Dispatcher onto the in-flight turn's own recognition record."""
    path = _dir() / ("absorbed-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if path.exists():
        try:
            kept = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            kept = []
    kept.append({"text": str(text)[:200], "at": datetime.now(timezone.utc).isoformat()})
    path.write_text(json.dumps(kept), encoding="utf-8")


def take_absorbed(session_id: Optional[str]) -> list:
    if not session_id:
        return []
    path = _dir() / ("absorbed-" + _path(str(session_id)).name)
    if not path.exists():
        return []
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        kept = []
    path.unlink()
    return kept if isinstance(kept, list) else []


def note_pause(session_id: str, *, notice: bool, message: str) -> None:
    """The turn that carries ``message`` leaves the goal's work session (a
    pause): that message is answered outside it. Bound to the message, never
    to "the next turn" — the next turn may be the item already on its way,
    and that one must finish inside the session. ``notice`` says whether the
    reply should open with the one-line pause notice."""
    path = _dir() / ("pause-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notice": bool(notice), "message": str(message)}),
                    encoding="utf-8")


def take_pause(session_id: Optional[str], message: Any) -> Optional[Dict[str, Any]]:
    """Consume the pause noted for exactly this message, or None. A turn
    carrying any other message leaves the note where it is."""
    if not session_id:
        return None
    path = _dir() / ("pause-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        pause = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        path.unlink()
        return None
    if not isinstance(pause, dict) or str(pause.get("message", "")).strip() != str(
            message or "").strip():
        return None
    path.unlink()
    return pause


def note_goal(goal_id: str, note: str) -> None:
    """Note one pending thing about a goal's work, for its next request (e.g.
    its backlog was just released, so the next request for the work runs the
    keg pass first). One note per goal; consumed once."""
    path = _dir() / ("goal-" + _path(str(goal_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"note": note}), encoding="utf-8")


def goal_note(goal_id: str, *, take: bool = False) -> Optional[str]:
    path = _dir() / ("goal-" + _path(str(goal_id)).name)
    if not path.exists():
        return None
    try:
        note = json.loads(path.read_text(encoding="utf-8")).get("note")
    except ValueError:
        note = None
    if take:
        path.unlink()
    return note


def note_reissued(session_id: str, request: str) -> None:
    """A request was just re-issued into a clean session. If that request
    still cannot run there, it must NOT be re-issued again: one hop, then say
    so. Read against the same request in that session's first turn."""
    path = _dir() / ("hop-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"request": str(request)}), encoding="utf-8")


def take_reissued(session_id: Optional[str], message: Any) -> bool:
    if not session_id:
        return False
    path = _dir() / ("hop-" + _path(str(session_id)).name)
    if not path.exists():
        return False
    try:
        hop = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        hop = {}
    path.unlink()
    return str(hop.get("request", "")).strip() == str(message or "").strip()


def note_turn(session_id: str, turn_uid: Optional[str], note: str,
              text: Optional[str] = None) -> None:
    """Note one fact about the running turn for the Dispatcher's review of
    its reply (e.g. the model declared it is asking the operator a question).
    Read only against the same turn."""
    path = _dir() / ("note-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"turn_uid": turn_uid, "note": note,
                                "text": (str(text)[:500] if text else None)}),
                    encoding="utf-8")


def turn_note_text(session_id: Optional[str], turn_uid: Optional[str]) -> Optional[str]:
    """The text kept with this turn's note (the question a model declared)."""
    if not session_id or not turn_uid:
        return None
    path = _dir() / ("note-" + _path(str(session_id)).name)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(record, dict) and record.get("turn_uid") == turn_uid:
        return record.get("text")
    return None


def turn_note(session_id: Optional[str], turn_uid: Optional[str]) -> Optional[str]:
    if not session_id or not turn_uid:
        return None
    path = _dir() / ("note-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if isinstance(record, dict) and record.get("turn_uid") == turn_uid:
        return record.get("note")
    return None


def offer_actions(session_id: str, actions: Mapping[str, Any]) -> None:
    """Offer the operator buttons on this turn's reply (a pending item's
    card): ``{"item_id", "buttons": [[action, label], ...]}``. Read once by
    the gateway as the turn ends. The buttons are a convenience over typing;
    what a press may do is decided when it arrives, never here."""
    path = _dir() / ("actions-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(actions), sort_keys=True), encoding="utf-8")


def take_actions(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Consume the reply actions offered for a session's turn, if any."""
    if not session_id:
        return None
    path = _dir() / ("actions-" + _path(str(session_id)).name)
    if not path.exists():
        return None
    try:
        actions = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        actions = None
    path.unlink()
    return actions if isinstance(actions, dict) and actions.get("buttons") else None


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
