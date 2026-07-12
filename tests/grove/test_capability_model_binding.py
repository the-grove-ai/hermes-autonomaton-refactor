"""R5 (browser-read-surface-v1) — Capability.model_binding schema + round-trip.

Proves: tier_override / specialty round-trip byte-identically; absence leaves the
92 existing records unchanged (no model_binding key emitted); and every malformed
binding fails loud at construction (unknown type, bad/missing tier, specialty
with a tier, binding on a non-skill record).
"""

from __future__ import annotations

import pytest

from grove.capability import Capability, ModelBinding


def _skill_dict(model_binding=None, kind="skill", with_skill_block=True):
    d = {
        "id": "skill.test.demo",
        "kind": kind,
        "trigger": {"intents": [], "keywords": [], "dock_affinity": [], "always": True, "disclosure": "proactive"},
        "bindings": {"tools": [], "credentials": None, "toolset_key": None},
        "tier_rule": {
            "eligible": [1, 2, 3], "preferred": 1, "promotion_criteria": {},
            "validation": {"strategy": "shadow_compare", "confidence_threshold": 0.95, "shadow_window": 20},
        },
        "zone": "green",
        "telemetry": {"feed": "intent_feed", "track": ["invocation"]},
        "context": {"disclosure": "eager", "payload": "", "dock_composition": "none"},
        "lifecycle": {
            "state": "active", "provenance": "migrated", "created_at": "2026-07-01T00:00:00+00:00",
            "last_used": None, "use_count": 0, "flywheel_eligible": True,
        },
        "lineage": {"source_patterns": [], "parent_id": None, "decision_log": []},
        "failure": {"fallback": "halt_and_surface", "diagnostic_context": [], "circuit_breaker": {"threshold": 3, "window_seconds": 300}},
    }
    if with_skill_block:
        d["skill"] = {"category": "test"}
    if model_binding is not None:
        d["model_binding"] = model_binding
    return d


def test_tier_override_round_trips():
    cap = Capability.from_dict(_skill_dict({"type": "tier_override", "tier": "T2"}))
    assert isinstance(cap.model_binding, ModelBinding)
    assert cap.model_binding.type == "tier_override"
    assert cap.model_binding.tier == "T2"
    d = cap.to_dict()
    assert d["model_binding"] == {"type": "tier_override", "tier": "T2"}
    assert Capability.from_dict(d).to_dict() == d  # stable round-trip


def test_specialty_round_trips_without_tier():
    cap = Capability.from_dict(_skill_dict({"type": "specialty"}))
    assert cap.model_binding.type == "specialty"
    assert cap.model_binding.tier is None
    assert cap.to_dict()["model_binding"] == {"type": "specialty"}


def test_absent_binding_emits_no_key():
    cap = Capability.from_dict(_skill_dict())  # no model_binding
    assert cap.model_binding is None
    assert "model_binding" not in cap.to_dict()  # byte-identical to pre-R5 records


@pytest.mark.parametrize("bad", [
    {"type": "bogus"},                       # unknown type
    {"type": "tier_override"},               # tier_override missing tier
    {"type": "tier_override", "tier": "T5"}, # tier out of range
    {"type": "tier_override", "tier": "T0"}, # T0 is the cache, not a reasoning tier
    {"type": "specialty", "tier": "T2"},     # specialty carries no tier
])
def test_malformed_binding_fails_loud(bad):
    with pytest.raises(ValueError):
        Capability.from_dict(_skill_dict(bad))


# ── aux-model-bindings-v1 — type="model" (exact provider-slug pin) ───────────


def test_model_pin_round_trips():
    cap = Capability.from_dict(_skill_dict({"type": "model", "model": "z-ai/glm-5.2"}))
    assert isinstance(cap.model_binding, ModelBinding)
    assert cap.model_binding.type == "model"
    assert cap.model_binding.model == "z-ai/glm-5.2"
    assert cap.model_binding.tier is None
    d = cap.to_dict()
    assert d["model_binding"] == {"type": "model", "model": "z-ai/glm-5.2"}
    assert Capability.from_dict(d).to_dict() == d  # stable round-trip


@pytest.mark.parametrize("bad", [
    {"type": "model"},                                   # missing model slug
    {"type": "model", "model": ""},                      # empty slug
    {"type": "model", "model": "   "},                   # whitespace-only slug
    {"type": "model", "model": "z-ai/glm-5.2", "tier": "T2"},  # model forbids tier
    {"type": "tier_override", "tier": "T2", "model": "z-ai/glm-5.2"},  # tier_override forbids model
    {"type": "specialty", "model": "z-ai/glm-5.2"},      # specialty forbids model
])
def test_malformed_model_pin_fails_loud(bad):
    with pytest.raises(ValueError):
        Capability.from_dict(_skill_dict(bad))


def test_binding_on_non_skill_record_fails_loud():
    with pytest.raises(ValueError, match="only valid on kind=skill"):
        Capability.from_dict(
            _skill_dict({"type": "tier_override", "tier": "T2"}, kind="verb", with_skill_block=False)
        )
