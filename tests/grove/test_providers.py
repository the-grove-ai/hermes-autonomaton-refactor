"""Tests for grove.providers — the Cognitive Router provider bridge."""

import logging

import pytest

import grove.router
from grove.classify import ClassificationResult
from grove.providers import resolve_tier_to_runtime, route_for_agent
from grove.router import TierConfig

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


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch):
    """Each test starts with no module router and a clean GROVE_* env."""
    grove.router._default_router = None
    monkeypatch.delenv("GROVE_TIER", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)
    yield
    grove.router._default_router = None


def _init_router(tmp_path):
    cfg = tmp_path / "routing.config.yaml"
    cfg.write_text(VALID_CONFIG, encoding="utf-8")
    grove.router.initialize(cfg)


def test_route_for_agent_returns_decision(tmp_path):
    _init_router(tmp_path)
    decision = route_for_agent()
    assert decision is not None
    assert decision.tier == "T2"
    assert decision.reason == "default"
    assert decision.tier_config.model == "claude-sonnet-4-6"


def test_route_for_agent_operator_model(tmp_path):
    _init_router(tmp_path)
    decision = route_for_agent(explicit_model="claude-opus-4-6")
    assert decision.tier == "T3"
    assert decision.reason == "operator_model_preference"


def test_route_for_agent_operator_tier(tmp_path):
    _init_router(tmp_path)
    decision = route_for_agent(explicit_tier="T1")
    assert decision.tier == "T1"
    assert decision.reason == "operator_override"


def test_route_for_agent_reads_grove_tier_env(tmp_path, monkeypatch):
    _init_router(tmp_path)
    monkeypatch.setenv("GROVE_TIER", "T3")
    decision = route_for_agent()
    assert decision.tier == "T3"
    assert decision.reason == "operator_override"


def test_route_for_agent_fallback_when_router_unavailable(monkeypatch):
    """Regression guard: no routing config -> None -> caller uses legacy chain."""

    def _raise(*_args, **_kwargs):
        raise FileNotFoundError("no routing config")

    monkeypatch.setattr(grove.router, "initialize", _raise)
    assert route_for_agent() is None


def test_resolve_tier_to_runtime(tmp_path, monkeypatch):
    _init_router(tmp_path)
    fake_runtime = {
        "provider": "anthropic",
        "api_key": "test-key",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
        "credential_pool": None,
    }
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(rp, "resolve_runtime_provider", lambda **_kwargs: fake_runtime)
    t2 = grove.router._default_router.get_tier_config("T2")
    runtime = resolve_tier_to_runtime(t2)
    assert runtime["model"] == "claude-sonnet-4-6"
    assert runtime["provider"] == "anthropic"
    assert runtime["api_key"] == "test-key"
    assert runtime["base_url"] == "https://api.anthropic.com"
    assert runtime["api_mode"] == "anthropic_messages"


def test_resolve_tier_to_runtime_rejects_handler_tier():
    t0 = TierConfig(
        tier="T0",
        handler="pattern_cache",
        provider=None,
        model=None,
        max_tokens=None,
        max_latency_ms=50,
        description="",
    )
    with pytest.raises(ValueError):
        resolve_tier_to_runtime(t0)


def test_route_for_agent_logs_routing_decision(tmp_path, caplog):
    _init_router(tmp_path)
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        route_for_agent()
    assert "routing_decision" in caplog.text


def test_route_for_agent_logs_ratchet_candidate(tmp_path, caplog):
    _init_router(tmp_path)
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        route_for_agent()  # default -> T2, a premium tier
    assert "ratchet_candidate" in caplog.text


# ----- Sprint 12: classification feeds routing --------------------------------


def test_route_for_agent_classification_escalates(tmp_path, monkeypatch):
    """A low-confidence classification escalates the default tier."""
    _init_router(tmp_path)
    low_conf = ClassificationResult(
        intent_class="analysis",
        pattern_hash="abc123",
        confidence=0.3,  # below the 0.6 escalation threshold
        register_class="technical",
        complexity_signal="complex",
    )
    monkeypatch.setattr("grove.classify.classify_for_routing", lambda _m: low_conf)
    decision = route_for_agent(message="analyze this codebase")
    assert decision.tier == "T3"  # T2 default escalated one step
    assert decision.reason == "escalation"


def test_route_for_agent_no_classification_default_routing(tmp_path, monkeypatch):
    """A failed/absent classification falls back to default routing."""
    _init_router(tmp_path)
    monkeypatch.setattr("grove.classify.classify_for_routing", lambda _m: None)
    decision = route_for_agent(message="something")
    assert decision.tier == "T2"
    assert decision.reason == "default"  # no confidence -> no escalation


def test_route_for_agent_logs_classification_fields(tmp_path, monkeypatch, caplog):
    """The routing_decision event is enriched with the classification fields."""
    _init_router(tmp_path)
    classified = ClassificationResult(
        intent_class="code_generation",
        pattern_hash="deadbeef0000",
        confidence=0.9,  # high — no escalation
        register_class="technical",
        complexity_signal="moderate",
    )
    monkeypatch.setattr("grove.classify.classify_for_routing", lambda _m: classified)
    with caplog.at_level(logging.INFO, logger="grove.telemetry"):
        route_for_agent(message="write a parser")
    assert "code_generation" in caplog.text  # intent_class enriches the event
    assert "deadbeef0000" in caplog.text  # pattern_hash enriches the event
