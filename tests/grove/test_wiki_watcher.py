"""Tests for grove.wiki.watcher — lazy scan-and-ingest over the fleet sinks.

Sprint K1 (living-cellar-v1) Phase 5. The watcher walks the four fleet sink
dirs (derived from FLEET_ADAPTERS) under the hermes home, glob-matches each
with its adapter, skips files unchanged by mtime, and compacts new/changed docs
through the pipeline. It tolerates absent dirs (cultivator), ignores off-glob
files, fails loud on a glob-matching malformed file (A2), and uses NO event
watcher / NO write_file hook — lazy/poll only.
"""

from __future__ import annotations

import inspect
import json
import os

import pytest
import yaml

import grove.wiki.watcher as watcher
from grove.wiki.adapters import MalformedSourceDoc
from grove.wiki.watcher import scan_and_ingest


# ── fake T1 (same routing as the pipeline tests) ────────────────────────


class _FakeT1:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        self.calls += 1
        if tool is not None:
            return {"complete": True, "accurate": True,
                    "quality_score": 0.9, "issues": []}
        return (
            "---\ntitle: Compacted\ntopics: [t]\nkey_entities: [e]\n---\n\nbody\n"
        )


def _install_t1(monkeypatch):
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _FakeT1())


def _scout_digest():
    return {
        "generated_at": "2026-06-25T00:00:00Z",
        "keyword_clusters_searched": [],
        "opportunities": [],
        "flagged_for_review": [],
        "summary": {},
    }


def _researcher_brief():
    return {
        "generated_at": "2026-06-25T00:00:00Z",
        "source_article": {},
        "operator_intent": {},
        "research": {},
        "synthesis": {},
    }


def _drafter_draft():
    fm = {
        "title": "D", "format": "linkedin", "source_brief": "x", "angle": "a",
        "audience": "y", "word_count": 1, "status": "staged", "drafted_at": "z",
    }
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\nbody\n"


def _home_with_sinks(tmp_path, *, with_cultivator=False, extra=None):
    """Build a fake hermes home with scout/researcher/drafter sinks populated
    (cultivator optional). Returns (home, wiki_root)."""
    home = tmp_path / "home"
    (home / "scout").mkdir(parents=True)
    (home / "researcher").mkdir(parents=True)
    (home / "drafter").mkdir(parents=True)
    (home / "scout" / "digest-2026-06-25.json").write_text(
        json.dumps(_scout_digest()), encoding="utf-8"
    )
    (home / "researcher" / "brief-2026-06-25-x.json").write_text(
        json.dumps(_researcher_brief()), encoding="utf-8"
    )
    (home / "drafter" / "draft-2026-06-25-x.md").write_text(
        _drafter_draft(), encoding="utf-8"
    )
    if with_cultivator:
        (home / "cultivator").mkdir(parents=True)
    return home, tmp_path / "wiki"


# ── walk + glob-match ───────────────────────────────────────────────────


def test_scan_ingests_each_populated_sink(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    got = {p.source_type for p in pages}
    assert got == {"scout_digest", "researcher_brief", "drafter_draft"}
    # pages physically written under the wiki pages tree
    for st in got:
        assert list((wiki / "pages" / st).glob("*.md"))


def test_off_glob_files_ignored(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    # off-contract residue in the researcher sink — must be skipped
    (home / "researcher" / "thinkpiece-2026-06-25-x.md").write_text(
        "not a brief", encoding="utf-8"
    )
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    briefs = [p for p in pages if p.source_type == "researcher_brief"]
    assert len(briefs) == 1  # only the brief-*.json, not the thinkpiece


# ── mtime skip / re-ingest ──────────────────────────────────────────────


def test_unchanged_files_skipped_on_second_scan(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    first = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(first) == 3
    second = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert second == []  # nothing changed → nothing re-ingested


def test_changed_file_is_reingested(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    brief = home / "researcher" / "brief-2026-06-25-x.json"
    brief.write_text(json.dumps(_researcher_brief()), encoding="utf-8")
    os.utime(brief, (brief.stat().st_atime, brief.stat().st_mtime + 10))
    again = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert [p.source_type for p in again] == ["researcher_brief"]


# ── absent dir tolerance / A2 fail-loud ─────────────────────────────────


def test_absent_cultivator_dir_tolerated(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path, with_cultivator=False)
    assert not (home / "cultivator").exists()
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)  # must not raise
    assert "cultivator_prospects" not in {p.source_type for p in pages}


def test_glob_match_malformed_file_fails_loud(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    # a brief-*.json that matches the glob but violates the shape
    (home / "researcher" / "brief-2026-06-25-bad.json").write_text(
        json.dumps({"generated_at": "x"}), encoding="utf-8"
    )
    with pytest.raises(MalformedSourceDoc):
        scan_and_ingest(wiki_root=wiki, hermes_home=home)


# ── lazy/poll only — no event watcher, no write hook ────────────────────


def test_no_event_watcher_or_write_hook():
    # Strip docstrings (they name the forbidden mechanisms to say there are
    # none) and scan the actual code.
    import re

    src = inspect.getsource(watcher)
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    for forbidden in ("inotify", "watchdog", "Observer", "write_file"):
        assert forbidden not in code


def test_ledger_persists_between_scans(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    ledger = wiki / ".index" / "ingest_state.json"
    assert ledger.exists()
    data = json.loads(ledger.read_text())
    assert any("brief-2026-06-25-x.json" in k for k in data)
