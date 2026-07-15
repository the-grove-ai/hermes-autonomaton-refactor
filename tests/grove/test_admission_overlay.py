"""operator-mutable-admission-v1 Phase 1 — additive admission overlay.

The ~/.grove/capabilities/state/<id>.yaml overlay gains two ADDITIVE admission
keys, read PER TURN at the builder (no restart, no cache):

* ``added_intents`` (list[str]) — UNION with the repo record's trigger.intents.
* ``force_always`` (bool, only ``true`` honored) — OR with repo trigger.always.

Invariants pinned here:
  I1 additive-only — offered_set(repo ∪ overlay) ⊇ offered_set(repo), always.
  I2 malformed overlay ⇒ per-record fallback to repo definition + Andon warning;
     never an empty offered set, never a halt.
  Cross-writer preservation — an admission write never erases model_binding /
     lifecycle, and a model_binding write never erases admission keys.
  Write-strict — the sanctioned writer rejects force_always:false, non-list
     added_intents, non-str intents, and unknown ids (fail loud).
  Per-turn — an overlay edit is visible on the NEXT resolution with no cache reset.
"""
from __future__ import annotations

import logging

import pytest

import grove.capability_registry as reg
from grove.capability import (
    Bindings,
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
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    TriggerDisclosure,
    Zone,
)
from grove.capability_registry import (
    read_admission_overlay,
    set_admission_overlay,
)
from grove.context_budget import _registry_allowed_names, reset_caps_index_cache

_ID = "verb.demo.gated"
_TOOL = "demo_gated_tool"
_FILE = "verb__demo__gated.yaml"


def _cap_yaml() -> str:
    cap = Capability(
        id=_ID,
        kind=CapabilityKind.VERB,
        trigger=Trigger(
            intents=["research"], always=False,
            disclosure=TriggerDisclosure.PROACTIVE,
        ),
        bindings=Bindings(tools=[_TOOL], toolset_key=None),
        tier_rule=TierRule(
            eligible=[1, 2, 3], preferred=1,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=Zone.GREEN,
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(
            disclosure=Disclosure.EAGER, payload="native demo tool",
            dock_composition=DockComposition.NONE,
        ),
        lifecycle=Lifecycle(
            state=LifecycleState.ACTIVE, provenance=Provenance.OPERATOR_AUTHORED,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
    )
    return cap.to_yaml()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    defn = tmp_path / "defn"
    defn.mkdir()
    (defn / _FILE).write_text(_cap_yaml(), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: defn)
    monkeypatch.setattr(reg, "grove_home_capabilities_dir", lambda: tmp_path / "overlay")
    monkeypatch.setattr(reg, "capability_state_dir", lambda: state)
    reset_caps_index_cache()
    yield defn, state
    reset_caps_index_cache()


def _write_overlay(state_dir, body: str):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _FILE).write_text(body, encoding="utf-8")


def _offered(intent):
    names, _ = _registry_allowed_names(intent, "moderate", current_tier=None)
    return names


# ── baseline: repo record gates on its declared intent ─────────────────────

def test_repo_only_gates_on_intent(env):
    assert _TOOL in _offered("research")            # declared intent
    assert _TOOL not in _offered("creative_writing")  # not declared


# ── added_intents: union ───────────────────────────────────────────────────

def test_added_intents_unions_with_repo(env):
    _, state = env
    _write_overlay(state, f"id: {_ID}\nadded_intents: [creative_writing]\n")
    assert _TOOL in _offered("creative_writing")    # overlay-added
    assert _TOOL in _offered("research")            # repo intent PRESERVED (union)
    assert _TOOL not in _offered("memory_operation")  # not added → still gated


# ── force_always: OR ───────────────────────────────────────────────────────

def test_force_always_offers_on_every_intent(env):
    _, state = env
    _write_overlay(state, f"id: {_ID}\nforce_always: true\n")
    assert _TOOL in _offered("creative_writing")
    assert _TOOL in _offered("memory_operation")
    assert _TOOL in _offered("unknown")             # rides even the unknown core


# ── I1 additive-only: overlay can never SHRINK the offered set ─────────────

@pytest.mark.parametrize("body", [
    f"id: {_ID}\nadded_intents: []\n",
    f"id: {_ID}\nadded_intents: [creative_writing]\n",
    f"id: {_ID}\nforce_always: true\n",
])
def test_additive_only_never_shrinks(env, body):
    _, state = env
    base = _offered("research")
    _write_overlay(state, body)
    after = _offered("research")
    assert base <= after, "overlay shrank the offered set — additive-only violated"


# ── I2 malformed overlay: repo fallback + Andon, never empty ───────────────

def test_malformed_overlay_falls_back_and_andons(env, caplog):
    _, state = env
    _write_overlay(state, f"id: {_ID}\nadded_intents: 'not-a-list'\n")
    with caplog.at_level(logging.WARNING):
        offered = _offered("research")
    assert _TOOL in offered, "malformed overlay must fall back to repo definition"
    assert offered, "offered set must never be empty on a malformed overlay"
    assert any("overlay" in r.message.lower() for r in caplog.records), "Andon warning expected"


def test_malformed_overlay_does_not_add(env):
    _, state = env
    _write_overlay(state, f"id: {_ID}\nforce_always: 'yes'\n")  # non-bool
    assert _TOOL not in _offered("creative_writing"), "malformed force_always must not admit"


# ── per-turn: overlay edit visible next resolution, NO cache reset ─────────

def test_overlay_read_is_per_turn(env):
    _, state = env
    assert _TOOL not in _offered("creative_writing")   # primes the cached repo projection
    _write_overlay(state, f"id: {_ID}\nadded_intents: [creative_writing]\n")
    # NO reset_caps_index_cache() — the overlay must be read fresh per resolution.
    assert _TOOL in _offered("creative_writing")


# ── writer: write-strict validation ────────────────────────────────────────

def test_writer_rejects_force_always_false(env):
    with pytest.raises(ValueError):
        set_admission_overlay(_ID, force_always=False)


def test_writer_rejects_non_list_intents(env):
    with pytest.raises(ValueError):
        set_admission_overlay(_ID, add_intents="creative_writing")


def test_writer_rejects_non_str_intent(env):
    with pytest.raises(ValueError):
        set_admission_overlay(_ID, add_intents=[123])


def test_writer_rejects_unknown_id(env):
    with pytest.raises(reg.CapabilityLoadError):
        set_admission_overlay("verb.does.not.exist", add_intents=["research"])


# ── writer + reader round-trip, and offered effect ─────────────────────────

def test_writer_applies_and_offers(env):
    _, state = env
    assert set_admission_overlay(_ID, add_intents=["creative_writing"]) == "applied"
    overlay = read_admission_overlay()
    assert "creative_writing" in overlay[_ID][0]
    assert _TOOL in _offered("creative_writing")


# ── cross-writer preservation ───────────────────────────────────────────────

def test_admission_write_preserves_prior_state_keys(env):
    _, state = env
    # a pre-existing Capability-state key (lifecycle) in the same file...
    _write_overlay(state, f"id: {_ID}\nlifecycle:\n  pinned: true\n  use_count: 9\n")
    set_admission_overlay(_ID, add_intents=["creative_writing"])
    from grove.capability_registry import load_capabilities
    reset_caps_index_cache()
    cap = load_capabilities()[_ID]
    # ...must survive the admission write (one sovereignty seam, no clobber).
    assert cap.lifecycle.pinned is True and cap.lifecycle.use_count == 9
    assert "creative_writing" in read_admission_overlay()[_ID][0]


def test_state_snapshot_write_preserves_admission_keys(env):
    _, state = env
    set_admission_overlay(_ID, add_intents=["creative_writing"])
    # A Capability-state write (lifecycle) routes through _write_state_snapshot,
    # whose full-snapshot dump must NOT erase the additive admission keys.
    reg.update_lifecycle_fields(_ID, use_count=5)
    assert "creative_writing" in read_admission_overlay()[_ID][0]
