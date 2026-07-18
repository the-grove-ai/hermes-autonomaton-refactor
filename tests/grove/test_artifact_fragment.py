"""artifact-continuation-v1 C1 — in-shell artifact fragment.

/portal/fragments/artifact/{id}: shared resolution+containment with the raw
route, pinned nh3 profile + unconditional anchor rewrite, model-content
demarcation, metadata-only for non-md classes, uniform 404 fragment, portal
auth gating. Raw-route behavior is pinned unchanged by test_artifact_route.py.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from grove.api.artifacts import (
    handle_artifact,
    handle_artifact_fragment,
    resolve_artifact_roots,
)
from grove.api.portal import portal_auth_middleware
from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.kaizen_ledger import KaizenLedger


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(home / "wiki"))
    (home / "wiki" / "pages").mkdir(parents=True)
    return home


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    app["artifact_roots"] = resolve_artifact_roots(config={})
    app["_artifact_index"] = {}
    app.router.add_get("/artifact/{artifact_id}", handle_artifact)
    app.router.add_get(
        "/portal/fragments/artifact/{artifact_id}", handle_artifact_fragment
    )
    async with TestClient(TestServer(app)) as c:
        yield c


def _emit(path_str: str) -> str:
    canonical = canonical_artifact_path(path_str)
    aid = artifact_id(canonical)
    KaizenLedger("artifact-fragment-test").record(
        "artifact_written", path=canonical, artifact_id=aid, turn_id="t#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
    )
    return aid


# ── md rendering in-shell ────────────────────────────────────────────────────


async def test_md_fragment_envelope_demarcation_anchor_rewrite(client, grove_home):
    f = grove_home / "note.md"
    f.write_text("# Title\n\n[ok](https://example.com)\n", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    assert resp.status == 200
    body = await resp.text()
    assert "<html" not in body                       # bare fragment envelope
    assert '<article id="artifact-detail">' in body
    assert f"<h2>artifact {aid}</h2>" in body        # in-markup title
    assert '<div class="model-content">' in body     # demarcation container
    assert "model-generated content" in body         # persistent label
    # Unconditional anchor rewrite on model-authored anchors:
    assert 'href="https://example.com"' in body
    a_tag = body[body.index('href="https://example.com"') - 3:]
    assert 'rel="noopener noreferrer"' in a_tag[:200]
    assert 'target="_blank"' in a_tag[:200]
    # Raw-route escape hatch present:
    assert f'<a href="/artifact/{aid}"' in body and "open raw" in body


async def test_md_fragment_strips_active_content(client, grove_home):
    f = grove_home / "hostile.md"
    f.write_text(
        "# X\n\n<script>alert(1)</script>\n"
        "<iframe src='http://evil'></iframe>\n"
        '<a hx-post="/portal/actions/anything" href="https://e.com">tap</a>\n'
        "[bad](data:text/html;base64,PHNjcmlwdD4=)\n"
        '<span class="badge">shell-look</span><div class="card">x</div>\n',
        encoding="utf-8",
    )
    aid = _emit(str(f))
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    body = await resp.text()
    assert resp.status == 200
    assert "<script" not in body
    assert "<iframe" not in body
    assert "hx-post" not in body                     # htmx attrs cannot survive
    assert "data:" not in body                       # data: URI stripped
    assert '<span class="badge">' not in body        # pinned profile: no span
    assert '<div class="card">' not in body          # no shell-look divs


# ── non-md classes: metadata only ────────────────────────────────────────────


async def test_non_md_fragment_metadata_only(client, grove_home):
    f = grove_home / "blob.html"
    f.write_text("<html>SECRET-INLINE-BYTES</html>", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    body = await resp.text()
    assert resp.status == 200
    assert "SECRET-INLINE-BYTES" not in body         # no inline content
    assert "blob.html" in body                       # basename metadata
    assert ".html" in body                           # type metadata
    assert f'<a href="/artifact/{aid}"' in body      # raw-route link only


# ── uniform 404 fragment ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["zzzz", "A" * 16, "a" * 15])
async def test_malformed_id_404_fragment(client, grove_home, bad):
    resp = await client.get(f"/portal/fragments/artifact/{bad}")
    assert resp.status == 404
    assert "No such artifact." in await resp.text()


async def test_unknown_id_404_fragment(client, grove_home):
    resp = await client.get("/portal/fragments/artifact/" + "a" * 16)
    assert resp.status == 404
    assert "No such artifact." in await resp.text()


async def test_vanished_file_404_fragment_not_500(client, grove_home):
    f = grove_home / "gone.md"
    f.write_text("x", encoding="utf-8")
    aid = _emit(str(f))
    f.unlink()
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    assert resp.status == 404
    assert "No such artifact." in await resp.text()


# ── auth gating ──────────────────────────────────────────────────────────────


async def test_fragment_route_is_auth_gated():
    async def handler(request):
        return web.Response(text="ok")

    gated = make_mocked_request(
        "GET", "/portal/fragments/artifact/" + "a" * 16
    )
    resp = await portal_auth_middleware(gated, handler)
    assert resp.status == 403  # mocked request has no peer → denied


# ── cross-ref parity ─────────────────────────────────────────────────────────


async def test_fragment_shows_ingested_as(client, grove_home):
    f = grove_home / "note-b.md"
    f.write_text("# B\n", encoding="utf-8")
    aid = _emit(str(f))
    pages = grove_home / "wiki" / "pages" / "agent_session"
    pages.mkdir(parents=True)
    (pages / f"b-{aid[:8]}.md").write_text("---\n---\nx", encoding="utf-8")
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    body = await resp.text()
    assert "ingested as" in body
    assert (
        f'href="/portal#fragments/cellar/pages/agent_session/b-{aid[:8]}"'
        in body
    )
