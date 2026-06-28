"""R2″ node-compositor-view-v1 Phase 3 — composition nodes JSON API endpoint.

Covers ``GET /api/substrate/composition/nodes``: the response envelope shape,
that the handler passes pre-loaded ``zone_map`` + ``mcp_servers_config`` into
the accessor (C3), and that the zone map is mtime-cached (read once, not per
request). The accessor itself (``get_composition_status``) is mocked here — its
behaviour is covered by tests/test_composition_status.py.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import grove.api.composition_fragments as composition_fragments
import grove.api.portal as portal
from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.api.composition_fragments import register_composition_routes

_FAKE_NODES = [
    {
        "server_name": "grove-browser",
        "node_id": "grove-browser",
        "version": "1.0.0",
        "grv_standard": "GRV-004",
        "url": "http://100.80.12.118:8830/mcp",
        "is_composed": True,
        "is_connected": True,
        "health": "healthy",
        "connect_failure_type": None,
        "error_count": 0,
        "tools": [
            {"name": "browser_search", "proposed_zone": "green", "granted_zone": "green"},
        ],
    },
    {
        "server_name": "notion",
        "node_id": None,
        "version": None,
        "grv_standard": None,
        "url": "https://mcp.notion.com",
        "is_composed": False,
        "is_connected": False,
        "health": "healthy",
        "connect_failure_type": None,
        "error_count": 0,
        "tools": [],
    },
]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Isolated substrate (temp GROVE_HOME) with the portal routes mounted.
    _get_zone_map reads the REAL repo zones.schema.yaml; _load_mcp_config finds
    no config in the temp home and returns {}."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    register_composition_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_composition_nodes_envelope(client, monkeypatch):
    """Endpoint wraps the accessor output in the canonical envelope and passes
    pre-loaded zone_map + mcp_servers_config into the accessor (C3)."""
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return _FAKE_NODES

    monkeypatch.setattr(portal, "get_composition_status", _fake)

    r = await client.get("/api/substrate/composition/nodes")
    assert r.status == 200
    body = await r.json()

    # Envelope shape.
    assert body["meta"]["governance_state"] is None
    assert isinstance(body["meta"]["timestamp"], str) and body["meta"]["timestamp"]
    assert body["meta"]["count"] == 2
    assert body["data"] == _FAKE_NODES

    # C3: both inputs resolved by the handler and passed in pre-loaded.
    assert "zone_map" in captured and isinstance(captured["zone_map"], dict)
    assert "mcp_servers_config" in captured and isinstance(captured["mcp_servers_config"], dict)
    # _get_zone_map actually loaded the repo schema (grove-browser entries live
    # at config/zones.schema.yaml:466-467).
    assert captured["zone_map"].get("mcp_grove_browser_browser_search") == "green"


async def test_zone_map_cached_by_mtime(client, monkeypatch):
    """The schema is read ONCE across repeated requests while its mtime is
    unchanged — the C3 per-request-no-disk-read guarantee."""
    calls = {"n": 0}
    real_loader = portal._load_zone_map_from_schema

    def _counting(path=None):
        calls["n"] += 1
        return real_loader(path)

    monkeypatch.setattr(portal, "_load_zone_map_from_schema", _counting)
    monkeypatch.setattr(portal, "get_composition_status", lambda **kwargs: [])

    await client.get("/api/substrate/composition/nodes")
    await client.get("/api/substrate/composition/nodes")

    assert calls["n"] == 1  # mtime unchanged -> schema loaded once, then cached


async def test_composition_nodes_empty(client, monkeypatch):
    """Empty composition state -> empty data, count 0, still a valid envelope."""
    monkeypatch.setattr(portal, "get_composition_status", lambda **kwargs: [])
    r = await client.get("/api/substrate/composition/nodes")
    assert r.status == 200
    body = await r.json()
    assert body["data"] == []
    assert body["meta"]["count"] == 0


# ---------------------------------------------------------------------------
# Phase 4 — composition panel HTML fragment
# ---------------------------------------------------------------------------


async def test_composition_panel_renders(client, monkeypatch):
    """Composed + dark node render with health badge, tools, and the dark note."""
    monkeypatch.setattr(
        composition_fragments, "get_composition_status", lambda **kwargs: _FAKE_NODES
    )
    r = await client.get("/portal/fragments/composition/panel")
    assert r.status == 200
    assert r.content_type == "text/html"
    html = await r.text()

    # Section header + container.
    assert '<h2>Composition</h2>' in html
    assert 'id="composition-panel"' in html
    # Composed node identity, protocol, health.
    assert "grove-browser" in html
    assert "GRV-004" in html
    assert '<span class="badge badge-green">Connected</span>' in html
    # Matching zones (green == green) -> single granted badge, no arrow on this row.
    assert '<span class="badge badge-green">green</span>' in html
    # Dark node: muted note (I4).
    assert "No GRV-004 declaration" in html
    assert "dark-node" in html


async def test_composition_panel_authority_inversion(client, monkeypatch):
    """proposed != granted -> 'proposed -> [granted badge]' (I3)."""
    nodes = [{
        "server_name": "grove-browser", "node_id": "grove-browser",
        "version": "1.0.0", "grv_standard": "GRV-004", "url": None,
        "is_composed": True, "is_connected": True, "health": "healthy",
        "connect_failure_type": None, "error_count": 0,
        "tools": [{"name": "browser_search", "proposed_zone": "green", "granted_zone": "yellow"}],
    }]
    monkeypatch.setattr(composition_fragments, "get_composition_status", lambda **kwargs: nodes)
    html = await (await client.get("/portal/fragments/composition/panel")).text()

    assert '<span class="tag">green</span>' in html       # proposed (subordinate)
    assert "&rarr;" in html                                # the inversion arrow
    assert '<span class="badge badge-yellow">yellow</span>' in html  # granted (primary)


async def test_composition_panel_health_states(client, monkeypatch):
    """connect_failed -> red + failure type; breaker_open -> yellow + breaker."""
    nodes = [
        {"server_name": "a", "node_id": "a", "version": "1", "grv_standard": "GRV-004",
         "url": None, "is_composed": True, "is_connected": False, "health": "connect_failed",
         "connect_failure_type": "reauth", "error_count": 0, "tools": []},
        {"server_name": "b", "node_id": "b", "version": "1", "grv_standard": "GRV-004",
         "url": None, "is_composed": True, "is_connected": False, "health": "breaker_open",
         "connect_failure_type": None, "error_count": 5, "tools": []},
    ]
    monkeypatch.setattr(composition_fragments, "get_composition_status", lambda **kwargs: nodes)
    html = await (await client.get("/portal/fragments/composition/panel")).text()

    assert '<span class="badge badge-red">reauth</span>' in html
    assert '<span class="badge badge-yellow">Circuit breaker open</span>' in html


async def test_composition_panel_ordering(client, monkeypatch):
    """Composed nodes (by node_id) precede dark nodes (by server_name)."""
    nodes = [
        {"server_name": "zeta-dark", "node_id": None, "version": None, "grv_standard": None,
         "url": None, "is_composed": False, "is_connected": True, "health": "healthy",
         "connect_failure_type": None, "error_count": 0, "tools": []},
        {"server_name": "mid", "node_id": "mid-node", "version": "1", "grv_standard": "GRV-004",
         "url": None, "is_composed": True, "is_connected": True, "health": "healthy",
         "connect_failure_type": None, "error_count": 0, "tools": []},
    ]
    monkeypatch.setattr(composition_fragments, "get_composition_status", lambda **kwargs: nodes)
    html = await (await client.get("/portal/fragments/composition/panel")).text()

    # The composed node card appears before the dark node card.
    assert html.index("mid-node") < html.index("zeta-dark")


async def test_composition_panel_generic_no_node_name_branch(client, monkeypatch):
    """I1: an arbitrary node name renders through the same template — proof the
    rendering carries no hardcoded server/node-name conditional."""
    nodes = [{
        "server_name": "totally-novel-node", "node_id": "novel",
        "version": "9.9", "grv_standard": "GRV-004", "url": "http://x",
        "is_composed": True, "is_connected": True, "health": "healthy",
        "connect_failure_type": None, "error_count": 0,
        "tools": [{"name": "do_thing", "proposed_zone": "red", "granted_zone": "red"}],
    }]
    monkeypatch.setattr(composition_fragments, "get_composition_status", lambda **kwargs: nodes)
    html = await (await client.get("/portal/fragments/composition/panel")).text()

    assert "novel" in html
    assert "do_thing" in html
    assert '<span class="badge badge-red">red</span>' in html


async def test_composition_panel_empty(client, monkeypatch):
    """Empty list -> the 'No MCP servers configured' placeholder."""
    monkeypatch.setattr(composition_fragments, "get_composition_status", lambda **kwargs: [])
    html = await (await client.get("/portal/fragments/composition/panel")).text()
    assert '<p class="placeholder">No MCP servers configured.</p>' in html
