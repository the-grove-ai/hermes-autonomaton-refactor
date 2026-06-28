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

import grove.api.portal as portal
from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)

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
