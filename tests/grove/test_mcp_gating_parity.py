"""GRV-009 E4 — registry-driven MCP gating parity.

Per-tier x per-entrypoint MCP tool exposure under the registry gate (kind=mcp
Capability records: tier_rule.eligible + trigger): explicit T1 AND T2 MCP-free
assertions and a T3 match/no-match disclosure pair.

C2 proved the registry gate IDENTICAL to the legacy gate (manifest mcp_allow +
exclude_mcp); C4 retired the legacy gate, so this file now asserts the registry
behavior directly (the legacy comparison is gone with the code it compared).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.context_budget import load_taxonomy, resolve_tools_for_tier, _is_mcp, _name_of
from grove.tier_budget import load_tier_budgets

REPO = Path(__file__).resolve().parents[2]
TAX = load_taxonomy(REPO / "config" / "tool_groups.yaml")
BUDGETS = load_tier_budgets()

MCP_READ = "mcp_notion_notion_search"
MCP_WRITE = "mcp_notion_notion_create_pages"


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": ""}}


def _native_surface(platform):
    """The platform's native construction surface + the two synthetic notion
    MCP tools (which arrive from the live MCP connection in production)."""
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import ToolRegistry, register_builtin_tools
    from model_tools import get_tool_definitions

    reg = ToolRegistry(); register_builtin_tools(reg)
    ets = sorted(_get_platform_tools({}, platform))
    tools = get_tool_definitions(reg, enabled_toolsets=ets, disabled_toolsets=[], quiet_mode=True)
    return list(tools) + [_tool(MCP_READ), _tool(MCP_WRITE)]


def _registry_allow(tier, intent, message):
    """The migrated mcp_allow: the new registry-driven _compute_mcp_allow."""
    import run_agent
    import grove.providers as P
    a = object.__new__(run_agent.AIAgent)
    a.tools = []
    a._current_messages = [{"role": "user", "content": message}]
    P._last_routed_tier = tier
    return a._compute_mcp_allow(intent, None)


def _mcp_exposed(surface, tier, intent, message, allow):
    res = resolve_tools_for_tier(surface, intent, "moderate", TAX, BUDGETS[tier], mcp_allow=allow)
    return {_name_of(t) for t in res.tools if _is_mcp(_name_of(t))}


# Turn shapes: (label, intent, message, expect_notion_on_T3)
TURNS = [
    ("intent_match", "research", "summarize the findings", True),
    ("keyword_match", "conversation", "search my notion workspace", True),
    ("no_match", "conversation", "what is the weather today", False),
]
PLATFORMS = ["telegram", "cli"]


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("tier", ["T1", "T2"])
@pytest.mark.parametrize("label,intent,message,_t3", TURNS)
def test_T1_and_T2_are_mcp_free_under_registry(platform, tier, label, intent, message, _t3):
    surface = _native_surface(platform)
    exposed = _mcp_exposed(surface, tier, intent, message, _registry_allow(tier, intent, message))
    assert exposed == set(), f"{platform}/{tier}/{label} leaked MCP: {exposed}"


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("label,intent,message,expect", TURNS)
def test_T3_match_no_match_disclosure_pair(platform, label, intent, message, expect):
    surface = _native_surface(platform)
    exposed = _mcp_exposed(surface, "T3", intent, message, _registry_allow("T3", intent, message))
    if expect:
        assert {MCP_READ, MCP_WRITE} <= exposed, f"{platform}/{label}: T3 should disclose notion, got {exposed}"
    else:
        assert exposed == set(), f"{platform}/{label}: T3 no-match should withhold notion, got {exposed}"
