from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grove.dispatcher import Dispatcher
from tests._runtime_ctx import MOCK_RUNTIME_CTX


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str):
    """Sprint 39 — construct via Dispatcher so per-API-call
    ``SessionUpdateTokensIntent`` yields land at the Dispatcher's
    ``self.session`` (the test-supplied mock)."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        d = Dispatcher(
            runtime_ctx=MOCK_RUNTIME_CTX,
            session_db=session_db,
            agent_kwargs=dict(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_id=f"{platform}-session",
                platform=platform,
            ),
        )
    agent = d.agent
    # Mark the session row as created so the Dispatcher's
    # update_token_counts handler doesn't bail out early.
    d._session_row_created = True
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "telegram-session"


def test_run_conversation_persists_tokens_for_cron_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="cron")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "cron-session"


# Sprint 39 — the third test in this file
# (``test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one``)
# exercised the now-deleted ``_get_session_db_for_recall`` silent-
# fallback bootstrap. Sprint 39 removed that path: the recall tool reads
# through ``self._dispatcher_singleton.session`` and there is no Agent-
# side SessionDB construction. The scenario the test asserted no longer
# exists; the test is deleted rather than repurposed (the contract it
# verified — "Agent creates a SessionDB when entrypoint forgot" — is
# specifically what Sprint 39 deletes as silent degradation).
