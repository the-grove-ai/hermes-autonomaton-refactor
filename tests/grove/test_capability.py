"""Tests for the Capability primitive (GRV-009 E1, capability-record-v1)."""

import pytest
import yaml

from grove.capability import (
    Capability,
    CapabilityKind,
    CircuitBreaker,
    Context,
    Disclosure,
    DockComposition,
    Failure,
    FailureFallback,
    IllegalTransitionError,
    Lifecycle,
    LifecycleState,
    Lineage,
    Provenance,
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    ValidationStrategy,
    Zone,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_valid(**overrides) -> Capability:
    """A fully valid Capability; collection fields left to their factory
    defaults so isolation can be exercised. Override any field by keyword."""
    base = dict(
        id="cap.research.web",
        kind=CapabilityKind.VERB,
        zone=Zone.GREEN,
        trigger=Trigger(intents=["research"]),
        tier_rule=TierRule(
            eligible=[1, 2],
            preferred=1,
            validation=TierValidation(
                strategy=ValidationStrategy.SHADOW_COMPARE,
                confidence_threshold=0.9,
                shadow_window=5,
            ),
        ),
        telemetry=Telemetry(feed="intent_feed"),
        lifecycle=Lifecycle(state=LifecycleState.PROPOSED),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=60)),
    )
    base.update(overrides)
    return Capability(**base)


# ── Happy path ───────────────────────────────────────────────────────────────


def test_happy_path_construction():
    cap = make_valid()
    assert cap.id == "cap.research.web"
    assert cap.kind is CapabilityKind.VERB
    assert cap.zone is Zone.GREEN
    assert cap.lifecycle.state is LifecycleState.PROPOSED
    assert cap.telemetry.feed == "intent_feed"
    assert cap.trigger.intents == ["research"]
    # safe empty defaults
    assert cap.telemetry.track == []
    assert cap.lineage.decision_log == []
    assert cap.context.disclosure is Disclosure.PULL
    assert cap.context.dock_composition is DockComposition.NONE
    assert cap.failure.fallback is FailureFallback.HALT_AND_SURFACE
    assert cap.lifecycle.provenance is Provenance.OPERATOR_AUTHORED


# ── One test per validation rule (raises; message names the field) ───────────


def test_validate_id_non_empty():
    with pytest.raises(ValueError, match="id"):
        make_valid(id="")


def test_validate_kind_enum_member():
    with pytest.raises(ValueError, match="kind"):
        make_valid(kind="verb")  # raw string, not the enum


def test_validate_zone_enum_member():
    with pytest.raises(ValueError, match="zone"):
        make_valid(zone="green")  # raw string, not the enum


def test_validate_lifecycle_state_enum_member():
    with pytest.raises(ValueError, match="lifecycle.state"):
        make_valid(lifecycle=Lifecycle(state="proposed"))  # raw string


def test_validate_trigger_requires_strict_trigger():
    with pytest.raises(ValueError, match="trigger"):
        # dock_affinity alone does not count as a strict trigger
        make_valid(trigger=Trigger(dock_affinity=["dock.research"]))


def test_validate_telemetry_feed_non_empty():
    with pytest.raises(ValueError, match="telemetry.feed"):
        make_valid(telemetry=Telemetry(feed=""))


def test_validate_tier_eligible_non_empty():
    with pytest.raises(ValueError, match="tier_rule.eligible"):
        make_valid(
            tier_rule=TierRule(
                eligible=[],
                preferred=1,
                validation=TierValidation(confidence_threshold=0.9, shadow_window=5),
            )
        )


def test_validate_tier_eligible_subset():
    with pytest.raises(ValueError, match="subset"):
        make_valid(
            tier_rule=TierRule(
                eligible=[4],
                preferred=4,
                validation=TierValidation(confidence_threshold=0.9, shadow_window=5),
            )
        )


def test_validate_tier_preferred_in_eligible():
    with pytest.raises(ValueError, match="preferred"):
        make_valid(
            tier_rule=TierRule(
                eligible=[1, 2],
                preferred=3,
                validation=TierValidation(confidence_threshold=0.9, shadow_window=5),
            )
        )


def test_validate_confidence_threshold_range():
    with pytest.raises(ValueError, match="confidence_threshold"):
        make_valid(
            tier_rule=TierRule(
                eligible=[1, 2],
                preferred=1,
                validation=TierValidation(confidence_threshold=1.5, shadow_window=5),
            )
        )


def test_validate_shadow_window_positive():
    with pytest.raises(ValueError, match="shadow_window"):
        make_valid(
            tier_rule=TierRule(
                eligible=[1, 2],
                preferred=1,
                validation=TierValidation(confidence_threshold=0.9, shadow_window=0),
            )
        )


def test_validate_circuit_breaker_threshold_positive():
    with pytest.raises(ValueError, match="threshold"):
        make_valid(
            failure=Failure(circuit_breaker=CircuitBreaker(threshold=0, window_seconds=60))
        )


def test_validate_circuit_breaker_window_positive():
    with pytest.raises(ValueError, match="window_seconds"):
        make_valid(
            failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=0))
        )


# ── Mutable-defaults isolation ───────────────────────────────────────────────


def test_mutable_defaults_do_not_share_state():
    a = make_valid()
    b = make_valid()

    # list defaults
    a.telemetry.track.append("latency")
    a.lineage.source_patterns.append("pat.001")
    a.failure.diagnostic_context.append("ctx")
    # dict default
    a.tier_rule.promotion_criteria["hits"] = 10

    assert b.telemetry.track == []
    assert b.lineage.source_patterns == []
    assert b.failure.diagnostic_context == []
    assert b.tier_rule.promotion_criteria == {}
    # and the objects are genuinely distinct instances
    assert a.telemetry.track is not b.telemetry.track
    assert a.tier_rule.promotion_criteria is not b.tier_rule.promotion_criteria


# ── Lifecycle state machine ──────────────────────────────────────────────────


def test_every_legal_transition_and_decision_log_append():
    cap = make_valid()  # state proposed
    chain = [
        (LifecycleState.QUARANTINE, "proposed", "quarantine"),
        (LifecycleState.APPROVED, "quarantine", "approved"),
        (LifecycleState.ACTIVE, "approved", "active"),
        (LifecycleState.REFINED, "active", "refined"),
        (LifecycleState.ACTIVE, "refined", "active"),
        (LifecycleState.DEPRECATED, "active", "deprecated"),
    ]
    for i, (to_state, frm, to) in enumerate(chain, start=1):
        rec = cap.transition(to_state, actor="operator", reason=f"step {i}")
        assert cap.lifecycle.state is to_state
        assert len(cap.lineage.decision_log) == i
        assert rec.from_state == frm
        assert rec.to_state == to
        assert rec.actor == "operator"
        assert rec.timestamp  # ISO-8601 stamp present

    assert cap.lifecycle.state is LifecycleState.DEPRECATED
    assert len(cap.lineage.decision_log) == 6


def test_illegal_transition_skip_raises():
    cap = make_valid()  # proposed
    with pytest.raises(IllegalTransitionError, match="proposed -> active"):
        cap.transition(LifecycleState.ACTIVE, actor="operator", reason="skip")


def test_illegal_transition_backwards_raises():
    cap = make_valid(lifecycle=Lifecycle(state=LifecycleState.ACTIVE))
    with pytest.raises(IllegalTransitionError):
        cap.transition(LifecycleState.PROPOSED, actor="operator", reason="rewind")


def test_no_transition_out_of_deprecated():
    cap = make_valid(lifecycle=Lifecycle(state=LifecycleState.DEPRECATED))
    for target in LifecycleState:
        with pytest.raises(IllegalTransitionError):
            cap.transition(target, actor="operator", reason="terminal")


# ── YAML round-trip ──────────────────────────────────────────────────────────


def test_yaml_round_trip_equality_with_decision_log():
    cap = make_valid(
        trigger=Trigger(intents=["research"], keywords=["lookup"]),
        context=Context(
            disclosure=Disclosure.EAGER,
            payload="tool_schema:web_search",
            dock_composition=DockComposition.GOAL_CONTEXT,
        ),
        lineage=Lineage(source_patterns=["pat.a", "pat.b"], parent_id="cap.parent"),
    )
    # populate decision_log via real transitions (nested + enum casting proof)
    cap.transition(LifecycleState.QUARANTINE, actor="operator", reason="review")
    cap.transition(
        LifecycleState.APPROVED,
        actor="operator",
        reason="approved",
        evidence=["shadow_pass=12"],
    )
    assert len(cap.lineage.decision_log) == 2

    restored = Capability.from_yaml(cap.to_yaml())
    assert restored == cap
    # spot-check nested enum + record fidelity
    assert restored.context.disclosure is Disclosure.EAGER
    assert restored.lineage.decision_log[1].evidence == ["shadow_pass=12"]
    assert restored.lifecycle.state is LifecycleState.APPROVED


def test_from_yaml_missing_governance_field_fails_loud():
    cap = make_valid()
    d = cap.to_dict()
    del d["id"]  # drop a governance-bearing field
    text = yaml.safe_dump(d)
    with pytest.raises(TypeError):
        Capability.from_yaml(text)
