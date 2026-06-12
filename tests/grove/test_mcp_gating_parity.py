"""GRV-009 E4 C2 — registry-driven MCP gating parity.

Proves per-tier x per-entrypoint MCP tool exposure is IDENTICAL under the
legacy gate (manifest mcp_allow + exclude_mcp) and the registry gate
(kind=mcp Capability records: tier_rule.eligible + trigger), with explicit
T1 AND T2 MCP-free assertions and a T3 match/no-match disclosure pair.

Both gates are flip-ON in this env: load_manifest falls back to the repo
config/manifest.yaml (notion unit) and load_capabilities reads the repo
config/capabilities (notion records) — so the comparison is real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.context_budget import load_taxonomy, resolve_tools_for_tier, _is_mcp, _name_of
from grove.tier_budget import load_tier_budgets
from grove.manifest import load_manifest, mcp_match_reasons

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


def _legacy_allow(intent, message):
    """The pre-migration mcp_allow: manifest disclose-on-match (no tier fold-in;
    the ceiling is exclude_mcp, applied by resolve_tools_for_tier)."""
    units = load_manifest()
    return set(mcp_match_reasons(units, intent_class=intent, message=message))


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
@pytest.mark.parametrize("tier", ["T1", "T2", "T3"])
@pytest.mark.parametrize("label,intent,message,_t3", TURNS)
def test_legacy_and_registry_exposure_identical(platform, tier, label, intent, message, _t3):
    surface = _native_surface(platform)
    legacy = _mcp_exposed(surface, tier, intent, message, _legacy_allow(intent, message))
    registry = _mcp_exposed(surface, tier, intent, message, _registry_allow(tier, intent, message))
    assert legacy == registry, f"{platform}/{tier}/{label}: legacy={legacy} registry={registry}"


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
