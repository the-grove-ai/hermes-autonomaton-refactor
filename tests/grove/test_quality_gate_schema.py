"""drafter-quality-checks-v1 P1 / artifact-review-v1 P2 —
governance.quality_gate schema pins.

The rubric was extracted to config/rubrics.yaml (artifact-review-v1). The
record now carries ``rubric_ref`` (a by-name pointer resolved against the
registry at load time) instead of the embedded ``rubric_version`` +
``criteria``. These SCHEMA pins resolve ``rubric_ref`` against a STUB
registry with TEST-ONLY keys — the schema contract is "rubric_ref must be a
resolvable key," not "these particular keys exist," so a criterion's wording
changing in the real registry must never turn a schema pin red. The real
registry + real records are guarded separately in
``tests/grove/test_rubric_registry_integration.py``.

Pin families:

* SHAPE — valid blocks (with/without optional threshold / evaluator_tier /
  context_inputs) pass; every malformed variant gains a non-destructive
  ``quality_gate_error`` sibling and the operator's block is never destroyed.
* RESOLUTION (R-11) — a shape-valid block whose ``rubric_ref`` does not
  resolve gains the same non-destructive error sibling (the seam P3's HALT
  is built on).
* LEGACY (R-2) — a record still carrying ``criteria`` / ``rubric_version``
  is a loud rejection whose message NAMES the registry.
* STALE ERROR — a now-valid block clears a stale quality_gate_error.
* ROUND-TRIP — the block survives from_dict/to_dict/to_yaml, the
  transition_record lifecycle write path, and the set_model_binding write
  path (the two sanctioned record writers).
* GENERALIZABILITY (R-A11 / R-12) — validation keys on block presence only;
  the module contains zero producer names.
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
from grove.fleet.rubric_registry import (
    Criterion,
    Rubric,
    RubricRegistry,
    compute_content_hash,
)

_CATALOG = [{"slug": "z-ai/glm-5.2"}]


def _stub_rubric(key, threshold, criteria):
    return Rubric(
        key=key,
        default_threshold=threshold,
        criteria=criteria,
        content_hash=compute_content_hash(threshold, criteria),
    )


# ── STUB registry: TEST-ONLY keys (RULING A.1 — never the real keys) ─────────
_STUB_REGISTRY = RubricRegistry(
    rubrics={
        "test-class@1": _stub_rubric(
            "test-class@1",
            0.7,
            [
                Criterion(id="c1", type="llm", definition="a test criterion"),
                Criterion(id="c2", type="llm", definition="another test criterion"),
            ],
        ),
        "other-class@2": _stub_rubric(
            "other-class@2",
            0.9,
            [Criterion(id="c1", type="llm", definition="a test criterion")],
        ),
    }
)


@pytest.fixture(autouse=True)
def _stub_rubric_registry(monkeypatch):
    """Resolve rubric_ref against the stub, not the live config/rubrics.yaml —
    the schema contract is 'resolvable key', decoupled from registry content.
    The loader is imported function-locally inside the validator, so patching
    the module attribute covers every resolution path (incl. load_capabilities)."""
    monkeypatch.setattr(
        "grove.fleet.rubric_registry.load_rubric_registry",
        lambda *a, **k: _STUB_REGISTRY,
    )


_VALID_GATE = {
    "rubric_ref": "test-class@1",
    "threshold": 0.7,
    "redraft_limit": 1,
    "evaluator_tier": "T1",
}

# A1 (R-A12) — optional task-context declaration; absent → criteria-only.
_VALID_GATE_WITH_CONTEXT = dict(
    _VALID_GATE, context_inputs=["angle", "source_digest"]
)

_MINIMAL_GATE = {
    # threshold (R-3), evaluator_tier, and context_inputs are all optional
    # (consumer defaults: rubric default_threshold / T1 / criteria-only).
    "rubric_ref": "other-class@2",
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


def test_gate_without_threshold_loads_clean():
    """R-3 — threshold is optional; absent is valid (rubric default applies).
    The positive twin of the deleted 'missing threshold' malformed param."""
    gate = {"rubric_ref": "test-class@1", "redraft_limit": 1}
    gov = {"quality_gate": dict(gate)}
    _validate_quality_gate(gov, "test.record")
    assert "quality_gate_error" not in gov


def test_absent_gate_is_untouched():
    gov = {"write_zone": {"staging": "x"}}
    _validate_quality_gate(gov, "test.record")
    assert gov == {"write_zone": {"staging": "x"}}


# ── SHAPE: malformed variants (non-destructive error sibling) ────────────────


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("not-a-mapping", id="not-a-mapping"),
        pytest.param({}, id="empty-dict"),
        pytest.param(dict(_VALID_GATE, surprise=1), id="unknown-key"),
        # rubric_ref field validation (replaces the old rubric_version params)
        pytest.param(
            {k: v for k, v in _VALID_GATE.items() if k != "rubric_ref"},
            id="missing-rubric_ref",
        ),
        pytest.param(dict(_VALID_GATE, rubric_ref=""), id="rubric_ref-empty"),
        pytest.param(dict(_VALID_GATE, rubric_ref=1.0), id="rubric_ref-nonstr"),
        # threshold VALUE validation retained (R-3); 'missing' is no longer here
        pytest.param(dict(_VALID_GATE, threshold="0.7"), id="threshold-nonnumber"),
        pytest.param(dict(_VALID_GATE, threshold=1.5), id="threshold-too-high"),
        pytest.param(dict(_VALID_GATE, threshold=-0.1), id="threshold-negative"),
        pytest.param(dict(_VALID_GATE, threshold=True), id="threshold-bool"),
        pytest.param(
            {k: v for k, v in _VALID_GATE.items() if k != "redraft_limit"},
            id="missing-redraft_limit",
        ),
        pytest.param(dict(_VALID_GATE, redraft_limit=2), id="redraft_limit-2"),
        pytest.param(dict(_VALID_GATE, redraft_limit="1"), id="redraft_limit-nonint"),
        pytest.param(dict(_VALID_GATE, redraft_limit=True), id="redraft_limit-bool"),
        pytest.param(dict(_VALID_GATE, evaluator_tier=""), id="evaluator_tier-empty"),
        pytest.param(dict(_VALID_GATE, evaluator_tier=1), id="evaluator_tier-nonstr"),
        pytest.param(dict(_VALID_GATE, context_inputs=[]), id="context_inputs-empty"),
        pytest.param(dict(_VALID_GATE, context_inputs=["ok", ""]), id="context_inputs-has-empty"),
        pytest.param(dict(_VALID_GATE, context_inputs="angle"), id="context_inputs-nonstr"),
    ],
)
def test_malformed_gate_gets_nondestructive_error_sibling(bad):
    gov = {"quality_gate": copy.deepcopy(bad)}
    _validate_quality_gate(gov, "test.record")
    assert gov["quality_gate"] == bad, "operator's block must never be destroyed"
    assert gov.get("quality_gate_error"), f"no quality_gate_error for {bad!r}"


# ── LEGACY (R-2): embedded-rubric fields are a loud rejection naming registry ──


@pytest.mark.parametrize(
    "legacy_gate",
    [
        pytest.param(dict(_VALID_GATE, rubric_version="1.0"), id="legacy-rubric_version"),
        pytest.param(dict(_VALID_GATE, criteria=["x"]), id="legacy-criteria"),
        pytest.param(
            dict(_VALID_GATE, rubric_version="1.0", criteria=["x"]), id="legacy-both"
        ),
    ],
)
def test_legacy_fields_rejected_naming_registry(legacy_gate):
    """R-2 — criteria / rubric_version no longer live on the record; their
    presence is a loud rejection whose message NAMES the registry (converted
    from the old rubric_version/criteria field-validation params — coverage
    moved, not deleted)."""
    gov = {"quality_gate": copy.deepcopy(legacy_gate)}
    _validate_quality_gate(gov, "test.record")
    err = gov.get("quality_gate_error")
    assert err, "legacy fields must be rejected"
    assert "config/rubrics.yaml" in err, "rejection must name the registry"


# ── RESOLUTION (R-11): unresolvable rubric_ref — the seam P3's HALT sits on ───


def test_unresolvable_rubric_ref_stamps_error():
    """R-11 — a shape-valid gate whose rubric_ref does not resolve gains the
    non-destructive error sibling. LOAD-BEARING and standalone: P3's worker
    HALT is built directly on this behavior."""
    gate = dict(_VALID_GATE, rubric_ref="does-not-exist@9")
    gov = {"quality_gate": copy.deepcopy(gate)}
    _validate_quality_gate(gov, "test.record")
    err = gov.get("quality_gate_error")
    assert err, "an unresolvable rubric_ref must stamp an error"
    assert "does-not-exist@9" in err
    assert "config/rubrics.yaml" in err
    assert gov["quality_gate"] == gate, "operator's block must never be destroyed"


def test_valid_gate_clears_stale_error():
    gov = {"quality_gate": dict(_VALID_GATE), "quality_gate_error": "stale"}
    _validate_quality_gate(gov, "test.record")
    assert "quality_gate_error" not in gov


def test_malformed_gate_flagged_through_from_dict():
    d = _record_dict({"rubric_ref": ""})  # shape-invalid: empty rubric_ref
    cap = Capability.from_dict(d)
    assert cap.governance["quality_gate"] == {"rubric_ref": ""}
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

    state_dir = tmp_path / "state"
    result = transition_record(
        "skill.test.qualitygate",
        LifecycleState.REFINED,
        actor="test",
        reason="quality_gate write-path pin",
        directory=caps_dir,
        state_dir=state_dir,
    )
    assert result.status == "applied"
    # fleet-hygiene-sweep P2 — the transition writes the STATE overlay
    # (state=refined + decision_log); the DEFINITION (with its quality_gate)
    # stays byte-clean and read-only.
    st = _yaml.safe_load(
        (state_dir / "skill__test__qualitygate.yaml").read_text(encoding="utf-8")
    )
    assert st["lifecycle"]["state"] == "refined"
    defn = _yaml.safe_load(path.read_text(encoding="utf-8"))
    assert defn["lifecycle"]["state"] == "active"                 # definition untouched
    assert defn["governance"]["quality_gate"] == _VALID_GATE
    # artifact-review-v1 P2 — a resolvable gate stamps NO error, so the
    # definition YAML never carries a quality_gate_error sibling (the latent
    # defect the old embedded-rubric fixture masked — now pinned closed).
    assert "quality_gate_error" not in defn["governance"]


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
    # fleet-hygiene-sweep P2 — the pin lands in the STATE overlay; the composed
    # load carries BOTH the definition's quality_gate AND the state's pin (the
    # anti-shadow guarantee: state merges a field, never masks the record).
    from grove.capability_registry import load_capabilities

    reloaded = load_capabilities()["skill.test.qualitygate"]
    assert reloaded.model_binding is not None
    assert reloaded.governance["quality_gate"] == _VALID_GATE
    assert "quality_gate_error" not in reloaded.governance


# ── GENERALIZABILITY (R-A11 / R-12) ───────────────────────────────────────────


def test_validator_names_no_producers():
    import inspect

    import grove.capability as capability_mod

    src = inspect.getsource(capability_mod._quality_gate_shape_error) + inspect.getsource(
        capability_mod._validate_quality_gate
    )
    for producer in ("drafter", "cultivator"):
        assert producer not in src
