"""GRV-009 E5 — offer-parity snapshot gate (D7).

The committed golden (``fixtures/offer_parity_snapshot.json``) is the native
offered surface from ``resolve_tools_for_tier`` across tier x intent x complexity,
captured at C-VERBS before the C-RESOLVE swap. C-VERBS records are inert, so this
asserts today's behavior. At C-RESOLVE the registry-driven resolver must reproduce
this byte-for-byte — any divergence is a parity violation and fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from grove.classify import COMPLEXITY_SIGNALS, INTENT_CLASSES
from grove.context_budget import load_taxonomy, resolve_tools_for_tier
from grove.tier_budget import load_tier_budgets

_REPO = Path(__file__).resolve().parents[2]
_GOLDEN = Path(__file__).parent / "fixtures" / "offer_parity_snapshot.json"


def _live_surface():
    from hermes_cli.tools_config import _cli_registry

    reg = _cli_registry()
    native = sorted(n for n in {e.name for e in reg._tools.values()} if not n.startswith("mcp_"))
    tools = [{"type": "function", "function": {"name": n}} for n in native]
    budgets = load_tier_budgets(_REPO / "config" / "routing.config.yaml",
                                taxonomy_path=_REPO / "config" / "tool_groups.yaml")
    tax = load_taxonomy(_REPO / "config" / "tool_groups.yaml")
    intents = list(INTENT_CLASSES) + ["__unknown__"]
    out = {}
    for tier in ("T1", "T2", "T3"):
        b = budgets[tier]
        for intent in intents:
            ic = None if intent == "__unknown__" else intent
            for cx in COMPLEXITY_SIGNALS:
                res = resolve_tools_for_tier(tools, ic, cx, tax, b, mcp_allow=None)
                out[f"{tier}|{intent}|{cx}"] = sorted(t["function"]["name"] for t in res.tools)
    return out


def test_offer_parity_matches_golden():
    golden = json.loads(_GOLDEN.read_text())
    live = _live_surface()
    # Same cells.
    assert set(live) == set(golden), (
        f"cell mismatch: +{sorted(set(live) - set(golden))[:5]} "
        f"-{sorted(set(golden) - set(live))[:5]}"
    )
    # Same offered surface per cell — the parity invariant.
    diffs = {k: (sorted(set(live[k]) - set(golden[k])), sorted(set(golden[k]) - set(live[k])))
             for k in golden if live[k] != golden[k]}
    assert not diffs, f"offer-parity divergence in {len(diffs)} cell(s): " + json.dumps(
        {k: {"added": a, "dropped": d} for k, (a, d) in list(diffs.items())[:8]}, indent=2
    )
