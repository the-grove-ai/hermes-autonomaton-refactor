"""Tests for hermes_cli.wiki_command — the `hermes wiki` CLI.

Sprint K1 (living-cellar-v1) Phase 5. ingest (file→fleet adapter / file→
operator_curated / dir→scan_and_ingest), search (with filters), rebuild —
mirroring hermes_cli/index_command.py. Mocks call_t1; no live API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from hermes_cli.wiki_command import cmd_ingest, cmd_rebuild, cmd_search, register_cli


class _FakeT1:
    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        # P2: all three pipeline calls are forced tools — route by NAME.
        if tool["name"] == "wiki_evaluation":
            return {"complete": True, "accurate": True,
                    "quality_score": 0.88, "issues": []}
        return {"title": "Page", "topics": ["t"],
                "key_entities": ["e"], "body": "the body\n"}


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _FakeT1())
    return tmp_path / "home", tmp_path / "wiki"


def _researcher_brief():
    return {
        "generated_at": "2026-06-25T00:00:00Z",
        "source_article": {},
        "operator_intent": {},
        "research": {},
        "synthesis": {},
    }


def _args(**kw):
    return argparse.Namespace(**kw)


# ── registration ────────────────────────────────────────────────────────


def test_register_cli_wires_subcommands():
    parser = argparse.ArgumentParser(prog="wiki")
    register_cli(parser)
    for argv in (["ingest", "p"], ["search", "q"], ["rebuild"]):
        ns = parser.parse_args(argv)
        assert callable(ns.func)


def test_search_accepts_filters():
    parser = argparse.ArgumentParser(prog="wiki")
    register_cli(parser)
    ns = parser.parse_args(
        ["search", "moat", "--source-type", "scout_digest", "--dock-goal", "g", "-k", "3"]
    )
    assert ns.source_type == "scout_digest"
    assert ns.dock_goal == "g"
    assert ns.k == 3


# ── ingest: file → fleet adapter ────────────────────────────────────────


def test_ingest_file_fleet_adapter(monkeypatch, tmp_path, capsys):
    home, wiki = _env(monkeypatch, tmp_path)
    src = tmp_path / "brief-2026-06-25-x.json"
    src.write_text(json.dumps(_researcher_brief()), encoding="utf-8")
    rc = cmd_ingest(_args(path=str(src)))
    assert rc == 0
    pages = list((wiki / "pages" / "researcher_brief").glob("*.md"))
    assert len(pages) == 1


def test_ingest_file_operator_curated(monkeypatch, tmp_path):
    home, wiki = _env(monkeypatch, tmp_path)
    src = tmp_path / "my-notes.md"
    src.write_text("# Notes\n\nfree-form operator content\n", encoding="utf-8")
    rc = cmd_ingest(_args(path=str(src)))
    assert rc == 0
    pages = list((wiki / "pages" / "operator_curated").glob("*.md"))
    assert len(pages) == 1


# ── ingest: directory → scan_and_ingest ─────────────────────────────────


def test_ingest_dir_scans_fleet_sinks(monkeypatch, tmp_path):
    home, wiki = _env(monkeypatch, tmp_path)
    (home / "researcher").mkdir(parents=True)
    (home / "researcher" / "brief-2026-06-25-x.json").write_text(
        json.dumps(_researcher_brief()), encoding="utf-8"
    )
    rc = cmd_ingest(_args(path=str(home)))
    assert rc == 0
    assert list((wiki / "pages" / "researcher_brief").glob("*.md"))


# ── search ──────────────────────────────────────────────────────────────


def test_search_prints_ranked_results(monkeypatch, tmp_path, capsys):
    home, wiki = _env(monkeypatch, tmp_path)
    # seed one page via the pipeline
    from grove.wiki.adapters import NormalizedDoc
    from grove.wiki.pipeline import compact

    src = tmp_path / "brief-2026-06-25-x.json"
    src.write_text(json.dumps(_researcher_brief()), encoding="utf-8")
    doc = NormalizedDoc(
        source_type="researcher_brief", source_path=str(src),
        source_mtime=src.stat().st_mtime, dock_goal_refs=[],
        raw_content="quantum tunneling diodes",
    )
    # writer body carries the query term (P2: route by forced-tool NAME)
    monkeypatch.setattr(
        "grove.wiki.pipeline.call_t1",
        lambda *a, **k: (
            {"complete": True, "accurate": True, "quality_score": 0.9, "issues": []}
            if k["tool"]["name"] == "wiki_evaluation"
            else {"title": "P", "topics": ["t"], "key_entities": ["e"],
                  "body": "quantum tunneling\n"}
        ),
    )
    compact(doc)
    capsys.readouterr()
    rc = cmd_search(_args(query="quantum tunneling", k=5, source_type=None, dock_goal=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "researcher_brief" in out


def test_search_no_results(monkeypatch, tmp_path, capsys):
    home, wiki = _env(monkeypatch, tmp_path)
    (wiki / "pages").mkdir(parents=True)
    rc = cmd_search(_args(query="nothing here", k=5, source_type=None, dock_goal=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No results" in out


# ── rebuild ─────────────────────────────────────────────────────────────


def test_rebuild_reports_count(monkeypatch, tmp_path, capsys):
    home, wiki = _env(monkeypatch, tmp_path)
    src = tmp_path / "brief-2026-06-25-x.json"
    src.write_text(json.dumps(_researcher_brief()), encoding="utf-8")
    cmd_ingest(_args(path=str(src)))
    capsys.readouterr()
    rc = cmd_rebuild(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "rebuilt" in out.lower()


# ── main.py wiring (structural) ─────────────────────────────────────────


def test_main_py_wires_wiki_verb():
    src = Path("hermes_cli/main.py").read_text(encoding="utf-8")
    assert '"wiki"' in src
    assert "wiki_command import register_cli" in src
