"""Question cards: a question of its own, sent after the reply with its own
buttons. A press is delivered as the operator's message, naming what the card
was about; and an answer that arrives while the next item is on its way waits
its turn instead of pausing the work."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.platforms import telegram as tg
from gateway.platforms.base import BasePlatformAdapter

CARD = {"proposal_id": "remedy:abc123def456",
        "text": "I noticed you say “ship it” to mean confirm.",
        "buttons": [["Yes", "alias yes #abc123def456"], ["Not now", "alias later #abc123def456"]]}


def _adapter(monkeypatch):
    monkeypatch.setattr(tg, "InlineKeyboardButton",
                        lambda label, callback_data: (label, callback_data))
    monkeypatch.setattr(tg, "InlineKeyboardMarkup", lambda rows: rows)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    a = SimpleNamespace(
        _bot=object(), name="telegram", delivered=[], sent=sent,
        _send_message_with_thread_fallback=send, _link_preview_kwargs=lambda: {},
        _metadata_thread_id=lambda metadata: None,
        _thread_kwargs_for_send=lambda *args, **kw: {})
    for name in ("offer_cards", "take_cards"):
        setattr(a, name, getattr(BasePlatformAdapter, name).__get__(a))
    for name in ("send_offered_cards", "_handle_callback_query"):
        setattr(a, name, getattr(tg.TelegramAdapter, name).__get__(a))
    return a


def test_a_card_is_its_own_message_with_buttons_that_say_what_they_mean(monkeypatch):
    a = _adapter(monkeypatch)
    a.offer_cards("77", [CARD])
    asyncio.run(a.send_offered_cards("77"))
    [message] = a.sent
    assert message["chat_id"] == 77 and message["text"] == CARD["text"]
    assert message["reply_markup"] == [[("Yes", "cd:alias yes #abc123def456"),
                                        ("Not now", "cd:alias later #abc123def456")]]
    asyncio.run(a.send_offered_cards("77"))                    # sent once
    assert len(a.sent) == 1


def test_a_platform_with_no_buttons_says_what_to_type(monkeypatch):
    said = []

    async def send(chat_id, content, metadata=None):
        said.append(content)

    a = SimpleNamespace(send=send)
    for name in ("offer_cards", "take_cards", "send_offered_cards"):
        setattr(a, name, getattr(BasePlatformAdapter, name).__get__(a))
    a.offer_cards("77", [CARD])
    asyncio.run(a.send_offered_cards("77"))
    assert said == [CARD["text"] + "\nReply “alias yes #abc123def456” or "
                    "“alias later #abc123def456”."]


def _press(data):
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


def test_a_press_is_the_operators_message_and_the_buttons_come_off(monkeypatch):
    a = _adapter(monkeypatch)
    a._is_callback_user_authorized = lambda *args, **kw: True
    a.build_source = lambda **kw: SimpleNamespace(**kw)

    async def handle(event):
        a.delivered.append(event)

    a.handle_message = handle
    update, answered, cleared = _press("cd:alias yes #abc123def456")
    asyncio.run(a._handle_callback_query(update, None))
    [event] = a.delivered
    assert event.text == "alias yes #abc123def456" and event.source.user_id == "9"
    assert cleared == [None]


def test_only_an_authorized_user_can_answer(monkeypatch):
    a = _adapter(monkeypatch)
    a._is_callback_user_authorized = lambda *args, **kw: False
    a.handle_message = None
    update, answered, cleared = _press("cd:alias yes #abc123def456")
    asyncio.run(a._handle_callback_query(update, None))
    assert answered == ["⛔ You are not authorized to answer this."] and a.delivered == []


def test_an_answer_while_the_next_item_is_on_its_way_waits_and_does_not_pause(
        monkeypatch, tmp_path):
    from gateway import run as gw
    from grove import decision_work as dw
    from grove import reissue

    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    cfg = SimpleNamespace(
        goal_id="message-triage",
        work_session=SimpleNamespace(enabled=True, confirm=("ok", "confirm"), start=(),
                                     learned=(("confirm", "ship it"),)),
        adaptation=SimpleNamespace(enabled=True, forget=("forget",), answer_words=("yes",)),
        keg=SimpleNamespace(request="tag the next message", requests=("next",),
                            match_threshold=0.8, verb_bonus=0.0))
    monkeypatch.setattr(dw, "config_for_goal", lambda goal, dock=None: cfg)
    queued = {}
    monkeypatch.setattr(gw, "merge_pending_message_event",
                        lambda pending, key, event: queued.setdefault(key, []).append(event.text))
    runner = SimpleNamespace(
        session_store=SimpleNamespace(
            get_or_create_session=lambda source: SimpleNamespace(session_id="sess")),
        _ws_advance={"chat": {"goal": "message-triage"}},
        _session_db=SimpleNamespace(get_meta=lambda key: "message-triage"))
    for name in ("_work_session_goal", "_work_session_busy_message"):
        setattr(runner, name, getattr(gw.GatewayRunner, name).__get__(runner))
    say = lambda text: runner._work_session_busy_message(
        SimpleNamespace(text=text, source=SimpleNamespace()), "chat",
        SimpleNamespace(_pending_messages={}))
    for text in ("alias yes #abc123def456", "alias later #abc123def456", "forget ship it"):
        assert say(text) is True
        assert reissue.take_pause("sess", text) is None, text       # no pause was noted
    assert queued == {"chat": ["alias yes #abc123def456", "alias later #abc123def456",
                               "forget ship it"]}
    # Anything else is still a change of subject.
    assert say("what's the weather") is True
    assert reissue.take_pause("sess", "what's the weather")["notice"] is True
