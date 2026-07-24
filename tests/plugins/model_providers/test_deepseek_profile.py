"""Unit tests for the DeepSeek provider profile's thinking-mode wiring.

DeepSeek V4 (and the legacy ``deepseek-reasoner``) expects every request to
carry an explicit ``extra_body.thinking`` parameter.  Omitting it makes the
server default to thinking-mode ON, which then enforces the
``reasoning_content``-must-be-echoed-back contract on subsequent turns and
breaks the conversation with HTTP 400 (#15700, #17212, #17825).

These tests pin the profile's wire-shape contract so DeepSeek requests stay
correctly shaped without going live.
"""

from __future__ import annotations

import pytest

from grove.router import ModelFacts

# binding-opacity-v1 P4b 1c — thinking-capability is the declared fact
# model_facts.reasoning_support, threaded into the profile via the transport's
# params carrier. The DeepSeek V4 family / deepseek-reasoner declare
# reasoning_support: true; V3 declares false.
_THINKING = ModelFacts(reasoning_support=True)
_NO_THINKING = ModelFacts(reasoning_support=False)


@pytest.fixture
def deepseek_profile():
    """Resolve the registered DeepSeek profile.

    Going through ``providers.get_provider_profile`` keeps the test honest —
    if someone later replaces the registered class with a plain
    ``ProviderProfile``, every assertion below collapses.
    """
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the DeepSeek profile in the global provider registry.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("deepseek")
    assert profile is not None, "deepseek provider profile must be registered"
    return profile


class TestDeepSeekThinkingWireShape:
    """``build_api_kwargs_extras`` produces DeepSeek's exact wire format."""

    def test_v4_pro_default_enables_thinking_without_effort(self, deepseek_profile):
        """No reasoning_config → thinking enabled, server picks default effort."""
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek-v4-pro", model_facts=_THINKING
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    def test_v4_pro_enabled_with_high_effort(self, deepseek_profile):
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_standard_efforts_pass_through(self, deepseek_profile, effort):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert top_level == {"reasoning_effort": effort}

    @pytest.mark.parametrize("effort", ["xhigh", "max", "MAX", "  Max  "])
    def test_xhigh_and_max_normalize_to_max(self, deepseek_profile, effort):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert top_level == {"reasoning_effort": "max"}

    def test_explicitly_disabled_sends_disabled_marker(self, deepseek_profile):
        """``reasoning_config.enabled=False`` → ``thinking.type=disabled``.

        The crucial bit is that the parameter is *sent* at all — DeepSeek
        defaults to thinking-on when ``thinking`` is absent.
        """
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="deepseek-v4-pro", model_facts=_THINKING
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        # No effort when disabled — DeepSeek rejects it.
        assert top_level == {}

    def test_disabled_ignores_effort_field(self, deepseek_profile):
        """Effort silently dropped when thinking is off."""
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert top_level == {}

    def test_unknown_effort_omits_top_level(self, deepseek_profile):
        """Garbage effort → omit reasoning_effort so DeepSeek applies its default."""
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "garbage"},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert top_level == {}

    def test_empty_effort_omits_top_level(self, deepseek_profile):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": ""},
            model="deepseek-v4-pro", model_facts=_THINKING,
        )
        assert top_level == {}


class TestDeepSeekModelGating:
    """Thinking is gated on the DECLARED fact, not the model name.

    binding-opacity-v1 P4b 1c — EXCISED the two name-roster parametrizations
    (test_thinking_capable_models_emit_thinking iterated deepseek-v4-*/
    -reasoner NAMES; test_non_thinking_models_emit_nothing iterated
    deepseek-chat/v3/unknown NAMES). Both pinned _model_supports_thinking()'s
    name-substring detection — the inference the migration deleted. The
    surviving behavior — reasoning_support drives thinking — is pinned below.
    """

    def test_declared_reasoning_support_emits_thinking(self, deepseek_profile):
        extra_body, _ = deepseek_profile.build_api_kwargs_extras(
            reasoning_config=None, model="any-slug", model_facts=_THINKING
        )
        assert extra_body == {"thinking": {"type": "enabled"}}

    def test_undeclared_reasoning_support_emits_nothing(self, deepseek_profile):
        # The VM-on-deploy path: undeclared -> wire format untouched (safe/loud).
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="any-slug",
            model_facts=_NO_THINKING,
        )
        assert extra_body == {}
        assert top_level == {}

    def test_absent_model_facts_emits_nothing(self, deepseek_profile):
        # No model_facts in context at all (e.g. a legacy caller) -> safe default.
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model="any-slug"
        )
        assert extra_body == {}
        assert top_level == {}


class TestDeepSeekFullKwargsIntegration:
    """End-to-end: the transport's full kwargs match DeepSeek's live wire format.

    The live test harness in ``tests/run_agent/test_deepseek_v4_thinking_live.py``
    sends ``{"reasoning_effort": "high", "extra_body": {"thinking": {"type":
    "enabled"}}}``.  Confirm the transport produces that exact shape when wired
    through the registered DeepSeek profile.
    """

    def test_full_kwargs_match_live_wire_shape(self, deepseek_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v4-pro", model_facts=_THINKING,
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=deepseek_profile,
            reasoning_config={"enabled": True, "effort": "high"},
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )
        assert kwargs["model"] == "deepseek-v4-pro"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_v3_chat_full_kwargs_omit_thinking(self, deepseek_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-chat",
            model_facts=_NO_THINKING,
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=deepseek_profile,
            reasoning_config={"enabled": True, "effort": "high"},
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )
        assert "reasoning_effort" not in kwargs
        assert "extra_body" not in kwargs or "thinking" not in kwargs.get("extra_body", {})
