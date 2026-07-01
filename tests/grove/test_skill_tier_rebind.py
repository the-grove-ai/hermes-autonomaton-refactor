"""R5 gate (AC-5') — the Dispatcher per-skill tier rebind, end to end.

Exercises the real _rebind_agent_for_skill + _bind_agent_to_tier + live router:
bound skill routes to its tier; operator config wins; two invoke_skill calls in
one turn with different bindings show NO bleed (skill B rebinds off A's tier);
specialty validates and no-ops; and the accepted trailing-tier is asserted as a
known, tested behavior (the last skill's tier is held on the turn marker).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import grove.router as router
from grove.router import get_tier_config, RoutingDecision
from grove.dispatcher import Dispatcher
from grove.capability import ModelBinding

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _init_router():
    router.initialize(REPO / "config" / "routing.config.yaml")


class MockAgent:
    """Minimal agent shell: apply_tier records the bound model (same-provider path)."""
    def __init__(self):
        self.provider = ""
        self.model = None
        self.max_tokens = None

    def apply_tier(self, model, max_tokens):
        self.model = model
        self.max_tokens = max_tokens

    def switch_model(self, **kw):
        self.model = kw.get("new_model")


def _dispatcher(turn_tier="T1"):
    d = object.__new__(Dispatcher)
    d._current_turn_routing_decision = RoutingDecision(
        tier=turn_tier, tier_config=get_tier_config(turn_tier),
        reason="default", confidence=None, pattern_cache_hit=False,
    )
    d._current_turn_skill_bound_tier = None
    return d


def _bind_cap(d, mapping):
    """Stub the skill->capability lookup with a name->ModelBinding|None mapping."""
    d._capability_for_skill = lambda name: SimpleNamespace(model_binding=mapping.get(name))


@pytest.fixture(autouse=True)
def _clean_operator_env(monkeypatch):
    monkeypatch.delenv("GROVE_TIER", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)


def test_bound_skill_routes_to_its_tier():
    d = _dispatcher(turn_tier="T1")
    _bind_cap(d, {"demo": ModelBinding("tier_override", "T2")})
    agent = MockAgent()
    assert d._rebind_agent_for_skill(agent, "demo") == "T2"
    assert agent.model == get_tier_config("T2").model
    assert d._current_turn_skill_bound_tier == "T2"


def test_operator_config_wins(monkeypatch):
    monkeypatch.setenv("GROVE_TIER", "T3")  # operator pins T3; the turn is on T3
    d = _dispatcher(turn_tier="T3")
    _bind_cap(d, {"demo": ModelBinding("tier_override", "T2")})
    agent = MockAgent()
    assert d._rebind_agent_for_skill(agent, "demo") == "T3"
    assert agent.model == get_tier_config("T3").model


def test_two_skills_one_turn_no_bleed():
    d = _dispatcher(turn_tier="T1")
    _bind_cap(d, {"A": ModelBinding("tier_override", "T2"), "B": None})
    agent = MockAgent()
    d._rebind_agent_for_skill(agent, "A")
    assert agent.model == get_tier_config("T2").model  # A -> T2
    d._rebind_agent_for_skill(agent, "B")
    # B has no binding: rebinds off A's T2 back to the turn default. NO bleed.
    assert agent.model == get_tier_config("T1").model
    assert d._current_turn_skill_bound_tier == "T1"


def test_specialty_no_ops_to_turn_default():
    d = _dispatcher(turn_tier="T1")
    _bind_cap(d, {"demo": ModelBinding("specialty")})
    agent = MockAgent()
    assert d._rebind_agent_for_skill(agent, "demo") == "T1"
    assert agent.model == get_tier_config("T1").model


def test_trailing_tier_held_after_last_skill():
    # ACCEPTED coarse semantic (documented, not a surprise): after the last
    # invoke_skill, the marker holds that skill's tier to turn end.
    d = _dispatcher(turn_tier="T1")
    _bind_cap(d, {"demo": ModelBinding("tier_override", "T2")})
    d._rebind_agent_for_skill(MockAgent(), "demo")
    assert d._current_turn_skill_bound_tier == "T2"


def test_batch_hook_rebinds_last_invoke_skill():
    d = _dispatcher(turn_tier="T1")
    _bind_cap(d, {"A": ModelBinding("tier_override", "T2"), "B": ModelBinding("tier_override", "T3")})
    agent = MockAgent()
    batch = [
        SimpleNamespace(tool_name="invoke_skill", arguments={"name": "A"}),
        SimpleNamespace(tool_name="read_file", arguments={"path": "x"}),
        SimpleNamespace(tool_name="invoke_skill", arguments={"name": "B"}),
    ]
    d._apply_skill_tier_binding(agent, batch)  # last invoke_skill (B) wins
    assert agent.model == get_tier_config("T3").model
    assert d._current_turn_skill_bound_tier == "T3"
