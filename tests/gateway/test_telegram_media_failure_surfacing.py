"""
Tests for loud media-failure surfacing in gateway/platforms/telegram.py.

Covers the telegram-media-failure-surfacing-v1 contract:
  - the shared notice helper's category -> copy mapping (incl. the "too big"
    copy refinement)
  - caption preservation (the notice APPENDS, never clobbers)
  - every media catch site routing failures through the helper
  - the oversize pre-check skipping the download and notifying
  - video_note / animation surfacing an unsupported-type notice
  - the sub-cap success path remaining untouched (no notice injected)

Note: python-telegram-bot may not be installed in the test environment.
We mock the telegram module at import time to avoid collection errors.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType


# ---------------------------------------------------------------------------
# Mock the telegram package if it's not installed
# ---------------------------------------------------------------------------

def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from gateway.platforms.telegram import (  # noqa: E402
    TelegramAdapter,
    _TELEGRAM_GETFILE_MAX_BYTES,
)

# The classification tests need the real exception classes.
_REAL_TELEGRAM = "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__")


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------

def _make_file_obj(data: bytes = b"payload"):
    f = AsyncMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    f.file_path = "audio/file.mp3"
    return f


def _make_media(file_size=1024, file_obj=None, raises=None):
    """A generic media object (audio/voice/video/photo) with get_file."""
    m = MagicMock()
    m.file_size = file_size
    if raises is not None:
        m.get_file = AsyncMock(side_effect=raises)
    else:
        m.get_file = AsyncMock(return_value=file_obj or _make_file_obj())
    return m


def _make_message(
    *, photo=None, voice=None, audio=None, video=None,
    video_note=None, animation=None, document=None, caption=None,
):
    msg = MagicMock()
    msg.message_id = 42
    msg.text = caption or ""
    msg.caption = caption
    msg.date = None
    msg.photo = photo
    msg.video = video
    msg.audio = audio
    msg.voice = voice
    msg.sticker = None
    msg.video_note = video_note
    msg.animation = animation
    msg.document = document
    msg.media_group_id = None
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Test User"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Test User"
    msg.message_thread_id = None
    return msg


def _make_update(msg):
    update = MagicMock()
    update.message = msg
    return update


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.handle_message = AsyncMock()
    return a


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point every media cache at tmp_path so tests don't touch ~/.grove."""
    for attr in ("AUDIO_CACHE_DIR", "VIDEO_CACHE_DIR", "IMAGE_CACHE_DIR", "DOCUMENT_CACHE_DIR"):
        monkeypatch.setattr(f"gateway.platforms.base.{attr}", tmp_path / attr.lower())


# ---------------------------------------------------------------------------
# 1. Helper: category -> copy mapping (pure, no telegram needed)
# ---------------------------------------------------------------------------

class TestNoticeCopyMapping:
    def test_rejected_generic(self):
        text = TelegramAdapter._media_failure_notice("audio", "rejected", "chat not found")
        assert "Telegram rejected the file" in text
        assert "20 MB" not in text  # generic copy does not name the cap

    def test_rejected_too_big_refines_copy(self):
        text = TelegramAdapter._media_failure_notice(
            "audio", "rejected", "Bad Request: File is too big"
        )
        assert "20 MB" in text and "getFile" in text

    def test_too_big_matching_is_case_insensitive(self):
        text = TelegramAdapter._media_failure_notice("audio", "rejected", "FILE IS TOO BIG")
        assert "20 MB" in text

    def test_transient(self):
        text = TelegramAdapter._media_failure_notice("voice", "transient", None)
        assert "network" in text.lower() and "resend" in text.lower()

    def test_cache(self):
        text = TelegramAdapter._media_failure_notice("video", "cache", None)
        assert "could not be saved" in text.lower()

    def test_oversize_names_cap_and_size(self):
        text = TelegramAdapter._media_failure_notice("audio", "oversize", 26214400)
        assert "25.0 MB" in text and "20 MB" in text

    def test_oversize_unverified_size(self):
        text = TelegramAdapter._media_failure_notice("document", "oversize", None)
        assert "unverified size" in text

    def test_unsupported(self):
        text = TelegramAdapter._media_failure_notice("video_note", "unsupported", None)
        assert "video note" in text and "can't process" in text.lower()

    def test_labels_are_type_specific(self):
        assert "voice message" in TelegramAdapter._media_failure_notice("voice", "cache", None)
        assert "animation (GIF)" in TelegramAdapter._media_failure_notice("animation", "unsupported", None)


# ---------------------------------------------------------------------------
# 1b. Classification is by exception TYPE (BadRequest before NetworkError)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _REAL_TELEGRAM, reason="requires real python-telegram-bot")
class TestClassification:
    def test_badrequest_is_rejected(self):
        from telegram.error import BadRequest
        assert TelegramAdapter._classify_media_failure(BadRequest("File is too big")) == "rejected"

    def test_timedout_is_transient(self):
        from telegram.error import TimedOut
        assert TelegramAdapter._classify_media_failure(TimedOut()) == "transient"

    def test_networkerror_is_transient(self):
        from telegram.error import NetworkError
        assert TelegramAdapter._classify_media_failure(NetworkError("boom")) == "transient"

    def test_oserror_is_cache(self):
        assert TelegramAdapter._classify_media_failure(OSError("disk full")) == "cache"

    def test_valueerror_is_cache(self):
        assert TelegramAdapter._classify_media_failure(ValueError("bad bytes")) == "cache"


# ---------------------------------------------------------------------------
# 2. Caption preservation — the notice APPENDS, never clobbers
# ---------------------------------------------------------------------------

class TestCaptionPreservation:
    def test_appends_to_existing_caption(self, adapter):
        event = MessageEvent(text="Please transcribe this")
        adapter._surface_media_failure(event, "audio", category="cache")
        assert event.text.startswith("Please transcribe this")
        assert "could not be saved" in event.text.lower()

    def test_uses_notice_when_no_caption(self, adapter):
        event = MessageEvent(text="")
        adapter._surface_media_failure(event, "audio", category="cache")
        assert event.text.startswith("[")

    def test_exc_derives_category_and_detail(self, adapter):
        event = MessageEvent(text="hi")
        adapter._surface_media_failure(event, "audio", exc=ValueError("nope"))
        assert event.text.startswith("hi")
        assert "could not be saved" in event.text.lower()


# ---------------------------------------------------------------------------
# 3. Every catch site routes exceptions through the helper
# ---------------------------------------------------------------------------

class TestCatchSitesReachHelper:
    @pytest.mark.asyncio
    async def test_audio_too_big_surfaces_cap_notice(self, adapter):
        """The reported bug: oversized audio getFile -> loud cap notice, dispatched."""
        exc = (
            __import__("telegram").error.BadRequest("File is too big")
            if _REAL_TELEGRAM else RuntimeError("File is too big")
        )
        audio = _make_media(file_size=1024, raises=exc)  # sub-cap size; failure at get_file
        msg = _make_message(audio=audio, caption="what does this say?")
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "what does this say?" in event.text
        if _REAL_TELEGRAM:
            assert "20 MB" in event.text and "getFile" in event.text
        assert event.media_urls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["photo", "voice", "audio", "video"])
    async def test_download_exception_reaches_helper(self, adapter, kind):
        media = _make_media(file_size=1024, raises=RuntimeError("api down"))
        kwargs = {kind: [media] if kind == "photo" else media}
        msg = _make_message(**kwargs)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.text.strip().startswith("[")  # a notice was injected
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_document_download_exception_reaches_helper(self, adapter):
        doc = MagicMock()
        doc.file_name = "report.pdf"
        doc.mime_type = "application/pdf"
        doc.file_size = 1024
        doc.get_file = AsyncMock(side_effect=RuntimeError("api down"))
        msg = _make_message(document=doc)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert "[" in event.text and "document" in event.text


# ---------------------------------------------------------------------------
# 4. Oversize pre-check skips the download and notifies
# ---------------------------------------------------------------------------

class TestOversizePreCheck:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["photo", "voice", "audio", "video"])
    async def test_precheck_skips_download(self, adapter, kind):
        over = _TELEGRAM_GETFILE_MAX_BYTES + 1
        media = _make_media(file_size=over)
        kwargs = {kind: [media] if kind == "photo" else media}
        msg = _make_message(**kwargs)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        media.get_file.assert_not_called()  # download skipped
        event = adapter.handle_message.call_args[0][0]
        assert "download limit" in event.text
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_document_precheck_skips_download(self, adapter):
        doc = MagicMock()
        doc.file_name = "huge.pdf"
        doc.mime_type = "application/pdf"
        doc.file_size = _TELEGRAM_GETFILE_MAX_BYTES + 1
        doc.get_file = AsyncMock()
        msg = _make_message(document=doc)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        doc.get_file.assert_not_called()
        event = adapter.handle_message.call_args[0][0]
        assert "download limit" in event.text


# ---------------------------------------------------------------------------
# 5. Unhandled types (video_note, animation) surface an unsupported notice
# ---------------------------------------------------------------------------

class TestUnhandledTypes:
    @pytest.mark.asyncio
    async def test_video_note_unsupported_notice(self, adapter):
        vn = MagicMock()
        msg = _make_message(video_note=vn)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "video note" in event.text and "can't process" in event.text.lower()
        assert event.media_urls == []

    @pytest.mark.asyncio
    async def test_animation_unsupported_notice(self, adapter):
        anim = MagicMock()
        # A real animation update also sets .document; animation must win.
        doc = MagicMock()
        doc.file_name = "funny.gif"
        msg = _make_message(animation=anim, document=doc)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "animation (GIF)" in event.text
        assert event.media_urls == []


# ---------------------------------------------------------------------------
# 6. Sub-cap success path is untouched (no notice injected)
# ---------------------------------------------------------------------------

class TestSubCapSuccessUntouched:
    @pytest.mark.asyncio
    async def test_audio_subcap_caches_and_no_notice(self, adapter):
        file_obj = _make_file_obj(b"ID3 fake mp3 bytes")
        audio = _make_media(file_size=512 * 1024, file_obj=file_obj)
        msg = _make_message(audio=audio)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        audio.get_file.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert len(event.media_urls) == 1
        assert os.path.exists(event.media_urls[0])
        assert event.media_types == ["audio/mp3"]
        assert "[" not in (event.text or "")  # no failure notice injected

    @pytest.mark.asyncio
    async def test_voice_subcap_caches_and_no_notice(self, adapter):
        file_obj = _make_file_obj(b"OggS fake voice")
        voice = _make_media(file_size=64 * 1024, file_obj=file_obj)
        msg = _make_message(voice=voice)
        await adapter._handle_media_message(_make_update(msg), MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert len(event.media_urls) == 1
        assert event.media_types == ["audio/ogg"]
        assert "[" not in (event.text or "")
