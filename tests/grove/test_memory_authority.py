"""Sprint 40 — memory-authority. Dispatcher owns the MemoryStore + MemoryManager.

These tests assert the Phase 1 contract:

* ``Dispatcher.memory_store`` and ``Dispatcher.memory_manager`` are the
  single Agent-path memory authorities.
* ``Dispatcher.open_memory()`` builds the store + manager pre-Agent;
  ``Dispatcher.hydrate_memory_context()`` returns the three system-prompt
  blocks without any Agent involvement. THE Sprint 35 precondition.
* ``MemoryWriteIntent`` round-trips: the Agent yields, the Dispatcher
  catches and executes, the Dispatcher ``.send()``s a
  ``MemoryWriteResult`` back — the Sprint 26 bidirectional protocol
  applied to memory.
* ``MemoryLifecycleIntent`` fire-and-forget: each event routes to the
  corresponding ``memory_manager.*`` method; the generator resumes with
  a trivial ``Observation``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher
from grove.intents import (
    FinalResponse,
    MemoryLifecycleIntent,
    MemoryWriteIntent,
    MemoryWriteResult,
)


# ── Construction + ownership ────────────────────────────────────────────


class TestMemoryOwnership:
    def test_caller_supplied_store_and_manager_are_used(
        self, mock_memory_store, mock_memory_manager, mock_runtime_ctx
    ):
        d = Dispatcher(
            runtime_ctx=mock_runtime_ctx,
            memory_store=mock_memory_store,
            memory_manager=mock_memory_manager,
        )
        assert d.memory_store is mock_memory_store
        assert d.memory_manager is mock_memory_manager

    def test_memory_attributes_default_to_none_when_caller_omits(
        self, mock_runtime_ctx
    ):
        # No caller-supplied handles and no agent_kwargs → open_memory
        # is still invoked but config has no memory.enabled/provider
        # so both stay None.
        d = Dispatcher(runtime_ctx=mock_runtime_ctx)
        assert d.memory_store is None
        assert d.memory_manager is None


# ── Pre-construction read (THE Sprint 35 precondition) ─────────────────


# hydrate_memory_context() removed by legacy-memory-tool-retirement-v1
# (no production caller; the classifier no longer reads a legacy memory block).
# The external-manager hydration it also did is exercised via the composer's
# _external_memory_provider, not this deleted pre-Agent path.


# ── MemoryWriteIntent round-trip ───────────────────────────────────────


class TestMemoryWriteIntentRoundTrip:
    # builtin_memory round-trip tests removed by legacy-memory-tool-retirement-v1
    # (the legacy `memory` tool + its on_memory_write bridge are gone). Only the
    # provider_tool path remains below.

    def test_provider_tool_routes_to_manager_handle_tool_call(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        mock_memory_manager.handle_tool_call.return_value = "provider-said-ok"
        d = dispatcher_with_memory

        observed: dict = {}

        def gen():
            result = yield MemoryWriteIntent(
                kind="provider_tool",
                tool_name="honcho_query",
                arguments={"q": "hi"},
            )
            observed["result"] = result
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())

        mock_memory_manager.handle_tool_call.assert_called_once_with(
            "honcho_query", {"q": "hi"},
        )
        assert observed["result"].success is True
        assert observed["result"].value == "provider-said-ok"

    def test_unknown_kind_returns_failure_result(
        self, dispatcher_with_memory,
    ):
        d = dispatcher_with_memory
        observed: dict = {}

        def gen():
            observed["result"] = yield MemoryWriteIntent(kind="bogus")
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        assert observed["result"].success is False
        assert "bogus" in (observed["result"].error or "")


# ── MemoryLifecycleIntent (fire-and-forget) ─────────────────────────────


class TestMemoryLifecycleIntent:
    def test_on_session_end_routes_to_manager(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory
        msgs = [{"role": "user", "content": "hi"}]

        def gen():
            yield MemoryLifecycleIntent(event="on_session_end", messages=msgs)
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        mock_memory_manager.on_session_end.assert_called_once_with(msgs)

    def test_on_session_switch_routes_to_manager(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory
        d.session_id = "new_sid"

        def gen():
            yield MemoryLifecycleIntent(
                event="on_session_switch",
                parent_session_id="old_sid",
                reason="compression",
            )
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        mock_memory_manager.on_session_switch.assert_called_once_with(
            "new_sid",
            parent_session_id="old_sid",
            reset=False,
            reason="compression",
        )

    def test_on_pre_compress_routes_to_manager(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory
        msgs = [{"role": "assistant", "content": "x"}]

        def gen():
            yield MemoryLifecycleIntent(event="on_pre_compress", messages=msgs)
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        mock_memory_manager.on_pre_compress.assert_called_once_with(msgs)

    def test_sync_turn_calls_sync_all_and_queue_prefetch_all(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory
        d.session_id = "sess_42"

        def gen():
            yield MemoryLifecycleIntent(
                event="sync_turn",
                original_user_message="hello",
                final_response="hi back",
                interrupted=False,
            )
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        # MemoryManager.sync_all(user_content, assistant_content, *, session_id)
        mock_memory_manager.sync_all.assert_called_once_with(
            "hello", "hi back", session_id="sess_42",
        )
        mock_memory_manager.queue_prefetch_all.assert_called_once_with(
            "hello", session_id="sess_42",
        )

    def test_sync_turn_skips_when_interrupted(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory

        def gen():
            yield MemoryLifecycleIntent(
                event="sync_turn",
                original_user_message="hello",
                final_response="partial",
                interrupted=True,
            )
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        mock_memory_manager.sync_all.assert_not_called()
        mock_memory_manager.queue_prefetch_all.assert_not_called()

    def test_shutdown_routes_to_shutdown_all(
        self, dispatcher_with_memory, mock_memory_manager,
    ):
        d = dispatcher_with_memory

        def gen():
            yield MemoryLifecycleIntent(event="shutdown")
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        mock_memory_manager.shutdown_all.assert_called_once_with()


# ── open_memory builds from runtime_ctx.config ─────────────────────────


class TestOpenMemoryFromConfig:
    def test_open_memory_skips_when_neither_enabled(
        self, mock_runtime_ctx,
    ):
        d = Dispatcher(runtime_ctx=mock_runtime_ctx)
        d.open_memory(memory_config={})  # both flags default False
        assert d.memory_store is None
        assert d._memory_enabled is False
        assert d._user_profile_enabled is False

    # test_open_memory_builds_store_when_memory_enabled removed by
    # legacy-memory-tool-retirement-v1 — open_memory no longer builds a legacy
    # MemoryStore (self.memory_store stays None); it builds only the external
    # MemoryManager, covered by test_open_memory_skips_when_neither_enabled and
    # the TestMemoryLifecycleIntent suite.
