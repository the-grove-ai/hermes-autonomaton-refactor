"""Tests for the Fleet Background-Worker runtime (background-worker-runtime-v1).

Covers the Phase-1 load-bearing guards: the read_surfaces capability field
(Condition 2), the fleet_workers.yaml loud dup-id guard (Condition 1), generic
read_surface enforcement (index declare-but-unwired + the contract guard), the
atomic path-jailed staging primitive, and the worker's always-write-a-terminal-
event contract. GROVE_HOME is per-test isolated by the autouse conftest fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.capability import READ_SURFACE_VOCABULARY, Capability
from grove.capability_registry import load_capabilities
from grove.fleet import config, paths, read_surfaces, staging, worker_entry
from grove.fleet.errors import FleetWorkerAndon


def _forge_record() -> Capability:
    return load_capabilities()["skill.fleet.forge-jobsearch"]


# ── Condition 2: read_surfaces capability field ──────────────────────────────


def test_read_surfaces_defaults_empty_and_serializes_byte_identical():
    # A record that declares no read_surfaces (a plain verb) stays empty and is
    # serialized byte-identically — the additive field is invisible when unused.
    # (forge now declares [corpus_file] as of Phase 4, so use read_file here.)
    rec = load_capabilities()["read_file"]
    assert rec.read_surfaces == []
    assert "read_surfaces" not in rec.to_dict()  # absent -> not emitted
    assert Capability.from_yaml(rec.to_yaml()).read_surfaces == []


def test_read_surfaces_present_key_round_trip():
    d = _forge_record().to_dict()
    d["read_surfaces"] = ["corpus_file"]
    cap = Capability.from_dict(d)
    assert cap.read_surfaces == ["corpus_file"]
    assert cap.to_dict()["read_surfaces"] == ["corpus_file"]
    assert Capability.from_yaml(cap.to_yaml()).read_surfaces == ["corpus_file"]


def test_read_surfaces_unknown_token_fails_loud():
    d = _forge_record().to_dict()
    d["read_surfaces"] = ["not_a_surface"]
    with pytest.raises(ValueError, match="not in the known vocabulary"):
        Capability.from_dict(d)


def test_read_surfaces_duplicate_token_fails_loud():
    d = _forge_record().to_dict()
    d["read_surfaces"] = ["cellar", "cellar"]
    with pytest.raises(ValueError, match="must not repeat"):
        Capability.from_dict(d)


def test_all_existing_records_still_load():
    # The additive field must not break any bundled record.
    caps = load_capabilities()
    assert len(caps) > 50


# ── Condition 1: fleet_workers.yaml loader + loud dup-id guard ────────────────


def test_shipped_registry_has_only_the_disabled_forge_worker():
    # As of Phase 4 the committed registry declares one worker — forge, disabled
    # until the Phase-5 smoke authorizes it. It must load cleanly and stay off.
    workers = config.load_fleet_workers(config.default_fleet_workers_path())
    assert set(workers) == {"forge"}
    assert workers["forge"].enabled is False


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fleet_workers.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_worker_loads(tmp_path):
    p = _write(
        tmp_path,
        "workers:\n"
        "  - id: forge\n"
        "    skill: skill.fleet.forge-jobsearch\n"
        "    enabled: false\n"
        "    cadence: '*/30 * * * *'\n",
    )
    workers = config.load_fleet_workers(p)
    assert set(workers) == {"forge"}
    assert workers["forge"].skill == "skill.fleet.forge-jobsearch"
    assert workers["forge"].enabled is False


def test_duplicate_worker_id_across_entries_fails_loud(tmp_path):
    p = _write(
        tmp_path,
        "workers:\n"
        "  - {id: forge, skill: skill.fleet.forge-jobsearch, enabled: false}\n"
        "  - {id: forge, skill: skill.fleet.scout, enabled: false}\n",
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        config.load_fleet_workers(p)
    assert ei.value.check == "duplicate_worker_id"


def test_duplicate_key_within_mapping_fails_loud(tmp_path):
    p = _write(
        tmp_path,
        "workers:\n"
        "  - id: forge\n"
        "    skill: skill.fleet.forge-jobsearch\n"
        "    enabled: false\n"
        "    enabled: true\n",
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        config.load_fleet_workers(p)
    assert ei.value.check == "duplicate_key"


def test_missing_enabled_is_explicit_failure(tmp_path):
    p = _write(
        tmp_path,
        "workers:\n  - id: forge\n    skill: skill.fleet.forge-jobsearch\n",
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        config.load_fleet_workers(p)
    assert ei.value.check == "missing_enabled"


def test_bad_worker_id_slug_rejected(tmp_path):
    p = _write(
        tmp_path,
        "workers:\n  - {id: '../evil', skill: skill.fleet.scout, enabled: false}\n",
    )
    with pytest.raises(FleetWorkerAndon) as ei:
        config.load_fleet_workers(p)
    assert ei.value.check == "bad_worker_id"


def test_missing_registry_file_fails_loud(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        config.load_fleet_workers(tmp_path / "nope.yaml")
    assert ei.value.check == "registry_missing"


# ── read_surfaces enforcement (generic, record-driven) ───────────────────────


def _forge_with_surfaces(surfaces):
    d = _forge_record().to_dict()
    d["read_surfaces"] = surfaces
    return Capability.from_dict(d)


def test_corpus_file_surface_passes_enforcement():
    cap = _forge_with_surfaces(["corpus_file"])
    assert read_surfaces.enforce_declared_surfaces(cap, "forge") == ["corpus_file"]


@pytest.mark.parametrize("surface", sorted(read_surfaces.INDEX_SURFACES))
def test_index_surface_declare_but_unwired_andons(surface):
    cap = _forge_with_surfaces([surface])
    with pytest.raises(FleetWorkerAndon) as ei:
        read_surfaces.enforce_declared_surfaces(cap, "forge")
    assert ei.value.check == "index_surface_unwired"
    assert ei.value.surface == surface


def test_undeclared_surface_guard_andons():
    cap = _forge_with_surfaces(["corpus_file"])
    with pytest.raises(FleetWorkerAndon) as ei:
        read_surfaces.assert_surface_allowed(cap, "wiki", "forge")
    assert ei.value.check == "undeclared_surface"


def test_surface_partition_covers_vocabulary():
    assert (
        read_surfaces.PLAIN_FILE_SURFACES | read_surfaces.INDEX_SURFACES
        == READ_SURFACE_VOCABULARY
    )


# ── atomic, path-jailed staging ──────────────────────────────────────────────


def test_stage_draft_atomic_and_jailed(tmp_path):
    sink = tmp_path / "pending_review"
    out = staging.stage_draft(sink, "../../etc/passwd", "content")
    assert out.parent == sink and out.name == "passwd"
    assert out.read_text() == "content"
    # no lingering .tmp
    assert not list(sink.glob("*.tmp"))


def test_stage_draft_rejects_pure_traversal(tmp_path):
    with pytest.raises(FleetWorkerAndon) as ei:
        staging.stage_draft(tmp_path, "..", "x")
    assert ei.value.check == "path_escape"


def test_terminal_event_atomic(tmp_path):
    dest = tmp_path / "events" / "r1.json"
    staging.write_terminal_event(dest, {"status": "success"})
    assert json.loads(dest.read_text())["status"] == "success"


# ── worker path guards + terminal-event contract ─────────────────────────────


def test_worker_id_must_be_safe_slug():
    with pytest.raises(FleetWorkerAndon):
        paths.worker_dir("../escape")


def test_unregistered_worker_writes_failed_event_and_exits_nonzero():
    wid, rid = "ghostw", "run-xyz"
    inbox = paths.inbox_path(wid, rid)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps({"worker_id": wid, "run_id": rid, "payload": {"a": 1}}))
    rc = worker_entry.main(["--worker-id", wid, "--run-id", rid])
    assert rc == 1
    event = json.loads(paths.event_path(wid, rid).read_text())
    assert event["status"] == "failed"
    assert event["check"] == "worker_not_registered"
    assert "traceback" in event  # diagnostics preserved


def test_missing_inbox_is_catastrophic_failed_event():
    wid, rid = "noinbox", "run-1"
    # no inbox brokered
    rc = worker_entry.main(["--worker-id", wid, "--run-id", rid])
    assert rc == 1
    event = json.loads(paths.event_path(wid, rid).read_text())
    assert event["status"] == "failed"
    assert event["check"] == "inbox_missing"
