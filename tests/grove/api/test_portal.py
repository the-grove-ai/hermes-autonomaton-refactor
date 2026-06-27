"""Sprint P1 (portal-api-scaffold-v1) — Operator Portal substrate API.

Covers the eight read-only /api/substrate/ endpoints, the localhost/Tailscale
auth middleware, the response envelope, and the enum-aware skills serializer.

Substrate is isolated to a temp GROVE_HOME per test; the wiki/cellar indices
build lazily there. Skills load the repo's bundled capability records (real
nested enums), so the serializer is exercised against real data.
"""

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import grove.dock as _dockmod
from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.capability import CapabilityKind, LifecycleState, Zone
from grove.eval import proposal_queue
from grove.memory.events import MemoryCreated
from grove.memory.record import DECAY_RATES
from grove.memory.store import MemoryStore

_VALID_VECTOR = sorted(_dockmod._VALID_VECTORS)[0]
_VALID_STATUS = sorted(_dockmod._VALID_STATUSES)[0]

_ENUM_REPR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Z][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Isolated substrate: a wiki page, a cellar identity file, one active
    memory record, and one pending proposal. No dock.yaml (not installed)."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))

    # Canonical pages are nested in per-source_type subdirs on the real cellar
    # (dock_goal/, scout_digest/, ...). Seed two subdirs to exercise recursion
    # and the path-qualified page_id.
    pages = tmp_path / "wiki" / "pages"
    (pages / "dock_goal").mkdir(parents=True)
    (pages / "scout_digest").mkdir(parents=True)
    (pages / "dock_goal" / "sov-abc123.md").write_text(
        "---\ntitle: Sovereignty\nsource_type: research\n"
        "topics:\n  - sovereignty\nkey_entities:\n  - Grove\n"
        "dock_goal_refs:\n  - goal-sov\nconfidence: 0.9\n---\n"
        "# Sovereignty\nModel independence and sovereignty matter.\n",
        encoding="utf-8",
    )
    (pages / "scout_digest" / "ai-gov-def456.md").write_text(
        "---\ntitle: AI Governance\nsource_type: scout_digest\n"
        "topics:\n  - governance\n---\n# Governance\nGovernance as architecture.\n",
        encoding="utf-8",
    )
    (tmp_path / "operator.md").write_text(
        "# Operator\nThe operator values sovereignty.\n", encoding="utf-8"
    )

    store = MemoryStore(base_dir=tmp_path)
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp="2026-06-26T00:00:00Z", record_id="mem_1",
        entity_type="DomainFact", content="Grove is sovereign.", confidence=0.9,
        dock_goal_ref=None, sources=[{"session_id": "s1", "turn_id": "t1"}],
        supersedes=None,
    ))

    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id="routing_update:abc123",
        type="routing_update",
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
    ))
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


def _assert_envelope(body):
    assert "data" in body
    meta = body["meta"]
    assert meta["governance_state"] is None
    assert isinstance(meta["timestamp"], str) and meta["timestamp"]
    assert "count" in meta


def _find_enum_leaks(obj, path="$"):
    leaks = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            leaks += _find_enum_leaks(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            leaks += _find_enum_leaks(v, f"{path}[{i}]")
    elif isinstance(obj, str) and _ENUM_REPR.match(obj):
        leaks.append((path, obj))
    return leaks


# ---------------------------------------------------------------------------
# Envelope + per-endpoint structure
# ---------------------------------------------------------------------------


async def test_cellar_pages_lists_nested(client):
    """Recursive discovery: pages nested in source_type subdirs are listed,
    with subdir-qualified page_ids (the GATE-6 fix)."""
    r = await client.get("/api/substrate/cellar/pages")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    assert body["meta"]["count"] == 2
    page_ids = {p["page_id"] for p in body["data"]}
    assert page_ids == {"dock_goal/sov-abc123", "scout_digest/ai-gov-def456"}
    sov = next(p for p in body["data"] if p["page_id"] == "dock_goal/sov-abc123")
    assert sov["title"] == "Sovereignty"
    assert sov["topics"] == ["sovereignty"]


async def test_cellar_page_detail_nested(client):
    """Detail resolves a subdir-qualified page_id (slashes in the route)."""
    r = await client.get("/api/substrate/cellar/pages/dock_goal/sov-abc123")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    assert body["data"]["page_id"] == "dock_goal/sov-abc123"
    assert body["data"]["frontmatter"]["title"] == "Sovereignty"
    assert "sovereignty" in body["data"]["body"].lower()


async def test_cellar_page_detail_404(client):
    r = await client.get("/api/substrate/cellar/pages/does-not-exist")
    assert r.status == 404
    body = await r.json()
    assert body["error"] == "not_found"


async def test_memory_records_envelope(client):
    r = await client.get("/api/substrate/memory/records")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    assert body["meta"]["count"] == 1
    rec = body["data"][0]
    assert rec["id"] == "mem_1"
    assert rec["status"] == "active"
    assert rec["entity_type"] == "DomainFact"


async def test_dock_not_installed_returns_null(client):
    r = await client.get("/api/substrate/dock/goals")
    assert r.status == 200
    body = await r.json()
    assert body["data"] is None
    assert body["meta"]["count"] == 0


async def test_dock_installed_returns_goals(client, grove_home):
    dock_dir = grove_home / "dock"
    dock_dir.mkdir(parents=True, exist_ok=True)
    (dock_dir / "dock.yaml").write_text(
        "version: 1\ncontext_char_budget: 5000\ngoals:\n"
        f"  - id: goal-sov\n    name: Sovereignty\n    vector: {_VALID_VECTOR}\n"
        f"    status: {_VALID_STATUS}\n    definition_of_done: Ship it.\n"
        "    context_sources: []\n    keywords: [sovereignty]\n"
        "    unlocked_skills: [scout]\n    deadline: 2026-12-01\n",
        encoding="utf-8",
    )
    r = await client.get("/api/substrate/dock/goals")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 1
    g = body["data"][0]
    assert g["id"] == "goal-sov"
    assert g["keywords"] == ["sovereignty"]
    assert g["unlocked_skills"] == ["scout"]
    # YAML-native date in extra must be coerced to a string (JSON-safe).
    assert isinstance(g["extra"]["deadline"], str)


async def test_proposals_pending(client):
    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    assert body["meta"]["count"] == 1
    p = body["data"][0]
    assert p["type"] == "routing_update"
    assert p["evidence"] == ["turn_1"]  # tuple -> list via to_dict()


async def test_skills_envelope_and_no_enum_objects(client):
    r = await client.get("/api/substrate/skills/")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    skills = body["data"]
    assert len(skills) > 0  # repo bundles skill capabilities
    leaks = []
    for s in skills:
        assert s["kind"] == "skill"
        assert s["zone"] in {z.value for z in Zone}
        assert s["lifecycle"]["state"] in {st.value for st in LifecycleState}
        leaks += _find_enum_leaks(s)
    assert not leaks, f"enum-repr leaks in skills payload: {leaks[:5]}"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_partitioned(client):
    r = await client.get("/api/substrate/search?q=sovereignty")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    data = body["data"]
    assert isinstance(data, dict)
    assert set(data.keys()) == {"wiki", "cellar"}  # partitioned, not merged
    assert isinstance(data["wiki"], list) and isinstance(data["cellar"], list)
    assert body["meta"]["count"] == len(data["wiki"]) + len(data["cellar"])


async def test_search_empty_q_returns_empty_arrays(client):
    r = await client.get("/api/substrate/search?q=")
    assert r.status == 200
    body = await r.json()
    assert body["data"] == {"wiki": [], "cellar": []}
    assert body["meta"]["count"] == 0


async def test_search_bad_k_returns_400(client):
    r = await client.get("/api/substrate/search?q=sovereignty&k=abc")
    assert r.status == 400
    body = await r.json()
    assert body["error"] == "bad_request"


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


async def test_meta_enumerations(client):
    r = await client.get("/api/substrate/meta")
    assert r.status == 200
    body = await r.json()
    _assert_envelope(body)
    enums = body["data"]["enumerations"]
    assert set(enums.keys()) == {
        "lifecycle_states", "zone_classes", "proposal_types",
        "memory_entity_types", "capability_kinds",
    }
    # Sourced from the live enums/constants — parity, not hardcoded lists.
    assert set(enums["lifecycle_states"]) == {s.value for s in LifecycleState}
    assert len(enums["lifecycle_states"]) == 7
    assert set(enums["zone_classes"]) == {z.value for z in Zone}
    assert set(enums["capability_kinds"]) == {k.value for k in CapabilityKind}
    assert set(enums["memory_entity_types"]) == set(DECAY_RATES.keys())
    assert "consolidation_proposal" in enums["proposal_types"]
    assert "consolidation" not in enums["proposal_types"]


# ---------------------------------------------------------------------------
# Auth middleware (unit-level — request.remote can't be faked over a socket)
# ---------------------------------------------------------------------------


class _FakeReq:
    def __init__(self, path, remote):
        self.path = path
        self.remote = remote


async def _ok_handler(request):
    return web.Response(text="ok", status=200)


async def test_auth_blocks_public_ip():
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", "8.8.8.8"), _ok_handler)
    assert resp.status == 403


async def test_auth_allows_loopback_v4():
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", "127.0.0.1"), _ok_handler)
    assert resp.status == 200


async def test_auth_allows_loopback_v6():
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", "::1"), _ok_handler)
    assert resp.status == 200


async def test_auth_allows_tailscale_cgnat():
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", "100.100.1.1"), _ok_handler)
    assert resp.status == 200


async def test_auth_blocks_non_cgnat_100():
    # 100.200.x is OUTSIDE 100.64.0.0/10 (which ends at 100.127.255.255)
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", "100.200.1.1"), _ok_handler)
    assert resp.status == 403


async def test_auth_passthrough_non_substrate_path_even_public_ip():
    resp = await portal_auth_middleware(
        _FakeReq("/v1/chat/completions", "8.8.8.8"), _ok_handler)
    assert resp.status == 200


async def test_auth_blocks_missing_remote():
    resp = await portal_auth_middleware(
        _FakeReq("/api/substrate/meta", None), _ok_handler)
    assert resp.status == 403
