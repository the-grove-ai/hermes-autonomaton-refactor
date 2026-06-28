"""R2″ node-compositor-view-v1 Phase 2 — composition status accessor.

Tests ``grove.composition.declaration.get_composition_status`` against the
module-global composition state in ``tools.mcp_tool`` (the five dicts snapshot
under ``_lock``). No live MCP connection is required: each test sets the
globals in place (house style, matching test_connector_failure_andon.py) and
passes ``mcp_servers_config`` + ``zone_map`` explicitly so no disk read fires.
"""

from __future__ import annotations

import time

import pytest

import tools.mcp_tool as mt
from grove.composition.declaration import (
    NodeDeclaration,
    _zone_lookup_key,
    get_composition_status,
)


def _decl(node_id: str = "grove-browser", tools=None, endpoint="http://node:8830/mcp") -> NodeDeclaration:
    return NodeDeclaration(
        node_id=node_id,
        version="1.0.0",
        grv_standard="GRV-004",
        proposed_tools=tools or [],
        raw={"edge": {"node": node_id, "endpoint": endpoint, "tools": []}},
    )


@pytest.fixture(autouse=True)
def _reset_globals():
    """Clear all five snapshot globals before and after each test (in place,
    so get_composition_status reads the same dict objects)."""
    for d in (
        mt._composed_nodes,
        mt._servers,
        mt._server_connect_failed,
        mt._server_error_counts,
        mt._server_breaker_opened_at,
    ):
        d.clear()
    yield
    for d in (
        mt._composed_nodes,
        mt._servers,
        mt._server_connect_failed,
        mt._server_error_counts,
        mt._server_breaker_opened_at,
    ):
        d.clear()


# ── Composed + healthy node with tools ───────────────────────────────────────


def test_composed_healthy_node_with_tools():
    mt._composed_nodes["grove-browser"] = _decl(tools=[
        {"name": "browser_search", "proposed_zone": "green"},
        {"name": "browser_fetch_page", "proposed_zone": "green"},
    ])
    mt._servers["grove-browser"] = object()  # presence = connected
    zone_map = {
        "mcp_grove_browser_browser_search": "green",
        "mcp_grove_browser_browser_fetch_page": "green",
    }
    config = {"grove-browser": {"url": "http://100.80.12.118:8830/mcp"}}

    out = get_composition_status(mcp_servers_config=config, zone_map=zone_map)

    assert len(out) == 1
    node = out[0]
    assert node["server_name"] == "grove-browser"
    assert node["node_id"] == "grove-browser"
    assert node["grv_standard"] == "GRV-004"
    assert node["version"] == "1.0.0"
    assert node["is_composed"] is True
    assert node["is_connected"] is True
    assert node["health"] == "healthy"
    assert node["connect_failure_type"] is None
    assert node["error_count"] == 0
    assert node["url"] == "http://100.80.12.118:8830/mcp"
    assert len(node["tools"]) == 2
    assert node["tools"][0] == {
        "name": "browser_search", "proposed_zone": "green", "granted_zone": "green",
    }


# ── Dark node (in config, no declaration) — I4 ───────────────────────────────


def test_dark_node_in_config_no_declaration():
    config = {"notion": {"url": "https://mcp.notion.com"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    assert len(out) == 1
    node = out[0]
    assert node["server_name"] == "notion"
    assert node["is_composed"] is False
    assert node["node_id"] is None
    assert node["version"] is None
    assert node["grv_standard"] is None
    assert node["tools"] == []
    assert node["url"] == "https://mcp.notion.com"
    assert node["is_connected"] is False
    assert node["health"] == "healthy"


# ── Connect-failed node ──────────────────────────────────────────────────────


def test_connect_failed_node():
    mt._server_connect_failed["notion"] = "reauth"
    config = {"notion": {"url": "https://mcp.notion.com"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    node = out[0]
    assert node["health"] == "connect_failed"
    assert node["connect_failure_type"] == "reauth"


def test_connect_failed_wins_over_breaker():
    # Both signals present — connect_failed is reported (it is checked first).
    mt._server_connect_failed["grove-browser"] = "unreachable"
    mt._server_error_counts["grove-browser"] = 9
    mt._server_breaker_opened_at["grove-browser"] = time.monotonic()
    mt._composed_nodes["grove-browser"] = _decl()
    config = {"grove-browser": {"url": "http://x"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    assert out[0]["health"] == "connect_failed"


# ── Breaker-open node ────────────────────────────────────────────────────────


def test_breaker_open_node():
    mt._server_error_counts["grove-browser"] = 5  # >= threshold (3)
    mt._server_breaker_opened_at["grove-browser"] = time.monotonic()  # within cooldown
    mt._composed_nodes["grove-browser"] = _decl()
    config = {"grove-browser": {"url": "http://x"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    node = out[0]
    assert node["health"] == "breaker_open"
    assert node["error_count"] == 5


def test_breaker_cooldown_elapsed_is_healthy():
    # Count past threshold but cooldown window elapsed -> breaker closed -> healthy.
    mt._server_error_counts["grove-browser"] = 5
    mt._server_breaker_opened_at["grove-browser"] = (
        time.monotonic() - (mt._CIRCUIT_BREAKER_COOLDOWN_SEC + 10)
    )
    mt._composed_nodes["grove-browser"] = _decl()
    config = {"grove-browser": {"url": "http://x"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    assert out[0]["health"] == "healthy"


# ── Empty state (no servers configured) ──────────────────────────────────────


def test_empty_state_no_servers():
    out = get_composition_status(mcp_servers_config={}, zone_map={})
    assert out == []


# ── Zone map lookup: proposed != granted, == granted, missing — I3 ───────────


def test_zone_lookup_proposed_differs_from_granted():
    # Node proposes green; engine grants yellow — authority inversion visible.
    mt._composed_nodes["grove-browser"] = _decl(
        tools=[{"name": "browser_search", "proposed_zone": "green"}]
    )
    zone_map = {"mcp_grove_browser_browser_search": "yellow"}

    out = get_composition_status(mcp_servers_config={"grove-browser": {}}, zone_map=zone_map)

    tool = out[0]["tools"][0]
    assert tool["proposed_zone"] == "green"
    assert tool["granted_zone"] == "yellow"


def test_zone_lookup_proposed_equals_granted():
    mt._composed_nodes["grove-browser"] = _decl(
        tools=[{"name": "browser_search", "proposed_zone": "green"}]
    )
    zone_map = {"mcp_grove_browser_browser_search": "green"}

    out = get_composition_status(mcp_servers_config={"grove-browser": {}}, zone_map=zone_map)

    tool = out[0]["tools"][0]
    assert tool["proposed_zone"] == "green"
    assert tool["granted_zone"] == "green"


def test_zone_lookup_missing_entry_is_none():
    mt._composed_nodes["grove-browser"] = _decl(
        tools=[{"name": "mystery_tool", "proposed_zone": "red"}]
    )

    out = get_composition_status(mcp_servers_config={"grove-browser": {}}, zone_map={})

    tool = out[0]["tools"][0]
    assert tool["proposed_zone"] == "red"
    assert tool["granted_zone"] is None


# ── Helpers + union ordering ──────────────────────────────────────────────────


def test_zone_lookup_key_sanitizes_components():
    assert _zone_lookup_key("grove-browser", "browser_search") == \
        "mcp_grove_browser_browser_search"


def test_url_falls_back_to_declaration_endpoint():
    # No url in config -> use the declaration's self-declared edge endpoint.
    mt._composed_nodes["grove-browser"] = _decl(endpoint="http://node:8830/mcp")

    out = get_composition_status(mcp_servers_config={"grove-browser": {}}, zone_map={})

    assert out[0]["url"] == "http://node:8830/mcp"


def test_union_of_config_and_composed_sorted():
    # grove-browser composed but absent from config; notion dark in config.
    mt._composed_nodes["grove-browser"] = _decl()
    config = {"notion": {"url": "https://n"}}

    out = get_composition_status(mcp_servers_config=config, zone_map={})

    names = [n["server_name"] for n in out]
    assert names == ["grove-browser", "notion"]  # sorted union of both sources
    assert out[0]["is_composed"] is True
    assert out[1]["is_composed"] is False
