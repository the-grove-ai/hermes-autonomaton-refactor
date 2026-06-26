"""Tests for grove.wiki.adapters — the source Strategy adapters.

Sprint K1 (living-cellar-v1) Phase 3. Four glob-keyed fleet adapters with
heterogeneous per-source parsers, plus one path-invoked operator_curated
adapter. Strict glob (off-glob files ignored at the walk, never errored);
A2 — a file matching its glob but failing its parser shape FAILS LOUD.
"""

from __future__ import annotations

import json

import pytest
import yaml

from grove.wiki.adapters import (
    ADAPTERS,
    FLEET_ADAPTERS,
    MalformedSourceDoc,
    NormalizedDoc,
    fleet_adapter_for,
)


# ── builders for valid declared/live shapes ─────────────────────────────


def _scout_digest(extra=None):
    d = {
        "generated_at": "2026-06-25T00:00:00Z",
        "keyword_clusters_searched": [],
        "opportunities": [],
        "flagged_for_review": [],
        "summary": {},
    }
    if extra:
        d.update(extra)
    return d


def _researcher_brief(extra=None):
    d = {
        "generated_at": "2026-06-25T00:00:00Z",
        "source_article": {},
        "operator_intent": {},
        "research": {},
        "synthesis": {},
    }
    if extra:
        d.update(extra)
    return d


def _cultivator_prospects(extra=None):
    d = {
        "generated_at": "2026-06-25T00:00:00Z",
        "input_source": "topic",
        "input_detail": "x",
        "prospects": [],
        "summary": {},
    }
    if extra:
        d.update(extra)
    return d


def _drafter_draft(fm_extra=None, body="The draft body."):
    fm = {
        "title": "A Draft",
        "format": "linkedin",
        "source_brief": "~/.grove/researcher/brief-x.json",
        "angle": "an angle",
        "audience": "CTOs",
        "word_count": 1200,
        "status": "staged",
        "drafted_at": "2026-06-25T14:52:00Z",
    }
    if fm_extra:
        fm.update(fm_extra)
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n"


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(
        content if isinstance(content, str) else json.dumps(content),
        encoding="utf-8",
    )
    return p


# ── happy-path parsing per adapter ──────────────────────────────────────


def test_scout_digest_parses(tmp_path):
    p = _write(tmp_path, "digest-2026-06-25.json", _scout_digest())
    doc = ADAPTERS["scout_digest"].parse(p)
    assert isinstance(doc, NormalizedDoc)
    assert doc.source_type == "scout_digest"
    assert doc.source_path == str(p)
    assert doc.dock_goal_refs == []
    assert "keyword_clusters_searched" in doc.raw_content


def test_researcher_brief_parses(tmp_path):
    p = _write(tmp_path, "brief-2026-06-25-topic.json", _researcher_brief())
    doc = ADAPTERS["researcher_brief"].parse(p)
    assert doc.source_type == "researcher_brief"
    assert "synthesis" in doc.raw_content


def test_drafter_draft_parses(tmp_path):
    p = _write(tmp_path, "draft-2026-06-25-topic.md", _drafter_draft())
    doc = ADAPTERS["drafter_draft"].parse(p)
    assert doc.source_type == "drafter_draft"
    assert "The draft body." in doc.raw_content


def test_cultivator_prospects_parses(tmp_path):
    p = _write(tmp_path, "prospects-2026-06-25-topic.json", _cultivator_prospects())
    doc = ADAPTERS["cultivator_prospects"].parse(p)
    assert doc.source_type == "cultivator_prospects"


def test_cultivator_marked_unvalidated_against_live(tmp_path):
    assert ADAPTERS["cultivator_prospects"].unvalidated_against_live is True
    # the four-fleet siblings with live instances are NOT flagged
    assert ADAPTERS["scout_digest"].unvalidated_against_live is False


# ── deterministic metadata (adapter-owned) ──────────────────────────────


def test_doc_carries_source_mtime(tmp_path):
    p = _write(tmp_path, "digest-2026-06-25.json", _scout_digest())
    doc = ADAPTERS["scout_digest"].parse(p)
    assert doc.source_mtime == p.stat().st_mtime


def test_dock_goal_refs_extracted_when_present(tmp_path):
    p = _write(
        tmp_path,
        "brief-2026-06-25-topic.json",
        _researcher_brief({"dock_goal_refs": ["grow-fleet", "ship-wiki"]}),
    )
    doc = ADAPTERS["researcher_brief"].parse(p)
    assert doc.dock_goal_refs == ["grow-fleet", "ship-wiki"]


# ── strict glob matching (watcher dispatch) ─────────────────────────────


def test_fleet_adapter_for_matches_each_glob(tmp_path):
    assert fleet_adapter_for("digest-2026-06-25.json").source_type == "scout_digest"
    assert fleet_adapter_for("brief-2026-06-25-x.json").source_type == "researcher_brief"
    assert fleet_adapter_for("draft-2026-06-25-x.md").source_type == "drafter_draft"
    assert (
        fleet_adapter_for("prospects-2026-06-25-x.json").source_type
        == "cultivator_prospects"
    )


def test_off_glob_files_excluded(tmp_path):
    # the known off-contract residue in the researcher sink
    assert fleet_adapter_for("thinkpiece-2026-06-25-x.md") is None
    assert fleet_adapter_for("notes.txt") is None
    assert fleet_adapter_for("README.md") is None
    # operator_curated is path-invoked, never matched by the fleet walk
    assert fleet_adapter_for("anything.md") is None


def test_operator_curated_has_no_glob():
    assert ADAPTERS["operator_curated"].glob is None
    assert ADAPTERS["operator_curated"] not in FLEET_ADAPTERS


# ── A2: glob-match + shape-mismatch fails loud ──────────────────────────


def test_brief_missing_required_key_fails_loud(tmp_path):
    bad = _researcher_brief()
    del bad["synthesis"]
    p = _write(tmp_path, "brief-2026-06-25-x.json", bad)
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["researcher_brief"].parse(p)


def test_digest_not_json_fails_loud(tmp_path):
    p = _write(tmp_path, "digest-2026-06-25.json", "this is not json {")
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["scout_digest"].parse(p)


def test_cultivator_shape_mismatch_fails_loud(tmp_path):
    bad = _cultivator_prospects()
    del bad["prospects"]
    p = _write(tmp_path, "prospects-2026-06-25-x.json", bad)
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["cultivator_prospects"].parse(p)


def test_drafter_no_frontmatter_fails_loud(tmp_path):
    p = _write(tmp_path, "draft-2026-06-25-x.md", "no frontmatter, just prose\n")
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["drafter_draft"].parse(p)


def test_drafter_missing_frontmatter_key_fails_loud(tmp_path):
    p = _write(tmp_path, "draft-2026-06-25-x.md", _drafter_draft({"title": None}))
    # title present-but-empty is a shape violation
    bad = "---\nformat: linkedin\n---\n\nbody\n"  # missing title
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["drafter_draft"].parse(p)


# ── operator_curated (path-invoked) ─────────────────────────────────────


def test_operator_curated_plain_md(tmp_path):
    p = _write(tmp_path, "my-notes.md", "# Heading\n\nplain content, no frontmatter\n")
    doc = ADAPTERS["operator_curated"].parse(p)
    assert doc.source_type == "operator_curated"
    assert "plain content" in doc.raw_content
    assert doc.dock_goal_refs == []


def test_operator_curated_with_frontmatter(tmp_path):
    content = (
        "---\ntitle: Note\ndock_goal_refs: [grow-fleet]\n---\n\nthe body text\n"
    )
    p = _write(tmp_path, "note.md", content)
    doc = ADAPTERS["operator_curated"].parse(p)
    assert doc.dock_goal_refs == ["grow-fleet"]
    assert doc.raw_content.strip() == "the body text"


def test_operator_curated_malformed_frontmatter_best_effort(tmp_path):
    # SPEC: frontmatter is best-effort — unparseable frontmatter is tolerated
    # (treated as body), NOT a fail-loud condition.
    content = "---\ntitle: [unterminated\n---\n\nbody after bad fm\n"
    p = _write(tmp_path, "note.md", content)
    doc = ADAPTERS["operator_curated"].parse(p)
    assert "body after bad fm" in doc.raw_content
    assert doc.dock_goal_refs == []


def test_operator_curated_empty_file_fails_loud(tmp_path):
    p = _write(tmp_path, "empty.md", "   \n  \n")
    with pytest.raises(MalformedSourceDoc):
        ADAPTERS["operator_curated"].parse(p)


def test_operator_curated_txt_accepted(tmp_path):
    p = _write(tmp_path, "thoughts.txt", "just some text\n")
    doc = ADAPTERS["operator_curated"].parse(p)
    assert "just some text" in doc.raw_content


# ── registry ────────────────────────────────────────────────────────────


def test_fleet_adapters_declare_sink_dir():
    sinks = {a.source_type: a.sink_dir for a in FLEET_ADAPTERS}
    assert sinks == {
        "scout_digest": "scout",
        "researcher_brief": "researcher",
        "drafter_draft": "drafter",
        "cultivator_prospects": "cultivator",
    }
    # path-invoked adapter has no sink dir
    assert ADAPTERS["operator_curated"].sink_dir is None


def test_registry_keyed_by_source_type():
    assert set(ADAPTERS) == {
        "scout_digest",
        "researcher_brief",
        "drafter_draft",
        "cultivator_prospects",
        "operator_curated",
    }
    assert {a.source_type for a in FLEET_ADAPTERS} == {
        "scout_digest",
        "researcher_brief",
        "drafter_draft",
        "cultivator_prospects",
    }
