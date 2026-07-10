"""Tests for grove.cellar — the FTS5 cellar retrieval substrate (Sprint 13).

Every test builds a throwaway cellar under tmp_path; no test touches the
operator's real ~/.grove/.
"""

import argparse
import logging
import os
import sqlite3
import time

import pytest

import grove.cellar as cellar
from grove.cellar import (
    CellarIndex,
    CellarResult,
    _extract_title,
    _format_cellar_context,
    _sanitize_fts_query,
    retrieve_cellar_context,
)
from grove.telemetry import log_retrieval
from hermes_cli.index_command import cmd_rebuild, register_cli


# ----- fixtures ---------------------------------------------------------------


def _make_cellar(root):
    """Write a representative cellar (the full D2 set) under ``root``."""
    skills = root / "skills"
    (skills / "weekly-sync").mkdir(parents=True)
    (skills / "weekly-sync" / "SKILL.md").write_text(
        "---\nname: weekly-sync\n---\n# Weekly Sync\n"
        "Generate the weekly sync report from calendar entries and "
        "meeting notes.",
        encoding="utf-8",
    )
    (skills / ".andon" / "draft-tool").mkdir(parents=True)
    (skills / ".andon" / "draft-tool" / "SKILL.md").write_text(
        "---\nname: draft-tool\n---\n"
        "A proposed prototype skill awaiting operator promotion review.",
        encoding="utf-8",
    )
    (root / "soul.md").write_text(
        "---\nname: Autonomaton\n---\nThe operator's sovereign soul.",
        encoding="utf-8",
    )
    (root / "goals.md").write_text(
        "# Goals\nShip grove-autonomaton v0.1.", encoding="utf-8"
    )
    (root / "zones.schema.yaml").write_text(
        "zones:\n  green: {}\n", encoding="utf-8"
    )
    (root / "routing.config.yaml").write_text(
        "routing:\n  default_tier: T2\n", encoding="utf-8"
    )
    (root / "memory.md").write_text(
        "# Memory\nThe operator prefers terse responses.", encoding="utf-8"
    )
    # Must NOT be indexed (D2).
    (root / "telemetry.db").write_text("binary telemetry rows", encoding="utf-8")
    return root


def _index(root):
    return CellarIndex(cellar_dir=root, index_path=root / "index" / "cellar.db")


@pytest.fixture
def temp_cellar(tmp_path, monkeypatch):
    """A populated cellar with CellarIndex repointed at it process-wide,
    so retrieve_cellar_context() and cmd_rebuild() hit tmp, not ~/.grove."""
    root = _make_cellar(tmp_path / "cellar")
    index_path = root / "index" / "cellar.db"
    real = cellar.CellarIndex
    monkeypatch.setattr(
        cellar,
        "CellarIndex",
        lambda: real(cellar_dir=root, index_path=index_path),
    )
    return root


# ----- build / query ----------------------------------------------------------


def test_build_index_indexes_the_d2_set(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    assert idx.build_index() == 7  # telemetry.db excluded; 7 real sources


def test_telemetry_db_is_excluded(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    idx.build_index()
    results = idx.query("binary telemetry rows")
    assert all(r.source_path != "telemetry.db" for r in results)


def test_query_ranks_the_relevant_skill_first(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    results = idx.query("how do I run the weekly sync report")
    assert results
    assert results[0].source_path == "skills/weekly-sync/SKILL.md"


def test_promoted_skill_tagged_skill(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    results = idx.query("weekly sync report calendar")
    top = results[0]
    assert top.content_type == "skill"


def test_proposed_skill_tagged_skill_proposed(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    results = idx.query("proposed prototype awaiting promotion review")
    top = results[0]
    assert top.content_type == "skill_proposed"
    assert ".andon" in top.source_path


def test_identity_and_config_content_types(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    idx.build_index()
    by_path = {
        r.source_path: r.content_type
        for r in idx.query("operator soul goals routing zones terse")
    }
    assert by_path.get("goals.md") == "identity"
    assert by_path.get("routing.config.yaml") == "config"
    assert by_path.get("memory.md") == "memory"


def test_empty_query_returns_empty(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    assert idx.query("") == []
    assert idx.query("   ") == []


def test_no_match_returns_empty(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    assert idx.query("quuxfrobnicate zzyzx") == []


def test_empty_cellar_builds_and_queries_clean(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    idx = _index(empty)
    assert idx.build_index() == 0
    assert idx.query("anything") == []


def test_fts_keyword_tokens_do_not_break_query(tmp_path):
    """Tokens like 'and'/'or'/'not' are quoted, not parsed as operators."""
    idx = _index(_make_cellar(tmp_path / "c"))
    assert idx.query("and or not near the report") is not None  # no crash


def test_lazy_build_on_first_query(tmp_path):
    idx = _index(_make_cellar(tmp_path / "c"))
    assert not idx.index_path.exists()
    idx.query("weekly sync")
    assert idx.index_path.exists()


def test_incremental_reindex_on_mtime_change(tmp_path):
    root = _make_cellar(tmp_path / "c")
    idx = _index(root)
    idx.build_index()
    goals = root / "goals.md"
    goals.write_text("# Goals\nThe operator now wants a quantum compiler.")
    future = time.time() + 1000
    os.utime(goals, (future, future))
    results = idx.query("quantum compiler")
    assert any(r.source_path == "goals.md" for r in results)


def test_incremental_drops_deleted_file(tmp_path):
    root = _make_cellar(tmp_path / "c")
    idx = _index(root)
    idx.build_index()
    (root / "goals.md").unlink()
    results = idx.query("ship grove-autonomaton goals")
    assert all(r.source_path != "goals.md" for r in results)


def test_index_path_property(tmp_path):
    idx = _index(tmp_path / "c")
    assert idx.index_path == tmp_path / "c" / "index" / "cellar.db"


# ----- helpers ----------------------------------------------------------------


def test_extract_title_from_frontmatter(tmp_path):
    title = _extract_title("---\nname: weekly-sync\n---\nbody", tmp_path / "x.md")
    assert title == "weekly-sync"


def test_extract_title_from_heading(tmp_path):
    assert _extract_title("# My Heading\ntext", tmp_path / "x.md") == "My Heading"


def test_extract_title_filename_fallback(tmp_path):
    assert _extract_title("plain text, no heading", tmp_path / "notes.md") == "notes"


def test_sanitize_quotes_each_token_and_or_joins():
    assert _sanitize_fts_query("Weekly Sync Report") == (
        '"weekly" OR "sync" OR "report"'
    )
    assert _sanitize_fts_query("") == ""
    assert _sanitize_fts_query("a I") == ""  # single chars dropped


# ----- retrieve_cellar_context ------------------------------------------------


def test_retrieve_returns_cellar_context_block(temp_cellar):
    block = retrieve_cellar_context("how do I run the weekly sync report")
    assert block.startswith("<cellar_context>")
    assert block.rstrip().endswith("</cellar_context>")
    assert "<result source=" in block
    assert "weekly-sync" in block


def test_retrieve_non_str_returns_empty():
    assert retrieve_cellar_context([{"type": "image"}]) == ""
    assert retrieve_cellar_context(None) == ""


def test_retrieve_empty_message_returns_empty():
    assert retrieve_cellar_context("   ") == ""


def test_retrieve_applies_relevance_floor(monkeypatch):
    fake = [
        CellarResult("strong.md", "identity", "Strong", "strong match", 0.9),
        CellarResult("weak.md", "identity", "Weak", "weak match", 0.05),
    ]
    monkeypatch.setattr(CellarIndex, "query", lambda self, text, k=5: fake)
    block = retrieve_cellar_context("anything")
    assert "strong.md" in block
    assert "weak.md" not in block  # 0.05 is below the 0.1 floor


def test_retrieve_graceful_on_error(monkeypatch, caplog):
    def _boom(self, text, k=5):
        raise sqlite3.DatabaseError("index corrupt")

    monkeypatch.setattr(CellarIndex, "query", _boom)
    with caplog.at_level(logging.ERROR, logger="grove.cellar"):
        result = retrieve_cellar_context("anything")
    assert result == ""  # interaction proceeds; RAG never gates it
    assert "retrieval failed" in caplog.text


def test_retrieve_emits_telemetry(temp_cellar, caplog):
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        retrieve_cellar_context("how do I run the weekly sync report")
    assert "retrieval" in caplog.text


# ----- _format_cellar_context -------------------------------------------------


def test_format_emits_d7_result_blocks():
    results = [
        CellarResult("skills/a/SKILL.md", "skill", "A", "alpha body", 0.92),
        CellarResult("goals.md", "identity", "Goals", "goals body", 0.41),
    ]
    block = _format_cellar_context(results)
    assert '<result source="skills/a/SKILL.md" type="skill" relevance="0.92">' in block
    assert "alpha body" in block
    assert block.count("<result ") == 2


def test_format_budget_drops_low_ranked_results():
    big = "x" * 3000
    results = [
        CellarResult(f"f{i}.md", "identity", f"F{i}", big, 1.0 - i * 0.1)
        for i in range(5)
    ]
    block = _format_cellar_context(results)
    assert block.count("<result ") < 5  # budget caps inclusion
    assert "f0.md" in block  # strongest hit kept


def test_format_keeps_strongest_even_when_alone_over_budget():
    huge = "y" * 20000
    block = _format_cellar_context(
        [CellarResult("big.md", "identity", "Big", huge, 1.0)]
    )
    assert "big.md" in block


# ----- log_retrieval ----------------------------------------------------------


def test_log_retrieval_event_shape_and_no_content(caplog):
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        event = log_retrieval(
            sources=["goals.md", "soul.md"],
            content_types=["identity", "identity"],
            scores=[0.9, 0.4],
        )
    assert event["event_type"] == "retrieval"
    assert event["result_count"] == 2
    assert event["sources"] == ["goals.md", "soul.md"]
    assert event["scores"] == [0.9, 0.4]
    # D8: paths and scores only — never the retrieved content.
    assert not any(k in event for k in ("snippet", "body", "content", "title"))
    assert "retrieval" in caplog.text


# ----- hermes index rebuild verb ----------------------------------------------


def test_index_verb_wires_rebuild_and_bare_default():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    register_cli(subs.add_parser("index"))
    assert parser.parse_args(["index", "rebuild"]).func is cmd_rebuild
    assert parser.parse_args(["index"]).func is cmd_rebuild  # bare → rebuild


def test_cmd_rebuild_rebuilds_and_reports(temp_cellar, capsys):
    rc = cmd_rebuild(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuilt" in out
    assert "7 file" in out  # the D2 set from _make_cellar


# ----- promoted-artifact-persistence-v1 P4 — canonical-only corpus ------------


def _make_workspace_cellar(root):
    """A drafter-shaped workspace exercising all four P4 filter classes:
    flat canonical (kept), nested non-dot subdir (kept), pending_review
    (staged — excluded), .archive (rejected — excluded), .feedback
    (dot-dir — excluded), .andon (excluded, prior-skip subsumed)."""
    ws = root / "drafter"
    ws.mkdir(parents=True)
    (ws / "draft-approved.md").write_text("canonical approved zebra draft")
    (ws / "series").mkdir()
    (ws / "series" / "draft-part-two.md").write_text("canonical zebra sequel")
    (ws / "pending_review" / "u1").mkdir(parents=True)
    (ws / "pending_review" / "u1" / "draft-staged.md").write_text(
        "staged unapproved zebra draft")
    (ws / ".archive" / "u2-20260101T000000Z").mkdir(parents=True)
    (ws / ".archive" / "u2-20260101T000000Z" / "draft-rejected.md").write_text(
        "rejected zebra draft")
    (ws / ".feedback").mkdir()
    (ws / ".feedback" / "note.md").write_text("dot-dir zebra residue")
    (ws / ".andon" / "q").mkdir(parents=True)
    (ws / ".andon" / "q" / "draft-quarantined.md").write_text(
        "quarantined zebra draft")
    return ws


def test_p4_filter_excludes_staged_archived_dotdirs_keeps_canonical(tmp_path):
    """Verdict A: staged/archived/dot-dir excluded; flat canonical AND nested
    non-dot subdir content still indexed; .andon still skipped."""
    root = tmp_path / "cellar"
    _make_workspace_cellar(root)
    idx = CellarIndex(cellar_dir=root, index_path=root / "index" / "cellar.db")
    idx.build_index()
    indexed = {
        p for p, _t in ((s, t) for s, t in idx._iter_sources())
    }
    names = sorted(p.name for p in indexed)
    assert names == ["draft-approved.md", "draft-part-two.md"]
    # retrieval sees only the canonical entries
    results = idx.query("zebra", k=10)
    got = sorted(r.source_path for r in results)
    assert got == ["drafter/draft-approved.md", "drafter/series/draft-part-two.md"]


def test_p4_removal_only_against_raw_glob(tmp_path):
    """Verdict B: the enumeration equals the raw **/*.md walk MINUS exactly
    the excluded classes — removals only, no additions."""
    root = tmp_path / "cellar"
    ws = _make_workspace_cellar(root)
    idx = CellarIndex(cellar_dir=root, index_path=root / "index" / "cellar.db")
    raw = set(ws.glob("**/*.md"))
    excluded = {
        p for p in raw
        if any(seg == "pending_review" or seg.startswith(".")
               for seg in p.relative_to(ws).parts[:-1])
    }
    enumerated = {p for p, _t in idx._iter_sources()}
    assert enumerated == raw - excluded  # removal-only, structurally exact
    assert len(excluded) == 4  # staged + archived + feedback + andon


def test_p4_stale_leaked_entries_evicted_on_refresh(tmp_path):
    """V3 pin: entries indexed under the OLD enumeration (leaked staged file)
    are evicted by update_index's stale-drop when they leave enumeration."""
    root = tmp_path / "cellar"
    ws = _make_workspace_cellar(root)
    idx = CellarIndex(cellar_dir=root, index_path=root / "index" / "cellar.db")
    idx.build_index()
    # simulate a pre-P4 leaked row: inject the staged file directly
    import sqlite3 as _sq
    conn = _sq.connect(idx.index_path)
    conn.execute(
        "INSERT INTO cellar_fts (source_path, content_type, title, body) "
        "VALUES (?, ?, ?, ?)",
        ("drafter/pending_review/u1/draft-staged.md", "drafter", "leak",
         "staged unapproved zebra draft"),
    )
    conn.execute(
        "INSERT INTO cellar_meta (source_path, mtime) VALUES (?, ?)",
        ("drafter/pending_review/u1/draft-staged.md", 0.0),
    )
    conn.commit(); conn.close()
    idx.update_index()  # the deploy-time refresh path
    results = idx.query("zebra", k=10)
    assert all("pending_review" not in r.source_path for r in results)


def test_p4_filter_is_producer_blind():
    """Verdict C: the workspace filter is path-segment structural — zero
    producer names in _iter_sources beyond the declared subdir/content-type
    table itself (which predates P4 and is data, not branching)."""
    import inspect

    src = inspect.getsource(CellarIndex._iter_sources)
    # the filter expression itself names no producer:
    filter_lines = [l for l in src.splitlines() if "pending_review" in l]
    assert filter_lines, "structural filter missing"
    for line in filter_lines:
        for name in ("forge", "cultivator", "researcher"):
            assert name not in line
