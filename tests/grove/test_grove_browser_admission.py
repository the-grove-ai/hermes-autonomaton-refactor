"""browser-read-surface-v1 R3 — grove-browser MCP admission parity.

Proves the ADMISSION layer, distinct from the R2 zone grant: the
``grove_browser_read`` kind=mcp capability record is the sole authority on
whether the grove-browser server's five read tools are offered on the gateway
path. Mirrors tests/grove/test_mcp_gating_parity.py — load the live registry,
compute _compute_mcp_allow, resolve tools, assert exposure — so this is proof
of admission, not just that a YAML file parses.

Server-id note: the tools register as mcp_grove_browser_* and _mcp_server_of
splits on the first underscore -> "grove"; the record's mcp_schema pointer is
"grove" to bind. See the record header for the collision caveat (any future
grove-* MCP server would also parse to "grove").
"""

from __future__ import annotations

from pathlib import Path

from grove.context_budget import resolve_tools_for_tier, _is_mcp, _name_of
from grove.zones import ZoneClassifier

REPO = Path(__file__).resolve().parents[2]

GB_TOOLS = [
    "mcp_grove_browser_browser_search",
    "mcp_grove_browser_browser_read_page",
    "mcp_grove_browser_browser_extract",
    "mcp_grove_browser_browser_screenshot",
    "mcp_grove_browser_browser_session",
]


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": ""}}


def _registry_allow(tier, intent, message):
    """The live registry-driven mcp_allow (mirror of the parity harness)."""
    import run_agent
    import grove.providers as P

    a = object.__new__(run_agent.AIAgent)
    a.tools = [_tool(n) for n in GB_TOOLS]
    a._current_messages = [{"role": "user", "content": message}]
    P._last_routed_tier = tier
    return a._compute_mcp_allow(intent, None)


def _mcp_exposed(surface, intent, message, allow):
    res = resolve_tools_for_tier(surface, intent, "moderate", mcp_allow=allow)
    return {_name_of(t) for t in res.tools if _is_mcp(_name_of(t))}


def test_grove_browser_admitted_on_always_on_turn():
    """always:true — the five read tools disclose even on a turn matching no
    intent and no keyword (the ungated Green read control surface)."""
    surface = [_tool(n) for n in GB_TOOLS]
    allow = _registry_allow("T1", "conversation", "what is the weather today")
    assert "grove" in allow, f"grove-browser server not admitted; allow={sorted(allow)}"
    exposed = _mcp_exposed(surface, "conversation", "what is the weather today", allow)
    assert set(GB_TOOLS) <= exposed, f"missing {set(GB_TOOLS) - exposed}"


def test_grove_browser_tools_green_at_dispatch():
    """R3 gate is 'admitted AND Green' — the zones authority grants every read
    tool Green at dispatch (parity with the admission record's zone: green)."""
    clf = ZoneClassifier(REPO / "config" / "zones.schema.yaml")
    for name in GB_TOOLS:
        assert clf.classify(name).zone == "green", f"{name} not green at dispatch"


def test_record_owns_exactly_the_five_read_tools():
    """Guards binding drift: the record owns the five read tools and nothing
    else (strict 1:1 tool ownership; no write tool ever admitted here)."""
    from grove.capability_registry import load_capabilities
    from grove.capability import CapabilityKind

    rec = load_capabilities().get("grove_browser_read")
    assert rec is not None and rec.kind == CapabilityKind.MCP
    assert sorted(rec.bindings.tools) == sorted(GB_TOOLS)
    assert rec.trigger.always is True
    assert rec.zone.value == "green"
