"""Tests for grove.router — the Cognitive Router config loader."""

import logging
from pathlib import Path

import pytest

from grove.router import CognitiveRouter, RoutingDecision, TierConfig

VALID_CONFIG = """\
routing:
  schema_version: 1
  default_tier: T2
  tier_preferences:
    T0:
      handler: pattern_cache
      description: Deterministic recall.
      max_latency_ms: 50
    T1:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      description: Cheap cognition.
      max_tokens: 4096
    T2:
      provider: anthropic
      model: claude-sonnet-4-6
      description: Premium cognition.
      max_tokens: 8192
    T3:
      provider: anthropic
      model: claude-opus-4-6
      description: Apex cognition.
      max_tokens: 16384
  escalation:
    threshold: 0.6
    description: Confidence dial.
  telemetry:
    tier: T1
    description: Scoring tier.
"""


def _write(tmp_path: Path, text: str = VALID_CONFIG) -> Path:
    cfg = tmp_path / "routing.config.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_t1_get_tier_config_returns_sonnet(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    t2 = router.get_tier_config("T2")
    assert isinstance(t2, TierConfig)
    assert t2.tier == "T2"
    assert t2.provider == "anthropic"
    assert t2.model == "claude-sonnet-4-6"
    assert t2.max_tokens == 8192


def test_t2_t0_returns_pattern_cache_handler(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    t0 = router.get_tier_config("T0")
    assert t0.handler == "pattern_cache"
    assert t0.provider is None
    assert t0.model is None
    assert t0.max_latency_ms == 50


def test_t3_default_tier(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    assert router.get_default_tier() == "T2"


def test_t4_escalation_threshold(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    threshold = router.get_escalation_threshold()
    assert threshold == 0.6
    assert isinstance(threshold, float)


def test_t5_model_independence_swap(tmp_path):
    """Principle 7: swap T1 to a local provider by config edit alone."""
    swapped = VALID_CONFIG.replace(
        "      provider: anthropic\n      model: claude-haiku-4-5-20251001",
        "      provider: ollama\n      model: gemma-4",
    )
    router = CognitiveRouter(_write(tmp_path, swapped))
    t1 = router.get_tier_config("T1")
    assert t1.provider == "ollama"
    assert t1.model == "gemma-4"


def test_t6_reload_picks_up_valid_change(tmp_path):
    cfg = _write(tmp_path)
    router = CognitiveRouter(cfg)
    assert router.get_default_tier() == "T2"
    cfg.write_text(
        VALID_CONFIG.replace("default_tier: T2", "default_tier: T3"),
        encoding="utf-8",
    )
    router.reload()
    assert router.get_default_tier() == "T3"


def test_t7_reload_invalid_keeps_last_good(tmp_path, caplog):
    cfg = _write(tmp_path)
    router = CognitiveRouter(cfg)
    cfg.write_text("routing: {unclosed", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        router.reload()
    assert router.get_default_tier() == "T2"
    assert router.get_tier_config("T2").model == "claude-sonnet-4-6"
    assert any("reload failed" in r.message for r in caplog.records)


def test_t8_missing_config_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        CognitiveRouter(tmp_path / "does-not-exist.yaml")


def test_t9_unknown_tier_raises_keyerror(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    with pytest.raises(KeyError):
        router.get_tier_config("T9")


def test_t10_bad_schema_version_raises_valueerror(tmp_path):
    bad = VALID_CONFIG.replace("schema_version: 1", "schema_version: 2")
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


# ----- Sprint 11: route() ------------------------------------------------------

ZONE_OVERRIDE_CONFIG = VALID_CONFIG.replace(
    "  tier_preferences:",
    "  zone_overrides:\n    red: T3\n  tier_preferences:",
)


def test_route_default_returns_t2(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route()
    assert isinstance(d, RoutingDecision)
    assert d.tier == "T2"
    assert d.reason == "default"
    assert d.tier_config.model == "claude-sonnet-4-6"
    assert d.pattern_cache_hit is False


def test_route_operator_tier_overrides(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_tier="T3")
    assert d.tier == "T3"
    assert d.reason == "operator_override"
    assert d.tier_config.model == "claude-opus-4-6"


def test_route_operator_model_preference(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_model="claude-opus-4-6")
    assert d.tier == "T3"
    assert d.reason == "operator_model_preference"
    assert d.tier_config.model == "claude-opus-4-6"


def test_route_operator_model_untiered(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_model="some-unlisted-model")
    assert d.tier == "T2"  # runs in the default tier's slot
    assert d.reason == "operator_model_untiered"
    assert d.tier_config.model == "some-unlisted-model"
    assert d.tier_config.provider == "anthropic"  # default tier's provider


def test_route_operator_tier_beats_operator_model(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_tier="T1", operator_model="claude-opus-4-6")
    assert d.tier == "T1"
    assert d.reason == "operator_override"


def test_route_model_to_tier(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    assert router.model_to_tier("claude-sonnet-4-6") == "T2"
    assert router.model_to_tier("claude-opus-4-6") == "T3"
    assert router.model_to_tier("not-a-bound-model") is None


def test_route_zone_override(tmp_path):
    router = CognitiveRouter(_write(tmp_path, ZONE_OVERRIDE_CONFIG))
    d = router.route(zone="red")
    assert d.tier == "T3"
    assert d.reason == "zone_override"
    d_green = router.route(zone="green")
    assert d_green.tier == "T2"
    assert d_green.reason == "default"


def test_route_escalation_on_low_confidence(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(confidence=0.4)  # below threshold 0.6
    assert d.tier == "T3"  # T2 default escalated one step
    assert d.reason == "escalation"
    d_ok = router.route(confidence=0.9)  # above threshold
    assert d_ok.tier == "T2"
    assert d_ok.reason == "default"


def test_route_t0_pattern_cache_always_miss(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    for kwargs in ({}, {"operator_tier": "T3"}, {"confidence": 0.4}):
        assert router.route(**kwargs).pattern_cache_hit is False


def test_route_circuit_breaker_threshold_zero(tmp_path):
    cfg = VALID_CONFIG.replace("threshold: 0.6", "threshold: 0.0")
    router = CognitiveRouter(_write(tmp_path, cfg))
    d = router.route(confidence=0.01)  # would escalate if enabled
    assert d.tier == "T2"
    assert d.reason == "default"  # escalation disabled at threshold 0.0
