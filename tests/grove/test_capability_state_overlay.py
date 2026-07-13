"""fleet-hygiene-sweep P1 — capability STATE overlay loader.

State (~/.grove/capabilities/state/<id>.yaml, allowlisted keys keyed by id)
layers field-wise over the repo-bundled DEFINITIONS at load time. Pins:

* MERGE — each allowlisted key (model_binding, lifecycle.{state,pinned,
  use_count,last_used}, lineage.decision_log) shadows the definition.
* ANTI-SHADOW (R-A9) — a NEW field added to the bundled definition still
  renders on a record that carries state (field merge, not whole-record).
* DECISION_LOG — full-list replacement; a first state-write carrying the
  definition's seed entries + a new entry renders the complete chain.
* R-B1 FALLBACK — unknown key / malformed value / torn file → that record
  drops STATE, keeps its pure definition, never poisons the load.
* GHOST — state for an unknown id is warned + ignored, load survives.
* ABSENT-DIR NO-OP — no state dir = definitions load unchanged.
"""
from __future__ import annotations

import pytest

import grove.capability_registry as reg
from grove.capability import (
    Capability,
    CapabilityKind,
    CircuitBreaker,
    Context,
    Disclosure,
    DockComposition,
    Failure,
    Lifecycle,
    LifecycleState,
    Provenance,
    SkillPresentation,
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    TriggerDisclosure,
    Zone,
)
from grove.capability_registry import load_capabilities

_ID = "skill.demo.stateful"


def _cap_yaml(**overrides) -> str:
    cap = Capability(
        id=_ID,
        kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(
            eligible=[1, 2], preferred=2,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=Zone.YELLOW,
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(
            disclosure=Disclosure.PULL, payload="---\nname: x\n---\nb",
            dock_composition=DockComposition.NONE,
        ),
        lifecycle=Lifecycle(
            state=LifecycleState.ACTIVE, provenance=Provenance.OPERATOR_AUTHORED,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category="demo"),
    )
    return cap.to_yaml()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Dual-dir + state-dir monkeypatch idiom (R-A8): repo defn dir, empty
    whole-file overlay, and the state dir — all under tmp."""
    defn = tmp_path / "defn"
    defn.mkdir()
    (defn / "skill__demo__stateful.yaml").write_text(_cap_yaml(), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: defn)
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: tmp_path / "overlay")
    monkeypatch.setattr(reg, "capability_state_dir", lambda: state)
    return defn, state


def _write_state(state_dir, body: str):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "skill__demo__stateful.yaml").write_text(body, encoding="utf-8")


def _load(env):
    return load_capabilities()[_ID]


# ── MERGE ─────────────────────────────────────────────────────────────────────


def test_absent_state_dir_is_noop(env):
    cap = _load(env)
    assert cap.model_binding is None
    assert cap.lifecycle.state == LifecycleState.ACTIVE
    assert cap.lifecycle.pinned is False


def test_model_binding_merges(env):
    _, state = env
    _write_state(state, f"id: {_ID}\nmodel_binding:\n  type: model\n  model: prov/x\n")
    cap = _load(env)
    assert cap.model_binding is not None
    assert cap.model_binding.model == "prov/x"


def test_lifecycle_and_pinned_merge(env):
    _, state = env
    _write_state(state, f"id: {_ID}\nlifecycle:\n  pinned: true\n  use_count: 7\n")
    cap = _load(env)
    assert cap.lifecycle.pinned is True
    assert cap.lifecycle.use_count == 7
    # unset state keys keep the definition value
    assert cap.lifecycle.state == LifecycleState.ACTIVE


def test_lifecycle_state_transition_merges(env):
    _, state = env
    _write_state(state, f"id: {_ID}\nlifecycle:\n  state: refined\n")
    assert _load(env).lifecycle.state == LifecycleState.REFINED


def test_model_binding_null_clears(env):
    _, state = env
    # definition has no pin; a null state binding is a legal no-op clear
    _write_state(state, f"id: {_ID}\nmodel_binding: null\n")
    assert _load(env).model_binding is None


# ── ANTI-SHADOW (R-A9) ────────────────────────────────────────────────────────


def test_definition_upgrade_visible_through_state(env):
    """A field added to the bundled DEFINITION renders even when the record
    carries state — field merge, never whole-record shadow."""
    defn, state = env
    # simulate a definition upgrade: add governance.quality_gate to the defn
    text = (defn / "skill__demo__stateful.yaml").read_text()
    text += (
        "governance:\n"
        "  quality_gate:\n"
        "    rubric_version: '1.0'\n"
        "    criteria: [c1]\n"
        "    threshold: 0.7\n"
        "    redraft_limit: 1\n"
    )
    (defn / "skill__demo__stateful.yaml").write_text(text, encoding="utf-8")
    _write_state(state, f"id: {_ID}\nmodel_binding:\n  type: model\n  model: prov/x\n")
    cap = _load(env)
    assert cap.model_binding.model == "prov/x"          # state applied
    assert cap.governance["quality_gate"]["rubric_version"] == "1.0"  # defn upgrade visible


# ── DECISION_LOG (full-list replacement, seed carried forward) ───────────────


def test_decision_log_full_replacement_with_seed(env):
    """The writer carries the definition's seed entries into state; the loader
    renders the complete chain (state replaces, never concatenates)."""
    defn, state = env
    # seed the definition with one decision_log entry
    text = (defn / "skill__demo__stateful.yaml").read_text()
    text += (
        "lineage:\n"
        "  decision_log:\n"
        "  - actor: operator\n"
        "    timestamp: '2026-01-01T00:00:00+00:00'\n"
        "    from_state: new\n"
        "    to_state: active\n"
        "    reason: seed\n"
        "    evidence: []\n"
    )
    (defn / "skill__demo__stateful.yaml").write_text(text, encoding="utf-8")
    # state carries seed + a new transition (the writer's lossless obligation)
    _write_state(state, (
        f"id: {_ID}\n"
        "lineage:\n"
        "  decision_log:\n"
        "  - actor: operator\n"
        "    timestamp: '2026-01-01T00:00:00+00:00'\n"
        "    from_state: new\n"
        "    to_state: active\n"
        "    reason: seed\n"
        "    evidence: []\n"
        "  - actor: curator\n"
        "    timestamp: '2026-07-13T00:00:00+00:00'\n"
        "    from_state: active\n"
        "    to_state: refined\n"
        "    reason: promoted\n"
        "    evidence: []\n"
    ))
    cap = _load(env)
    assert len(cap.lineage.decision_log) == 2
    assert cap.lineage.decision_log[0].reason == "seed"
    assert cap.lineage.decision_log[1].reason == "promoted"


# ── R-B1 FALLBACK ─────────────────────────────────────────────────────────────


def test_unknown_top_key_drops_state_keeps_definition(env, caplog):
    _, state = env
    _write_state(state, f"id: {_ID}\nzone: red\n")  # zone is NOT allowlisted state
    with caplog.at_level("CRITICAL"):
        cap = _load(env)
    assert cap.zone == Zone.YELLOW  # definition intact — state dropped
    assert any("invalid" in r.message for r in caplog.records)


def test_unknown_lifecycle_subkey_drops_state(env, caplog):
    _, state = env
    _write_state(state, f"id: {_ID}\nlifecycle:\n  provenance: agent_proposed\n")
    with caplog.at_level("CRITICAL"):
        cap = _load(env)
    assert cap.lifecycle.provenance == Provenance.OPERATOR_AUTHORED


def test_malformed_model_binding_value_falls_back(env, caplog):
    _, state = env
    # type=model but no model slug → validate() rejects → R-B1 fallback
    _write_state(state, f"id: {_ID}\nmodel_binding:\n  type: model\n")
    with caplog.at_level("CRITICAL"):
        cap = _load(env)
    assert cap.model_binding is None  # pure definition
    assert any("dropping STATE" in r.message for r in caplog.records)


def test_torn_file_falls_back(env, caplog):
    _, state = env
    _write_state(state, f"id: {_ID}\nmodel_binding:\n  type: model\n  model: prov/x\n  {{{{ torn")
    with caplog.at_level("CRITICAL"):
        cap = _load(env)
    assert cap.model_binding is None
    assert any("invalid" in r.message for r in caplog.records)


def test_missing_id_falls_back_loud(env, caplog):
    _, state = env
    _write_state(state, "model_binding:\n  type: model\n  model: prov/x\n")  # no id
    with caplog.at_level("CRITICAL"):
        cap = _load(env)
    assert cap.model_binding is None


# ── GHOST ─────────────────────────────────────────────────────────────────────


def test_ghost_state_for_unknown_id_ignored(env, caplog):
    _, state = env
    _write_state(state, "id: skill.demo.nonexistent\nmodel_binding:\n  type: model\n  model: prov/x\n")
    with caplog.at_level("WARNING"):
        caps = load_capabilities()
    assert _ID in caps  # load survived
    assert caps[_ID].model_binding is None
    assert any("ghost state" in r.message for r in caplog.records)
