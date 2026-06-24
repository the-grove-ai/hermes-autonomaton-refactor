# tests/grove/test_capability_platform.py
import pytest
from grove.capability import (
    Capability,
    Zone,
    CapabilityKind,
    LifecycleState,
    Lifecycle,
    Telemetry,
    Trigger,
    TierRule,
    TierValidation,
    ValidationStrategy,
    Failure,
    CircuitBreaker,
)


def make_valid(**kwargs):
    defaults = dict(
        id="test_cap",
        kind=CapabilityKind.VERB,
        zone=Zone.GREEN,
        trigger=Trigger(intents=["test_intent"]),
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
        lifecycle=Lifecycle(state=LifecycleState.APPROVED),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=60)),
    )
    defaults.update(kwargs)
    return Capability(**defaults)


def test_platform_defaults_to_all():
    cap = make_valid()
    assert cap.platform == "all"


def test_platform_list_valid():
    cap = make_valid(platform=["telegram", "cli"])
    assert cap.platform == ["telegram", "cli"]


def test_platform_all_string_valid():
    cap = make_valid(platform="all")
    assert cap.platform == "all"


def test_platform_invalid_string_raises():
    with pytest.raises(ValueError, match="platform"):
        make_valid(platform="unknown_surface")


def test_platform_list_with_invalid_value_raises():
    with pytest.raises(ValueError, match="platform"):
        make_valid(platform=["telegram", "invalid_surface"])


def test_platform_empty_list_raises():
    with pytest.raises(ValueError, match="platform"):
        make_valid(platform=[])


def test_platform_non_list_non_string_raises():
    with pytest.raises(ValueError, match="platform"):
        make_valid(platform=42)
