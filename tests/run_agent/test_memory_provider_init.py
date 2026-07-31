"""Regression tests for memory provider selection during AIAgent init."""

from unittest.mock import patch
from tests._runtime_ctx import MOCK_RUNTIME_CTX, MOCK_CAPABILITY_PROVIDER


def test_blank_memory_provider_does_not_auto_enable_external():
    """Blank memory.provider must stay opt-out — no external provider is loaded or saved."""
    cfg = {"memory": {"provider": ""}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.save_config") as save_config,
        patch("plugins.memory.load_memory_provider") as load_memory_provider,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(runtime_ctx=MOCK_RUNTIME_CTX,
            api_mode="chat_completions",
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False, get_available_tools=lambda *_a, **_k: ([])
        )

    # Sprint 40 retired the Agent-held ``_memory_manager`` (now resolved via
    # dispatcher on demand); a blank provider must leave memory disabled.
    assert agent._memory_enabled is False
    load_memory_provider.assert_not_called()
    save_config.assert_not_called()
