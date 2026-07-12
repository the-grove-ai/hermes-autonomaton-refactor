"""drafter-quality-checks-v1 P1 — governance.quality_gate schema pins.

Pin families:

* SHAPE — valid blocks (with and without the optional evaluator_tier /
  context_inputs keys, A1) pass; every malformed variant gains a
  non-destructive ``quality_gate_error`` sibling and the operator's block
  is never destroyed (the _validate_emit precedent, third sibling).
* STALE ERROR — a now-valid block clears a stale quality_gate_error.
* ROUND-TRIP — the block survives from_dict/to_dict/to_yaml, the
  transition_record lifecycle write path, and the set_model_binding write
  path (the two sanctioned record writers).
* GENERALIZABILITY (R-A11) — validation keys on block presence only; the
  module contains zero producer names.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml as _yaml

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
    _validate_quality_gate,
)
from grove.capability_registry import set_model_binding, transition_record

_CATALOG = [{"slug": "z-ai/glm-5.2"}]

_VALID_GATE = {
    "rubric_version": "1.0",
    "criteria": ["makes one falsifiable claim", "evidence is specific"],
    "threshold": 0.7,
    "redraft_limit": 1,
    "evaluator_tier": "T1",
}

# A1 (R-A12) — optional task-context declaration; absent → criteria-only.
_VALID_GATE_WITH_CONTEXT = dict(
    _VALID_GATE, context_inputs=["angle", "source_digest"]
)

_MINIMAL_GATE = {
    # evaluator_tier and context_inputs are optional (consumer defaults
    # T1 / criteria-only).
    "rubric_version": "1.0",
    "criteria": ["c1"],
    "threshold": 0.7,
    "redraft_limit": 1,
}


def _record_dict(gate) -> dict:
    d = {
        "id": "skill.test.qualitygate", "kind": "skill", "zone": "green",
        "trigger": {"always": True},
        "tier_rule": {"eligible": [2], "preferred": 2,
                      "validation": {"confidence_threshold": 0.95, "shadow_window": 20}},
        "telemetry": {"feed": "intent_feed"},
        "lifecycle": {"state": "active"},
        "failure": {"circuit_breaker": {"threshold": 3, "window_seconds": 300}},
        "skill": {"category": "test"},
        "governance": {"quality_gate": copy.deepcopy(gate)},
    }
    return d


# ── SHAPE: valid variants ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "gate", [_VALID_GATE, _VALID_GATE_WITH_CONTEXT, _MINIMAL_GATE]
)
def test_valid_gate_loads_without_error_sibling(gate):
    cap = Capability.from_dict(_record_dict(gate))
    assert cap.governance["quality_gate"] == gate
    assert "quality_gate_error" not in cap.governance


def test_absent_gate_is_untouched():
    gov = {"write_zone": {"staging": "x"}}
    _validate_quality_gate(gov, "test.record")
    assert gov == {"write_zone": {"staging": "x"}}


# ── SHAPE: malformed variants (non-destructive error sibling) ────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-mapping",
        {},
        dict(_VALID_GATE, surprise=1),                       # unknown key
        {k: v for k, v in _VALID_GATE.items() if k != "rubric_version"},
        dict(_VALID_GATE, rubric_version=""),
        dict(_VALID_GATE, rubric_version=1.0),
        dict(_VALID_GATE, criteria=[]),
        dict(_VALID_GATE, criteria=["ok", ""]),
        dict(_VALID_GATE, criteria="one string"),
        {k: v for k, v in _VALID_GATE.items() if k != "threshold"},
        dict(_VALID_GATE, threshold="0.7"),
        dict(_VALID_GATE, threshold=1.5),
        dict(_VALID_GATE, threshold=-0.1),
        dict(_VALID_GATE, threshold=True),                   # bool is not a number
        {k: v for k, v in _VALID_GATE.items() if k != "redraft_limit"},
        dict(_VALID_GATE, redraft_limit=2),                  # v1 fixes the cycle at 1
        dict(_VALID_GATE, redraft_limit="1"),
        dict(_VALID_GATE, redraft_limit=True),
        dict(_VALID_GATE, evaluator_tier=""),
        dict(_VALID_GATE, evaluator_tier=1),
        dict(_VALID_GATE, context_inputs=[]),                # A1: present must be non-empty
        dict(_VALID_GATE, context_inputs=["ok", ""]),
        dict(_VALID_GATE, context_inputs="angle"),
    ],
)
def test_malformed_gate_gets_nondestructive_error_sibling(bad):
    gov = {"quality_gate": copy.deepcopy(bad)}
    _validate_quality_gate(gov, "test.record")
    assert gov["quality_gate"] == bad, "operator's block must never be destroyed"
    assert gov.get("quality_gate_error"), f"no quality_gate_error for {bad!r}"


def test_valid_gate_clears_stale_error():
    gov = {"quality_gate": dict(_VALID_GATE), "quality_gate_error": "stale"}
    _validate_quality_gate(gov, "test.record")
    assert "quality_gate_error" not in gov


def test_malformed_gate_flagged_through_from_dict():
    d = _record_dict({"rubric_version": "1.0"})  # missing required keys
    cap = Capability.from_dict(d)
    assert cap.governance["quality_gate"] == {"rubric_version": "1.0"}
    assert cap.governance.get("quality_gate_error")


# ── ROUND-TRIP: serialization + the two sanctioned record writers ─────────────


def test_gate_round_trips_from_dict_to_yaml():
    cap = Capability.from_dict(_record_dict(_VALID_GATE_WITH_CONTEXT))
    assert cap.to_dict()["governance"]["quality_gate"] == _VALID_GATE_WITH_CONTEXT
    cap2 = Capability.from_yaml(cap.to_yaml())
    assert cap2.governance["quality_gate"] == _VALID_GATE_WITH_CONTEXT
    assert "quality_gate_error" not in cap2.governance


def test_gate_survives_lifecycle_write(tmp_path):
    caps_dir = tmp_path / "caps"
    caps_dir.mkdir()
    cap = Capability.from_dict(_record_dict(_VALID_GATE))
    path = caps_dir / "skill__test__qualitygate.yaml"
    path.write_text(cap.to_yaml(), encoding="utf-8")

    result = transition_record(
        "skill.test.qualitygate",
        LifecycleState.REFINED,
        actor="test",
        reason="quality_gate write-path pin",
        directory=caps_dir,
    )
    assert result.status == "applied"
    reloaded = _yaml.safe_load(path.read_text(encoding="utf-8"))
    assert reloaded["lifecycle"]["state"] == "refined"
    assert reloaded["governance"]["quality_gate"] == _VALID_GATE


@pytest.fixture
def caps_env(tmp_path, monkeypatch):
    """Hermetic registry home (the binding-writer test precedent)."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    repo_caps = tmp_path / "repo_caps"
    repo_caps.mkdir()
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: repo_caps)
    monkeypatch.setattr(
        reg, "grove_home_capabilities_dir", lambda: tmp_path / "capabilities"
    )
    monkeypatch.setattr(
        "grove.config.model_catalog.load_catalog", lambda: list(_CATALOG)
    )
    return repo_caps


def test_gate_survives_model_binding_write(caps_env):
    cap = Capability.from_dict(_record_dict(_VALID_GATE))
    path = caps_env / "skill__test__qualitygate.yaml"
    path.write_text(cap.to_yaml(), encoding="utf-8")

    set_model_binding(
        "qualitygate", {"type": "model", "model": "z-ai/glm-5.2"}, surface="portal"
    )
    reloaded = Capability.from_yaml(path.read_text(encoding="utf-8"))
    assert reloaded.model_binding is not None
    assert reloaded.governance["quality_gate"] == _VALID_GATE


# ── GENERALIZABILITY (R-A11) ──────────────────────────────────────────────────


def test_validator_names_no_producers():
    import inspect

    import grove.capability as capability_mod

    src = inspect.getsource(capability_mod._quality_gate_shape_error) + inspect.getsource(
        capability_mod._validate_quality_gate
    )
    for producer in ("drafter", "cultivator"):
        assert producer not in src
