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

from pathlib import Path

import pytest

from grove.classify import COMPLEXITY_SIGNALS, INTENT_CLASSES
from grove.context_budget import resolve_tools_for_tier
from grove.tier_budget import load_tier_budgets

_REPO = Path(__file__).resolve().parents[2]
_CFG = _REPO / "config" / "routing.config.yaml"

BUDGETS = load_tier_budgets(_CFG)
T3 = BUDGETS["T3"]


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
    simple = _names(resolve_tools_for_tier(TOOLS, "conversation", "simple", None, T3, mcp_allow=None))
    complex_ = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", None, T3, mcp_allow=None))
    assert "browser_navigate" not in simple, "complexity record leaked onto a low-complexity T3 turn"
    assert "browser_navigate" in complex_, "complexity record missing on a complex T3 turn"
    t1c = _names(resolve_tools_for_tier(TOOLS, "conversation", "complex", None, BUDGETS["T1"], mcp_allow=None))
    assert "browser_navigate" not in t1c


@pytest.mark.parametrize("fb_tool", ["spotify_search", "kanban_list", "discord", "computer_use"])
def test_fallback_record_absent_on_every_known_intent_present_only_in_fallback(fb_tool):
    for tname in ("T1", "T2", "T3"):
        b = BUDGETS[tname]
        for intent in INTENT_CLASSES:
            for cx in COMPLEXITY_SIGNALS:
                got = _names(resolve_tools_for_tier(TOOLS, intent, cx, None, b, mcp_allow=None))
                assert fb_tool not in got, f"fallback record {fb_tool} leaked onto known cell {tname}|{intent}|{cx}"
    t3_unknown = _names(resolve_tools_for_tier(TOOLS, None, "simple", None, T3, mcp_allow=None))
    assert fb_tool in t3_unknown, f"fallback record {fb_tool} missing from maximal unknown fallback"


def test_fallback_explicitly_t3_research_complex():
    got = _names(resolve_tools_for_tier(TOOLS, "research", "complex", None, T3, mcp_allow=None))
    for fb in ("spotify_search", "kanban_list", "computer_use", "todo", "invoke_skill"):
        assert fb not in got, f"{fb} (fallback) must be absent on T3|research|complex"
