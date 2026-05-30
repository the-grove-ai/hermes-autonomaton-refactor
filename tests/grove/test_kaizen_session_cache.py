"""Sprint 32 — Kaizen session cache tests (Phase 1c).

The Dispatcher's ``_session_deny_cache`` and ``_session_allow_cache``
remember operator dispositions for the lifetime of the Dispatcher.
Subsequent identical halts auto-apply silently and emit a
``session_cache_hit`` ledger telemetry event.

These tests exercise the cache directly via ``_handle_andon_halt`` so
they don't depend on the full dispatch-turn drive loop. The
integration-level coverage (cache survives across turns) lives in
``test_dispatch_turn.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.intents import ToolIntent
from grove.zones import ZoneResult


def _halt(
    tool: str = "terminal",
    arguments=None,
    zone: str = "yellow",
) -> AndonHalt:
    arguments = arguments or {"command": "ls -la"}
    intents = [ToolIntent(tool_name=tool, arguments=arguments, call_id="c1")]
    zr = [ZoneResult(zone=zone, matched_rule="r", source="default")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


@pytest.fixture
def dispatcher() -> Dispatcher:
    """A Dispatcher whose pending-andon marker is no-op'd so tests
    can drive ``_handle_andon_halt`` without filesystem side effects."""
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    return d


@pytest.fixture
def agent_stub():
    return MagicMock()


# ── Cache mutation by disposition ────────────────────────────────────


class TestCacheMutationByDisposition:
    def test_deny_disposition_populates_deny_cache(
        self, dispatcher, agent_stub,
    ):
        dispatcher._sovereign_prompt_handler = lambda halt: "deny"
        halt = _halt()
        assert not dispatcher._session_deny_cache
        result = dispatcher._handle_andon_halt(agent_stub, halt)
        assert result == "deny"
        assert len(dispatcher._session_deny_cache) == 1
        assert not dispatcher._session_allow_cache

    def test_session_populates_allow_cache(self, dispatcher, agent_stub):
        dispatcher._sovereign_prompt_handler = lambda halt: "session"
        result = dispatcher._handle_andon_halt(agent_stub, _halt())
        assert result == "session"
        assert len(dispatcher._session_allow_cache) == 1
        assert not dispatcher._session_deny_cache

    def test_always_populates_allow_cache(self, dispatcher, agent_stub):
        dispatcher._sovereign_prompt_handler = lambda halt: "always"
        result = dispatcher._handle_andon_halt(agent_stub, _halt())
        assert result == "always"
        assert len(dispatcher._session_allow_cache) == 1

    def test_once_does_not_populate_either_cache(
        self, dispatcher, agent_stub,
    ):
        dispatcher._sovereign_prompt_handler = lambda halt: "once"
        result = dispatcher._handle_andon_halt(agent_stub, _halt())
        assert result == "once"
        assert not dispatcher._session_deny_cache
        assert not dispatcher._session_allow_cache

# ── Cache hit auto-applies silently ──────────────────────────────────


class TestCacheHitAutoApply:
    def test_deny_cache_hit_returns_deny_without_invoking_handler(
        self, dispatcher, agent_stub,
    ):
        # Seed the cache directly.
        halt = _halt()
        key = dispatcher._kaizen_cache_key(
            halt.intents[0].tool_name, halt.intents[0].arguments,
        )
        dispatcher._session_deny_cache.add(key)

        # Handler raises if invoked — proves the cache short-circuited.
        def _explode(_h):
            raise AssertionError("handler should not be invoked on cache hit")
        dispatcher._sovereign_prompt_handler = _explode

        result = dispatcher._handle_andon_halt(agent_stub, halt)
        assert result == "deny"

    def test_allow_cache_hit_returns_once_without_invoking_handler(
        self, dispatcher, agent_stub,
    ):
        halt = _halt()
        key = dispatcher._kaizen_cache_key(
            halt.intents[0].tool_name, halt.intents[0].arguments,
        )
        dispatcher._session_allow_cache.add(key)

        def _explode(_h):
            raise AssertionError("handler should not be invoked on cache hit")
        dispatcher._sovereign_prompt_handler = _explode

        result = dispatcher._handle_andon_halt(agent_stub, halt)
        assert result == "once"

    def test_cache_hit_writes_telemetry_event(
        self, dispatcher, agent_stub,
    ):
        halt = _halt()
        key = dispatcher._kaizen_cache_key(
            halt.intents[0].tool_name, halt.intents[0].arguments,
        )
        dispatcher._session_allow_cache.add(key)

        ledger = MagicMock()
        dispatcher._sovereign_prompt_handler = lambda halt: "deny"  # not invoked
        dispatcher._handle_andon_halt(agent_stub, halt, ledger=ledger)
        ledger.record.assert_called_once()
        call = ledger.record.call_args
        assert call.args[0] == "session_cache_hit"
        assert call.kwargs == {"tool": "terminal", "type": "allow"}


# ── Cache key semantics ──────────────────────────────────────────────


class TestCacheKey:
    def test_same_args_same_key(self, dispatcher):
        k1 = dispatcher._kaizen_cache_key("terminal", {"command": "ls"})
        k2 = dispatcher._kaizen_cache_key("terminal", {"command": "ls"})
        assert k1 == k2

    def test_different_args_different_key(self, dispatcher):
        k1 = dispatcher._kaizen_cache_key("terminal", {"command": "ls"})
        k2 = dispatcher._kaizen_cache_key("terminal", {"command": "rm"})
        assert k1 != k2

    def test_different_tool_different_key(self, dispatcher):
        k1 = dispatcher._kaizen_cache_key("terminal", {"x": 1})
        k2 = dispatcher._kaizen_cache_key("execute_code", {"x": 1})
        assert k1 != k2

    def test_argument_order_insensitive(self, dispatcher):
        # Canonical JSON sorts keys, so {a:1, b:2} == {b:2, a:1}.
        k1 = dispatcher._kaizen_cache_key("t", {"a": 1, "b": 2})
        k2 = dispatcher._kaizen_cache_key("t", {"b": 2, "a": 1})
        assert k1 == k2

    def test_non_json_serializable_value_stringifies_safely(
        self, dispatcher,
    ):
        # Argument values that are not JSON-serializable (e.g., a
        # set, a custom object) MUST NOT crash the cache.
        k1 = dispatcher._kaizen_cache_key("t", {"s": {1, 2, 3}})
        k2 = dispatcher._kaizen_cache_key("t", {"s": {1, 2, 3}})
        # Sets stringify deterministically; the hash holds.
        assert k1 == k2


# ── Cache persistence across multiple halts ──────────────────────────


class TestCachePersistence:
    def test_second_identical_halt_hits_cache(
        self, dispatcher, agent_stub,
    ):
        """First halt → handler asks operator → operator denies →
        cache populated. Second identical halt → handler NOT invoked
        → auto-deny."""
        call_count = {"n": 0}
        def _counting_handler(_h):
            call_count["n"] += 1
            return "deny"
        dispatcher._sovereign_prompt_handler = _counting_handler

        halt = _halt()
        dispatcher._handle_andon_halt(agent_stub, halt)
        assert call_count["n"] == 1
        # Second halt with the same tool + args.
        dispatcher._handle_andon_halt(agent_stub, _halt())
        assert call_count["n"] == 1, "handler must NOT re-prompt on cache hit"

    def test_different_args_re_prompt(
        self, dispatcher, agent_stub,
    ):
        """Different command args MUST produce a fresh prompt — the
        cache key includes the arguments."""
        call_count = {"n": 0}
        def _counting_handler(_h):
            call_count["n"] += 1
            return "deny"
        dispatcher._sovereign_prompt_handler = _counting_handler

        dispatcher._handle_andon_halt(
            agent_stub, _halt(arguments={"command": "ls"}),
        )
        dispatcher._handle_andon_halt(
            agent_stub, _halt(arguments={"command": "rm"}),
        )
        assert call_count["n"] == 2
