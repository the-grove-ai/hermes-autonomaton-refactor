"""binding-governance-surfaces-v1 P4 — once-per-session pin-refusal notice.

The Dispatcher sets an agent-resident pending payload the FIRST time a
session's skill invocation refuses a fleet-only model pin; the run_agent
answer-then-surface hook renders and consumes it. Proves:

* NOTICE ONCE — the first refusal stages the notice; the second invocation
  of the same skill does not (Dispatcher ``_binding_refusal_notified``
  seen-set dedup).
* PER SKILL — a different pinned skill notices independently.
* RENDER — the hook appends the notice after the answer and consumes the
  pending payload; a notice-less turn passes through byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import grove.router as router
from grove.capability import ModelBinding
from grove.dispatcher import Dispatcher

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _init_router():
    router.initialize(REPO / "config" / "routing.config.yaml")


def _shell_dispatcher():
    d = object.__new__(Dispatcher)
    d._current_turn_routing_decision = SimpleNamespace(tier="T1")
    d._current_turn_skill_bound_tier = None
    d._binding_refusal_notified = set()
    return d


def _pinned_cap():
    return SimpleNamespace(
        model_binding=ModelBinding(type="model", model="pin-org/pin-model"),
    )


def _agent():
    return SimpleNamespace(
        provider="", apply_tier=lambda *a, **k: None,
        _binding_refusal_notice=None,
    )


def test_notice_fires_once_then_dedups():
    d = _shell_dispatcher()
    d._capability_for_skill = lambda name: _pinned_cap()
    agent = _agent()

    d._rebind_agent_for_skill(agent, "forge-jobsearch")
    notice = agent._binding_refusal_notice
    assert notice == {
        "skill": "forge-jobsearch", "model": "pin-org/pin-model", "tier": "T1",
    }

    # Second invocation of the SAME skill this session — no new notice.
    agent._binding_refusal_notice = None
    d._rebind_agent_for_skill(agent, "forge-jobsearch")
    assert agent._binding_refusal_notice is None


def test_notice_is_per_skill():
    d = _shell_dispatcher()
    d._capability_for_skill = lambda name: _pinned_cap()
    agent = _agent()

    d._rebind_agent_for_skill(agent, "skill-a")
    assert agent._binding_refusal_notice["skill"] == "skill-a"
    agent._binding_refusal_notice = None
    d._rebind_agent_for_skill(agent, "skill-b")
    assert agent._binding_refusal_notice["skill"] == "skill-b"


def test_no_notice_without_refusal():
    d = _shell_dispatcher()
    d._capability_for_skill = lambda name: SimpleNamespace(model_binding=None)
    agent = _agent()
    d._rebind_agent_for_skill(agent, "unbound-skill")
    assert agent._binding_refusal_notice is None
    assert d._binding_refusal_notified == set()


# ── the answer-then-surface hook ─────────────────────────────────────────────


def _hook_agent():
    from run_agent import AIAgent
    a = object.__new__(AIAgent)
    return a


def test_hook_appends_and_consumes():
    from run_agent import AIAgent

    a = _hook_agent()
    a._binding_refusal_notice = {
        "skill": "forge-jobsearch", "model": "pin-org/pin-model", "tier": "T2",
    }
    out = AIAgent._append_binding_refusal_notice(a, "the answer")
    assert out.startswith("the answer")                 # answer-then-surface
    assert "`forge-jobsearch` is pinned to pin-org/pin-model" in out
    assert "turn tier T2" in out
    assert a._binding_refusal_notice is None            # consumed


def test_hook_passthrough_without_notice():
    from run_agent import AIAgent

    a = _hook_agent()
    assert AIAgent._append_binding_refusal_notice(a, "answer") == "answer"
    a._binding_refusal_notice = None
    assert AIAgent._append_binding_refusal_notice(a, "answer") == "answer"


def test_hook_never_touches_empty_response():
    from run_agent import AIAgent

    a = _hook_agent()
    a._binding_refusal_notice = {"skill": "s", "model": "m", "tier": "T1"}
    assert AIAgent._append_binding_refusal_notice(a, "") == ""
    # Pending payload survives an empty-response turn (nothing surfaced).
    assert a._binding_refusal_notice is not None
