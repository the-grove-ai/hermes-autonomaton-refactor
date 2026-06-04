"""Sprint 58 — Telegram Kaizen inline keyboard verification (T28-T31).

T28: the kz: / kp: keyboards are constructed with the right emoji labels and
     ``<prefix>:<choice>:<short_id>`` callback data, under Telegram's 64-byte cap.
T29: a kz: button tap resolves the pending Event with the tapped disposition.
T30: a stale tap (unknown id) answers "Action expired" and resolves nothing.
T31: no tap within the timeout auto-resolves the handler with "deny".

Exercises the real registries, the real _handle_callback_query routing, and
the real run.py handler builder — only the Telegram transport + event loop are
faked.
"""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

import gateway.platforms.telegram as tgmod
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig
from gateway.run import _build_kaizen_prompt_handler


# Real, inspectable button/markup classes so we can assert labels + data.
class _FakeButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class _FakeMarkup:
    def __init__(self, rows):
        self.inline_keyboard = rows


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _patch_buttons():
    return patch.multiple(
        tgmod, InlineKeyboardButton=_FakeButton, InlineKeyboardMarkup=_FakeMarkup,
    )


def _flatten(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ── T28: keyboard construction ────────────────────────────────────────


class TestT28KeyboardConstruction:
    @pytest.mark.asyncio
    async def test_kaizen_prompt_buttons_and_callback_data(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
        kid, _entry = adapter.register_kaizen_pending("sess-key")

        with _patch_buttons():
            result = await adapter.send_kaizen_prompt(
                chat_id="12345", kaizen_id=kid,
                description="run a command on your machine",
            )

        assert result.success is True
        markup = adapter._bot.send_message.call_args[1]["reply_markup"]
        buttons = _flatten(markup)
        labels = [b.text for b in buttons]
        assert labels == [
            "🟢 Always allow", "🟡 Allow session", "🟠 Allow once", "🔴 Don't allow",
        ]
        data = {b.callback_data for b in buttons}
        assert data == {
            f"kz:always:{kid}", f"kz:session:{kid}",
            f"kz:once:{kid}", f"kz:deny:{kid}",
        }
        # 64-byte callback_data cap (Telegram hard limit).
        for b in buttons:
            assert len(b.callback_data.encode("utf-8")) <= 64
        # The prompt text carries the plain-language description, no jargon.
        text = adapter._bot.send_message.call_args[1]["text"]
        assert "run a command on your machine" in text
        for jargon in ("Andon", "zone", "Dispatcher", "sovereignty"):
            assert jargon not in text

    @pytest.mark.asyncio
    async def test_promotion_buttons_and_callback_data(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=8))
        pid, _entry = adapter.register_kaizen_promo("sess", "google-workspace", "sha256:abc")

        with _patch_buttons():
            result = await adapter.send_kaizen_promotion(
                chat_id="12345", promo_id=pid, skill_name="google-workspace",
            )

        assert result.success is True
        markup = adapter._bot.send_message.call_args[1]["reply_markup"]
        buttons = _flatten(markup)
        assert [b.text for b in buttons] == ["🟢 Promote", "🟡 Not yet", "🔴 Never"]
        assert {b.callback_data for b in buttons} == {
            f"kp:promote:{pid}", f"kp:not_yet:{pid}", f"kp:never:{pid}",
        }
        for b in buttons:
            assert len(b.callback_data.encode("utf-8")) <= 64


# ── T29: callback resolution returns the tapped disposition ───────────


def _callback(adapter, data: str):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.from_user = MagicMock()
    query.from_user.first_name = "Jim"
    query.from_user.id = "12345"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


class TestT29CallbackResolution:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("disposition", ["once", "session", "always", "deny"])
    async def test_kz_tap_resolves_disposition(self, disposition):
        adapter = _make_adapter()
        kid, entry = adapter.register_kaizen_pending("sess")
        update, query = _callback(adapter, f"kz:{disposition}:{kid}")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        # Event fired, disposition stamped, id popped (ghost-tap safe next time).
        assert entry["event"].is_set()
        assert entry["disposition"] == disposition
        assert kid not in adapter._kaizen_state
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()


# ── T30: stale tap → "Action expired" ─────────────────────────────────


class TestT30StaleCallback:
    @pytest.mark.asyncio
    async def test_kz_stale_tap_action_expired(self):
        adapter = _make_adapter()
        update, query = _callback(adapter, "kz:once:9999")  # never registered

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        query.answer.assert_called_once()
        assert "expired" in query.answer.call_args[1]["text"].lower()
        query.edit_message_reply_markup.assert_called_once()  # buttons removed

    @pytest.mark.asyncio
    async def test_kp_stale_tap_action_expired(self):
        adapter = _make_adapter()
        update, query = _callback(adapter, "kp:promote:9999")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        query.answer.assert_called_once()
        assert "expired" in query.answer.call_args[1]["text"].lower()


# ── T31: timeout auto-resolves with deny ──────────────────────────────


class TestT31Timeout:
    def test_kaizen_handler_times_out_to_deny(self):
        adapter = _make_adapter()
        edited = {}

        async def _send(chat_id, kaizen_id, description, metadata=None):
            with adapter._kaizen_lock:
                e = adapter._kaizen_state.get(kaizen_id)
                if e:
                    e["message_id"] = 555
                    e["chat_id"] = chat_id
            return SimpleNamespace(success=True, message_id="555")

        async def _edit(chat_id, message_id, label):
            edited["label"] = label

        adapter.send_kaizen_prompt = _send
        adapter.edit_kaizen_resolved = _edit

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            handler = _build_kaizen_prompt_handler(
                adapter=adapter, chat_id=7, loop=loop, metadata=None,
                session_key="s", timeout=1,
            )
            halt = SimpleNamespace(
                triggering_index=0,
                intents=[SimpleNamespace(tool_name="terminal", arguments={"command": "echo hi"})],
            )
            t0 = time.time()
            disposition = handler(halt)
            elapsed = time.time() - t0
            assert disposition == "deny"
            assert 1.0 <= elapsed < 5.0  # waited the timeout, then denied
            time.sleep(0.3)  # let the scheduled timeout-edit run
            assert "Timed out" in edited.get("label", "")
            # registry cleaned up
            assert adapter._kaizen_state == {}
        finally:
            loop.call_soon_threadsafe(loop.stop)
