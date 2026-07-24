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
    names = _registry_allowed_names(intent, "moderate")
    return names


# native-presence-declared-v1 RETIRED test_repo_only_gates_on_intent and
# test_added_intents_unions_with_repo: intent-match admission is DELETED. A
# proactive+always:false record (this demo) is declared absent — never offered by
# intent match — and the added_intents union overlay is gone. Presence is now
# changed only via force_always (covered below).


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


# native-presence-declared-v1 RETIRED test_malformed_overlay_falls_back_and_andons:
# it asserted an added_intents overlay fallback re-admits the demo tool by its repo
# intent — but intent-match admission is deleted and added_intents is no longer an
# allowlisted state key. Malformed-force_always fallback is still covered by
# test_malformed_overlay_does_not_add below.


def test_malformed_overlay_does_not_add(env):
    _, state = env
    _write_overlay(state, f"id: {_ID}\nforce_always: 'yes'\n")  # non-bool
    assert _TOOL not in _offered("creative_writing"), "malformed force_always must not admit"


# native-presence-declared-v1 RETIRED test_overlay_read_is_per_turn,
# test_writer_applies_and_offers, test_writer_rejects_non_list_intents,
# test_writer_rejects_non_str_intent: all exercised the deleted added_intents
# overlay path (set_admission_overlay no longer accepts add_intents=, and
# added_intents is not an allowlisted state key). Per-turn freshness is still
# covered for force_always by the additive-only + force_always tests above.


# ── writer: write-strict validation ────────────────────────────────────────

def test_writer_rejects_force_always_false(env):
    with pytest.raises(ValueError):
        set_admission_overlay(_ID, force_always=False)


def test_writer_rejects_unknown_id(env):
    # native-presence-declared-v1: force_always is the sole overlay lever now.
    with pytest.raises(reg.CapabilityLoadError):
        set_admission_overlay("verb.does.not.exist", force_always=True)


# ── writer + reader round-trip (force_always-only shape) ───────────────────

def test_writer_force_always_applies_and_reads_back(env):
    # read_admission_overlay now returns {record_id: True} (force_always-only bool).
    assert set_admission_overlay(_ID, force_always=True) == "applied"
    assert read_admission_overlay()[_ID] is True
    assert _TOOL in _offered("creative_writing")  # force_always OR → offered everywhere


# ── cross-writer preservation ───────────────────────────────────────────────

def test_admission_write_preserves_prior_state_keys(env):
    _, state = env
    # a pre-existing Capability-state key (lifecycle) in the same file...
    _write_overlay(state, f"id: {_ID}\nlifecycle:\n  pinned: true\n  use_count: 9\n")
    set_admission_overlay(_ID, force_always=True)
    from grove.capability_registry import load_capabilities
    reset_caps_index_cache()
    cap = load_capabilities()[_ID]
    # ...must survive the admission write (one sovereignty seam, no clobber).
    assert cap.lifecycle.pinned is True and cap.lifecycle.use_count == 9
    assert read_admission_overlay()[_ID] is True


def test_state_snapshot_write_preserves_admission_keys(env):
    _, state = env
    set_admission_overlay(_ID, force_always=True)
    # A Capability-state write (lifecycle) routes through _write_state_snapshot,
    # whose full-snapshot dump must NOT erase the additive admission keys.
    reg.update_lifecycle_fields(_ID, use_count=5)
    assert read_admission_overlay()[_ID] is True
