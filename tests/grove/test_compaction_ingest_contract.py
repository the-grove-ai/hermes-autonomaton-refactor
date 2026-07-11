"""Sprint R1 (compaction-ingest-contract-v1) — acceptance tests.

Proves the five acceptance criteria as observable behavior:

1. Green auto-flow — POST a scout digest path to the ingest endpoint compacts it
   to a canonical wiki page (the contract the scout SKILL.md terminal curl hits).
2. Idempotency — a second POST of the unchanged file is an mtime-ledger no-op,
   no LLM recompaction.
3. Supersede + atomic tombstone — a second scout digest (same lineage_key)
   unlinks the prior page AND purges its wiki_fts row (no phantom); researcher
   briefs supersede by slug and coexist across slugs.
4. Yellow isolation — a drafter draft under <sink>/pending_review/ is walked past
   by scan_and_ingest; the canonical control is ingested.
5. CLI parity — `hermes wiki ingest <file>` routes through the shared ingest_file
   gate and is idempotent.

The compaction LLM (call_t1) is stubbed deterministically; everything else — the
adapters, ledger, writer, supersede, and FTS tombstone — is the real path.
"""

from __future__ import annotations

import argparse
import json

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import portal_auth_middleware, register_portal_routes
from grove.wiki.index import WikiIndex
from grove.wiki.watcher import ingest_file, scan_and_ingest


# ── deterministic compaction (no LLM), same routing as the pipeline tests ──
class _FakeT1:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        # P2: all three pipeline calls are forced tools — route by NAME.
        self.calls += 1
        if tool["name"] == "wiki_evaluation":
            return {"complete": True, "accurate": True,
                    "quality_score": 0.9, "issues": []}
        return {"title": "Compacted", "topics": ["t"],
                "key_entities": ["e"], "body": "searchbody\n"}


@pytest.fixture
def t1(monkeypatch):
    fake = _FakeT1()
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", fake)
    return fake


@pytest.fixture
def cellar(tmp_path, monkeypatch):
    """Clean isolated cellar under tmp. Returns (home, wiki_root)."""
    home = tmp_path / "home"
    wiki = home / "wiki"
    home.mkdir(parents=True)
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(wiki))
    return home, wiki


@pytest.fixture
async def client(cellar, t1):
    app = web.Application(middlewares=[portal_auth_middleware])
    register_portal_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── source-shape builders (mirror tests/grove/test_wiki_watcher.py) ────────
def _scout_digest(generated_at="2026-06-25T00:00:00Z"):
    return {"generated_at": generated_at, "keyword_clusters_searched": [],
            "opportunities": [], "flagged_for_review": [], "summary": {}}


def _researcher_brief(generated_at="2026-06-25T00:00:00Z"):
    return {"generated_at": generated_at, "source_article": {},
            "operator_intent": {}, "research": {}, "synthesis": {}}


def _drafter_draft():
    fm = {"title": "D", "format": "linkedin", "source_brief": "x", "angle": "a",
          "audience": "y", "word_count": 1, "status": "staged", "drafted_at": "z"}
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\nbody\n"


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(obj) if isinstance(obj, dict) else obj
    path.write_text(body, encoding="utf-8")
    return path


def _md_names(directory):
    return sorted(p.name for p in directory.glob("*.md")) if directory.is_dir() else []


# ═══════════════ 1. GREEN AUTO-FLOW ═══════════════
async def test_green_autoflow_endpoint_compacts(client, cellar):
    home, wiki = cellar
    digest = _write(home / "scout" / "digest-2026-06-25.json", _scout_digest())

    r = await client.post("/api/substrate/ingest", json={"path": str(digest)})
    assert r.status == 200
    body = await r.json()

    assert body["data"]["ingested"] is True
    assert body["data"]["source_type"] == "scout_digest"
    assert body["meta"]["governance_state"] is None  # standard envelope reused
    # a canonical wiki page now exists — no CLI was invoked
    assert len(_md_names(wiki / "pages" / "scout_digest")) == 1


# ═══════════════ 2. IDEMPOTENCY ═══════════════
async def test_idempotent_second_post_is_noop(client, cellar, t1):
    home, wiki = cellar
    digest = _write(home / "scout" / "digest-2026-06-25.json", _scout_digest())

    r1 = await client.post("/api/substrate/ingest", json={"path": str(digest)})
    assert (await r1.json())["data"]["ingested"] is True
    calls_after_first = t1.calls
    assert calls_after_first >= 2  # writer + evaluator ran at least once

    r2 = await client.post("/api/substrate/ingest", json={"path": str(digest)})
    assert (await r2.json())["data"]["ingested"] is False
    assert t1.calls == calls_after_first  # mtime short-circuit: NO recompaction
    assert len(_md_names(wiki / "pages" / "scout_digest")) == 1  # no duplicate


# ═══════════════ 3. SUPERSEDE / ATOMIC TOMBSTONE ═══════════════
def test_scout_supersede_unlinks_and_purges_fts(cellar, t1):
    home, wiki = cellar
    scout_pages = wiki / "pages" / "scout_digest"

    d1 = _write(home / "scout" / "digest-2026-06-25.json", _scout_digest())
    p1 = ingest_file(d1, wiki_root=wiki, hermes_home=home)
    assert p1 is not None

    # build the FTS index so the prior page carries a real wiki_fts row
    idx = WikiIndex(wiki_root=wiki)
    idx.build_index()
    old_rel = str(p1.path.relative_to(wiki / "pages"))
    assert any(r.source_path == old_rel
               for r in idx.query("searchbody", source_type="scout_digest",
                                  ensure_fresh=False))

    # a second digest: different source + content + mtime, SAME lineage_key
    d2 = _write(home / "scout" / "digest-2026-06-26.json",
                _scout_digest("2026-06-26T00:00:00Z"))
    p2 = ingest_file(d2, wiki_root=wiki, hermes_home=home)
    assert p2 is not None and p2.path != p1.path

    assert not p1.path.exists()                    # prior file unlinked
    assert _md_names(scout_pages) == [p2.path.name]  # active count == 1
    # no phantom: the old page's FTS row is gone (no resurrection, no 500)
    phantom = [r for r in idx.query("searchbody", source_type="scout_digest",
                                    ensure_fresh=False)
               if r.source_path == old_rel]
    assert phantom == []


def test_researcher_supersede_by_slug_and_coexist(cellar, t1):
    home, wiki = cellar
    rdir = wiki / "pages" / "researcher_brief"

    b_alpha1 = _write(home / "researcher" / "brief-2026-06-25-alpha.json",
                      _researcher_brief())
    p_alpha1 = ingest_file(b_alpha1, wiki_root=wiki, hermes_home=home)

    # a DIFFERENT slug coexists
    b_beta = _write(home / "researcher" / "brief-2026-06-25-beta.json",
                    _researcher_brief())
    p_beta = ingest_file(b_beta, wiki_root=wiki, hermes_home=home)
    assert len(_md_names(rdir)) == 2

    # the SAME slug on a later date supersedes the prior alpha
    b_alpha2 = _write(home / "researcher" / "brief-2026-06-26-alpha.json",
                      _researcher_brief("2026-06-26T00:00:00Z"))
    p_alpha2 = ingest_file(b_alpha2, wiki_root=wiki, hermes_home=home)

    assert not p_alpha1.path.exists()   # prior alpha superseded
    assert p_beta.path.exists()         # beta (different slug) untouched
    assert p_alpha2.path.exists()       # new alpha present
    assert len(_md_names(rdir)) == 2    # beta + new alpha


# ═══════════════ 4. YELLOW ISOLATION ═══════════════
def test_yellow_pending_review_not_ingested(cellar, t1):
    home, wiki = cellar
    # canonical control — SHOULD ingest
    _write(home / "drafter" / "draft-2026-06-25-canonical.md", _drafter_draft())
    # pending_review staging — must be walked past
    _write(home / "drafter" / "pending_review" / "draft-2026-06-25-staged.md",
           _drafter_draft())

    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)

    assert len(pages) == 1
    assert pages[0].source_type == "drafter_draft"
    # exactly one drafter page reached the cellar — the canonical control
    assert len(_md_names(wiki / "pages" / "drafter_draft")) == 1


# ═══════════════ 5. CLI PARITY ═══════════════
def test_cli_file_branch_is_idempotent(cellar, t1, capsys):
    from hermes_cli.wiki_command import cmd_ingest

    home, wiki = cellar
    digest = _write(home / "scout" / "digest-2026-06-25.json", _scout_digest())
    ns = argparse.Namespace(path=str(digest))

    assert cmd_ingest(ns) == 0
    assert "Ingested 1 page" in capsys.readouterr().out
    calls_after = t1.calls

    assert cmd_ingest(ns) == 0
    assert "No new or changed documents" in capsys.readouterr().out
    assert t1.calls == calls_after  # routed through ingest_file → mtime no-op
