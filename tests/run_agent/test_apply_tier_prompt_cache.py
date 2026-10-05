"""apply_tier re-evaluates the prompt-cache decision for the newly bound model.

The decision was made once at construction, so an agent built on a non-Claude
tier and then tier-swapped to a Claude model (same OpenRouter client) sent no
cache_control markers and re-billed the full prompt on every call.
"""

from __future__ import annotations

import run_agent
from grove.router import ModelFacts


def _openrouter_agent():
    agent = object.__new__(run_agent.AIAgent)
    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_mode = "chat_completions"
    agent.model = "z-ai/glm-5.2"
    agent.max_tokens = 8192
    agent._model_facts = ModelFacts(declared=True)        # prompt_cache_style "none"
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    return agent


def test_swap_to_a_cache_marker_model_turns_caching_on():
    agent = _openrouter_agent()
    agent.apply_tier(
        "anthropic/claude-sonnet-5", 8192,
        model_facts=ModelFacts(declared=True, prompt_cache_style="anthropic"),
    )
    assert agent._use_prompt_caching is True
    assert agent._use_native_cache_layout is False        # OpenRouter envelope layout


def test_swap_away_turns_caching_off_again():
    agent = _openrouter_agent()
    agent.apply_tier(
        "anthropic/claude-sonnet-5", 8192,
        model_facts=ModelFacts(declared=True, prompt_cache_style="anthropic"),
    )
    agent.apply_tier("z-ai/glm-5.3", 8192, model_facts=ModelFacts(declared=True))
    assert agent._use_prompt_caching is False


def test_swap_with_no_facts_is_safe_default_off():
    agent = _openrouter_agent()
    agent._use_prompt_caching = True                       # stale from a prior model
    agent.apply_tier("some/undeclared-model", None)
    assert agent._use_prompt_caching is False
    assert agent.max_tokens == 8192                        # None keeps the budget
