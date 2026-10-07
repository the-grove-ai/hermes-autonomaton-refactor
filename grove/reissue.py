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
             andon_id: Optional[str] = None, request: Optional[str] = None) -> None:
    """Pin the NEXT turn of a session to a tier, once. Written by the gateway
    as it re-issues a request one tier up; consumed by the Dispatcher at the
    start of that turn (:func:`take_pin`). ``attempts`` are the earlier tries
    at this request, carried so the turn that finally answers can show them.
    ``request`` is the request being re-issued: the pin is for that request
    and no other."""
    path = _dir() / ("tier-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "tier": tier, "attempts": list(attempts or []), "andon_id": andon_id,
        "request": (str(request) if request is not None else None),
    }), encoding="utf-8")


def take_pin(session_id: Optional[str], message: Any = None) -> Optional[Dict[str, Any]]:
    """Consume the tier pin for a session's next turn: ``{"tier", "attempts",
    "andon_id"}``, or None. Exactly once — and only for the request it was
    armed for. A pin whose request is not this turn's message is dropped, not
    kept: live, 2026-10-06, the operator answered a question while a retry
    was armed, and the pin was taken by the next ordinary turn, which ran one
    tier too high."""
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
    armed_for = pin.get("request")
    if (armed_for is not None and message is not None
            and str(armed_for).strip() != str(message).strip()):
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


def open_question(session_id: Optional[str]) -> Optional[str]:
    """The question a model last asked the operator in this session, while it
    is still open: nothing has been recorded since. None otherwise."""
    if not session_id:
        return None
    path = _dir() / ("note-" + _path(str(session_id)).name)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(record, dict) and record.get("note") == "asked" and record.get("text"):
        return str(record["text"])
    return None


def clear_turn_note(session_id: Optional[str]) -> None:
    """Drop a session's turn note: what it noted has been answered."""
    if session_id:
        (_dir() / ("note-" + _path(str(session_id)).name)).unlink(missing_ok=True)


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


def offer_card(session_id: str, card: Mapping[str, Any]) -> None:
    """Offer the operator a card of its own after this turn's reply: a
    question with its buttons, ``{"text", "buttons": [[label, message], ...]}``.
    Each button delivers its message as the operator's own, naming what the
    card was about, so what a press may do is decided when it arrives. Read
    once by the gateway as the turn ends; the work goes on and the card waits."""
    path = _dir() / ("cards-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    if path.exists():
        try:
            cards = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            cards = []
    cards.append(dict(card))
    path.write_text(json.dumps(cards, sort_keys=True), encoding="utf-8")


def take_cards(session_id: Optional[str]) -> list:
    """Consume the cards offered for a session's turn."""
    if not session_id:
        return []
    path = _dir() / ("cards-" + _path(str(session_id)).name)
    if not path.exists():
        return []
    try:
        cards = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        cards = []
    path.unlink()
    return [c for c in cards if isinstance(c, dict) and c.get("text")]


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


# ── held for a signature ──────────────────────────────────────────────
# When Kaizen proposes a change to standard work during a work session, the
# session pauses after the item in hand: the proposal is put in front of the
# operator as its own card, and the next item is not brought until they sign
# it, send it back, or say "later". The hold is a transient note, like every
# other in this module; the proposal and its disposition are the records.


def hold(session_id: str, info: Mapping[str, Any]) -> None:
    """Hold a session for the operator's ruling on a proposal:
    ``{"goal", "proposal_id", "what"}``."""
    path = _dir() / ("hold-" + _path(str(session_id)).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**dict(info), "session_id": str(session_id)},
                               sort_keys=True), encoding="utf-8")


def held(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The hold on a session, if any, without lifting it."""
    if not session_id:
        return None
    path = _dir() / ("hold-" + _path(str(session_id)).name)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("proposal_id") else None


def release_hold(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Lift a session's hold. Returns what it was held for, or None."""
    info = held(session_id)
    if session_id:
        (_dir() / ("hold-" + _path(str(session_id)).name)).unlink(missing_ok=True)
    return info


def holds() -> list:
    """Every hold in force, as its own note."""
    out = []
    directory = _dir()
    if directory.is_dir():
        for path in sorted(directory.glob("hold-*")):
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and info.get("proposal_id") and info.get("session_id"):
                out.append(info)
    return out


# The gateway registers how to bring a held session its next item (it owns the
# chat and its queue). Process-local: the portal and the chat surfaces run in
# the same gateway process. With none registered, a lifted hold simply waits
# for the operator's next request.
_waker = None


def set_waker(waker: Any) -> None:
    global _waker
    _waker = waker


def proposal_resolved(proposal_id: str) -> list:
    """A proposal was signed, sent back or withdrawn: lift every hold that
    waited on it and bring each of those sessions its next item. Returns the
    session ids released."""
    released = []
    for info in holds():
        if info["proposal_id"] != proposal_id:
            continue
        release_hold(info["session_id"])
        released.append(info["session_id"])
        if _waker is not None:
            try:
                _waker(info["session_id"], info)
            except Exception:  # noqa: BLE001 — the hold is lifted either way
                import logging
                logging.getLogger(__name__).exception(
                    "[reissue] could not wake session %s", info["session_id"])
    return released
