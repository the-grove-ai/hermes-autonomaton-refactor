"""C-SEAM5 web_search admission — targeted intent widen (preserves the cost HOLD).

The offered-surface gate is intent-match against the web_search capability
record, NOT the routing tier (the tier gate is inert since fallback-retirement-v1
Phase 2; web_search.tier_rule.eligible is [1,2,3]). web_search is deliberately
held at always: false — a cost-gating decision (paid search API) from
tool-admission-widening-v1 (2d97a8eea). The C-SEAM5 block reported at T1/T2 was
NOT a tier bug: the classifier returned creative_writing / memory_operation for
the operator's research turns, and those intents were absent from
web_search.trigger.intents.

Fix (targeted, cost-HOLD-preserving): ADD creative_writing + memory_operation to
web_search.trigger.intents — the operator's press-pitch and contact-lookup
contexts — WITHOUT flipping always: true. Pure conversation and unknown turns
still withhold the paid tool, honoring the HOLD.
"""
from __future__ import annotations

import pytest

from grove.capability_registry import load_capabilities
from grove.context_budget import (
    _is_mcp,
    _registry_allowed_names,
    reset_caps_index_cache,
    resolve_tools_for_tier,
)

# The operator's research contexts that must now offer web_search.
WIDENED_INTENTS = ["creative_writing", "memory_operation"]
# Cost-HOLD guard: these must STILL withhold web_search (always:false preserved).
HELD_INTENTS = ["conversation"]
ALL_TIERS = [1, 2, 3]


@pytest.fixture(autouse=True)
def _fresh_caps_projection():
    # The offered-surface resolver caches its registry projection; drop it so each
    # test reflects the on-disk records.
    reset_caps_index_cache()
    yield
    reset_caps_index_cache()


def _all_native_tools():
    names = set()
    for c in load_capabilities().values():
        for t in c.bindings.tools:
            if not _is_mcp(t):
                names.add(t)
    return [{"type": "function", "function": {"name": n}} for n in sorted(names)]


# ── characterization — the gate is intent, not tier ────────────────────────

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_web_search_offered_for_research_at_every_tier(tier):
    # research is a pre-existing web_search intent, offered at every tier —
    # proving tier does not gate (eligible=[1,2,3], gate inert).
    res = resolve_tools_for_tier(
        _all_native_tools(), "research", "moderate", current_tier=tier
    )
    assert "web_search" in res.allowed_names, (
        f"web_search must be offered for research at T{tier}"
    )


def test_web_search_eligible_at_all_tiers_in_record():
    # The record proves tier is not the lever: eligible spans every tier.
    rec = load_capabilities()["web_search"]
    assert set(rec.tier_rule.eligible) == {1, 2, 3}


# ── the fix: widened intents offered; cost HOLD preserved ──────────────────

@pytest.mark.parametrize("tier", ALL_TIERS)
@pytest.mark.parametrize("intent", WIDENED_INTENTS)
def test_web_search_offered_for_widened_intents_at_every_tier(intent, tier):
    # RED until web_search.yaml adds creative_writing + memory_operation. These
    # are the operator's press-pitch (creative_writing) and contact-lookup
    # (memory_operation) research contexts — unblock them at every tier.
    res = resolve_tools_for_tier(
        _all_native_tools(), intent, "moderate", current_tier=tier
    )
    assert "web_search" in res.allowed_names, (
        f"web_search must be offered for {intent!r} at T{tier}"
    )


@pytest.mark.parametrize("intent", HELD_INTENTS)
def test_web_search_still_withheld_on_held_intents(intent):
    # Cost HOLD preserved: web_search stays always:false, so an intent that names
    # none of its triggers (e.g. pure conversation) must NOT offer the paid tool.
    reset_caps_index_cache()
    allowed, _ = _registry_allowed_names(
        intent_class=intent, complexity_signal="simple", current_tier=None
    )
    assert "web_search" not in allowed, (
        f"web_search must stay withheld on {intent!r} (cost HOLD, always:false)"
    )


def test_web_search_still_withheld_on_unknown_intent():
    # Andon-on-uncertainty: an unknown turn admits always:true CORE only.
    # web_search is always:false, so it must NOT ride an unknown turn.
    reset_caps_index_cache()
    allowed, _ = _registry_allowed_names(
        intent_class="unknown", complexity_signal="simple", current_tier=None
    )
    assert "web_search" not in allowed


def test_web_search_record_stays_always_false_with_widened_intents():
    # The fix is surgical: always stays False (cost HOLD), intents gain exactly
    # the two operator research contexts.
    rec = load_capabilities()["web_search"]
    assert rec.trigger.always is False
    assert {"creative_writing", "memory_operation"} <= set(rec.trigger.intents)
    assert "conversation" not in rec.trigger.intents
