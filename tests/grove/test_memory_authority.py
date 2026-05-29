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


class TestHydrateMemoryContextPreConstruction:
    def test_hydrate_memory_context_returns_three_blocks(
        self, mock_memory_store, mock_memory_manager, mock_runtime_ctx
    ):
        # Configure the store + manager to return distinct prompt
        # blocks; assert hydrate_memory_context() returns them keyed
        # correctly, WITHOUT an Agent existing.
        def _store_block(target: str) -> str:
            return f"<<{target.upper()}>>"
        mock_memory_store.format_for_system_prompt.side_effect = _store_block
        mock_memory_manager.build_system_prompt.return_value = "<<EXTERNAL>>"
        d = Dispatcher(
            runtime_ctx=mock_runtime_ctx,
            memory_store=mock_memory_store,
            memory_manager=mock_memory_manager,
        )
        # Flip on the read flags (open_memory would set these from
        # config; we set them directly for the isolation test).
        d._memory_enabled = True
        d._user_profile_enabled = True

        assert d.agent is None  # THE Sprint 35 precondition under test
        ctx = d.hydrate_memory_context()
        assert ctx == {
            "memory": "<<MEMORY>>",
            "user": "<<USER>>",
            "external": "<<EXTERNAL>>",
        }

    def test_hydrate_memory_context_handles_missing_store(
        self, mock_memory_manager, mock_runtime_ctx
    ):
        mock_memory_manager.build_system_prompt.return_value = "ext"
        d = Dispatcher(
            runtime_ctx=mock_runtime_ctx,
            memory_manager=mock_memory_manager,
        )
        ctx = d.hydrate_memory_context()
        assert ctx == {"memory": "", "user": "", "external": "ext"}

    def test_hydrate_memory_context_handles_missing_manager(
        self, mock_memory_store, mock_runtime_ctx
    ):
        mock_memory_store.format_for_system_prompt.side_effect = (
            lambda target: "mem" if target == "memory" else ""
        )
        d = Dispatcher(
            runtime_ctx=mock_runtime_ctx,
            memory_store=mock_memory_store,
        )
        d._memory_enabled = True
        ctx = d.hydrate_memory_context()
        assert ctx == {"memory": "mem", "user": "", "external": ""}

    def test_hydrate_memory_context_empty_when_no_handles(self, mock_runtime_ctx):
        d = Dispatcher(runtime_ctx=mock_runtime_ctx)
        assert d.hydrate_memory_context() == {
            "memory": "", "user": "", "external": "",
        }


# ── MemoryWriteIntent round-trip ───────────────────────────────────────


class TestMemoryWriteIntentRoundTrip:
    def test_builtin_memory_yield_returns_write_result(
        self, dispatcher_with_memory, mock_memory_store, mock_memory_manager,
        monkeypatch,
    ):
        # Stub the standalone memory_tool function so the Dispatcher's
        # execute_memory_write calls it and we can assert the wiring.
        captured: dict = {}

        def fake_memory_tool(*, action, target, content, old_text, store):
            captured["action"] = action
            captured["target"] = target
            captured["content"] = content
            captured["store"] = store
            return "ok-builtin"
        monkeypatch.setattr(
            "tools.memory_tool.memory_tool", fake_memory_tool,
        )

        d = dispatcher_with_memory
        observed: dict = {}

        def gen():
            result = yield MemoryWriteIntent(
                kind="builtin_memory",
                action="add",
                target="memory",
                content="some fact",
                metadata={"task_id": "t1"},
            )
            observed["result"] = result
            yield FinalResponse(content="done")

        agent = MagicMock()
        ledger = MagicMock()
        d._drive_generator(agent, gen(), ledger)

        # The memory_tool was called with the intent's fields.
        assert captured == {
            "action": "add",
            "target": "memory",
            "content": "some fact",
            "store": mock_memory_store,
        }
        # The Dispatcher injected a MemoryWriteResult back into the
        # generator (Sprint 26 bidirectional protocol applied to memory).
        assert isinstance(observed["result"], MemoryWriteResult)
        assert observed["result"].success is True
        assert observed["result"].value == "ok-builtin"
        # The bridge fired: external provider sees the write.
        mock_memory_manager.on_memory_write.assert_called_once_with(
            "add", "memory", "some fact", metadata={"task_id": "t1"},
        )

    def test_builtin_memory_bridge_skipped_when_action_not_add_or_replace(
        self, dispatcher_with_memory, mock_memory_manager, monkeypatch,
    ):
        monkeypatch.setattr(
            "tools.memory_tool.memory_tool",
            lambda **_: "show-result",
        )
        d = dispatcher_with_memory

        def gen():
            yield MemoryWriteIntent(
                kind="builtin_memory",
                action="show",  # not add/replace
                target="memory",
            )
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        # No bridge notification when action isn't add/replace.
        mock_memory_manager.on_memory_write.assert_not_called()

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

    def test_builtin_memory_returns_failure_when_store_absent(
        self, mock_runtime_ctx, mock_memory_manager,
    ):
        d = Dispatcher(
            runtime_ctx=mock_runtime_ctx,
            memory_manager=mock_memory_manager,
        )
        observed: dict = {}

        def gen():
            observed["result"] = yield MemoryWriteIntent(
                kind="builtin_memory", action="add",
                target="memory", content="x",
            )
            yield FinalResponse(content="done")

        d._drive_generator(MagicMock(), gen(), MagicMock())
        assert observed["result"].success is False
        assert "store" in (observed["result"].error or "")

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

    def test_open_memory_builds_store_when_memory_enabled(
        self, mock_runtime_ctx, monkeypatch,
    ):
        built: dict = {}

        class FakeStore:
            def __init__(self, **kw):
                built.update(kw)
            def load_from_disk(self):
                built["loaded"] = True

        monkeypatch.setattr("tools.memory_tool.MemoryStore", FakeStore)
        d = Dispatcher(runtime_ctx=mock_runtime_ctx)
        d.open_memory(memory_config={
            "memory_enabled": True,
            "memory_char_limit": 999,
            "user_char_limit": 100,
        })
        assert isinstance(d.memory_store, FakeStore)
        assert built["memory_char_limit"] == 999
        assert built["user_char_limit"] == 100
        assert built["loaded"] is True
        assert d._memory_enabled is True
