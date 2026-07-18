"""artifact-identity-v1 C2 — /artifact/<id> route: containment, serving,
headers, cross-ref, allowlist roots, peer auth.

Hermetic: GROVE_HOME + GROVE_WIKI_PATH isolated per test; ledger events are
written through the real KaizenLedger (so the EVENT_TYPES registration and
the route's dir-glob read path are both exercised end-to-end).
"""

from __future__ import annotations

import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from grove.api.artifacts import (
    handle_artifact,
    resolve_artifact_roots,
)
from grove.api.portal import _peer_authorized, portal_auth_middleware
from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.kaizen_ledger import KaizenLedger


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(home / "wiki"))
    (home / "wiki" / "pages").mkdir(parents=True)
    return home


def _make_app(roots):
    app = web.Application(middlewares=[portal_auth_middleware])
    app["artifact_roots"] = roots
    app["_artifact_index"] = {}
    app.router.add_get("/artifact/{artifact_id}", handle_artifact)
    return app


@pytest.fixture
async def client(grove_home):
    app = _make_app(resolve_artifact_roots(config={}))  # default: GROVE_HOME
    async with TestClient(TestServer(app)) as c:
        yield c


def _emit(path_str: str) -> str:
    """File one artifact_written event through the REAL ledger for ``path``
    (identity-canonical form) and return the artifact id."""
    canonical = canonical_artifact_path(path_str)
    aid = artifact_id(canonical)
    ledger = KaizenLedger("artifact-route-test")
    ledger.record(
        "artifact_written", path=canonical, artifact_id=aid, turn_id="t#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
    )
    return aid


def _assert_artifact_headers(resp):
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"


# ---------------------------------------------------------------------------
# Serving classes + headers
# ---------------------------------------------------------------------------


async def test_md_rendered_sanitized(client, grove_home):
    f = grove_home / "note.md"
    f.write_text(
        "# Title\n\n<script>alert(1)</script>\n"
        "<iframe src='http://evil'></iframe>\n"
        "<object data='x'></object><style>p{}</style>\n"
        "[bad](data:text/html;base64,PHNjcmlwdD4=) [ok](https://example.com)\n",
        encoding="utf-8",
    )
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    _assert_artifact_headers(resp)
    body = await resp.text()
    assert "<script" not in body
    assert "<iframe" not in body
    assert "<object" not in body
    assert "<style" not in body
    assert "data:" not in body                    # data: URI stripped
    assert 'href="https://example.com"' in body   # http(s) survives
    assert "<h1>Title</h1>" in body


async def test_txt_served_plain(client, grove_home):
    f = grove_home / "log.txt"
    f.write_text("plain text artifact\n", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    _assert_artifact_headers(resp)
    assert "plain text artifact" in await resp.text()


async def test_binary_is_attachment(client, grove_home):
    f = grove_home / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\xff")
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert resp.content_type == "application/octet-stream"
    assert resp.headers["Content-Disposition"].startswith("attachment")
    _assert_artifact_headers(resp)


async def test_html_artifact_is_attachment_never_inline(client, grove_home):
    f = grove_home / "page.html"
    f.write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert resp.content_type != "text/html"
    assert resp.headers["Content-Disposition"].startswith("attachment")
    _assert_artifact_headers(resp)


# ---------------------------------------------------------------------------
# 404 classes (headers on those too)
# ---------------------------------------------------------------------------


async def test_unknown_id_404(client, grove_home):
    resp = await client.get("/artifact/" + "a" * 16)
    assert resp.status == 404
    _assert_artifact_headers(resp)


@pytest.mark.parametrize("bad", ["zzzz", "A" * 16, "a" * 15, "a" * 17, "..%2f"])
async def test_malformed_id_404(client, grove_home, bad):
    resp = await client.get(f"/artifact/{bad}")
    assert resp.status == 404
    _assert_artifact_headers(resp)


async def test_deleted_after_emit_is_404_not_500(client, grove_home):
    f = grove_home / "gone.md"
    f.write_text("x", encoding="utf-8")
    aid = _emit(str(f))
    f.unlink()
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 404
    _assert_artifact_headers(resp)


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


async def test_symlink_escape_404(client, grove_home, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = grove_home / "innocent.txt"   # inside the allowlisted root...
    link.symlink_to(outside)             # ...pointing outside it
    aid = _emit(str(link))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 404            # resolved form escapes → refused
    _assert_artifact_headers(resp)


async def test_root_that_is_symlink_still_serves(grove_home, tmp_path):
    # The VM shape: the configured root is itself a symlink (~/.grove →
    # /mnt/grove-data). Roots resolve at startup; the file's resolved form
    # lands under the resolved root → contained.
    real_root = tmp_path / "real-data"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root)
    f = link_root / "inside.txt"
    f.write_text("served", encoding="utf-8")
    aid = _emit(str(f))  # identity form preserves the symlink path
    roots = resolve_artifact_roots(
        config={"portal": {"artifact_roots": [str(link_root)]}}
    )
    assert roots == [real_root.resolve()]
    app = _make_app(roots)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get(f"/artifact/{aid}")
        assert resp.status == 200
        assert "served" in await resp.text()


# ---------------------------------------------------------------------------
# Allowlist roots resolution
# ---------------------------------------------------------------------------


def test_failing_root_rejected_loudly_not_silently(grove_home, caplog, tmp_path):
    good = tmp_path / "good-root"
    good.mkdir()
    with caplog.at_level("ERROR"):
        roots = resolve_artifact_roots(config={"portal": {"artifact_roots": [
            str(tmp_path / "does-not-exist"), str(good),
        ]}})
    assert roots == [good.resolve()]
    rejected = [r.getMessage() for r in caplog.records if "REJECTED" in r.getMessage()]
    assert len(rejected) == 1 and "does-not-exist" in rejected[0]


def test_default_root_is_grove_home(grove_home):
    roots = resolve_artifact_roots(config={})
    assert roots == [grove_home.resolve()]


async def test_lazy_index_refreshes_on_miss(client, grove_home):
    # Event filed AFTER the app (and its empty index) was built — the miss
    # triggers a rescan and finds it.
    f = grove_home / "late.txt"
    f.write_text("late artifact", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert "late artifact" in await resp.text()


# ---------------------------------------------------------------------------
# Peer auth (1b patch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer", [
    "127.0.0.1", "::1", "::ffff:127.0.0.1",
    "100.64.0.1", "100.127.255.254", "::ffff:100.64.1.2",
])
def test_peer_authorized_mesh_and_loopback(peer):
    assert _peer_authorized(peer) is True


@pytest.mark.parametrize("peer", [
    None, "", "8.8.8.8", "10.0.0.1", "100.63.255.255", "128.0.0.1",
    "fd00::1", "::ffff:8.8.8.8", "not-an-ip",
])
def test_peer_denied_non_mesh(peer):
    assert _peer_authorized(peer) is False


async def test_artifact_prefix_is_auth_gated():
    # A mocked request has no transport peer → remote is None → denied. The
    # same shape on an ungated path passes through to the handler.
    async def handler(request):
        return web.Response(text="ok")

    gated = make_mocked_request("GET", "/artifact/" + "a" * 16)
    resp = await portal_auth_middleware(gated, handler)
    assert resp.status == 403

    open_path = make_mocked_request("GET", "/health")
    resp = await portal_auth_middleware(open_path, handler)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Cross-ref, both directions
# ---------------------------------------------------------------------------


async def test_artifact_page_shows_ingested_as(client, grove_home):
    f = grove_home / "note-a.md"
    f.write_text("# Brief\n\nbody\n", encoding="utf-8")
    aid = _emit(str(f))
    pages = grove_home / "wiki" / "pages" / "agent_session"
    pages.mkdir(parents=True)
    (pages / f"brief-{aid[:8]}.md").write_text("---\n---\nx", encoding="utf-8")
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    body = await resp.text()
    assert "ingested as" in body
    assert f'href="/portal#fragments/cellar/pages/agent_session/brief-{aid[:8]}"' in body


async def test_artifact_page_no_crossref_without_cellar_match(client, grove_home):
    f = grove_home / "unmatched.md"
    f.write_text("# X\n", encoding="utf-8")
    aid = _emit(str(f))
    resp = await client.get(f"/artifact/{aid}")
    assert resp.status == 200
    assert "ingested as" not in await resp.text()


def test_cellar_detail_source_links_to_artifact(grove_home):
    from grove.api.fragments import _source_artifact_html

    src = str(grove_home / "sink" / "item-x.json")
    html = _source_artifact_html({"source": src})
    expected = artifact_id(canonical_artifact_path(src))
    assert f'href="/artifact/{expected}"' in html
    assert src in html


@pytest.mark.parametrize("src", ["dock.yaml#goal-1", "memory:rec-9", ""])
def test_cellar_detail_nonpath_source_not_linked(grove_home, src):
    from grove.api.fragments import _source_artifact_html

    html = _source_artifact_html({"source": src})
    assert "/artifact/" not in html


# ---------------------------------------------------------------------------
# Read-only: the route set registers no mutation endpoints
# ---------------------------------------------------------------------------


async def test_no_mutation_methods(client, grove_home):
    for method in ("post", "put", "patch", "delete"):
        resp = await getattr(client, method)("/artifact/" + "a" * 16)
        assert resp.status == 405
