"""Tests for grove.wiki.watcher — lazy scan-and-ingest over the fleet sinks.

Sprint K1 (living-cellar-v1) Phase 5. The watcher walks the four fleet sink
dirs (derived from FLEET_ADAPTERS) under the hermes home, glob-matches each
with its adapter, skips files unchanged by mtime, and compacts new/changed docs
through the pipeline. It tolerates absent dirs (cultivator), ignores off-glob
files, is loud PER FILE on a glob-matching malformed candidate (P3 quarantine,
GATE-B F2 — never a scan abort), and uses NO event watcher / NO write_file
hook — lazy/poll only.
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
        # P2: all three pipeline calls are forced tools — route by NAME.
        self.calls += 1
        if tool["name"] == "wiki_evaluation":
            return {"complete": True, "accurate": True,
                    "quality_score": 0.9, "issues": []}
        return {"title": "Compacted", "topics": ["t"],
                "key_entities": ["e"], "body": "body\n"}


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


def test_glob_match_malformed_file_is_loud_per_file_not_per_scan(
    monkeypatch, tmp_path, caplog
):
    """P3 (GATE-B F2) — migrated from the pre-P3 scan-abort pin: a glob-
    matching malformed file is STILL loud (WARNING + quarantine record), but
    per FILE. The scan completes. (The pre-P3 behavior — pytest.raises(
    MalformedSourceDoc) aborting the whole scan — is the class this phase
    kills; the poison-file pins below carry the full contract.)"""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    # a brief-*.json that matches the glob but violates the shape
    (home / "researcher" / "brief-2026-06-25-bad.json").write_text(
        json.dumps({"generated_at": "x"}), encoding="utf-8"
    )
    with caplog.at_level("WARNING"):
        pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)  # must not raise
    assert len(pages) == 3  # every healthy sink file compacted
    assert any(
        "QUARANTINED" in r.message and "brief-2026-06-25-bad.json" in r.message
        for r in caplog.records if r.levelname == "WARNING"
    )


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


# ── P3 per-file quarantine (wiki-writer-structured-output-v1, GATE-B F2) ────
#
# No single file may abort the scan. Poison-file isolation, bounded backoff
# (~1/~10/~60 poll-cycle multiples), parked terminal state, mtime reset, and
# the one-WARNING-per-transition log discipline.


def _poison(home, name="brief-2026-06-25-poison.json"):
    """A glob-matching researcher brief that deterministically fails
    adapter.parse (MalformedSourceDoc) — the poison candidate."""
    p = home / "researcher" / name
    p.write_text(json.dumps({"generated_at": "x"}), encoding="utf-8")
    return p


def _ledger(wiki):
    return json.loads(
        (wiki / ".index" / "ingest_state.json").read_text(encoding="utf-8")
    )


def _freeze_time(monkeypatch, t):
    monkeypatch.setattr(watcher.time, "time", lambda: t)


def test_poison_file_isolated_healthy_files_keep_ledger(monkeypatch, tmp_path):
    """THE pin for this phase: poison + healthy candidates in ONE scan →
    healthy files compact AND their ledger entries are SAVED; the poison file
    is quarantined; the scan completes. (Negative control run against the
    pre-P3 watcher at commit time: pre-P3, the scan raises
    MalformedSourceDoc, zero pages return, and NO ledger file is written —
    this test fails there on the first assert.)"""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    poison = _poison(home)
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert {p.source_type for p in pages} == {
        "scout_digest", "researcher_brief", "drafter_draft"
    }
    led = _ledger(wiki)  # persisted to DISK — the pre-P3 abort lost this
    healthy = [v for v in led.values() if isinstance(v, (int, float))]
    assert len(healthy) == 3
    q = led[str(poison)]
    assert q["state"] == "quarantined"
    assert q["attempts"] == 0
    assert "MalformedSourceDoc" in q["reason"]
    assert q["mtime"] == poison.stat().st_mtime
    assert q["first_failed_at"]
    # second scan immediately after: healthy files unchanged-skip, poison in
    # backoff → nothing recompacted, nothing raised
    assert scan_and_ingest(wiki_root=wiki, hermes_home=home) == []


def test_backoff_schedule_advances_then_parks(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    poison = _poison(home)
    t0 = 1_800_000_000.0

    _freeze_time(monkeypatch, t0)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    q = _ledger(wiki)[str(poison)]
    assert (q["state"], q["attempts"]) == ("quarantined", 0)
    assert q["next_retry_at"] == t0 + 60.0

    # 30s later: backoff pending → attempts must NOT advance
    _freeze_time(monkeypatch, t0 + 30)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert _ledger(wiki)[str(poison)]["attempts"] == 0

    # due (>60s): retry 1 fails → attempts 1, next at +600
    _freeze_time(monkeypatch, t0 + 61)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    q = _ledger(wiki)[str(poison)]
    assert (q["state"], q["attempts"]) == ("quarantined", 1)
    assert q["next_retry_at"] == t0 + 61 + 600.0

    # retry 2 fails → attempts 2, next at +3600
    _freeze_time(monkeypatch, t0 + 700)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    q = _ledger(wiki)[str(poison)]
    assert (q["state"], q["attempts"]) == ("quarantined", 2)
    assert q["next_retry_at"] == t0 + 700 + 3600.0

    # retry 3 fails → PARKED, terminal, no next_retry_at
    _freeze_time(monkeypatch, t0 + 5000)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    q = _ledger(wiki)[str(poison)]
    assert (q["state"], q["attempts"]) == ("parked", 3)
    assert "next_retry_at" not in q

    # parked: never retried again for this mtime (attempts frozen)
    for t in (t0 + 10_000, t0 + 100_000, t0 + 1_000_000):
        _freeze_time(monkeypatch, t)
        scan_and_ingest(wiki_root=wiki, hermes_home=home)
        assert _ledger(wiki)[str(poison)] == q


def test_mtime_change_resets_parked_file(monkeypatch, tmp_path):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    poison = _poison(home)
    t0 = 1_800_000_000.0
    for t in (t0, t0 + 61, t0 + 700, t0 + 5000):  # drive to parked
        _freeze_time(monkeypatch, t)
        scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert _ledger(wiki)[str(poison)]["state"] == "parked"

    # operator touch (still poison content): immediate re-candidate, fresh
    # quarantine with attempts RESET — not a parked skip, not attempts=4.
    os.utime(poison, (t0, t0 + 6000))
    _freeze_time(monkeypatch, t0 + 6001)
    scan_and_ingest(wiki_root=wiki, hermes_home=home)
    q = _ledger(wiki)[str(poison)]
    assert (q["state"], q["attempts"]) == ("quarantined", 0)
    assert q["mtime"] == t0 + 6000

    # fix the file + touch: quarantine clears to a healthy float entry
    poison.write_text(json.dumps(_researcher_brief()), encoding="utf-8")
    os.utime(poison, (t0, t0 + 7000))
    _freeze_time(monkeypatch, t0 + 7001)
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert [p.source_type for p in pages] == ["researcher_brief"]
    assert _ledger(wiki)[str(poison)] == t0 + 7000


def test_log_discipline_one_warning_per_transition(monkeypatch, tmp_path, caplog):
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    _poison(home)
    t0 = 1_800_000_000.0

    def _warnings():
        return [r for r in caplog.records if r.levelname == "WARNING"]

    with caplog.at_level("INFO", logger="grove.wiki.watcher"):
        # first quarantine: exactly ONE WARNING
        _freeze_time(monkeypatch, t0)
        scan_and_ingest(wiki_root=wiki, hermes_home=home)
        assert len(_warnings()) == 1 and "QUARANTINED" in _warnings()[0].message
        # summary line carries the counts
        assert any(
            "scan summary" in r.message and "1 quarantined" in r.message
            for r in caplog.records
        )

        # intermediate retries: quiet at INFO — no new WARNINGs
        caplog.clear()
        for t in (t0 + 61, t0 + 700):
            _freeze_time(monkeypatch, t)
            scan_and_ingest(wiki_root=wiki, hermes_home=home)
        assert _warnings() == []
        assert sum(
            1 for r in caplog.records if "quarantine retry" in r.message
        ) == 2

        # park transition: exactly ONE terminal WARNING
        caplog.clear()
        _freeze_time(monkeypatch, t0 + 5000)
        scan_and_ingest(wiki_root=wiki, hermes_home=home)
        assert len(_warnings()) == 1 and "PARKED" in _warnings()[0].message

        # parked thereafter: zero per-cycle records at WARNING, no per-file
        # lines — only the one summary INFO per scan
        caplog.clear()
        for t in (t0 + 10_000, t0 + 20_000, t0 + 30_000):
            _freeze_time(monkeypatch, t)
            scan_and_ingest(wiki_root=wiki, hermes_home=home)
        assert _warnings() == []
        assert all(
            "poison" not in r.message or "scan summary" in r.message
            for r in caplog.records
        )
        assert sum(1 for r in caplog.records if "scan summary" in r.message) == 3


def test_dock_failure_quarantines_not_aborts(monkeypatch, tmp_path):
    """The manifest is one more candidate (F2): a project_dock failure
    quarantines dock.yaml; fleet files in the same scan keep their entries."""
    _install_t1(monkeypatch)
    home, wiki = _home_with_sinks(tmp_path)
    (home / "dock").mkdir()
    (home / "dock" / "dock.yaml").write_text("goals: [", encoding="utf-8")  # bad YAML
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(pages) == 3  # fleet files unaffected
    led = _ledger(wiki)
    q = led[str(home / "dock" / "dock.yaml")]
    assert q["state"] == "quarantined"
