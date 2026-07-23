"""Tests for AIAgent._anthropic_prompt_cache_policy().

The policy returns ``(should_cache, use_native_layout)`` for the endpoint
classes. The test matrix pins the decision for each so a regression (e.g.
silently dropping caching on third-party Anthropic gateways, or applying
the native layout on OpenRouter) surfaces loudly.

binding-opacity-v1 P4b — whether a model honours Anthropic-style
cache_control is now a DECLARED fact (``model_facts.prompt_cache_style ==
"anthropic"``), not a "claude"/"qwen" name substring. The fixture carries
the declared facts; the *layout* (native vs envelope) still derives from
api_mode / endpoint / provider — provenance-clean signals, never the slug.

Excised in this phase (their subject was the NAME-inference the migration
deleted, per the P4b hybrid discriminator — "subject was the inference"):
  * test_qwen3_7_max_on_openrouter_caches_with_envelope_layout — pinned the
    exact catalog slug "qwen3.7-max" caching on OpenRouter; redundant with
    the declared-fact OpenRouter envelope test below.
  * test_grok_on_openrouter_does_not_inject_cache_control — pinned that the
    "qwen" substring did not over-match the grok name; substring
    over-matching no longer exists to guard against.
  * test_qwen35_plus_on_opencode_go — a "qwen3.5-plus" name variant of the
    opencode-go envelope behavior test.
  * test_non_qwen_on_opencode_go_does_not_cache — pinned that a non-"qwen"
    name (glm-5) on opencode-go stayed off; now the generic undeclared
    default (covered by the absence test in test_router).
  * test_kimi_on_opencode_go_does_not_cache — pinned the "kimi" name staying
    off; generic undeclared default.
  * test_qwen_vendored_slug_on_nous_portal_caches — pinned vendored-slug
    substring robustness ("qwen/qwen3.6-plus"); substring matching deleted.
  * test_non_qwen_non_claude_on_nous_portal_does_not_cache — pinned Portal
    name-narrowness (claude/qwen-only BY NAME); scope is a declared fact now.
  * test_custom_openai_wire_does_not_cache_even_with_claude_name — pinned
    "even with a claude NAME, OpenAI-wire stays off"; the name-based hazard
    is gone (caching is off unless the fact is declared).
  * test_overrides_take_precedence_over_self — pinned a model-NAME override
    flipping the cache decision; caching reads declared facts bound to the
    agent, not the ``model`` argument.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from run_agent import AIAgent
from grove.router import ModelFacts


def _make_agent(
    *,
    provider: str = "openrouter",
    base_url: str = "https://openrouter.ai/api/v1",
    api_mode: str = "chat_completions",
    model: str = "anthropic/claude-sonnet-4.6",
    prompt_cache_style: str = "none",
) -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.provider = provider
    agent.base_url = base_url
    agent.api_mode = api_mode
    agent.model = model
    agent._base_url_lower = (base_url or "").lower()
    # binding-opacity-v1 P4b — the __new__ seam bypasses __init__, so the
    # declared facts are set explicitly here. prompt_cache_style is the only
    # cache-relevant field; "anthropic" means the model honours cache_control.
    agent._model_facts = ModelFacts(prompt_cache_style=prompt_cache_style)
    agent.client = MagicMock()
    agent.quiet_mode = True
    return agent


class TestNativeAnthropic:
    # Native Anthropic caches via provider/host detection (provenance-clean);
    # it does not depend on the declared cache fact. Fixture carries default
    # facts only — the assertion holds regardless.
    def test_native_anthropic_caches_with_native_layout(self):
        agent = _make_agent(
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-sonnet-4-6",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)

    def test_api_anthropic_host_detected_even_when_provider_label_differs(self):
        # Some pool configurations label native Anthropic as "anthropic-direct"
        # or similar; falling back to hostname keeps caching on.
        agent = _make_agent(
            provider="anthropic-direct",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.6",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)


class TestOpenRouter:
    def test_anthropic_cache_model_on_openrouter_uses_envelope_layout(self):
        # A model DECLARED to honour anthropic cache_control, routed via
        # OpenRouter, caches with the envelope layout.
        agent = _make_agent(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="anthropic/claude-sonnet-4.6",
            prompt_cache_style="anthropic",
        )
        should, native = agent._anthropic_prompt_cache_policy()
        assert should is True
        assert native is False  # OpenRouter uses envelope layout

    def test_undeclared_model_on_openrouter_does_not_cache(self):
        # No declared cache fact -> no caching, even on OpenRouter. This is
        # the behavior the VM hits on deploy before the sovereign write.
        agent = _make_agent(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="openai/gpt-5.4",
        )
        assert agent._anthropic_prompt_cache_policy() == (False, False)


class TestThirdPartyAnthropicGateway:
    """Third-party gateways speaking the Anthropic protocol (MiniMax, Zhipu GLM, LiteLLM)."""

    def test_declared_cache_model_via_anthropic_messages_caches_native(self):
        agent = _make_agent(
            provider="custom",
            base_url="https://api.minimax.io/anthropic",
            api_mode="anthropic_messages",
            model="claude-sonnet-4-6",
            prompt_cache_style="anthropic",
        )
        should, native = agent._anthropic_prompt_cache_policy()
        assert should is True, "Third-party Anthropic gateway with a declared cache model must cache"
        assert native is True, "Third-party Anthropic gateway uses native cache_control layout"

    def test_undeclared_on_unknown_anthropic_gateway_does_not_cache(self):
        # A provider exposing a model via anthropic_messages transport from a
        # host we don't recognize, with no declared cache fact — stay
        # conservative (we don't know whether it supports cache_control).
        agent = _make_agent(
            provider="custom",
            base_url="https://some-unknown-gateway.example.com/anthropic",
            api_mode="anthropic_messages",
            model="glm-4.5",
        )
        assert agent._anthropic_prompt_cache_policy() == (False, False)


class TestMiniMaxAnthropicWire:
    """MiniMax's own model family on its Anthropic-compatible endpoint.

    MiniMax documents cache_control support on ``/anthropic`` (0.1× read
    pricing, 5-minute TTL). This branch is PROVIDER/HOST-based
    (provenance-clean) — it caches regardless of the declared cache fact, so
    these tests carry default facts and still pass.
    """

    def test_minimax_m27_on_provider_minimax_caches_native_layout(self):
        agent = _make_agent(
            provider="minimax",
            base_url="https://api.minimax.io/anthropic",
            api_mode="anthropic_messages",
            model="minimax-m2.7",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)

    def test_minimax_m25_on_provider_minimax_cn_caches_native_layout(self):
        agent = _make_agent(
            provider="minimax-cn",
            base_url="https://api.minimaxi.com/anthropic",
            api_mode="anthropic_messages",
            model="minimax-m2.5",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)

    def test_custom_provider_pointed_at_minimax_host_caches(self):
        # User wires a custom provider manually at MiniMax's Anthropic URL;
        # host match alone should be sufficient to enable caching.
        agent = _make_agent(
            provider="custom",
            base_url="https://api.minimax.io/anthropic",
            api_mode="anthropic_messages",
            model="minimax-m2.7",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)

    def test_minimax_host_china_endpoint_caches(self):
        agent = _make_agent(
            provider="custom",
            base_url="https://api.minimaxi.com/anthropic",
            api_mode="anthropic_messages",
            model="minimax-m2.1",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, True)

    def test_minimax_provider_on_openai_wire_does_not_cache(self):
        # chat_completions transport — MiniMax's cache_control support is
        # documented only for the /anthropic endpoint. Stay off.
        agent = _make_agent(
            provider="minimax",
            base_url="https://api.minimax.io/v1",
            api_mode="chat_completions",
            model="minimax-m2.7",
        )
        assert agent._anthropic_prompt_cache_policy() == (False, False)


class TestAlibabaFamily:
    """Alibaba-family providers (OpenCode / OpenCode-Go / DashScope) honour
    cache_control on OpenAI-wire when the model is DECLARED to cache.

    The provider set stays a provenance-clean allow-list; the model no longer
    carries the signal via its name. Envelope layout because the wire format
    is OpenAI chat.completions.
    """

    def test_declared_model_on_opencode_go_caches_with_envelope_layout(self):
        agent = _make_agent(
            provider="opencode-go",
            base_url="https://opencode.ai/v1",
            api_mode="chat_completions",
            model="qwen3.6-plus",
            prompt_cache_style="anthropic",
        )
        should, native = agent._anthropic_prompt_cache_policy()
        assert should is True, "declared cache model on opencode-go must cache"
        assert native is False, "opencode-go is OpenAI-wire; envelope layout"

    def test_declared_model_on_opencode_zen_caches(self):
        agent = _make_agent(
            provider="opencode",
            base_url="https://opencode.ai/v1",
            api_mode="chat_completions",
            model="qwen3-coder-plus",
            prompt_cache_style="anthropic",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, False)

    def test_declared_model_on_direct_alibaba_caches(self):
        agent = _make_agent(
            provider="alibaba",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_mode="chat_completions",
            model="qwen3-coder",
            prompt_cache_style="anthropic",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, False)

    def test_declared_model_on_openrouter_caches_with_envelope_layout(self):
        agent = _make_agent(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="qwen/qwen3-coder",
            prompt_cache_style="anthropic",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, False)

    def test_declared_model_on_nous_portal_caches_with_envelope_layout(self):
        # Nous Portal proxies to OpenRouter — same envelope-layout path.
        agent = _make_agent(
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1",
            api_mode="chat_completions",
            model="qwen3.6-plus",
            prompt_cache_style="anthropic",
        )
        assert agent._anthropic_prompt_cache_policy() == (True, False)


class TestExplicitOverrides:
    """Policy accepts endpoint keyword overrides for switch_model / fallback.

    The provider / base_url / api_mode overrides still steer the layout
    decision (they are provenance-clean). The model-NAME override no longer
    flips caching — that was the excised name-inference path.
    """

    def test_endpoint_override_evaluated_independently_of_self(self):
        # Agent declared to honour anthropic cache, currently on native
        # Anthropic, but the policy is asked to evaluate the OpenRouter target
        # (as switch_model does before mutating self): layout flips native ->
        # envelope while the declared cache fact persists on the agent.
        agent = _make_agent(
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            model="claude-opus-4.6",
            prompt_cache_style="anthropic",
        )
        should, native = agent._anthropic_prompt_cache_policy(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="anthropic/claude-sonnet-4.6",
        )
        assert (should, native) == (True, False)
