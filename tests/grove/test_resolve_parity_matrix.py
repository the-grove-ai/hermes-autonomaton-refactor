"""GRV-009 E5 C-RESOLVE — the registry resolver's disclosure-mode invariants.

The state×config intersection matrix (part 2) proved the registry resolver
reproduces the legacy group-level logic against a LIVE legacy oracle
(_resolve_intent_groups + _materialize over tool_groups.yaml). GRV-009 E5b C2
retired tool_groups.yaml, so the legacy oracle is gone and the matrix tests
retire with it — the committed offer-parity golden (test_offer_parity_snapshot)
is the ongoing offered-surface guard. What remains here is the part-3 mechanism
proof: the disclosure-mode invariants asserted directly against the resolver.
"""

from __future__ import annotations

import pytest

from grove.classify import COMPLEXITY_SIGNALS, INTENT_CLASSES
from grove.context_budget import resolve_tools_for_tier

# web-surface-admission-fix (Option B): the tier is bound via ``current_tier``
# (tier_rule.eligible is the sole gate); no per-tier ToolBudget is threaded.


def _native_surface():
    from hermes_cli.tools_config import _cli_registry

    reg = _cli_registry()
    names = sorted(n for n in {e.name for e in reg._tools.values()} if not n.startswith("mcp_"))
    return [{"type": "function", "function": {"name": n}} for n in names]


TOOLS = _native_surface()


def _names(res):
    return {t["function"]["name"] for t in res.tools}


# ── Disclosure-mode invariants (the mechanism, in code) ──────────────────────
# taxonomy arg is None — the resolver is registry-driven and ignores it (C-RETIRE).


def test_complexity_record_absent_on_simple_t3_present_on_complex():
    # web-surface-admission-fix (Option B) — the tier is bound via current_tier
    # (tier_rule.eligible is the sole gate); browser_navigate is a complexity
    # record eligible=[3], so it rides only a complex turn AT T3.
    simple = _names(resolve_tools_for_tier(TOOLS, "conversation", "simple", current_tier=3, mcp_allow=None))
    complex_ = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", current_tier=3, mcp_allow=None))
    assert "browser_navigate" not in simple, "complexity record leaked onto a low-complexity T3 turn"
    assert "browser_navigate" in complex_, "complexity record missing on a complex T3 turn"
    # neuter-tier-eligible-gate: on a COMPLEX turn the complexity record now also
    # rides at T1 — tier no longer strips. Complexity-disclosure gating (above)
    # is unchanged; only the tier ceiling is retired.
    t1c = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", current_tier=1, mcp_allow=None))
    assert "browser_navigate" in t1c   # tier ceiling retired → present at T1 on a complex turn


@pytest.mark.parametrize("fb_tool", ["spotify_search", "kanban_list", "discord", "computer_use"])
def test_fallback_record_absent_on_every_known_intent_present_only_in_fallback(fb_tool):
    for tier_int in (1, 2, 3):
        for intent in INTENT_CLASSES:
            for cx in COMPLEXITY_SIGNALS:
                got = _names(resolve_tools_for_tier(TOOLS, intent, cx, current_tier=tier_int, mcp_allow=None))
                assert fb_tool not in got, f"fallback record {fb_tool} leaked onto known cell T{tier_int}|{intent}|{cx}"
    t3_unknown = _names(resolve_tools_for_tier(TOOLS, None, "simple", current_tier=3, mcp_allow=None))
    assert fb_tool in t3_unknown, f"fallback record {fb_tool} missing from maximal unknown fallback"


def test_fallback_explicitly_t3_research_complex():
    got = _names(resolve_tools_for_tier(TOOLS, "research", "complex", current_tier=3, mcp_allow=None))
    # invoke_skill removed from this spot-check: invoke-skill-classification-hotfix-v1
    # flipped it to always:true/proactive, so it is now PRESENT on every classified
    # cell (asserted by test_invoke_skill_offered_on_every_recognized_intent below).
    for fb in ("spotify_search", "kanban_list", "computer_use", "todo"):
        assert fb not in got, f"{fb} (fallback) must be absent on T3|research|complex"


def test_invoke_skill_offered_on_every_recognized_intent():
    # invoke-skill-classification-hotfix-v1 regression guard. invoke_skill carried
    # disclosure:fallback — offered ONLY on the unknown maximal fallback, so a
    # classified turn ("forge the GoodRx application" -> system_admin) walled it off
    # (the live defect). Flipped to always:true/proactive, it is a core native verb
    # and MUST be offered on EVERY recognized intent at every tier — the exact
    # inverse of the fallback-absent invariant above.
    for tier_int in (1, 2, 3):
        for intent in INTENT_CLASSES:
            got = _names(resolve_tools_for_tier(TOOLS, intent, "moderate", current_tier=tier_int, mcp_allow=None))
            assert "invoke_skill" in got, f"invoke_skill missing on classified cell T{tier_int}|{intent}"
