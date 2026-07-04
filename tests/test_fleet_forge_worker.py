"""Phase-4 tests: forge as the first worker + Option-2 package staging.

Forge appears here for the first time (config + skill delta). Also covers the
generic Option-2 runtime primitives forge exercises — stage_package (two-level
path-jail) and _extract_fleet_package — and the gate-requested re-dispatch cycle
(a worker's not-running gate clears on exit and it re-dispatches next tick).
"""

from __future__ import annotations

import json

import pytest

from grove.capability import Capability
from grove.capability_registry import load_capabilities
from grove.fleet import manager as manager_mod, staging, worker_entry
from grove.fleet.config import default_fleet_workers_path, load_fleet_workers, WorkerConfig
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.read_surfaces import enforce_declared_surfaces


# ── forge capability record ──────────────────────────────────────────────────


def _forge():
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def test_forge_declares_only_corpus_file():
    assert _forge().read_surfaces == ["corpus_file"]


def test_forge_enforce_passes_corpus_only():
    assert enforce_declared_surfaces(_forge(), "forge") == ["corpus_file"]


def test_forge_sink_is_pending_review():
    gov = _forge().governance
    assert gov["write_zone"]["staging_dir"] == "forge/pending_review"


def test_derive_skill_name_is_category_qualified():
    # Skills are category-nested; invoke_skill needs "<category>/<name>", not the
    # bare name (which resolves to a nonexistent flat dir). Pinned by the live run.
    assert worker_entry._derive_skill_name(_forge(), "forge") == "fleet/forge-jobsearch"


def test_guard_fires_if_forge_reaches_an_index():
    # A forge that declared cellar/wiki must Andon — it must NOT be able to reach one.
    d = _forge().to_dict()
    d["read_surfaces"] = ["corpus_file", "cellar"]
    with pytest.raises(FleetWorkerAndon) as ei:
        enforce_declared_surfaces(Capability.from_dict(d), "forge")
    assert ei.value.check == "index_surface_unwired"


def test_forge_payload_still_byte_matches_repo_skill():
    from pathlib import Path
    repo = Path("skills/fleet/forge-jobsearch/SKILL.md").read_text(encoding="utf-8")
    assert _forge().context.payload.strip() == repo.strip()


# ── forge fleet_workers.yaml entry ───────────────────────────────────────────


def test_forge_worker_entry_disabled_and_correct():
    w = load_fleet_workers(default_fleet_workers_path())["forge"]
    assert w.enabled is False  # not enabled until Phase-5 smoke authorizes it
    assert w.skill == "skill.fleet.forge-jobsearch"
    ist = w.input_state
    assert ist["type"] == "notion_query"
    assert ist["data_source"] == "5eb5630d-42ae-4a7f-8eee-8b04f0e96eaa"
    assert ist["filter"] == {"Status": "To Apply"}
    # fleet-pipeline-v1 P0 — declarative single-unit selection + ranking
    assert ist["select_one"] is True and ist["skip_already_staged"] is True
    assert ist["order_by"] == [
        {"field": "Fit Score", "direction": "desc"},
        {"field": "id", "direction": "asc"},
    ]
    assert w.limits["wall_clock_secs"] == 900


# ── Option-2 package staging (two-level path-jail) ───────────────────────────


def test_stage_package_writes_files_under_slug(tmp_path):
    sink = tmp_path / "forge" / "pending_review"
    files = {"resume.md": "R", "cover-letter.md": "C", "meta.json": '{"row_id":"x"}'}
    staged = staging.stage_package(sink, "260704-acme-pm", files)
    slug_dir = sink / "260704-acme-pm"
    assert {p.name for p in staged} == set(files)
    assert all(p.parent == slug_dir.resolve() for p in staged)
    assert (slug_dir / "resume.md").read_text() == "R"
    assert not list(sink.rglob("*.tmp"))  # atomic, no torn tmp


def test_stage_package_slug_traversal_neutralized(tmp_path):
    sink = tmp_path / "sink"
    # basename() collapses the traversal; the file lands INSIDE the sink, jailed.
    staged = staging.stage_package(sink, "../evil", {"a.md": "x"})
    assert staged[0].is_relative_to(sink.resolve())


def test_stage_package_rejects_unsafe_slug(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path, "..", {"a.md": "x"})
    assert ei.value.check == "path_escape"


def test_stage_package_rejects_uppercase_slug(tmp_path):
    with pytest.raises(FleetWorkerAndon):
        staging.stage_package(tmp_path, "NotASlug", {"a.md": "x"})


def test_stage_package_file_traversal_neutralized(tmp_path):
    # basename() collapses the filename traversal; the file lands jailed under the
    # slug dir (not at /etc/passwd), never rejected — parity with stage_draft.
    sink = tmp_path / "sink"
    staged = staging.stage_package(sink, "slug", {"../../etc/passwd": "x"})
    assert staged[0].name == "passwd"
    assert staged[0].is_relative_to(sink.resolve())
    assert staged[0].parent == (sink / "slug").resolve()


def test_stage_package_file_reducing_to_dotdot_rejected(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path / "sink", "slug", {"..": "x"})
    assert ei.value.check == "path_escape"


def test_stage_package_empty_files_andons(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_package(tmp_path, "slug", {})
    assert ei.value.check == "empty_package"


# ── fleet_package extraction ─────────────────────────────────────────────────


def _msgs(text):
    return [{"role": "assistant", "content": text}]


def test_extract_bare_json_package():
    pkg = {"fleet_package": {"slug": "s", "files": {"a.md": "x"}}}
    out = worker_entry._extract_fleet_package(_msgs(json.dumps(pkg)))
    assert out == {"slug": "s", "files": {"a.md": "x"}}


def test_extract_fenced_json_package():
    body = "Here you go:\n```json\n" + json.dumps(
        {"fleet_package": {"slug": "s", "files": {"a.md": "x"}}}
    ) + "\n```\n"
    out = worker_entry._extract_fleet_package(_msgs(body))
    assert out["slug"] == "s"


def test_extract_missing_package_returns_none():
    assert worker_entry._extract_fleet_package(_msgs("no package here")) is None
    # a package with no files is invalid
    bad = json.dumps({"fleet_package": {"slug": "s", "files": {}}})
    assert worker_entry._extract_fleet_package(_msgs(bad)) is None


# ── re-dispatch cycle: not-running gate clears on exit (gate confirmation) ────


class _CyclingProc:
    """poll() returns None (running) until flipped, then the exit code."""

    def __init__(self):
        self.pid = 4242
        self._rc = None

    def poll(self):
        return self._rc

    def exit(self, code=0):
        self._rc = code


def test_worker_redispatches_after_exit(monkeypatch, tmp_path):
    # A stuck "running" flag would silently block ALL future dispatches — verify
    # the full cycle: dispatch -> running -> exit (reap clears) -> re-dispatch.
    dispatched = []
    proc = _CyclingProc()
    event_path = tmp_path / "e.json"

    class _Handle:
        worker_id = "forge"
        run_id = "r"
        wall_clock_secs = 900
        pgid = 4242

        def __init__(self):
            self.proc = proc
            self.event_path = event_path

    def _fake_dispatch(cfg, payload, run_id=None):
        dispatched.append(cfg.id)
        return _Handle()

    monkeypatch.setattr(manager_mod.runner, "dispatch", _fake_dispatch)
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda *_a, **_k: {"rows": [1]})
    monkeypatch.setattr(manager_mod, "surface_fleet_andon", lambda *a, **k: None)
    monkeypatch.setattr(manager_mod, "remove_pidfile", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_mod, "enforce_wall_clock", lambda *_a, **_k: False)
    # cadence=None -> always due, so re-dispatch is gated only by not-running.
    wc = WorkerConfig(id="forge", skill="s", enabled=True, cadence=None,
                      input_state={"type": "notion_query"}, limits={"wall_clock_secs": 900})
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *_a, **_k: {"forge": wc})

    m = manager_mod.FleetManager()
    m.tick()                              # tick 1: dispatch
    assert dispatched == ["forge"] and "forge" in m._running

    m.tick()                              # tick 2: still running -> no new dispatch
    assert dispatched == ["forge"] and "forge" in m._running

    proc.exit(0)                          # worker exits successfully
    event_path.write_text(json.dumps({"status": "success"}))
    m.tick()                              # tick 3: reap clears running, then re-dispatch
    assert dispatched == ["forge", "forge"]  # re-dispatched — gate cleared
    assert "forge" in m._running
