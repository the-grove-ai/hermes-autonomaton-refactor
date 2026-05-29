"""Sprint 39 — session-authority. Dispatcher owns the SessionDB lifecycle.

These tests assert the Phase 1 contract:

* ``Dispatcher.session`` is the single Agent-path SessionDB authority.
* ``Dispatcher.open_session()`` generates fresh ids and reuses resumes
  pre-construction — before any Agent exists. THE Sprint 35 precondition.
* ``Dispatcher.hydrate_history()`` returns the resumed session's
  conversation history without any Agent involvement.
* ``Dispatcher.rotate_session()`` performs the atomic compression
  boundary's 7-call sequence against ``self.session``.
* The Dispatcher's intent handlers route ``SessionRotateIntent`` and
  ``SessionUpdateTokensIntent`` to the lifecycle methods, mirroring the
  Sprint 26 ``ToolIntent`` pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext
from grove.intents import (
    SessionRotateIntent,
    SessionUpdateTokensIntent,
)


# ── Construction + ownership ────────────────────────────────────────────


class TestSessionOwnership:
    """``Dispatcher.session`` is the authority; constructor accepts a
    caller-supplied handle or self-builds lazily."""

    def test_caller_supplied_session_db_is_used(
        self, mock_session_db, mock_runtime_ctx
    ):
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        assert d.session is mock_session_db

    def test_session_attribute_is_none_until_open_when_caller_omits(
        self, mock_runtime_ctx
    ):
        d = Dispatcher(runtime_ctx=mock_runtime_ctx)
        # No caller-supplied session: stays None until open_session(). The
        # lazy build is deferred so test Dispatchers that never run a turn
        # don't pay the file-system cost.
        assert d.session is None
        assert d.session_id is None


# ── Pre-construction open_session + hydrate_history ────────────────────
# THE Sprint 35 precondition: callable without an Agent existing.


class TestOpenSessionPreConstruction:
    def test_open_session_generates_fresh_id_when_not_resuming(
        self, dispatcher_with_session
    ):
        d = dispatcher_with_session
        sid = d.open_session()
        assert sid
        assert d.session_id == sid
        # Fresh open: row not yet created (turn-boundary deferred).
        assert d._session_row_created is False

    def test_open_session_uses_supplied_id_when_not_resuming(
        self, dispatcher_with_session
    ):
        sid = dispatcher_with_session.open_session(session_id="explicit_id")
        assert sid == "explicit_id"
        assert dispatcher_with_session.session_id == "explicit_id"

    def test_open_session_resume_resolves_compression_chain(
        self, mock_session_db, mock_runtime_ctx
    ):
        # When the requested id is the empty head of a compression chain,
        # resolve_resume_session_id walks to the descendant that holds
        # the transcript.
        mock_session_db.resolve_resume_session_id.return_value = "tip_id"
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        sid = d.open_session(session_id="head_id", resume=True)
        assert sid == "tip_id"
        assert d.session_id == "tip_id"
        mock_session_db.reopen_session.assert_called_once_with("tip_id")
        assert d._session_row_created is True

    def test_open_session_resume_requires_session_id(self, dispatcher_with_session):
        with pytest.raises(ValueError, match="resume=True requires a session_id"):
            dispatcher_with_session.open_session(resume=True)

    def test_hydrate_history_returns_resumed_conversation(
        self, mock_session_db, mock_runtime_ctx
    ):
        # The Sprint 35 hook: history is readable BEFORE any Agent
        # exists. Verified by checking that hydrate_history works on a
        # Dispatcher whose .agent is None.
        mock_session_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "hi"},
            {"role": "session_meta", "content": "meta"},  # filtered out
            {"role": "assistant", "content": "hello"},
        ]
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        d.open_session(session_id="s1", resume=True)
        assert d.agent is None
        history = d.hydrate_history()
        assert history == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_hydrate_history_empty_when_no_session(
        self, dispatcher_with_session
    ):
        # Pre-open_session: empty.
        assert dispatcher_with_session.hydrate_history() == []


# ── Rotate / append / close ─────────────────────────────────────────────


class TestSessionLifecycleMethods:
    def test_open_turn_row_creates_db_row_once(
        self, dispatcher_with_session, mock_session_db
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s1")
        d.open_turn_row(source="cli", model="m")
        mock_session_db.create_session.assert_called_once()
        # Idempotent: second call is a no-op.
        d.open_turn_row(source="cli", model="m")
        mock_session_db.create_session.assert_called_once()

    def test_append_messages_writes_from_starting_index(
        self, dispatcher_with_session, mock_session_db
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s1")
        d.open_turn_row(source="cli", model="m")
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "tool", "content": "c", "tool_call_id": "t1"},
        ]
        flushed = d.append_messages(msgs, starting_index=1)
        assert flushed == 3
        assert mock_session_db.append_message.call_count == 2

    def test_rotate_session_executes_full_sequence(
        self, mock_session_db, mock_runtime_ctx
    ):
        mock_session_db.get_session_title.return_value = "Original Title"
        mock_session_db.get_next_title_in_lineage.return_value = (
            "Original Title (cont. 2)"
        )
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        d.open_session(session_id="old_sid")
        d._session_row_created = True  # simulate prior turn
        new_sid = d.rotate_session(
            reason="compression",
            new_system_prompt="<NEW PROMPT>",
            source="cli",
            model="m",
            model_config={"k": "v"},
        )
        assert new_sid
        assert new_sid != "old_sid"
        assert d.session_id == new_sid
        mock_session_db.end_session.assert_called_once_with("old_sid", "compression")
        # New session row created with parent_session_id pointing back.
        create_kwargs = mock_session_db.create_session.call_args.kwargs
        assert create_kwargs["session_id"] == new_sid
        assert create_kwargs["parent_session_id"] == "old_sid"
        # Title propagated through get_next_title_in_lineage.
        mock_session_db.set_session_title.assert_called_once_with(
            new_sid, "Original Title (cont. 2)",
        )
        # System prompt installed on the new session.
        mock_session_db.update_system_prompt.assert_called_once_with(
            new_sid, "<NEW PROMPT>",
        )

    def test_close_session_calls_end_session(
        self, dispatcher_with_session, mock_session_db
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s1")
        d.close_session("user_exit")
        mock_session_db.end_session.assert_called_with("s1", "user_exit")


# ── Intent handlers in _drive_generator ─────────────────────────────────


class TestSessionIntentHandlers:
    """Verify ``_drive_generator`` routes the new session intents to the
    Dispatcher's lifecycle methods. Uses a synthetic generator (no real
    Agent) to exercise the dispatch path directly."""

    def test_session_rotate_intent_drives_rotate_session(
        self, mock_session_db, mock_runtime_ctx
    ):
        mock_session_db.get_session_title.return_value = "T"
        mock_session_db.get_next_title_in_lineage.return_value = "T2"
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        d.open_session(session_id="old")
        d._session_row_created = True

        captured: dict = {}

        def gen():
            obs = yield SessionRotateIntent(
                reason="compression", new_system_prompt="<sp>",
            )
            captured["obs_value"] = obs.value
            captured["obs_success"] = obs.success
            from grove.intents import FinalResponse
            yield FinalResponse(content="done")

        agent = MagicMock()
        agent.platform = "cli"
        agent.model = "m"
        agent._session_init_model_config = None
        agent.session_id = "old"
        ledger = MagicMock()
        d._drive_generator(agent, gen(), ledger)
        # The rotation executed against self.session.
        mock_session_db.end_session.assert_called_with("old", "compression")
        # The new id flowed back through the Observation.
        assert captured["obs_success"] is True
        assert captured["obs_value"] == d.session_id
        assert d.session_id != "old"

    def test_session_update_tokens_intent_drives_update_token_counts(
        self, mock_session_db, mock_runtime_ctx
    ):
        d = Dispatcher(runtime_ctx=mock_runtime_ctx, session_db=mock_session_db)
        d.open_session(session_id="s1")
        d._session_row_created = True

        def gen():
            yield SessionUpdateTokensIntent(
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=10,
                cache_write_tokens=5,
                estimated_cost_usd=0.01,
                cost_status="metered",
                cost_source="provider",
                billing_provider="anthropic",
                billing_base_url="https://api.anthropic.com",
            )
            from grove.intents import FinalResponse
            yield FinalResponse(content="done")

        agent = MagicMock()
        agent.session_id = "s1"
        ledger = MagicMock()
        d._drive_generator(agent, gen(), ledger)
        mock_session_db.update_token_counts.assert_called_once()
        kwargs = mock_session_db.update_token_counts.call_args.kwargs
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["cache_read_tokens"] == 10
        assert kwargs["billing_provider"] == "anthropic"
