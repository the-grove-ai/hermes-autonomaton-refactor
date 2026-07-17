"""web-surface-admission-fix (Option B) — the offered surface ≡ the execution
seam, and the re-sourced D8 escalation.

neuter-tier-eligible-gate: the tier_rule.eligible gate is retired in the filter;
tool-admission-deadcode-removal-v1 then deleted the dead _seam5_tier_refusal seam
entirely. This file proves the two invariants that survive:

1. builder strips nothing on tier — the offered-surface builder
   (resolve_tools_for_tier) IGNORES tier, so it strips nothing at any tier and
   the offered surface IS the execution boundary (the PRIMARY offered-surface
   seam; there is no longer a separate tier seam to diverge from).

2. D8 escalation, re-sourced onto stripped_capabilities: a stripped cap escalates
   ONCE to the minimum covering tier; a null intersection fails loud naming the
   caps; the loop invariant (target covers the whole stripped set) holds.

Driven off the LIVE record corpus so it tracks the real eligibility the seam
enforces.
"""

from __future__ import annotations

import pytest

from grove.capability_registry import load_capabilities
from grove.context_budget import (
    _is_mcp,
    min_covering_tier,
    resolve_tools_for_tier,
)

TAXONOMY = {"version": 1, "core": [], "domain_chunks": {}, "exploratory": []}


def _seam_eligible_map():
    """tool name -> set(tier_rule.eligible) from the live record corpus — the
    (now-documentary) per-tool eligibility the victim-table test samples."""
    m = {}
    for c in load_capabilities().values():
        for t in c.bindings.tools:
            if not _is_mcp(t):
                m[t] = set(c.tier_rule.eligible)
    return m


def _all_native_tools():
    names = sorted(_seam_eligible_map())
    return [{"type": "function", "function": {"name": n}} for n in names]


SAMPLE_INTENTS = [
    "research", "retrieval", "analysis", "factual_lookup",
    "code_generation", "debugging", "planning", "memory_operation",
]


# ── invariant 1: builder ⊆ seam (no offered tool the seam would refuse) ─────


@pytest.mark.parametrize("tier", [1, 2, 3])
@pytest.mark.parametrize("intent", SAMPLE_INTENTS)
def test_builder_strips_nothing_on_tier_at_any_tier(tier, intent):
    # neuter-tier-eligible-gate (and tool-admission-deadcode-removal-v1, which
    # deleted the dead _seam5_tier_refusal seam): the builder IGNORES tier, so it
    # strips nothing at any tier and the offered surface IS the execution boundary
    # (the surviving PRIMARY offered-surface seam). Assert the builder offers a
    # non-empty surface and never tier-strips.
    res = resolve_tools_for_tier(
        _all_native_tools(), intent, "moderate", current_tier=tier
    )
    assert res.stripped_capabilities == frozenset(), (
        f"builder tier-stripped at T{tier} for {intent!r}; the tier gate is retired"
    )
    assert res.allowed_names, f"builder offered nothing for {intent!r} at T{tier}"


def test_victim_table_offered_at_eligible_tiers():
    # The web-verb victims are OFFERED at their eligible tiers on a triggering
    # intent — the regression (refused at execute) is closed.
    #
    # test-baseline-hygiene-v2 FG-5: load_capabilities() is uncached (~1.9s/call).
    # The original derived seam/tools/intent via three helpers that each reloaded
    # the corpus — 17 loads in one test body (2 upfront + 15 in the victim x tier
    # loop via _a_triggering_intent) — deterministically crossing the 30s guard.
    # Snapshot the corpus ONCE and derive both maps from it, reproducing the
    # helpers' semantics exactly: seam is last-writer-wins per tool; the triggering
    # intent is the first record carrying the tool with a non-empty intent list.
    # (The uncached-load latency itself is banked to capability-hot-reload — the
    # remedy is a process-level cache in production, not a stub here.)
    seam: dict = {}
    triggering: dict = {}
    for c in load_capabilities().values():
        for t in c.bindings.tools:
            if _is_mcp(t):
                continue
            seam[t] = set(c.tier_rule.eligible)
            if t not in triggering:
                ints = list(c.trigger.intents)
                if ints:
                    triggering[t] = ints[0]
    tools = [{"type": "function", "function": {"name": n}} for n in sorted(seam)]
    victims = ["web_search", "web_extract", "session_search", "search_files",
              "write_file"]
    for name in victims:
        if name not in seam:
            continue
        for tier in sorted(seam[name]):
            # an intent the record actually triggers on (precomputed above)
            res = resolve_tools_for_tier(
                tools, triggering.get(name, "research"), "moderate",
                current_tier=tier,
            )
            assert name in res.allowed_names, (
                f"{name} should be OFFERED at its eligible T{tier}"
            )


def _a_triggering_intent(tool_name):
    for c in load_capabilities().values():
        if tool_name in c.bindings.tools:
            ints = list(c.trigger.intents)
            if ints:
                return ints[0]
    return "research"


def test_research_at_t2_strips_nothing_after_widen():
    # research-tier-widen: x_search/search_files/session_search/web_search are all
    # eligible at T2 now (x_search widened [3]->[1,2,3] — restricted by accident),
    # so a research turn at T2 strips NOTHING → stripped_capabilities is empty →
    # the D8 strip-escalation never raises (dissolved at the root, not handled).
    res = resolve_tools_for_tier(
        _all_native_tools(), "research", "moderate", current_tier=2
    )
    assert res.stripped_capabilities == frozenset(), (
        "research @ T2 must strip nothing after the widen (no D8 escalation)"
    )
    assert "x_search" in res.allowed_names      # now served at T2
    assert "web_search" in res.allowed_names
    assert "session_search" in res.allowed_names


def test_research_at_t1_strips_nothing_after_widen():
    # The widen also covers T1: every research-family native verb is eligible at
    # T1, so a T1 research turn strips nothing either.
    res = resolve_tools_for_tier(
        _all_native_tools(), "research", "moderate", current_tier=1
    )
    assert res.stripped_capabilities == frozenset()
    assert "x_search" in res.allowed_names
    assert "search_files" in res.allowed_names


def test_none_tier_admits_all_intent_matched_like_the_seam():
    # No tier routed (cloud): the eligibility gate is bypassed — the seam admits
    # the no-tier path too (tier is None ⇒ admit).
    res = resolve_tools_for_tier(
        _all_native_tools(), "research", "moderate", current_tier=None
    )
    assert "x_search" in res.allowed_names      # eligible-[3] tool admitted (no gate)
    assert res.stripped_capabilities == frozenset()


# ── invariant 2: D8 re-sourced onto stripped_capabilities ──────────────────


def test_min_covering_tier_single_jump():
    # one cap eligible only at T3, stripped at T2 -> jump to T3 (the minimum).
    stripped = frozenset({("x_search", (3,))})
    assert min_covering_tier(stripped, 2) == 3


def test_min_covering_tier_picks_minimum_over_set():
    # two caps; the minimum tier covering BOTH (intersection ≥ current).
    stripped = frozenset({("a", (2, 3)), ("b", (3,))})
    assert min_covering_tier(stripped, 1) == 3      # only 3 covers both
    stripped2 = frozenset({("a", (1, 2, 3)), ("b", (2, 3))})
    assert min_covering_tier(stripped2, 1) == 2     # 2 is the min covering both


def test_min_covering_tier_null_intersection_is_none():
    # disjoint eligible sets — no single tier covers both -> fail-loud signal.
    stripped = frozenset({("a", (2,)), ("b", (3,))})
    assert min_covering_tier(stripped, 1) is None


def test_min_covering_tier_eligible_only_below_current_is_none():
    # a cap eligible only BELOW the current tier can never be covered going up.
    stripped = frozenset({("legacy_only", (1,))})
    assert min_covering_tier(stripped, 2) is None


def test_min_covering_tier_nothing_stripped_returns_current():
    assert min_covering_tier(frozenset(), 2) == 2


def test_loop_invariant_target_covers_whole_stripped_set():
    # The invariant the generator asserts before invoking the LLM: at the target
    # tier EVERY stripped cap is eligible (target drawn from the intersection).
    stripped = frozenset({("a", (2, 3)), ("b", (3,)), ("c", (1, 2, 3))})
    target = min_covering_tier(stripped, 1)
    assert target is not None
    assert all(target in set(elig) for (_cid, elig) in stripped)
