"""notes-research-ingest-v1 — attended-session ambient auto-ingest.

The poll walks ~/.grove/notes/ and ~/.grove/research/ under the agent_session
label (honest provenance — NOT operator_curated). Pins:

* INGEST — .md/.txt in both dirs compact to pages/agent_session/ (the portal
  category is that subdir; the URL scheme follows automatically).
* LABEL — source_type is agent_session, never operator_curated.
* MTIME NO-OP — a second scan re-ingests nothing (shared ledger).
* QUARANTINE ISOLATION — one malformed file is quarantined per-file; the
  healthy file in the same dir still ingests.
* ABSENT-DIR SKIP — a missing ambient dir is a silent no-op.
* STRICT GLOB — a .json residue file is ignored, never errored.
* EXPLICIT-PATH LABEL — ingest_file on a research/ file also earns
  agent_session (the no-second-ingest-path symmetry).
* OPERATOR_CURATED BYTE-PARITY — the plain-text base refactor leaves the
  operator_curated NormalizedDoc output unchanged.
"""
from __future__ import annotations

import json

import pytest
import yaml

from grove.wiki.watcher import ingest_file, scan_and_ingest


class _FakeT1:
    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        if tool["name"] == "wiki_evaluation":
            return {"complete": True, "accurate": True,
                    "quality_score": 0.9, "issues": []}
        return {"title": "Compacted", "topics": ["t"],
                "key_entities": ["e"], "body": "body\n"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _FakeT1())
    home = tmp_path / "home"
    home.mkdir()
    wiki = tmp_path / "wiki"
    return home, wiki


def _pages_by_source_type(wiki):
    out = {}
    pages_dir = wiki / "pages"
    if not pages_dir.is_dir():
        return out
    for p in pages_dir.glob("**/*.md"):
        meta = yaml.safe_load(p.read_text(encoding="utf-8").split("---")[1])
        out.setdefault(meta["source_type"], []).append(p)
    return out


# ── INGEST + LABEL ───────────────────────────────────────────────────────────


def test_both_dirs_ingest_under_agent_session(env):
    home, wiki = env
    (home / "notes").mkdir()
    (home / "research").mkdir()
    (home / "notes" / "meeting.md").write_text("# Meeting\nnotes body\n", encoding="utf-8")
    (home / "research" / "topic.txt").write_text("research findings\n", encoding="utf-8")

    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(pages) == 2
    assert all(p.source_type == "agent_session" for p in pages)
    # portal category = the subdir; both pages land under pages/agent_session/
    st = _pages_by_source_type(wiki)
    assert set(st) == {"agent_session"}
    assert len(st["agent_session"]) == 2
    # the URL scheme follows: pages/agent_session/<slug>-<hash>.md
    for p in st["agent_session"]:
        assert p.parent.name == "agent_session"


def test_label_is_not_operator_curated(env):
    home, wiki = env
    (home / "research").mkdir()
    (home / "research" / "x.md").write_text("body\n", encoding="utf-8")
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert pages[0].source_type == "agent_session"
    assert "operator_curated" not in _pages_by_source_type(wiki)


# ── MTIME NO-OP ──────────────────────────────────────────────────────────────


def test_second_scan_reingests_nothing(env):
    home, wiki = env
    (home / "notes").mkdir()
    (home / "notes" / "a.md").write_text("body\n", encoding="utf-8")
    assert len(scan_and_ingest(wiki_root=wiki, hermes_home=home)) == 1
    assert scan_and_ingest(wiki_root=wiki, hermes_home=home) == []


# ── QUARANTINE ISOLATION ─────────────────────────────────────────────────────


def test_malformed_file_quarantined_healthy_ingests(env):
    home, wiki = env
    (home / "research").mkdir()
    # empty file → MalformedSourceDoc (fails loud PER FILE)
    (home / "research" / "empty.md").write_text("   \n", encoding="utf-8")
    (home / "research" / "good.md").write_text("real body\n", encoding="utf-8")
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)  # must not raise
    assert len(pages) == 1
    assert pages[0].source == str(home / "research" / "good.md")


# ── ABSENT-DIR SKIP ──────────────────────────────────────────────────────────


def test_absent_ambient_dirs_are_silent_noop(env):
    home, wiki = env  # neither notes/ nor research/ created
    assert scan_and_ingest(wiki_root=wiki, hermes_home=home) == []  # must not raise


def test_one_absent_one_present(env):
    home, wiki = env
    (home / "research").mkdir()  # notes/ absent
    (home / "research" / "x.md").write_text("body\n", encoding="utf-8")
    assert len(scan_and_ingest(wiki_root=wiki, hermes_home=home)) == 1


# ── STRICT GLOB ──────────────────────────────────────────────────────────────


def test_json_residue_ignored_never_errored(env):
    home, wiki = env
    (home / "research").mkdir()
    (home / "research" / "leftover.json").write_text('{"k": "v"}', encoding="utf-8")
    (home / "research" / "real.md").write_text("body\n", encoding="utf-8")
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(pages) == 1  # only the .md; .json silently ignored
    assert pages[0].source.endswith("real.md")


# ── EXPLICIT-PATH LABEL (no-second-ingest-path symmetry) ─────────────────────


def test_ingest_file_on_research_path_labels_agent_session(env, monkeypatch):
    home, wiki = env
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(home))
    (home / "research").mkdir()
    f = home / "research" / "manual.md"
    f.write_text("manual body\n", encoding="utf-8")
    page = ingest_file(f, wiki_root=wiki, hermes_home=home)
    assert page is not None and page.source_type == "agent_session"


def test_ingest_file_outside_ambient_stays_operator_curated(env, monkeypatch):
    home, wiki = env
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(home))
    loose = home / "loose.md"
    loose.write_text("loose body\n", encoding="utf-8")
    page = ingest_file(loose, wiki_root=wiki, hermes_home=home)
    assert page is not None and page.source_type == "operator_curated"


def test_nested_subdir_under_research_not_agent_session(env, monkeypatch):
    """Only files ONE level in match the flat-surface scan; a nested path
    resolves through the normal fallback (operator_curated)."""
    home, wiki = env
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(home))
    nested = home / "research" / "sub"
    nested.mkdir(parents=True)
    f = nested / "deep.md"
    f.write_text("deep body\n", encoding="utf-8")
    page = ingest_file(f, wiki_root=wiki, hermes_home=home)
    assert page.source_type == "operator_curated"


# ── OPERATOR_CURATED BYTE-PARITY (the base refactor) ─────────────────────────


def test_operator_curated_normalizeddoc_unchanged(tmp_path):
    """The shared-base refactor must leave operator_curated's parse OUTPUT
    byte-identical — same fields, same frontmatter/body split, same
    dock_goal_refs extraction (refs valid per the M4 adapter door, so the
    dock-goal-ref-integrity-v1 validation passes them through untouched)."""
    import os
    from pathlib import Path

    import yaml as _yaml

    from grove.wiki.adapters import ADAPTERS

    # Minimal dock in the hermetic GROVE_HOME so g1/g2 are real goal ids —
    # this test pins parse-shape parity, not the M4 door (covered in
    # test_wiki_adapters).
    dock_dir = Path(os.environ["GROVE_HOME"]) / "dock"
    dock_dir.mkdir(parents=True, exist_ok=True)
    (dock_dir / "dock.yaml").write_text(
        _yaml.safe_dump({
            "version": 1,
            "goals": [
                {
                    "id": gid, "name": gid.upper(), "vector": "strategic",
                    "status": "accelerating", "definition_of_done": "d",
                    "context_sources": [], "keywords": [],
                    "unlocked_skills": [],
                }
                for gid in ("g1", "g2")
            ],
        }),
        encoding="utf-8",
    )

    p = tmp_path / "note.md"
    p.write_text(
        "---\ndock_goal_refs: [g1, g2]\ntitle: T\n---\nthe body\n", encoding="utf-8"
    )
    doc = ADAPTERS["operator_curated"].parse(p)
    assert doc.source_type == "operator_curated"
    assert doc.source_path == str(p)
    assert doc.dock_goal_refs == ["g1", "g2"]
    assert doc.raw_content == "the body\n"
    assert doc.lineage_key is None

    # agent_session shares the EXACT parse, distinct label only
    doc2 = ADAPTERS["agent_session"].parse(p)
    assert doc2.source_type == "agent_session"
    assert (doc2.source_path, doc2.dock_goal_refs, doc2.raw_content,
            doc2.lineage_key) == (
        doc.source_path, doc.dock_goal_refs, doc.raw_content, doc.lineage_key
    )
