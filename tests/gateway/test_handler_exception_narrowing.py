"""Andon discipline for the gateway agent-turn exception handler.

Background
----------
``GatewayRunner._handle_message_with_agent`` wraps the entire agent turn
in a try/except. Prior to the narrowing hotfix, the handler caught a bare
``Exception`` and returned a soft "Sorry, I encountered an error (...)"
string to the user. That swallowed architectural integrity violations
(ImportError after a consumer-miss rename, AttributeError from a wiring
drift, RuntimeError from a fail-loud assertion) — the operator saw a
friendly chat reply on Telegram while the substrate was structurally
broken. Pattern v1.3 Commitment 5 (Digital Jidoka) is categorical: Andon
gates exist to fire.

Two paths exist now:

  1. **Andon path** — an Andon-class exception (ImportError,
     ModuleNotFoundError, AttributeError, TypeError, NameError,
     RuntimeError) propagates out of the handler. The typing indicator
     is still stopped on the way through, but the exception is re-raised
     so the gateway boot/run surface can halt loudly.
  2. **UX path** — a transient API/network error (RateLimit, Timeout,
     ConnectionError, etc.) is logged and converted to a friendly chat
     reply. The status_code inspection logic for 401/402/429/529/400/500
     stays here.

These tests pin both paths so future refactors cannot regress either
direction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource


# ---------------------------------------------------------------------------
# Fixture: a GatewayRunner with the preamble of ``_handle_message_with_agent``
# stubbed away, configured to invoke a caller-supplied ``_run_agent`` so the
# test controls what exception the handler sees.
# ---------------------------------------------------------------------------


def _make_event_and_source():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u-test",
        chat_id="c-test",
        user_name="tester",
        chat_type="dm",
    )
    event = MessageEvent(
        text="hello",
        message_id="m-test",
        source=source,
    )
    return event, source


def _make_session_entry() -> SessionEntry:
    now = datetime.now(timezone.utc)
    return SessionEntry(
        session_key="sk-test",
        session_id="sid-test",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


def _build_runner(run_agent_side_effect) -> GatewayRunner:
    """Construct a minimal GatewayRunner that reaches the try/except surface.

    ``run_agent_side_effect`` is whatever ``_run_agent`` should raise (or
    return). Everything else is no-op mock infrastructure to satisfy the
    preamble of ``_handle_message_with_agent``.
    """
    runner = object.__new__(GatewayRunner)

    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
    )
    adapter = SimpleNamespace(
        stop_typing=AsyncMock(),
        send=AsyncMock(),
    )
    runner.adapters = {Platform.TELEGRAM: adapter}

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _make_session_entry()
    runner.session_store.switch_session.return_value = None

    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner._session_db = None
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._running_agents = {}
    runner._draining = False

    # Preamble methods that the handler calls before the try block. Each
    # is replaced with a no-op stub returning a sensible default.
    runner._cache_session_source = MagicMock(return_value=None)
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._record_telegram_topic_binding = MagicMock(return_value=None)
    runner._set_session_env = MagicMock(return_value=[])
    runner._clear_session_env = MagicMock(return_value=None)
    runner._bind_adapter_run_generation = MagicMock(return_value=None)
    runner._prepare_inbound_message_text = AsyncMock(return_value="hello")
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._get_guild_id = MagicMock(return_value=None)
    runner._deliver_platform_notice = AsyncMock(return_value=None)
    runner._evict_cached_agent = MagicMock(return_value=None)
    runner._format_session_info = MagicMock(return_value="")
    runner._cleanup_agent_resources = MagicMock(return_value=None)
    runner._set_session_reasoning_override = MagicMock(return_value=None)
    runner._resolve_session_agent_runtime = MagicMock(return_value=SimpleNamespace())

    # The actual agent call — the surface under test.
    runner._run_agent = AsyncMock(side_effect=run_agent_side_effect)

    return runner


# ---------------------------------------------------------------------------
# Andon path: architectural exceptions must propagate
# ---------------------------------------------------------------------------


_ANDON_CLASSES = [
    ImportError("module 'tools.registry' has no symbol 'register_builtin_tools'"),
    ModuleNotFoundError("No module named 'grove.dispatcher'"),
    AttributeError("'NoneType' object has no attribute 'registry'"),
    TypeError("Dispatcher.__init__() missing 1 required keyword-only argument"),
    NameError("name 'batch_auto_skip_handler' is not defined"),
    RuntimeError("Dispatcher missing — production path cannot fall back"),
]


@pytest.mark.parametrize(
    "exc",
    _ANDON_CLASSES,
    ids=[type(e).__name__ for e in _ANDON_CLASSES],
)
@pytest.mark.asyncio
async def test_architectural_exception_propagates_not_swallowed(exc, monkeypatch):
    """Andon classes must escape ``_handle_message_with_agent`` unswallowed.

    Catching them here would hide a substrate break (Sprint X+1 consumer
    miss, wiring drift, fail-loud assertion violation) behind a friendly
    Telegram reply. The whole point of Andon is that the halt is visible.
    """
    # The preamble reads gateway config from disk — short-circuit that.
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    runner = _build_runner(run_agent_side_effect=exc)
    event, source = _make_event_and_source()

    with pytest.raises(type(exc)):
        await runner._handle_message_with_agent(
            event=event,
            source=source,
            _quick_key="qk-test",
            run_generation=1,
        )

    # Typing indicator is still stopped on the way out — the Andon path
    # cleans up adapter state before re-raising so the user does not see
    # a perpetually-typing bot.
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.stop_typing.assert_awaited()


# ---------------------------------------------------------------------------
# UX path: transient API errors keep the friendly chat reply
# ---------------------------------------------------------------------------


class _FakeRateLimitError(Exception):
    """Minimal stand-in for the provider SDK's RateLimitError.

    Carries ``status_code`` so the handler's existing 429 branch fires.
    Not an Andon class — must surface as a friendly chat reply.
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code
        self.response = None


@pytest.mark.asyncio
async def test_rate_limit_returns_friendly_chat_reply(monkeypatch):
    """A 429 from the provider keeps the legitimate UX surface intact.

    The narrowing hotfix must not collapse the friendly-error path that
    handles transient API failures (rate limits, plan-usage caps, 401s
    needing re-login, 529 overloads). Those should continue to surface
    as chat-visible hints, not Andon halts.
    """
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    exc = _FakeRateLimitError("rate limited", status_code=429)
    runner = _build_runner(run_agent_side_effect=exc)
    event, source = _make_event_and_source()

    result = await runner._handle_message_with_agent(
        event=event,
        source=source,
        _quick_key="qk-test",
        run_generation=1,
    )

    # The handler returns a string (the message body) for the caller to
    # send back to the user.  Pin: the friendly UX surface stayed alive.
    assert isinstance(result, str)
    assert "rate-limited" in result.lower() or "rate limit" in result.lower()
    assert "_FakeRateLimitError" in result  # error_type tag is preserved

    # Typing indicator stopped on the UX path too.
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.stop_typing.assert_awaited()


@pytest.mark.asyncio
async def test_generic_transient_exception_returns_friendly_chat_reply(monkeypatch):
    """A non-Andon exception with no status_code still routes to the UX surface.

    Network timeouts, connection resets, malformed-response decode errors,
    etc. land here.  The narrowing hotfix lists only architectural
    classes for re-raise; everything else stays on the friendly path.
    """
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    runner = _build_runner(run_agent_side_effect=ConnectionError("transport closed"))
    event, source = _make_event_and_source()

    result = await runner._handle_message_with_agent(
        event=event,
        source=source,
        _quick_key="qk-test",
        run_generation=1,
    )

    assert isinstance(result, str)
    assert "ConnectionError" in result
    assert "transport closed" in result
