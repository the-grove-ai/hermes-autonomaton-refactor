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


def test_shipped_registry_declares_the_review_unified_producer_set():
    # fleet-review-unification-v1 C1b-2 — the committed registry declares the three
    # real fleet producers: forge (notion_query) and the file producers drafter /
    # cultivator (file_source), all enabled. It must load cleanly.
    # researcher-fleet-worker-v1 P2 — researcher joins as a one_shot file_source
    # worker, shipped DISABLED (the operator arms it via the override overlay).
    workers = config.load_fleet_workers(config.default_fleet_workers_path())
    assert set(workers) == {"forge", "drafter", "cultivator", "researcher"}
    assert all(w.enabled for w in workers.values() if w.id != "researcher")
    assert workers["researcher"].enabled is False
    assert workers["forge"].input_state["type"] == "notion_query"
    assert workers["drafter"].input_state["type"] == "file_source"
    assert workers["cultivator"].input_state["type"] == "file_source"
    assert workers["researcher"].input_state["type"] == "file_source"
    assert workers["researcher"].input_state["lifecycle"] == "one_shot"


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


# ── aux-model-bindings-v1: exact-model pin at the worker runtime seam ─────────


def _routing_stub(monkeypatch, tier_model="tier-org/tier-model", provider="openrouter"):
    """Stub route_for_agent + resolve_tier_to_runtime; capture what the
    credential bridge receives so the pin's envelope coherence is assertable."""
    from types import SimpleNamespace

    import grove.providers as providers
    from grove.router import TierConfig

    tc = TierConfig(
        tier="T2", handler=None, provider=provider, model=tier_model,
        max_tokens=1234, max_latency_ms=None, description="stub",
    )
    seen: dict = {}

    def fake_route(explicit_tier=None, classify=True, **kw):
        seen["explicit_tier"] = explicit_tier
        return SimpleNamespace(tier_config=tc)

    def fake_resolve(tier_config):
        seen["resolved_tier_config"] = tier_config
        return {"model": tier_config.model, "provider": tier_config.provider,
                "api_key": "k", "base_url": "u", "api_mode": "chat_completions",
                "credential_pool": None, "auth_type": "api_key"}

    monkeypatch.setattr(providers, "route_for_agent", fake_route)
    monkeypatch.setattr(providers, "resolve_tier_to_runtime", fake_resolve)
    return seen


def _forge_with_binding(binding):
    d = _forge_record().to_dict()
    if binding is None:
        d.pop("model_binding", None)
    else:
        d["model_binding"] = binding
    return Capability.from_dict(d)


def test_model_pin_binds_exact_slug_not_tier_model(monkeypatch, caplog):
    # F5 anti-masking pin: the pinned slug DIFFERS from the tier model, and the
    # Agent-bound model must be the pin — a tier-model pass-through would fail here.
    seen = _routing_stub(monkeypatch, tier_model="tier-org/tier-model")
    cap = _forge_with_binding({"type": "model", "model": "pin-org/pin-model"})
    import logging

    with caplog.at_level(logging.INFO, logger="grove.fleet.worker_entry"):
        model, max_tokens, runtime = worker_entry._resolve_worker_runtime(cap, "forge")
    assert model == "pin-org/pin-model"
    assert runtime["model"] == "pin-org/pin-model"
    # Envelope coherence: credential bridge saw the PINNED slug with the tier's
    # provider; max_tokens carried from the tier config.
    assert seen["resolved_tier_config"].model == "pin-org/pin-model"
    assert seen["resolved_tier_config"].provider == "openrouter"
    assert max_tokens == 1234
    # Exact branch payload (GATE-B F5) — asserted verbatim, inside the branch only.
    assert "model_binding: pinned=pin-org/pin-model bypassing tier=T2" in caplog.text


def test_no_binding_tier_path_unchanged(monkeypatch):
    seen = _routing_stub(monkeypatch, tier_model="tier-org/tier-model")
    cap = _forge_with_binding(None)
    model, max_tokens, runtime = worker_entry._resolve_worker_runtime(cap, "forge")
    assert model == "tier-org/tier-model"
    assert seen["resolved_tier_config"].model == "tier-org/tier-model"
    assert seen["explicit_tier"] == "T2"  # forge tier_rule.preferred == 2
    assert max_tokens == 1234


@pytest.mark.parametrize("bad_slug", [
    "noslash", "a/b/c", "/half", "half/", "   ", "",
])
def test_malformed_pinned_slug_fails_spawn_loud(monkeypatch, bad_slug):
    # Spawn-side shape guard, independent of record-load validation (which the
    # direct ModelBinding construction here deliberately bypasses). NO tier
    # fallback under any failure (GATE-B F3).
    from grove.capability import ModelBinding

    _routing_stub(monkeypatch)
    cap = _forge_with_binding(None)
    cap.model_binding = ModelBinding(type="model", model=bad_slug)
    with pytest.raises(FleetWorkerAndon) as ei:
        worker_entry._resolve_worker_runtime(cap, "forge")
    assert ei.value.check == "model_binding_malformed_slug"


def test_tier_override_binding_leaves_fleet_tier_path_unchanged(monkeypatch, caplog):
    # tier_override remains a Mylo-path (invoke_skill rebind) concept; the fleet
    # seam takes only the type=model branch. Existing behavior byte-identical:
    # tier model returned, no pin log emitted.
    import logging

    seen = _routing_stub(monkeypatch, tier_model="tier-org/tier-model")
    cap = _forge_with_binding({"type": "tier_override", "tier": "T2"})
    with caplog.at_level(logging.INFO, logger="grove.fleet.worker_entry"):
        model, _, _ = worker_entry._resolve_worker_runtime(cap, "forge")
    assert model == "tier-org/tier-model"
    assert seen["resolved_tier_config"].model == "tier-org/tier-model"
    assert "model_binding: pinned=" not in caplog.text
