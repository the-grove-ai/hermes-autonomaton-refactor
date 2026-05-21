"""Tests for grove.router — the Cognitive Router config loader."""

import logging
from pathlib import Path

import pytest

from grove.router import CognitiveRouter, TierConfig

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
