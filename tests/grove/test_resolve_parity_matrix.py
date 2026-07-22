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

from grove.classify import INTENT_CLASSES
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
    simple = _names(resolve_tools_for_tier(TOOLS, "conversation", "simple", mcp_allow=None))
    complex_ = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", mcp_allow=None))
    assert "browser_navigate" not in simple, "complexity record leaked onto a low-complexity T3 turn"
    assert "browser_navigate" in complex_, "complexity record missing on a complex T3 turn"
    # neuter-tier-eligible-gate: on a COMPLEX turn the complexity record now also
    # rides at T1 — tier no longer strips. Complexity-disclosure gating (above)
    # is unchanged; only the tier ceiling is retired.
    t1c = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", mcp_allow=None))
    assert "browser_navigate" in t1c   # tier ceiling retired → present at T1 on a complex turn


# fallback-retirement-v1 retired disclosure:fallback. The former "fallback record
# absent on every known intent, present only in the unknown fallback" invariant no
# longer holds — those records migrated to proactive (Class A: always:true core;
# Class B2/C: intent-gated). The two tests below assert the INVERSE, the behavior
# the migration installed.


@pytest.mark.parametrize("core_tool", ["kanban_list", "todo"])
def test_migrated_class_a_present_on_every_intent(core_tool):
    # Class A (todo, send_message, kanban_read/write) flipped to always:true —
    # core native verbs offered on EVERY recognized intent at every tier.
    for tier_int in (1, 2, 3):
        for intent in INTENT_CLASSES:
            got = _names(resolve_tools_for_tier(TOOLS, intent, "moderate", mcp_allow=None))
            assert core_tool in got, f"{core_tool} missing on classified cell T{tier_int}|{intent}"


def test_migrated_intent_gated_records_ride_only_their_intent():
    # Class B2/C (spotify_write, discord, homeassistant_read, discord_admin,
    # computer_use) became proactive+intent-gated: present iff the intent matches,
    # absent on unrelated classified turns (no longer fallback-only).
    sysadmin = _names(resolve_tools_for_tier(TOOLS, "system_admin", "moderate", mcp_allow=None))
    messaging = _names(resolve_tools_for_tier(TOOLS, "messaging", "moderate", mcp_allow=None))
    research = _names(resolve_tools_for_tier(TOOLS, "research", "moderate", mcp_allow=None))
    assert "spotify_search" in sysadmin           # spotify_write intents=[system_admin]
    assert "spotify_search" not in messaging
    assert "discord" in messaging                  # discord intents=[messaging]
    assert "discord" not in research


def test_invoke_skill_offered_on_every_recognized_intent():
    # invoke-skill-classification-hotfix-v1 regression guard. invoke_skill carried
    # disclosure:fallback — offered ONLY on the unknown maximal fallback, so a
    # classified turn ("forge the GoodRx application" -> system_admin) walled it off
    # (the live defect). Flipped to always:true/proactive, it is a core native verb
    # and MUST be offered on EVERY recognized intent at every tier — the exact
    # inverse of the fallback-absent invariant above.
    for tier_int in (1, 2, 3):
        for intent in INTENT_CLASSES:
            got = _names(resolve_tools_for_tier(TOOLS, intent, "moderate", mcp_allow=None))
            assert "invoke_skill" in got, f"invoke_skill missing on classified cell T{tier_int}|{intent}"
