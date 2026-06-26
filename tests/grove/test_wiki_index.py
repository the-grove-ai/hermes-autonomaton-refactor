"""Tests for grove.wiki.index — the WikiIndex FTS5 store.

Sprint K1 (living-cellar-v1) Phase 2. WikiIndex mirrors the CellarIndex
pattern (FTS5 + mtime-meta + bm25 0-1 normalize) but is a SEPARATE index with
DEDICATED frontmatter columns (source_type, dock_goal_refs, topics,
key_entities, confidence) supporting filter + boost. Malformed pages fail
loud — never a silent skip into the index.
"""

from __future__ import annotations

import os

import pytest
import yaml

from grove.wiki.index import MalformedWikiPage, WikiIndex


# ── helpers ─────────────────────────────────────────────────────────────


def _write_page(
    root,
    rel,
    *,
    source_type="researcher_brief",
    title="A Page",
    body="placeholder body text",
    topics=None,
    key_entities=None,
    dock_goal_refs=None,
    confidence=0.5,
    raw=None,
):
    """Write a wiki page under <root>/pages/<rel> with YAML frontmatter.

    ``raw`` overrides the entire file content (for malformed-page tests)."""
    path = root / "pages" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    fm = {"source_type": source_type, "title": title}
    if topics is not None:
        fm["topics"] = topics
    if key_entities is not None:
        fm["key_entities"] = key_entities
    if dock_goal_refs is not None:
        fm["dock_goal_refs"] = dock_goal_refs
    if confidence is not None:
        fm["confidence"] = confidence
    content = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def _idx(tmp_path):
    return WikiIndex(wiki_root=tmp_path)


# ── path resolution ─────────────────────────────────────────────────────
# (root resolution lives in hermes_constants.get_wiki_path() — see
# test_wiki_paths.py; here we only confirm the index db location.)


def test_index_db_lives_under_wiki_root(tmp_path):
    idx = WikiIndex(wiki_root=tmp_path)
    assert idx.index_path == tmp_path / ".index" / "wiki.db"


# ── FTS retrieval ───────────────────────────────────────────────────────


def test_build_and_query_returns_match(tmp_path):
    _write_page(tmp_path, "researcher_brief/alpha.md", body="quantum tunneling diodes")
    _write_page(tmp_path, "researcher_brief/beta.md", body="garden composting soil")
    idx = _idx(tmp_path)
    idx.build_index()
    results = idx.query("quantum tunneling", k=5)
    assert [r.source_path for r in results] == ["researcher_brief/alpha.md"]


def test_empty_query_returns_empty(tmp_path):
    _write_page(tmp_path, "researcher_brief/alpha.md", body="anything")
    idx = _idx(tmp_path)
    idx.build_index()
    assert idx.query("", k=5) == []
    assert idx.query("   ", k=5) == []


def test_topics_and_entities_are_searchable(tmp_path):
    _write_page(
        tmp_path,
        "researcher_brief/alpha.md",
        body="unrelated body",
        topics=["photonics"],
        key_entities=["Acme Corp"],
    )
    idx = _idx(tmp_path)
    idx.build_index()
    assert [r.source_path for r in idx.query("photonics", k=5)] == [
        "researcher_brief/alpha.md"
    ]
    assert [r.source_path for r in idx.query("Acme", k=5)] == [
        "researcher_brief/alpha.md"
    ]


# ── source_type filter ──────────────────────────────────────────────────


def test_source_type_filter(tmp_path):
    _write_page(
        tmp_path, "researcher_brief/a.md", source_type="researcher_brief",
        body="shared topic moat",
    )
    _write_page(
        tmp_path, "scout_digest/b.md", source_type="scout_digest",
        body="shared topic moat",
    )
    idx = _idx(tmp_path)
    idx.build_index()
    results = idx.query("moat", k=5, source_type="scout_digest")
    assert [r.source_path for r in results] == ["scout_digest/b.md"]
    assert all(r.source_type == "scout_digest" for r in results)


# ── boosts ──────────────────────────────────────────────────────────────


def test_dock_goal_match_boosts_ranking(tmp_path):
    # Identical body + confidence → identical base relevance; the dock_goal
    # match must break the tie in favor of the matching page.
    _write_page(
        tmp_path, "researcher_brief/with.md", body="moat moat moat",
        confidence=0.5, dock_goal_refs=["grow-fleet"],
    )
    _write_page(
        tmp_path, "researcher_brief/without.md", body="moat moat moat",
        confidence=0.5, dock_goal_refs=[],
    )
    idx = _idx(tmp_path)
    idx.build_index()
    results = idx.query("moat", k=5, dock_goal="grow-fleet")
    assert results[0].source_path == "researcher_brief/with.md"
    # boost, not filter — the non-matching page still appears
    assert {r.source_path for r in results} == {
        "researcher_brief/with.md",
        "researcher_brief/without.md",
    }


def test_confidence_boosts_ranking(tmp_path):
    _write_page(
        tmp_path, "researcher_brief/high.md", body="moat moat moat", confidence=0.9
    )
    _write_page(
        tmp_path, "researcher_brief/low.md", body="moat moat moat", confidence=0.1
    )
    idx = _idx(tmp_path)
    idx.build_index()
    results = idx.query("moat", k=5)
    assert results[0].source_path == "researcher_brief/high.md"


# ── mtime-incremental update ────────────────────────────────────────────


def test_update_index_adds_new_page(tmp_path):
    _write_page(tmp_path, "researcher_brief/a.md", body="alpha content")
    idx = _idx(tmp_path)
    idx.build_index()
    _write_page(tmp_path, "researcher_brief/b.md", body="bravo content")
    idx.update_index()
    assert [r.source_path for r in idx.query("bravo", k=5)] == [
        "researcher_brief/b.md"
    ]


def test_update_index_drops_deleted_page(tmp_path):
    _write_page(tmp_path, "researcher_brief/a.md", body="alpha content")
    p = _write_page(tmp_path, "researcher_brief/b.md", body="bravo content")
    idx = _idx(tmp_path)
    idx.build_index()
    p.unlink()
    idx.update_index()
    assert idx.query("bravo", k=5) == []
    assert [r.source_path for r in idx.query("alpha", k=5)] == [
        "researcher_brief/a.md"
    ]


def test_update_index_reindexes_modified_page(tmp_path):
    p = _write_page(tmp_path, "researcher_brief/a.md", body="original term")
    idx = _idx(tmp_path)
    idx.build_index()
    _write_page(tmp_path, "researcher_brief/a.md", body="replacement term")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
    idx.update_index()
    assert idx.query("replacement", k=5)
    assert idx.query("original", k=5) == []


# ── fail loud on malformed pages ────────────────────────────────────────


def test_unparseable_frontmatter_fails_loud(tmp_path):
    _write_page(
        tmp_path, "researcher_brief/bad.md",
        raw="---\nsource_type: [unterminated\n---\nbody",
    )
    idx = _idx(tmp_path)
    with pytest.raises(MalformedWikiPage):
        idx.build_index()


def test_missing_frontmatter_fails_loud(tmp_path):
    _write_page(tmp_path, "researcher_brief/bad.md", raw="no frontmatter here\n")
    idx = _idx(tmp_path)
    with pytest.raises(MalformedWikiPage):
        idx.build_index()


def test_missing_required_field_fails_loud(tmp_path):
    _write_page(
        tmp_path, "researcher_brief/bad.md",
        raw="---\ntitle: No Source Type\n---\nbody",
    )
    idx = _idx(tmp_path)
    with pytest.raises(MalformedWikiPage):
        idx.build_index()
