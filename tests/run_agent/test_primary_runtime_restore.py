"""Tests for the primary-runtime snapshot and transient transport recovery.

GRV-010 C2d-2 removed the ungoverned per-turn fallback restore (the silent
model-substitution chain); the restore-specific tests went with it. What
survives — and is verified here — is the CORE primary-transport path the
governed router downshift also relies on:
* the ``_primary_runtime`` snapshot captured at init / on switch_model;
* ``_try_recover_primary_transport`` — one rebuilt-client recovery cycle for
  transient transport errors (TCP reset, read/connect timeout), skipped for
  aggregator providers (OpenRouter, Nous) and non-transport errors.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from run_agent import AIAgent
from tests._runtime_ctx import MOCK_RUNTIME_CTX, MOCK_CAPABILITY_PROVIDER


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(fallback_model=None, provider="custom", base_url="https://my-llm.example.com/v1"):
    """Create a minimal AIAgent with optional fallback config."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(runtime_ctx=MOCK_RUNTIME_CTX, 
            api_mode="chat_completions",
            api_key="test-key-12345678",
            base_url=base_url,
            provider=provider,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model, get_available_tools=lambda *_a, **_k: (_make_tool_defs("web_search"))
        )
        agent.client = MagicMock()
        return agent


def _mock_resolve(base_url="https://openrouter.ai/api/v1", api_key="fallback-key-1234"):
    """Helper to create a mock client for resolve_provider_client."""
    mock_client = MagicMock()
    mock_client.api_key = api_key
    mock_client.base_url = base_url
    return mock_client


# =============================================================================
# _primary_runtime snapshot
# =============================================================================

class TestPrimaryRuntimeSnapshot:
    def test_snapshot_created_at_init(self):
        agent = _make_agent()
        assert hasattr(agent, "_primary_runtime")
        rt = agent._primary_runtime
        assert rt["model"] == agent.model
        assert rt["provider"] == "custom"
        assert rt["base_url"] == "https://my-llm.example.com/v1"
        assert rt["api_mode"] == agent.api_mode
        assert "client_kwargs" in rt
        assert "compressor_context_length" in rt

    def test_snapshot_includes_compressor_state(self):
        agent = _make_agent()
        rt = agent._primary_runtime
        cc = agent.context_compressor
        assert rt["compressor_model"] == cc.model
        assert rt["compressor_provider"] == cc.provider
        assert rt["compressor_context_length"] == cc.context_length
        assert rt["compressor_threshold_tokens"] == cc.threshold_tokens

    def test_snapshot_includes_anthropic_state_when_applicable(self):
        """Anthropic-mode agents should snapshot Anthropic-specific state."""
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        ):
            agent = AIAgent(runtime_ctx=MOCK_RUNTIME_CTX,
                api_key="sk-ant-test-12345678",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True, get_available_tools=lambda *_a, **_k: (_make_tool_defs("web_search"))
            )
        rt = agent._primary_runtime
        assert "anthropic_api_key" in rt
        assert "anthropic_base_url" in rt
        assert "is_anthropic_oauth" in rt

    def test_snapshot_omits_anthropic_for_openai_mode(self):
        agent = _make_agent(provider="custom")
        rt = agent._primary_runtime
        assert "anthropic_api_key" not in rt



# =============================================================================
# _try_recover_primary_transport()
# =============================================================================

def _make_transport_error(error_type="ReadTimeout"):
    """Create an exception whose type().__name__ matches the given name."""
    cls = type(error_type, (Exception,), {})
    return cls("connection timed out")


class TestTryRecoverPrimaryTransport:

    def test_recovers_on_read_timeout(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_recovers_on_connect_timeout(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ConnectTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_recovers_on_pool_timeout(self):
        agent = _make_agent(provider="zai")
        error = _make_transport_error("PoolTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_recovers_on_openai_api_connection_error(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("APIConnectionError")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_recovers_on_openai_api_timeout_error(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("APITimeoutError")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_skipped_when_already_on_fallback(self):
        agent = _make_agent(provider="custom")
        agent._fallback_activated = True
        error = _make_transport_error("ReadTimeout")

        result = agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3,
        )
        assert result is False

    def test_skipped_for_non_transport_error(self):
        """Non-transport errors (ValueError, APIError, etc.) skip recovery."""
        agent = _make_agent(provider="custom")
        error = ValueError("invalid model")

        result = agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3,
        )
        assert result is False

    def test_skipped_for_openrouter(self):
        agent = _make_agent(provider="openrouter", base_url="https://openrouter.ai/api/v1")
        error = _make_transport_error("ReadTimeout")

        result = agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3,
        )
        assert result is False

    def test_skipped_for_nous_provider(self):
        agent = _make_agent(provider="nous", base_url="https://inference.nous.nousresearch.com/v1")
        error = _make_transport_error("ReadTimeout")

        result = agent._try_recover_primary_transport(
            error, retry_count=3, max_retries=3,
        )
        assert result is False

    def test_allowed_for_anthropic_direct(self):
        """Direct Anthropic endpoint should get recovery."""
        agent = _make_agent(provider="anthropic", base_url="https://api.anthropic.com")
        # For non-anthropic_messages api_mode, it will use OpenAI client
        error = _make_transport_error("ConnectError")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_allowed_for_ollama(self):
        agent = _make_agent(provider="ollama", base_url="http://localhost:11434/v1")
        error = _make_transport_error("ConnectTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is True

    def test_wait_time_scales_with_retry_count(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep") as mock_sleep:
            agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )
            # wait_time = min(3 + retry_count, 8) = min(6, 8) = 6
            mock_sleep.assert_called_once_with(6)

    def test_wait_time_capped_at_8(self):
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep") as mock_sleep:
            agent._try_recover_primary_transport(
                error, retry_count=10, max_retries=3,
            )
            # wait_time = min(3 + 10, 8) = 8
            mock_sleep.assert_called_once_with(8)

    def test_closes_existing_client_before_rebuild(self):
        agent = _make_agent(provider="custom")
        old_client = agent.client
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", return_value=MagicMock()), \
             patch("time.sleep"), \
             patch.object(agent, "_close_openai_client") as mock_close:
            agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )
            mock_close.assert_called_once_with(
                old_client, reason="primary_recovery", shared=True,
            )

    def test_survives_rebuild_failure(self):
        """If client rebuild fails, returns False gracefully."""
        agent = _make_agent(provider="custom")
        error = _make_transport_error("ReadTimeout")

        with patch("run_agent.OpenAI", side_effect=Exception("socket error")), \
             patch("time.sleep"):
            result = agent._try_recover_primary_transport(
                error, retry_count=3, max_retries=3,
            )

        assert result is False


