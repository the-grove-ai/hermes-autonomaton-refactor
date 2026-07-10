"""structural-review-gate-v1 — per-capability write governance.

Covers the whole sprint as durable regression:
  * loader — the additive ``governance`` field survives from_dict/to_dict (the
    to_yaml write path that transition_record uses would otherwise erase it);
  * WHERE  — ``is_capability_write_allowed`` confines fleet writes to the staging
    dir, refusing canonical-sink and cross-capability writes;
  * WHETHER — ``capability_emission_precondition`` gates the terminal artifact on
    the turn's tool-class counts;
  * tool_classes — the name→class map + turn-ledger counter;
  * promotion — ``promote_artifact`` (orchestrator-only) atomically moves an
    approved artifact from staging to the canonical sink, validating staging
    membership and refusing anything else.

Isolation: a tmp ``GROVE_HOME`` (the gates + promotion resolve every write_zone
dir relative to it via ``get_hermes_home``). The gates never consult
``_tmp_roots``/manifests, so no source-shadowing dance is needed here.
"""

from __future__ import annotations

import os

import pytest

from grove.tool_classes import TOOL_CLASS_MAP, classify_tool, count_tool_classes
from grove.utils.fs_utils import (
    canonicalize_files,
    capability_emission_precondition,
    is_capability_write_allowed,
    promote_artifact,
)


# ── governance fixtures (hand-authored, mirroring the real fleet records) ─────

SCOUT_GOV = {
    "write_zone": {"staging_dir": "scout", "canonical_dir": "scout",
                   "promotion": "auto_ingest"},
    "emission_preconditions": {
        "required_tool_classes": [{"class": "retrieval", "min_calls": 2}],
        "terminal_artifact": {"tool": "write_file", "path_pattern": "digest-*.json"},
    },
    "approval_handoff": {"mode": "ingest_post"},
}

DRAFTER_GOV = {
    "write_zone": {"staging_dir": "drafter/pending_review", "canonical_dir": "drafter",
                   "promotion": "operator_approval"},
    "emission_preconditions": {
        "required_tool_classes": [{"class": "skill_invocation", "min_calls": 1}],
        "terminal_artifact": {"tool": "write_file", "path_pattern": "draft-*.md"},
    },
    "approval_handoff": {"mode": "action_surface_publish"},
}

FLEET = [("skill.fleet.scout", SCOUT_GOV), ("skill.fleet.drafter", DRAFTER_GOV)]

# auto_ingest with a DISTINCT canonical dir (unlike scout, whose staging ==
# canonical) so a promotion is a genuine relocation, not a no-op self-rename.
AUTO_GOV = {
    "write_zone": {"staging_dir": "scout", "canonical_dir": "scout_canon",
                   "promotion": "auto_ingest"},
}


@pytest.fixture
def grove(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    return home


# ── WHERE gate ────────────────────────────────────────────────────────────────


def test_where_staging_write_allowed(grove):
    assert is_capability_write_allowed(str(grove / "scout" / "digest-x.json"), FLEET) == (True, "")


def test_where_nested_staging_allowed(grove):
    ok, _ = is_capability_write_allowed(str(grove / "drafter" / "pending_review" / "draft-x.md"), FLEET)
    assert ok is True


def test_where_canonical_sink_refused(grove):
    """The WHERE failure: a draft written to the canonical sink instead of the
    staging dir. Refused, and the reason names the governing capability."""
    ok, reason = is_capability_write_allowed(str(grove / "drafter" / "draft-x.md"), FLEET)
    assert ok is False
    assert "canonical sink" in reason and "skill.fleet.drafter" in reason


def test_where_non_fleet_path_allowed(grove):
    assert is_capability_write_allowed(str(grove / "research" / "notes.md"), FLEET) == (True, "")


def test_where_boundary_safe(grove):
    """A sibling dir sharing a name prefix is never inside the staging dir."""
    assert is_capability_write_allowed(str(grove / "scout_evil" / "x.json"), FLEET) == (True, "")


def test_where_cross_capability_refused(grove):
    """A target inside one record's staging dir but ALSO inside a different
    record's governed zone is refused (synthetic overlapping zones)."""
    overlap = [
        ("cap.a", {"write_zone": {"staging_dir": "shared/a", "canonical_dir": "shared"}}),
        ("cap.b", {"write_zone": {"staging_dir": "shared/b", "canonical_dir": "shared"}}),
    ]
    # shared/a/x is in cap.a's staging AND cap.b's canonical umbrella ("shared").
    ok, reason = is_capability_write_allowed(str(grove / "shared" / "a" / "x"), overlap)
    assert ok is False
    assert "cap.a" in reason and "cap.b" in reason


def test_where_unresolvable_fails_closed(grove, monkeypatch):
    """A target that cannot be canonicalized is refused, never allowed open."""
    from grove.utils import fs_utils
    monkeypatch.setattr(fs_utils, "_canonical_write_target", lambda p: None)
    ok, reason = is_capability_write_allowed("anything", FLEET)
    assert ok is False and "fail-closed" in reason


def test_where_ignores_record_without_write_zone(grove):
    fleet = [("cap.x", {"emission_preconditions": {}})]  # no write_zone
    assert is_capability_write_allowed(str(grove / "anywhere" / "f"), fleet) == (True, "")


# ── WHETHER gate ──────────────────────────────────────────────────────────────


def test_whether_terminal_sufficient_allowed(grove):
    t = str(grove / "scout" / "digest-x.json")
    assert capability_emission_precondition(t, FLEET, {"retrieval": 2}) == (True, "")


def test_whether_terminal_short_refused(grove):
    t = str(grove / "scout" / "digest-x.json")
    ok, reason = capability_emission_precondition(t, FLEET, {"retrieval": 1})
    assert ok is False
    assert "retrieval 1/2" in reason and "skill.fleet.scout" in reason


def test_whether_terminal_zero_refused(grove):
    """The WHETHER failure: a hollow terminal artifact with no tool work."""
    t = str(grove / "researcher" / "brief-x.json")  # researcher not in FLEET
    # scout terminal with empty counts:
    t2 = str(grove / "scout" / "digest-x.json")
    ok, _ = capability_emission_precondition(t2, FLEET, {})
    assert ok is False


def test_whether_non_terminal_write_skipped(grove):
    """A non-terminal helper write inside staging is not gated, regardless of counts."""
    t = str(grove / "scout" / "notes.txt")  # basename != digest-*.json
    assert capability_emission_precondition(t, FLEET, {}) == (True, "")


def test_whether_drafter_skill_invocation(grove):
    t = str(grove / "drafter" / "pending_review" / "draft-x.md")
    assert capability_emission_precondition(t, FLEET, {"skill_invocation": 1}) == (True, "")
    ok, _ = capability_emission_precondition(t, FLEET, {})
    assert ok is False


def test_whether_non_fleet_skipped(grove):
    assert capability_emission_precondition(str(grove / "research" / "x.md"), FLEET, {}) == (True, "")


def test_whether_record_without_preconditions_allowed(grove):
    fleet = [("cap.np", {"write_zone": {"staging_dir": "np", "canonical_dir": "np"}})]
    assert capability_emission_precondition(str(grove / "np" / "art.json"), fleet, {}) == (True, "")


# ── tool_classes ──────────────────────────────────────────────────────────────


def test_tool_class_map_and_classify():
    assert classify_tool("web_search") == "retrieval"
    assert classify_tool("invoke_skill") == "skill_invocation"
    assert classify_tool("write_file") == "file_write"
    assert classify_tool("nonexistent") is None
    assert classify_tool(None) is None
    # the four retrieval tools all tag retrieval
    assert {TOOL_CLASS_MAP[t] for t in ("web_search", "x_search", "cellar_search", "web_extract")} == {"retrieval"}


def test_count_tool_classes_over_ledger():
    ledger = [
        {"tool": "web_search", "args": {}},
        {"tool": "x_search", "args": {}},
        {"tool": "write_file", "args": {}},
        {"tool": "unmapped_tool", "args": {}},
        {"not_a_tool_key": "x"},  # malformed entry — skipped
        "garbage",                # non-dict — skipped
    ]
    assert count_tool_classes(ledger) == {"retrieval": 2, "file_write": 1}


def test_count_tool_classes_empty():
    assert count_tool_classes([]) == {}


# ── promote_artifact (orchestrator-only) ──────────────────────────────────────


def test_promote_happy_path(grove):
    staging = grove / "drafter" / "pending_review"
    staging.mkdir(parents=True)
    src = staging / "draft-x.md"
    src.write_text("approved draft\n")

    canonical = promote_artifact(str(src), DRAFTER_GOV)

    assert canonical == str(grove / "drafter" / "draft-x.md")
    assert os.path.exists(canonical)
    assert not os.path.exists(str(src))  # atomic move — source gone
    assert open(canonical).read() == "approved draft\n"


def test_promote_creates_canonical_dir(grove):
    """auto_ingest capability whose canonical dir does not exist yet."""
    staging = grove / "scout"
    staging.mkdir(parents=True)
    src = staging / "digest-x.json"
    src.write_text("{}")
    # scout staging == canonical == scout, so canonical already exists here;
    # use a synthetic gov whose canonical dir is absent.
    gov = {"write_zone": {"staging_dir": "scout", "canonical_dir": "scout_canonical"}}
    canonical = promote_artifact(str(src), gov)
    assert canonical == str(grove / "scout_canonical" / "digest-x.json")
    assert os.path.isdir(str(grove / "scout_canonical"))
    assert os.path.exists(canonical)


def test_promote_basename_preserved(grove):
    staging = grove / "drafter" / "pending_review"
    staging.mkdir(parents=True)
    src = staging / "draft-2026-06-30-slug.md"
    src.write_text("x")
    canonical = promote_artifact(str(src), DRAFTER_GOV)
    assert os.path.basename(canonical) == "draft-2026-06-30-slug.md"


def test_promote_rejects_source_outside_staging(grove):
    (grove / "research").mkdir()
    src = grove / "research" / "draft-x.md"
    src.write_text("x")
    with pytest.raises(ValueError, match="does not resolve inside"):
        promote_artifact(str(src), DRAFTER_GOV)


def test_promote_rejects_staging_dir_itself(grove):
    staging = grove / "drafter" / "pending_review"
    staging.mkdir(parents=True)
    with pytest.raises(ValueError, match="does not resolve inside"):
        promote_artifact(str(staging), DRAFTER_GOV)


def test_promote_rejects_boundary_sibling(grove):
    """pending_review_evil is not pending_review."""
    sib = grove / "drafter" / "pending_review_evil"
    sib.mkdir(parents=True)
    src = sib / "draft-x.md"
    src.write_text("x")
    with pytest.raises(ValueError, match="does not resolve inside"):
        promote_artifact(str(src), DRAFTER_GOV)


def test_promote_requires_write_zone(grove):
    with pytest.raises(ValueError, match="must declare both"):
        promote_artifact(str(grove / "x"), {"emission_preconditions": {}})


def test_promote_requires_canonical_dir(grove):
    gov = {"write_zone": {"staging_dir": "scout"}}  # no canonical_dir
    with pytest.raises(ValueError, match="must declare both"):
        promote_artifact(str(grove / "scout" / "x"), gov)


def test_promote_auto_ingest_triggers_ingest(grove, monkeypatch):
    """auto_ingest capability: the canonical path is funnelled through
    ingest_file immediately (poller not waited on)."""
    calls = []
    import grove.wiki.watcher as watcher
    monkeypatch.setattr(watcher, "ingest_file", lambda p, **kw: calls.append(str(p)))

    staging = grove / "scout"
    staging.mkdir(parents=True)
    src = staging / "digest-x.json"
    src.write_text("{}")
    canonical = promote_artifact(str(src), AUTO_GOV)  # promotion=auto_ingest

    assert canonical == str(grove / "scout_canon" / "digest-x.json")
    assert calls == [canonical]


def test_promote_operator_approval_skips_ingest(grove, monkeypatch):
    """operator_approval capability: the move IS the approval effect; no inline
    ingest — the poller picks it up next cycle."""
    calls = []
    import grove.wiki.watcher as watcher
    monkeypatch.setattr(watcher, "ingest_file", lambda p, **kw: calls.append(str(p)))

    staging = grove / "drafter" / "pending_review"
    staging.mkdir(parents=True)
    src = staging / "draft-x.md"
    src.write_text("x")
    promote_artifact(str(src), DRAFTER_GOV)  # promotion=operator_approval

    assert calls == []


def test_promote_ingest_failure_does_not_unwind_move(grove, monkeypatch):
    """The atomic move is the primary guarantee: an ingest fault is logged loud
    but the promotion still succeeds (poller is the backstop)."""
    import grove.wiki.watcher as watcher

    def _boom(p, **kw):
        raise RuntimeError("pipeline down")

    monkeypatch.setattr(watcher, "ingest_file", _boom)

    staging = grove / "scout"
    staging.mkdir(parents=True)
    src = staging / "digest-x.json"
    src.write_text("{}")
    canonical = promote_artifact(str(src), AUTO_GOV)  # must NOT raise

    assert os.path.exists(canonical)      # move completed
    assert not os.path.exists(str(src))   # source gone (relocated to distinct canonical)


# ── loader round-trip (the write-path erasure guard) ──────────────────────────


def test_loader_governance_round_trips():
    from grove.capability import Capability

    src = {
        "id": "skill.test.gov", "kind": "skill", "zone": "green",
        "trigger": {"always": True},
        "tier_rule": {"eligible": [2], "preferred": 2,
                      "validation": {"confidence_threshold": 0.95, "shadow_window": 20}},
        "telemetry": {"feed": "intent_feed"},
        "lifecycle": {"state": "active"},
        "failure": {"circuit_breaker": {"threshold": 3, "window_seconds": 300}},
        "skill": {"category": "test"},
        "governance": SCOUT_GOV,
    }
    cap = Capability.from_dict(src)
    assert cap.governance == SCOUT_GOV
    # survives the to_yaml write path (transition_record / update_lifecycle_fields)
    cap2 = Capability.from_yaml(cap.to_yaml())
    assert cap2.governance == SCOUT_GOV


def test_loader_absent_governance_is_none_and_unemitted():
    from grove.capability import Capability

    src = {
        "id": "skill.test.nogov", "kind": "skill", "zone": "green",
        "trigger": {"always": True},
        "tier_rule": {"eligible": [2], "preferred": 2,
                      "validation": {"confidence_threshold": 0.95, "shadow_window": 20}},
        "telemetry": {"feed": "intent_feed"},
        "lifecycle": {"state": "active"},
        "failure": {"circuit_breaker": {"threshold": 3, "window_seconds": 300}},
        "skill": {"category": "test"},
    }
    cap = Capability.from_dict(src)
    assert cap.governance is None
    assert "governance" not in cap.to_dict()  # non-fleet shape unchanged


def test_real_fleet_records_carry_governance():
    """Integration: the four shipped fleet records load with their governance."""
    from grove.capability import CapabilityKind
    from grove.capability_registry import load_capabilities

    caps = load_capabilities()
    expected = {
        "skill.fleet.scout": "scout",
        "skill.fleet.researcher": "researcher",
        "skill.fleet.drafter": "drafter/pending_review",
        "skill.fleet.cultivator": "cultivator/pending_review",
    }
    for cid, staging in expected.items():
        cap = caps[cid]
        assert cap.kind is CapabilityKind.SKILL
        assert cap.governance is not None
        assert cap.governance["write_zone"]["staging_dir"] == staging


# ── canonicalize_files (THE shared canonicalization core, P1) ─────────────────


def test_canonicalize_moves_and_creates_dir(grove):
    src_dir = grove / "x" / "pending_review"
    src_dir.mkdir(parents=True)
    a, b = src_dir / "a.md", src_dir / "b.md"
    a.write_text("A"); b.write_text("B")
    out = canonicalize_files([a, b], grove / "x" / "unit-1")
    assert out == [str(grove / "x" / "unit-1" / "a.md"),
                   str(grove / "x" / "unit-1" / "b.md")]
    assert not a.exists() and not b.exists()  # moved, not copied
    assert (grove / "x" / "unit-1" / "a.md").read_text() == "A"


def test_canonicalize_skip_if_identical_leaves_source(grove):
    """P1 ruling 3: byte-identical canonical copy → skip; source untouched."""
    src_dir = grove / "x"; src_dir.mkdir()
    canon = grove / "canon"; canon.mkdir()
    src = src_dir / "f.md"; src.write_text("same")
    (canon / "f.md").write_text("same")
    out = canonicalize_files([src], canon)
    assert out == [str(canon / "f.md")]
    assert src.exists()  # skip — not moved


def test_canonicalize_divergent_overwrites_last_write_wins(grove):
    """P1 ruling 3: divergent content → overwrite via rename (no Andon)."""
    src_dir = grove / "x"; src_dir.mkdir()
    canon = grove / "canon"; canon.mkdir()
    src = src_dir / "f.md"; src.write_text("new")
    (canon / "f.md").write_text("old")
    out = canonicalize_files([src], canon)
    assert out == [str(canon / "f.md")]
    assert (canon / "f.md").read_text() == "new"
    assert not src.exists()


def test_canonicalize_source_gone_canonical_present_satisfied(grove):
    """P1 ruling 3: staging-gone + canonical-present → satisfied (re-tap)."""
    canon = grove / "canon"; canon.mkdir()
    (canon / "f.md").write_text("already")
    out = canonicalize_files([grove / "x" / "f.md"], canon)
    assert out == [str(canon / "f.md")]
    assert (canon / "f.md").read_text() == "already"


def test_canonicalize_source_gone_no_canonical_fails_loud(grove):
    with pytest.raises(FileNotFoundError, match="cannot be satisfied"):
        canonicalize_files([grove / "x" / "f.md"], grove / "canon")


def test_canonicalize_self_rename_is_noop(grove):
    """staging == canonical (scout): the self-rename resolves as a skip."""
    d = grove / "scout"; d.mkdir()
    f = d / "digest-x.json"; f.write_text("{}")
    out = canonicalize_files([f], d)
    assert out == [str(f)] and f.read_text() == "{}"


# ── promoted-artifact-persistence-v1 P2 — declaration + write-path pins ───────


def test_write_zone_ingest_round_trips_from_dict_to_dict():
    """P2 S1 pin: the additive write_zone.ingest declaration survives the
    from_dict/to_dict round trip verbatim (governance threads whole)."""
    from grove.capability_registry import load_capabilities
    from grove.capability import Capability

    cap = load_capabilities()["skill.fleet.forge-jobsearch"]
    declared = cap.governance["write_zone"]["ingest"]
    assert declared == {"surface": "canonical_subdirs",
                        "source_type": "forge_package"}
    rebuilt = Capability.from_dict(cap.to_dict())
    assert rebuilt.governance["write_zone"]["ingest"] == declared


def test_canonical_write_path_is_rename_only():
    """P2 S4 (Mitigation 2) pin: the canonical write path is RENAME-ONLY —
    no streaming writes to canonical paths, so the poller can never observe
    a partial artifact (rename within one mount is atomic)."""
    import inspect

    from grove.api import actions
    from grove.utils import fs_utils

    src = (inspect.getsource(canonicalize_files)
           + inspect.getsource(fs_utils.promote_artifact)
           + inspect.getsource(actions._fleet_promote_core)
           + inspect.getsource(actions._canonicalize_staged_package))
    assert "rename(" in src
    for streaming in (".write_text", ".write_bytes", "shutil.copy",
                      "copyfile", "open("):
        assert streaming not in src, (
            f"streaming write {streaming!r} in the canonical write path — "
            f"the poller could observe a partial artifact"
        )


# ── promoted-artifact-persistence-v1 P5 S1 — the storage chokepoint ───────────


def test_storage_transfer_is_the_one_chokepoint():
    """P5 S1 pins: storage_transfer is producer-blind, rename-only (atomic-or-
    loud; POSIX rename is the implementation, not the contract), and
    canonicalize_files is a TRUE alias of the same body — one implementation,
    zero copies."""
    import inspect

    from grove.utils import fs_utils

    assert fs_utils.canonicalize_files is fs_utils.storage_transfer
    src = inspect.getsource(fs_utils.storage_transfer)
    for name in ("forge", "scout", "drafter", "cultivator", "researcher"):
        assert name not in src, f"producer name {name!r} in the chokepoint"
    assert "rename(" in src
    for streaming in (".write_text", ".write_bytes", "shutil.copy",
                      "copyfile", "open("):
        assert streaming not in src


# ── promoted-artifact-persistence-v1 P5 S2 — retention declaration ────────────


def test_write_zone_retention_round_trips_and_is_uniform():
    """P5 S2 pin (mirrors the P2 ingest round-trip pin): the additive
    retention declaration survives from_dict/to_dict verbatim on every
    governance-bearing fleet record; persist-by-default shape exact."""
    from grove.capability import Capability
    from grove.capability_registry import load_capabilities

    expected = {"policy": "persist", "archive_dir": ".archive"}
    seen = 0
    for cid, cap in load_capabilities().items():
        wz = (cap.governance or {}).get("write_zone") if cap.governance else None
        if not wz:
            continue
        seen += 1
        assert wz.get("retention") == expected, cid
        rebuilt = Capability.from_dict(cap.to_dict())
        assert rebuilt.governance["write_zone"]["retention"] == expected, cid
    assert seen >= 6  # all six fleet records declare it


# ── promoted-artifact-persistence-v1 P5 S3 — purge core ──────────────────────

from pathlib import Path  # noqa: E402

from grove.utils.fs_utils import purge_artifacts  # noqa: E402

PURGE_GOV = {
    "write_zone": {"staging_dir": "sinkp/pending_review", "canonical_dir": "sinkp",
                   "promotion": "operator_approval",
                   "retention": {"policy": "persist", "archive_dir": ".archive"}},
}


def _canonical_unit(grove, unit="260101-acme-pm"):
    d = grove / "sinkp" / unit
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    (d / "cover-letter.md").write_text("C")
    return d


def test_purge_happy_path_archive_layout_and_manifest(grove):
    import json as _json
    from datetime import datetime, timezone
    d = _canonical_unit(grove)
    res = purge_artifacts(
        [str(d)], PURGE_GOV, unit="260101-acme-pm", reason="stale package",
        initiated_by="operator", effect_signature="sig-x",
        now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    dest = grove / "sinkp" / ".archive" / "260101-acme-pm-20260710T120000Z"
    assert res["archive_dir"] == str(dest) and res["resumed"] is False
    assert sorted(p.name for p in dest.iterdir()) == [
        "cover-letter.md", "purge-manifest.json", "resume.md"]
    assert not d.exists() or not any(d.iterdir())  # canonical files GONE
    m = _json.loads((dest / "purge-manifest.json").read_text())
    assert m["unit"] == "260101-acme-pm" and m["reason"] == "stale package"
    assert m["initiated_by"] == "operator" and m["effect_signature"] == "sig-x"
    assert m["purged_at"] == "2026-07-10T12:00:00+00:00"
    assert sorted(Path(f).name for f in m["archived_files"]) == [
        "cover-letter.md", "resume.md"]


def test_purge_flat_file_source(grove):
    (grove / "sinkp").mkdir(parents=True)
    f = grove / "sinkp" / "draft-x.md"
    f.write_text("D")
    res = purge_artifacts([str(f)], PURGE_GOV, unit="x", reason="r",
                          initiated_by="operator")
    assert not f.exists()
    assert Path(res["files"][0]).read_text() == "D"


def test_purge_refuses_outside_canonical(grove):
    (grove / "elsewhere").mkdir()
    f = grove / "elsewhere" / "a.md"
    f.write_text("x")
    with pytest.raises(ValueError, match="does not resolve inside"):
        purge_artifacts([str(f)], PURGE_GOV, unit="u", reason="r",
                        initiated_by="operator")


def test_purge_refuses_staged_pending_review(grove):
    d = grove / "sinkp" / "pending_review" / "u1"
    d.mkdir(parents=True)
    (d / "resume.md").write_text("staged")
    with pytest.raises(ValueError, match="STAGED"):
        purge_artifacts([str(d / "resume.md")], PURGE_GOV, unit="u1",
                        reason="r", initiated_by="operator")


def test_purge_refuses_archive_dir_itself(grove):
    d = grove / "sinkp" / ".archive" / "old-20260101T000000Z"
    d.mkdir(parents=True)
    (d / "resume.md").write_text("archived")
    with pytest.raises(ValueError, match="already inside the archive"):
        purge_artifacts([str(d / "resume.md")], PURGE_GOV, unit="old",
                        reason="r", initiated_by="operator")


def test_purge_retap_resumes_incomplete_archive(grove):
    """Crash-after-moves: files archived, manifest missing. The re-tap resumes
    the SAME archive dir (no duplicate <unit>-<ts> dirs), storage_transfer's
    source-gone semantics satisfy the moves, and the manifest completes."""
    d = _canonical_unit(grove)
    files = [d / "resume.md", d / "cover-letter.md"]
    # simulate the crash: moves done, no manifest
    crash_dest = grove / "sinkp" / ".archive" / "260101-acme-pm-20260101T000000Z"
    from grove.utils.fs_utils import storage_transfer
    storage_transfer(files, crash_dest)
    assert not (crash_dest / "purge-manifest.json").exists()

    res = purge_artifacts([str(d)], PURGE_GOV, unit="260101-acme-pm",
                          reason="r", initiated_by="operator")
    assert res["resumed"] is True
    assert res["archive_dir"] == str(crash_dest)  # SAME dir — no duplicates
    assert (crash_dest / "purge-manifest.json").is_file()
    archive_dirs = list((grove / "sinkp" / ".archive").glob("260101-acme-pm-*"))
    assert len(archive_dirs) == 1


def test_purge_default_archive_dir_when_retention_absent(grove):
    gov = {"write_zone": {"staging_dir": "sinkp/pending_review",
                          "canonical_dir": "sinkp"}}  # no retention block
    d = _canonical_unit(grove)
    res = purge_artifacts([str(d)], gov, unit="260101-acme-pm", reason="r",
                          initiated_by="operator")
    assert "/.archive/" in res["archive_dir"]  # persist-by-default destination


def test_purge_core_is_producer_blind_and_routes_chokepoint():
    import inspect

    from grove.utils import fs_utils

    src = inspect.getsource(fs_utils.purge_artifacts)
    for name in ("forge", "scout", "drafter", "cultivator", "researcher"):
        assert name not in src, f"producer name {name!r} in the purge core"
    assert "storage_transfer(" in src  # THE chokepoint, no second copy
