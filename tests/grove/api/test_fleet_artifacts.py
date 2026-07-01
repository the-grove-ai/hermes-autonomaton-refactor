"""fleet-artifact-viewer-v1 — fleet artifact API endpoints + portal pages.

Covers the three ``/api/substrate/fleet/`` JSON endpoints and the three
``/portal/fleet/`` standalone HTML pages against the real bundled fleet
capability records (kind=skill with a governance block), with fabricated
artifacts under a temp GROVE_HOME.

scout is Green-zone (staging == canonical, no pending tier); drafter is
Yellow-zone (staging == ``drafter/pending_review``, canonical == ``drafter``),
so the two governance_state tiers and the non-recursive canonical glob are both
exercised.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import register_portal_routes
from grove.api.fragments import register_fragment_routes


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Temp GROVE_HOME seeded with fleet artifacts: two Green scout digests
    (both canonical) and two Yellow drafter drafts (one pending, one canonical)."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))

    scout = tmp_path / "scout"
    scout.mkdir()
    (scout / "digest-2026-07-01.json").write_text(
        '{"generated_at":"2026-07-01","summary":{"total":2},"opportunities":[1,2]}',
        encoding="utf-8",
    )
    (scout / "digest-2026-06-30.json").write_text(
        '{"generated_at":"2026-06-30","opportunities":[]}', encoding="utf-8"
    )

    drafter_pending = tmp_path / "drafter" / "pending_review"
    drafter_pending.mkdir(parents=True)
    (drafter_pending / "draft-2026-07-01-x.md").write_text(
        "# Draft Title\n\nBody with **bold** text.\n", encoding="utf-8"
    )
    (tmp_path / "drafter" / "draft-approved.md").write_text(
        "# Approved\n\nAlready promoted.\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application()
    register_portal_routes(app)     # /api/substrate/fleet/*
    register_fragment_routes(app)   # /portal/fleet/*
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# JSON API — /api/substrate/fleet/
# ---------------------------------------------------------------------------


async def test_fleet_index_splits_green_and_yellow(client):
    resp = await client.get("/api/substrate/fleet/")
    assert resp.status == 200
    body = await resp.json()
    skills = {s["name"]: s for s in body["data"]["skills"]}
    # all four fleet skills are present (governance-block records)
    assert {"scout", "researcher", "drafter", "cultivator"} <= set(skills)

    scout = skills["scout"]
    assert scout["zone"] == "green"
    assert scout["canonical_count"] == 2 and scout["pending_count"] == 0
    assert scout["artifact_count"] == 2 and scout["latest_mtime"] is not None

    drafter = skills["drafter"]
    assert drafter["zone"] == "yellow"
    # non-recursive canonical glob must NOT count the pending_review draft
    assert drafter["pending_count"] == 1 and drafter["canonical_count"] == 1
    assert drafter["artifact_count"] == 2


async def test_fleet_skill_list_tags_governance_state(client):
    resp = await client.get("/api/substrate/fleet/drafter/")
    assert resp.status == 200
    arts = (await resp.json())["data"]["artifacts"]
    states = {a["filename"]: a["governance_state"] for a in arts}
    assert states["draft-2026-07-01-x.md"] == "pending_review"
    assert states["draft-approved.md"] == "canonical"
    for a in arts:
        assert set(a) == {"filename", "size", "mtime", "governance_state"}


async def test_fleet_artifact_json_verbatim(client):
    resp = await client.get("/api/substrate/fleet/scout/digest-2026-07-01.json")
    assert resp.status == 200
    assert resp.content_type == "application/json"
    assert (await resp.json())["summary"]["total"] == 2


async def test_fleet_artifact_md_rendered(client):
    resp = await client.get("/api/substrate/fleet/drafter/draft-2026-07-01-x.md")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert "<strong>bold</strong>" in await resp.text()


async def test_fleet_api_unknown_skill_404(client):
    assert (await client.get("/api/substrate/fleet/nonesuch/")).status == 404


async def test_fleet_api_missing_artifact_404(client):
    assert (await client.get("/api/substrate/fleet/scout/missing.json")).status == 404


async def test_fleet_api_traversal_refused(client):
    assert (await client.get("/api/substrate/fleet/scout/..%2f..%2f.env")).status == 404


# ---------------------------------------------------------------------------
# Portal pages — /portal/fleet/
# ---------------------------------------------------------------------------


async def test_portal_overview_renders_skill_cards(client):
    resp = await client.get("/portal/fleet/")
    assert resp.status == 200 and resp.content_type == "text/html"
    html = await resp.text()
    assert "<!DOCTYPE html>" in html
    assert 'href="/portal/fleet/scout/"' in html      # skill card link
    assert 'class="badge badge-green"' in html         # scout Green zone badge


async def test_portal_skill_page_lists_artifacts_with_state_badges(client):
    resp = await client.get("/portal/fleet/drafter/")
    assert resp.status == 200
    html = await resp.text()
    assert 'href="/portal/fleet/drafter/draft-2026-07-01-x.md"' in html
    # governance state badges — pending review (yellow) + canonical (green)
    assert "pending review" in html and "badge-yellow" in html
    assert "canonical" in html and "badge-green" in html
    assert "&rsaquo;" in html                 # breadcrumb separator
    assert 'class="meta breadcrumb"' in html  # breadcrumb carries the class token


async def test_portal_artifact_view_renders_markdown(client):
    resp = await client.get("/portal/fleet/drafter/draft-2026-07-01-x.md")
    assert resp.status == 200 and resp.content_type == "text/html"
    html = await resp.text()
    assert "<strong>bold</strong>" in html          # rendered markdown
    assert "Fleet</a>" in html and "drafter</a>" in html  # breadcrumb links


async def test_portal_artifact_view_renders_json_card(client):
    resp = await client.get("/portal/fleet/scout/digest-2026-07-01.json")
    assert resp.status == 200
    html = await resp.text()
    assert "<dt>generated_at</dt>" in html      # structured key extraction
    assert "[2 item(s)]" in html                # list summarized
    assert "<details>" in html and "Raw JSON" in html  # collapsible raw


async def test_portal_unknown_skill_404(client):
    resp = await client.get("/portal/fleet/nonesuch/")
    assert resp.status == 404
    body = await resp.text()
    assert "Unknown fleet skill" in body
    assert "404" in body  # styled 404 page carries the status token


async def test_portal_missing_artifact_404(client):
    resp = await client.get("/portal/fleet/scout/missing.json")
    assert resp.status == 404
    body = await resp.text()
    assert "not found" in body.lower()
    assert "404" in body


async def test_portal_trailing_slash_redirects_to_shell(client):
    resp = await client.get("/portal/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/portal"
