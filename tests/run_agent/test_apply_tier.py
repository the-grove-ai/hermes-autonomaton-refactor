"""Tests for AIAgent.apply_tier — in-place cognitive tier swap (Sprint 14.1).

apply_tier is deliberately minimal: it assigns ``model`` and (when the
tier declares one) ``max_tokens`` and nothing else. The tests exercise
the method against a plain namespace, since that contract is the whole
of its behaviour.
"""

from types import SimpleNamespace

from run_agent import AIAgent


def test_apply_tier_swaps_model_and_budget():
    agent = SimpleNamespace(model="claude-sonnet-4-6", max_tokens=8192)
    AIAgent.apply_tier(agent, "claude-opus-4-6", 16384)
    assert agent.model == "claude-opus-4-6"
    assert agent.max_tokens == 16384


def test_apply_tier_none_max_tokens_keeps_budget():
    """max_tokens=None means the caller declared no budget — keep the
    current one rather than clearing it."""
    agent = SimpleNamespace(model="claude-sonnet-4-6", max_tokens=8192)
    AIAgent.apply_tier(agent, "claude-haiku-4-5-20251001", None)
    assert agent.model == "claude-haiku-4-5-20251001"
    assert agent.max_tokens == 8192
