"""Sprint P2 (portal-knowledge-browser-v1) — Operator Portal Knowledge Browser.

Covers the HTML shell, vendored static assets, the /portal/fragments/* routes
(cellar listing + detail, memory/dock/proposals/skills, context sidebar,
search), server-side markdown rendering + nh3 sanitization, and the auth
middleware's extension to /portal paths.

Substrate is isolated to a temp GROVE_HOME per test (same pattern as the P1
test): a couple of nested wiki pages, one active memory record, one pending
proposal, no dock.yaml (so the Dock-not-installed path is exercised). Skills
load the repo's bundled capability records (real nested enums).
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.api.fragments import _PORTAL_ASSETS, _render_md, register_fragment_routes
from grove.memory.events import MemoryCreated
from grove.memory.store import MemoryStore
from grove.eval import proposal_queue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Isolated substrate: two topic-sharing nested wiki pages (one with a
    dock_goal_ref), one active memory record, one pending proposal, no dock."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))

    pages = tmp_path / "wiki" / "pages"
    (pages / "dock_goal").mkdir(parents=True)
    (pages / "scout_digest").mkdir(parents=True)
    # Page A: carries a dock_goal_ref and shares the topic "governance" with B.
    (pages / "dock_goal" / "sov-abc123.md").write_text(
        "---\ntitle: Sovereignty\nsource_type: research\n"
        "topics:\n  - sovereignty\n  - governance\nkey_entities:\n  - Grove\n"
        "dock_goal_refs:\n  - goal-sov\nconfidence: 0.9\n---\n"
        "# Sovereignty\n\nModel **independence** and sovereignty matter.\n\n"
        "| key | value |\n|-----|-------|\n| zone | green |\n",
        encoding="utf-8",
    )
    (pages / "scout_digest" / "gov-def456.md").write_text(
        "---\ntitle: AI Governance\nsource_type: scout_digest\n"
        "topics:\n  - governance\n---\n# Governance\n\nGovernance as architecture.\n",
        encoding="utf-8",
    )

    store = MemoryStore(base_dir=tmp_path)
    store.append_event(MemoryCreated(
        event_id="evt_1", timestamp="2026-06-26T00:00:00Z", record_id="mem_1",
        entity_type="DomainFact", content="Grove is sovereign.", confidence=0.9,
        dock_goal_ref="goal-sov", sources=[{"session_id": "s1", "turn_id": "t1"}],
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
    # The static mount lives in api_server.connect(), not register_fragment_routes;
    # add it here so the shell's asset serving is exercised end to end.
    app.router.add_static("/portal/static", str(_PORTAL_ASSETS))
    register_fragment_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# Shell + static
# ---------------------------------------------------------------------------


async def test_portal_shell_returns_three_panel_html(client):
    r = await client.get("/portal")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/html")
    body = await r.text()
    assert 'id="center-panel"' in body
    assert "left-panel" in body and "right-panel" in body
    assert "/portal/static/htmx.min.js" in body


async def test_htmx_static_served(client):
    r = await client.get("/portal/static/htmx.min.js")
    assert r.status == 200
    body = await r.text()
    assert "htmx" in body and 'version:"2' in body


# ---------------------------------------------------------------------------
# Cellar fragments
# ---------------------------------------------------------------------------


async def test_cellar_listing_fragment_has_list_items(client):
    r = await client.get("/portal/fragments/cellar/pages")
    assert r.status == 200
    body = await r.text()
    assert 'id="cellar-listing"' in body
    assert "<ul" in body and "<li" in body
    # Grouped by source_type subdirectory with <h3> headers.
    assert "<h3" in body and ">dock_goal<" in body
    # Detail link with subdir-qualified page_id + history push.
    assert "/portal/fragments/cellar/pages/dock_goal/sov-abc123" in body
    assert 'hx-push-url="true"' in body


async def test_cellar_detail_renders_markdown(client):
    r = await client.get("/portal/fragments/cellar/pages/dock_goal/sov-abc123")
    assert r.status == 200
    body = await r.text()
    assert 'id="page-detail"' in body
    # Rendered HTML, not raw markdown.
    assert "<strong>independence</strong>" in body
    assert "<table>" in body
    assert "**independence**" not in body


async def test_cellar_detail_has_oob_sidebar_swap(client):
    r = await client.get("/portal/fragments/cellar/pages/dock_goal/sov-abc123")
    body = await r.text()
    assert 'id="right-panel" hx-swap-oob="true"' in body
    assert "/portal/fragments/context/cellar/dock_goal/sov-abc123" in body
    assert 'hx-swap="outerHTML"' in body  # clean replace, no nested duplicate id


async def test_cellar_detail_404(client):
    r = await client.get("/portal/fragments/cellar/pages/dock_goal/nope")
    assert r.status == 404


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_render_md_strips_script():
    out = _render_md("ok\n\n<script>alert('xss')</script>\n")
    assert "<script>" not in out
    assert "alert" not in out or "<script" not in out


def test_render_md_strips_event_handler_attr():
    out = _render_md('<img src=x onerror="alert(1)">')
    assert "onerror" not in out


def test_render_md_preserves_legitimate_formatting():
    out = _render_md(
        "# H\n\n`code`\n\n[link](http://example.com)\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    assert "<table>" in out
    assert "<code>" in out
    assert 'href="http://example.com"' in out


# ---------------------------------------------------------------------------
# Memory / Dock / Proposals / Skills
# ---------------------------------------------------------------------------


async def test_memory_fragment_has_confidence_bar(client):
    r = await client.get("/portal/fragments/memory/records")
    assert r.status == 200
    body = await r.text()
    assert 'id="memory-listing"' in body
    assert "confidence-bar" in body
    assert "Grove is sovereign." in body
    # dock_goal_ref rendered as a clickable context link.
    assert "/portal/fragments/context/dock/goal-sov" in body


async def test_dock_not_installed_message(client):
    r = await client.get("/portal/fragments/dock/goals")
    assert r.status == 200
    body = await r.text()
    assert "Dock not installed" in body


async def test_proposals_fragment_renders_card(client):
    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    assert 'id="proposals-listing"' in body
    assert "routing_update" in body


async def test_skills_fragment_zone_badges(client):
    r = await client.get("/portal/fragments/skills/")
    assert r.status == 200
    body = await r.text()
    assert 'id="skills-listing"' in body
    # Bundled capability records carry zones — at least one zone badge renders.
    assert any(cls in body for cls in ("badge-green", "badge-yellow", "badge-red"))


# ---------------------------------------------------------------------------
# Context sidebar
# ---------------------------------------------------------------------------


async def test_context_cellar_returns_related_items(client):
    r = await client.get("/portal/fragments/context/cellar/dock_goal/sov-abc123")
    assert r.status == 200
    body = await r.text()
    assert 'id="right-panel"' in body
    assert "Related Goals" in body
    assert "Related Pages" in body
    # The topic-sharing page B is surfaced as a related link.
    assert "/portal/fragments/cellar/pages/scout_digest/gov-def456" in body


async def test_context_unknown_entity(client):
    r = await client.get("/portal/fragments/context/widget/xyz")
    assert r.status == 200
    body = await r.text()
    assert "No context available" in body


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_stacked_sections(client):
    r = await client.get("/portal/fragments/search?q=governance")
    assert r.status == 200
    body = await r.text()
    assert "<h3>Wiki Matches</h3>" in body
    assert "<h3>Cellar Matches</h3>" in body
    assert "<hr>" in body


async def test_search_empty_query(client):
    r = await client.get("/portal/fragments/search?q=")
    assert r.status == 200
    body = await r.text()
    assert "Enter a search term" in body


async def test_search_no_match(client):
    r = await client.get("/portal/fragments/search?q=zzznomatchqqq")
    assert r.status == 200
    body = await r.text()
    assert "No matches found" in body


# ---------------------------------------------------------------------------
# Auth middleware extension to /portal (unit-level — remote can't be faked
# over a socket; same approach as the P1 auth tests)
# ---------------------------------------------------------------------------


class _FakeReq:
    def __init__(self, path, remote):
        self.path = path
        self.remote = remote


async def _ok_handler(request):
    return web.Response(text="ok", status=200)


async def test_auth_blocks_public_ip_on_portal():
    resp = await portal_auth_middleware(
        _FakeReq("/portal/fragments/cellar/pages", "8.8.8.8"), _ok_handler)
    assert resp.status == 403


async def test_auth_allows_loopback_on_portal():
    resp = await portal_auth_middleware(
        _FakeReq("/portal", "127.0.0.1"), _ok_handler)
    assert resp.status == 200


async def test_auth_allows_tailscale_on_portal_static():
    resp = await portal_auth_middleware(
        _FakeReq("/portal/static/htmx.min.js", "100.100.1.1"), _ok_handler)
    assert resp.status == 200
