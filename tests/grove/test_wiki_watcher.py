"""Tests for grove.wiki.watcher — lazy scan-and-ingest over the fleet sinks.

Sprint K1 (living-cellar-v1) Phase 5. The watcher walks the four fleet sink
dirs (derived from FLEET_ADAPTERS) under the hermes home, glob-matches each
with its adapter, skips files unchanged by mtime, and compacts new/changed docs
through the pipeline. It tolerates absent dirs (cultivator), ignores off-glob
files, fails loud on a glob-matching malformed file (A2), and uses NO event
watcher / NO write_file hook — lazy/poll only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time

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


# ════════════════════════════════════════════════════════════════════════════
# P3 — Dock observed-target branch (parallel to the FLEET_ADAPTERS loop).
# ════════════════════════════════════════════════════════════════════════════


def _dock_goal(**over):
    g = {
        "id": "a",
        "name": "Goal A",
        "vector": "strategic",
        "status": "accelerating",
        "definition_of_done": "done",
        "context_sources": [],
        "keywords": ["alpha"],
        "unlocked_skills": [],
    }
    g.update(over)
    return g


def _write_home_dock(home, goals):
    d = home / "dock"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "dock.yaml"
    p.write_text(
        yaml.safe_dump({"version": 1, "goals": goals}), encoding="utf-8"
    )
    return p


def _dock_pages(wiki):
    return list((wiki / "pages" / "dock_goal").glob("*.md"))


def test_dock_present_triggers_projection(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = tmp_path / "home", tmp_path / "wiki"
    _write_home_dock(home, [_dock_goal(id="a"), _dock_goal(id="b")])
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len([p for p in pages if p.source_type == "dock_goal"]) == 2
    assert len(_dock_pages(wiki)) == 2


def test_dock_unchanged_mtime_skips(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = tmp_path / "home", tmp_path / "wiki"
    _write_home_dock(home, [_dock_goal(id="a")])
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    second = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert [p for p in second if p.source_type == "dock_goal"] == []


def test_dock_mtime_change_retriggers(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = tmp_path / "home", tmp_path / "wiki"
    dp = _write_home_dock(home, [_dock_goal(id="a", name="One")])
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    _write_home_dock(home, [_dock_goal(id="a", name="Two"), _dock_goal(id="b")])
    os.utime(dp, (dp.stat().st_atime, dp.stat().st_mtime + 10))
    again = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len([p for p in again if p.source_type == "dock_goal"]) == 2


def test_dock_absent_tolerated_and_no_reap(monkeypatch, tmp_path):
    """GUARD P3-a: absent dock.yaml = 'dock not installed' — no trigger, no
    reap. Existing dock_goal pages survive as last-known-good."""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)   # no dock/ dir
    out = wiki / "pages" / "dock_goal"
    out.mkdir(parents=True)
    survivor = out / "keep-aaaaaaaa.md"
    survivor.write_text(
        "---\ntitle: x\nsource_type: dock_goal\n---\n\nbody\n", encoding="utf-8"
    )
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)  # must not raise
    assert "dock_goal" not in {p.source_type for p in pages}
    assert survivor.exists()


def test_dock_emptied_reaps_all_via_watcher(monkeypatch, tmp_path):
    """GUARD P3-b: an emptied-but-present dock.yaml routes through the watcher to
    project_dock's reap-all (the watcher does NOT short-circuit on zero goals)."""
    _install_t1(monkeypatch)
    home, wiki = tmp_path / "home", tmp_path / "wiki"
    dp = _write_home_dock(home, [_dock_goal(id="a"), _dock_goal(id="b")])
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(_dock_pages(wiki)) == 2

    _write_home_dock(home, [])   # present, zero goals
    os.utime(dp, (dp.stat().st_atime, dp.stat().st_mtime + 10))
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert _dock_pages(wiki) == []


def test_dock_ledger_entry_under_dock_path_key(monkeypatch, tmp_path):
    """GUARD P3-c: the dock entry is keyed on the resolved dock_path and lands
    in the SAME ingest_state.json the fleet loop writes (single save)."""
    _install_t1(monkeypatch)
    home, wiki = tmp_path / "home", tmp_path / "wiki"
    dp = _write_home_dock(home, [_dock_goal(id="a")])
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    ledger = json.loads((wiki / ".index" / "ingest_state.json").read_text())
    assert str(dp) in ledger
    assert ledger[str(dp)] == dp.stat().st_mtime


def test_dock_and_fleet_share_one_ledger(monkeypatch, tmp_path):
    """GUARD P3-c/d: fleet and dock keys coexist in one ledger — no separate
    writer racing the file; the fleet loop is unchanged."""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    dp = _write_home_dock(home, [_dock_goal(id="a")])
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    # fleet still scanned
    assert {"scout_digest", "researcher_brief", "drafter_draft"} <= {
        p.source_type for p in pages
    }
    ledger = json.loads((wiki / ".index" / "ingest_state.json").read_text())
    assert str(dp) in ledger
    assert any("brief-2026-06-25-x.json" in k for k in ledger)


# ════════════════════════════════════════════════════════════════════════════
# cellar-link-resolution-v1 Scope 2 — mtime debounce + background poller.
#
# The debounce is a PARTIAL-WRITE guard for the autonomous poller only. It
# defaults OFF (0.0) so every explicit caller (the `hermes wiki ingest` CLI, the
# /api/substrate/ingest endpoint, and the pre-existing tests above) still
# ingests a freshly-written file immediately. Only the background poller passes
# a non-zero window (30s), so a sink file caught mid-write is deferred to the
# next cycle instead of compacting a torn read.
# ════════════════════════════════════════════════════════════════════════════


def _age_all_sink_files(home, seconds=120.0):
    """Backdate every fleet sink file's mtime so it clears a debounce window."""
    old = time.time() - seconds
    for sub in ("scout", "researcher", "drafter", "cultivator"):
        d = home / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file():
                os.utime(f, (old, old))


def test_debounce_defers_fresh_file(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)  # freshly written, mtime ~ now
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home, debounce_seconds=30)
    assert pages == []  # all younger than the debounce window → deferred


def test_debounce_ingests_aged_file(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    _age_all_sink_files(home, seconds=120)
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home, debounce_seconds=30)
    assert {p.source_type for p in pages} == {
        "scout_digest", "researcher_brief", "drafter_draft"
    }


def test_debounce_default_off_ingests_fresh(monkeypatch, tmp_path):
    """Backward-compat guard: the default (no debounce arg) ingests a fresh file
    immediately — existing CLI/endpoint/test behavior is unchanged."""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(pages) == 3


def test_debounce_does_not_reingest_ledgered_aged_file(monkeypatch, tmp_path):
    """An aged file already in the ledger (unchanged mtime) stays skipped — the
    debounce gate never causes a re-ingest; the mtime ledger still wins."""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    _age_all_sink_files(home, seconds=120)
    first = scan_and_ingest(wiki_root=wiki, hermes_home=home, debounce_seconds=30)
    assert len(first) == 3
    second = scan_and_ingest(wiki_root=wiki, hermes_home=home, debounce_seconds=30)
    assert second == []


# ── background poller (poll_forever) ────────────────────────────────────


async def test_poller_invokes_scan_with_configured_debounce(monkeypatch):
    calls = []

    def _fake_scan(*, wiki_root=None, hermes_home=None, debounce_seconds=0.0):
        calls.append(debounce_seconds)
        return []

    monkeypatch.setattr(watcher, "scan_and_ingest", _fake_scan)
    task = asyncio.create_task(
        watcher.poll_forever(interval_seconds=0.01, debounce_seconds=30)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls  # scanned at least once
    assert all(d == 30 for d in calls)


async def test_poller_survives_scan_exception(monkeypatch):
    calls = []

    def _fake_scan(**_kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("malformed sink file")
        return []

    monkeypatch.setattr(watcher, "scan_and_ingest", _fake_scan)
    task = asyncio.create_task(
        watcher.poll_forever(interval_seconds=0.01, debounce_seconds=30)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2  # first cycle raised, the loop continued
