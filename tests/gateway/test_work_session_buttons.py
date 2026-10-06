"""Work-session buttons on Telegram: attached to the item card, carrying the
item's id, cleared once the item is no longer the one waiting, and delivered
as a message that names the item."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.platforms import telegram as tg
from gateway.platforms.base import BasePlatformAdapter


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, reply_markup))


def _adapter(monkeypatch):
    monkeypatch.setattr(tg, "InlineKeyboardButton",
                        lambda label, callback_data: (label, callback_data))
    monkeypatch.setattr(tg, "InlineKeyboardMarkup", lambda rows: rows)
    a = SimpleNamespace(_bot=_Bot(), _ws_cards={}, name="telegram", delivered=[])
    for name in ("offer_reply_actions", "take_reply_actions"):
        setattr(a, name, getattr(BasePlatformAdapter, name).__get__(a))
    for name in ("attach_reply_actions", "_ws_clear_buttons", "_handle_callback_query"):
        setattr(a, name, getattr(tg.TelegramAdapter, name).__get__(a))
    return a


ACTIONS = {"item_id": "m01", "buttons": [["confirm", "Confirm"], ["revise", "Revise"]]}
SENT = SimpleNamespace(success=True, message_id="501")


def test_buttons_land_on_the_card_and_name_its_item(monkeypatch):
    a = _adapter(monkeypatch)
    a.offer_reply_actions("77", ACTIONS)
    asyncio.run(a.attach_reply_actions("77", SENT))
    assert a._bot.edits == [(77, 501, [[("Confirm", "ws:c:m01"), ("Revise", "ws:r:m01")]])]
    assert a._ws_cards == {"m01": (77, 501)}
    # Nothing offered for the next reply: nothing attached.
    asyncio.run(a.attach_reply_actions("77", SimpleNamespace(success=True, message_id="502")))
    assert len(a._bot.edits) == 1


def test_the_next_card_takes_the_buttons_off_the_last_one(monkeypatch):
    a = _adapter(monkeypatch)
    a.offer_reply_actions("77", ACTIONS)
    asyncio.run(a.attach_reply_actions("77", SENT))
    a.offer_reply_actions("77", {**ACTIONS, "item_id": "m02"})
    asyncio.run(a.attach_reply_actions("77", SimpleNamespace(success=True, message_id="502")))
    assert a._bot.edits[1] == (77, 501, None)                 # m01's card: buttons removed
    assert a._bot.edits[2][:2] == (77, 502) and a._ws_cards == {"m02": (77, 502)}


def test_an_item_id_too_long_for_a_button_gets_no_button(monkeypatch):
    a = _adapter(monkeypatch)
    a.offer_reply_actions("77", {**ACTIONS, "item_id": "x" * 80})
    asyncio.run(a.attach_reply_actions("77", SENT))
    assert a._bot.edits == [] and a._ws_cards == {}


def _press(data, authorized=True):
    answered, cleared = [], []

    async def answer(text=None):
        answered.append(text)

    async def clear(reply_markup=None):
        cleared.append(reply_markup)

    chat = SimpleNamespace(type="private", title=None, full_name="Pat")
    query = SimpleNamespace(
        data=data, answer=answer, edit_message_reply_markup=clear,
        message=SimpleNamespace(chat_id=77, chat=chat, message_thread_id=None),
        from_user=SimpleNamespace(id=9, first_name="Pat", full_name="Pat Example"))
    return SimpleNamespace(callback_query=query), answered, cleared


@pytest.mark.parametrize("data,expected", [("ws:c:m01", "confirm #m01"),
                                           ("ws:r:m01", "revise #m01")])
def test_a_press_is_delivered_as_the_operators_message_naming_the_item(
        monkeypatch, data, expected):
    a = _adapter(monkeypatch)
    a._ws_cards["m01"] = (77, 501)
    a._is_callback_user_authorized = lambda *args, **kw: True
    a.build_source = lambda **kw: SimpleNamespace(**kw)

    async def handle(event):
        a.delivered.append(event)

    a.handle_message = handle
    update, answered, cleared = _press(data)
    asyncio.run(a._handle_callback_query(update, None))
    [event] = a.delivered
    assert event.text == expected and event.source.user_id == "9"
    assert event.source.chat_id == "77" and event.source.chat_type == "dm"
    # Confirm takes its card's buttons away at once; Revise leaves them until decided.
    assert (cleared == [None]) is (data == "ws:c:m01")
    assert ("m01" in a._ws_cards) is (data == "ws:r:m01")


def test_only_an_authorized_user_can_press(monkeypatch):
    a = _adapter(monkeypatch)
    a._is_callback_user_authorized = lambda *args, **kw: False
    a.handle_message = None                                   # must never be reached
    update, answered, cleared = _press("ws:c:m01")
    asyncio.run(a._handle_callback_query(update, None))
    assert answered == ["⛔ You are not authorized to decide this."] and a.delivered == []
