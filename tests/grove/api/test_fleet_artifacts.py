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

import json

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

    # fleet-review-unification-v1 C2 — the index now aggregates the four-state
    # disposition (needs_review_count + a full state_counts breakdown), replacing
    # the two-state pending/canonical split.
    scout = skills["scout"]
    assert scout["zone"] == "green"
    # green: staging == canonical, both digests sit in canonical → promoted.
    assert scout["state_counts"] == {"promoted": 2}
    assert scout["needs_review_count"] == 0
    assert scout["artifact_count"] == 2 and scout["latest_mtime"] is not None

    drafter = skills["drafter"]
    assert drafter["zone"] == "yellow"
    # flat pending draft (no proposal) → legacy; the canonical draft → promoted.
    assert drafter["state_counts"] == {"legacy": 1, "promoted": 1}
    assert drafter["needs_review_count"] == 0
    assert drafter["artifact_count"] == 2


async def test_fleet_index_c2_passthrough_fields(client):
    """fleet-ui-reconciliation-v1 C2 — the row carries mode + operational
    passthrough (worker schedule from fleet_workers.yaml, last_run from the
    terminal-event bus, ingest freshness from the wiki ledger). DATA ONLY —
    no HTML ever appears in a row value."""
    resp = await client.get("/api/substrate/fleet/")
    skills = {s["name"]: s for s in (await resp.json())["data"]["skills"]}

    drafter = skills["drafter"]
    assert drafter["mode"] == "action_surface_publish"
    # drafter is a registered worker: schedule passthrough verbatim.
    assert drafter["worker"]["id"] == "drafter"
    assert drafter["worker"]["cadence"] == "0 8 * * *"
    assert drafter["worker"]["enabled"] is True
    # no terminal events / no ingest ledger in this fixture → honest nulls.
    assert drafter["last_run"] is None
    assert drafter["last_ingest"] is None and drafter["ingested_count"] == 0

    scout = skills["scout"]
    assert scout["mode"] == "ingest_post"
    assert scout["worker"] is None  # observers have no registry entry

    # F6 — zero HTML/layout content anywhere in the rows.
    body = json.dumps(skills)
    assert "<" not in body and "class=" not in body


async def test_fleet_skill_list_tags_governance_state(client):
    resp = await client.get("/api/substrate/fleet/drafter/")
    assert resp.status == 200
    arts = (await resp.json())["data"]["artifacts"]
    states = {a["filename"]: a["governance_state"] for a in arts}
    # fleet-review-unification-v1 C2 — four-state disposition. The flat pending draft
    # (no proposal) is legacy; the canonical draft is promoted.
    assert states["draft-2026-07-01-x.md"] == "legacy"
    assert states["draft-approved.md"] == "promoted"
    for a in arts:
        # C2 payload: adds unit_id / producer / revision_count. `size` is present for
        # single-file artifacts (canonical/flat), omitted for nested-staged packages.
        assert {"filename", "mtime", "governance_state",
                "unit_id", "producer", "revision_count"} <= set(a)


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
# Portal fragments — /portal/fragments/fleet/ (in-shell since
# fleet-ui-reconciliation-v1 C1; the legacy /portal/fleet/ paths 302)
# ---------------------------------------------------------------------------


async def test_portal_overview_renders_status_board(client):
    # fleet-ui-reconciliation-v1 C2 — the overview is the STATUS BOARD (mock
    # screen A): producer cards in a wide grid, observer strip below, split on
    # approval_handoff.mode.
    resp = await client.get("/portal/fragments/fleet/")
    assert resp.status == 200 and resp.content_type == "text/html"
    html = await resp.text()
    assert "<!DOCTYPE html>" not in html
    assert '<div class="content wide">' in html
    assert '<div class="board">' in html
    # producers (action_surface_publish) render as worker cards…
    assert 'class="card worker-card"' in html
    assert 'href="/portal#fragments/fleet/drafter/"' in html
    assert "Producer" in html and "daily 08:00" in html  # schedule passthrough
    # …with the needs_review pill always present (zero-styled at 0).
    assert 'class="state-pill zero"' in html and "needs review" in html
    assert 'dot dot-promoted' in html
    # observers (ingest_post) render in the strip, count-free without a ledger.
    assert 'class="observer-strip"' in html
    assert 'href="/portal#fragments/fleet/scout/"' in html
    assert "no ingest recorded" in html
    assert 'class="badge badge-green"' in html  # scout Green zone badge


async def test_fleet_nav_fragment_outline(client):
    # fleet-ui-reconciliation-v1 C2 — the data-driven outline nav: Fleet root
    # with the needs_review badge, producer nodes with nonzero state rows,
    # observers grouped count-free.
    resp = await client.get("/portal/fragments/nav/fleet")
    assert resp.status == 200
    html = await resp.text()
    assert 'href="/portal#fragments/fleet/"' in html      # root → board
    assert '<div class="nav-node open"' in html           # root expanded
    assert 'class="nav-node lvl1"' in html and "drafter" in html
    # drafter has promoted+legacy units → both state rows, no needs_review row.
    assert "dot dot-promoted" in html and "dot dot-legacy" in html
    assert "dot dot-needs_review" not in html             # nonzero states only
    assert 'class="obs-label">Observers' in html
    assert 'class="nav-item lvl1 observer"' in html and "scout" in html
    # badge counts needs_review ONLY → 0 here, and never hot at zero.
    assert 'nav-badge hot' not in html


async def test_fleet_nav_badge_counts_needs_review(grove_home):
    # Stage a drafter unit WITH an open fleet_artifact proposal → needs_review;
    # the Fleet badge and the producer badge both go hot with count 1 (F3: the
    # badge is the same join the queue page renders).
    import json as _json
    from types import SimpleNamespace
    from grove.api import fragments as F
    from grove.eval import proposal_queue

    unit = grove_home / "drafter" / "pending_review" / "moon-bot"
    unit.mkdir(parents=True)
    (unit / "draft-moon-bot.md").write_text("body", encoding="utf-8")
    (unit / "meta.json").write_text(
        _json.dumps({"unit_id": "moon-bot", "slug": "moon-bot"}), encoding="utf-8"
    )
    proposal_queue.file_agentless(
        type=proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
        payload={"slug": "moon-bot", "unit_id": "moon-bot",
                 "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
        evidence=("moon-bot",), justification="t", proposer="skill.fleet.drafter",
    )
    resp = await F.handle_fleet_nav(SimpleNamespace(match_info={}))
    html = resp.text
    assert '<span class="nav-badge hot">1</span>' in html
    assert "dot dot-needs_review" in html                 # state row appears
    assert 'class="nav-node lvl1 open"' in html           # producer auto-opens


async def test_portal_skill_fragment_renders_c3_review_cards(client):
    # fleet-review-unification-v1 C3 — the producer INBOX: four-state review-cards
    # with state rails/chips. The flat pending draft (no proposal) is legacy; the
    # canonical draft is promoted. C1 — served as an in-shell fragment at content
    # width.
    resp = await client.get("/portal/fragments/fleet/drafter/")
    assert resp.status == 200
    html = await resp.text()
    assert '<div class="content">' in html
    assert 'class="pending-pill' in html and "needs review" in html
    assert "review-card rail-legacy" in html
    assert "review-card rail-promoted" in html
    assert "chip-legacy" in html and "chip-promoted" in html
    assert "&rsaquo;" in html                 # breadcrumb separator
    assert 'class="meta breadcrumb"' in html  # breadcrumb carries the class token
    assert 'href="/portal#fragments/fleet/"' in html  # breadcrumb hash link


async def test_portal_artifact_fragment_renders_markdown(client):
    resp = await client.get("/portal/fragments/fleet/drafter/draft-2026-07-01-x.md")
    assert resp.status == 200 and resp.content_type == "text/html"
    html = await resp.text()
    assert "<strong>bold</strong>" in html          # rendered markdown
    assert "Fleet</a>" in html and "drafter</a>" in html  # breadcrumb links


async def test_portal_artifact_fragment_mounts_dock_oob(client):
    # C1 — the Mount-2 disposition dock rides an OOB #right-panel swap when the
    # artifact resolves to a C2 unit (the flat staged draft is a legacy unit).
    resp = await client.get("/portal/fragments/fleet/drafter/draft-2026-07-01-x.md")
    assert resp.status == 200
    html = await resp.text()
    assert 'id="right-panel"' in html and 'hx-swap-oob="true"' in html
    assert 'class="disposition-dock' in html


async def test_portal_artifact_fragment_renders_json_card(client):
    resp = await client.get("/portal/fragments/fleet/scout/digest-2026-07-01.json")
    assert resp.status == 200
    html = await resp.text()
    assert "<dt>generated_at</dt>" in html      # structured key extraction
    assert "[2 item(s)]" in html                # list summarized
    assert "<details>" in html and "Raw JSON" in html  # collapsible raw


async def test_portal_unknown_skill_404(client):
    resp = await client.get("/portal/fragments/fleet/nonesuch/")
    assert resp.status == 404
    body = await resp.text()
    assert "Unknown fleet skill" in body
    assert "404" in body  # error fragment carries the status token


async def test_portal_missing_artifact_404(client):
    resp = await client.get("/portal/fragments/fleet/scout/missing.json")
    assert resp.status == 404
    body = await resp.text()
    assert "not found" in body.lower()
    assert "404" in body


# ---------------------------------------------------------------------------
# Legacy standalone paths — 302 to the hash URLs (C1)
# ---------------------------------------------------------------------------


async def test_legacy_fleet_overview_redirects(client):
    resp = await client.get("/portal/fleet/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/portal#fragments/fleet/"


async def test_legacy_fleet_skill_redirects(client):
    resp = await client.get("/portal/fleet/drafter/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/portal#fragments/fleet/drafter/"


async def test_legacy_fleet_artifact_redirects(client):
    resp = await client.get(
        "/portal/fleet/drafter/draft-2026-07-01-x.md", allow_redirects=False
    )
    assert resp.status == 302
    assert resp.headers["Location"] == (
        "/portal#fragments/fleet/drafter/draft-2026-07-01-x.md"
    )


async def test_legacy_forge_slug_redirects(client):
    resp = await client.get(
        "/portal/fleet/forge-jobsearch/260707-acme/", allow_redirects=False
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/portal#fragments/forge/260707-acme/"


# ---------------------------------------------------------------------------
# Nav count freshness — HX-Trigger response header (C2)
# ---------------------------------------------------------------------------


async def test_nav_refresh_header_on_success_only():
    """The disposition wrapper adds both nav-refresh events (C3: the Fleet
    outline AND the Proposals badge listen) to 2xx responses ONLY — a failed
    disposition changes no counts. Response-header only (not a write-path
    change)."""
    from grove.api.actions import _with_nav_refresh

    async def ok(_request):
        return web.Response(text="ok", status=200)

    async def fail(_request):
        return web.Response(text="no", status=409)

    resp = await _with_nav_refresh(ok)(None)
    assert resp.headers["HX-Trigger"] == "fleet-disposition, proposal-disposition"
    resp = await _with_nav_refresh(fail)(None)
    assert "HX-Trigger" not in resp.headers


async def test_portal_trailing_slash_redirects_to_shell(client):
    resp = await client.get("/portal/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/portal"
