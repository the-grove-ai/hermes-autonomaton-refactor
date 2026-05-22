"""Tests for grove.classify — the T-telemetry classifier (Sprint 12).

All Anthropic calls are mocked — no real Haiku calls.
"""

import logging

import pytest

import grove.classify as classify
from grove.classify import (
    ClassificationResult,
    classify_for_routing,
)

_FAKE_RUNTIME = {
    "model": "claude-haiku-4-5-20251001",
    "provider": "anthropic",
    "api_key": "test-key",
    "base_url": None,
    "api_mode": "anthropic_messages",
}

# What the fake Anthropic returns: the JSON object minus the prefilled "{".
_VALID_BODY = (
    '"intent_class": "code_generation", "register_class": "technical", '
    '"complexity_signal": "moderate", "confidence": 0.85}'
)


class _FakeUsage:
    def __init__(self, input_tokens=120, output_tokens=40):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _install_fake_anthropic(monkeypatch, *, text=None, usage=None, error=None):
    """Patch anthropic.Anthropic so _call_classifier hits a fake client."""

    class _Block:
        def __init__(self, value):
            self.text = value

    class _Response:
        def __init__(self):
            self.content = [_Block(text)]
            self.usage = usage or _FakeUsage()

    class _Messages:
        def create(self, **kwargs):
            if error is not None:
                raise error
            return _Response()

    class _Anthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("anthropic.Anthropic", _Anthropic)


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch):
    """Stub the telemetry-tier resolution; reset the cost counter."""
    monkeypatch.setattr(
        classify, "_telemetry_tier_runtime", lambda: dict(_FAKE_RUNTIME)
    )
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False
    yield
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False


def test_classify_returns_result(monkeypatch):
    _install_fake_anthropic(monkeypatch, text=_VALID_BODY)
    result = classify_for_routing("Write a function to parse YAML.")
    assert isinstance(result, ClassificationResult)
    assert result.intent_class == "code_generation"
    assert result.register_class == "technical"
    assert result.complexity_signal == "moderate"
    assert result.confidence == 0.85
    assert len(result.pattern_hash) == 64  # SHA-256 hex digest


def test_classify_none_on_api_error(monkeypatch, caplog):
    _install_fake_anthropic(monkeypatch, error=RuntimeError("api down"))
    with caplog.at_level(logging.ERROR, logger="grove.classify"):
        result = classify_for_routing("anything")
    assert result is None
    assert "classification failed" in caplog.text


def test_classify_none_on_malformed_json(monkeypatch, caplog):
    _install_fake_anthropic(monkeypatch, text="not json at all")
    with caplog.at_level(logging.ERROR, logger="grove.classify"):
        result = classify_for_routing("anything")
    assert result is None


def test_classify_empty_message_returns_none(monkeypatch):
    _install_fake_anthropic(monkeypatch, text=_VALID_BODY)
    assert classify_for_routing("") is None
    assert classify_for_routing("   ") is None
    assert classify_for_routing(None) is None


def test_classify_tolerates_trailing_prose(monkeypatch):
    _install_fake_anthropic(monkeypatch, text=_VALID_BODY + "\n\nHope that helps!")
    result = classify_for_routing("Write code")
    assert result is not None
    assert result.intent_class == "code_generation"


def test_pattern_hash_deterministic(monkeypatch):
    _install_fake_anthropic(monkeypatch, text=_VALID_BODY)
    r1 = classify_for_routing("Refactor the parser")
    r2 = classify_for_routing("Refactor the parser")
    assert r1.pattern_hash == r2.pattern_hash


def test_confidence_clamped_to_range(monkeypatch):
    _install_fake_anthropic(
        monkeypatch,
        text='"intent_class": "analysis", "register_class": "technical", '
        '"complexity_signal": "simple", "confidence": 1.7}',
    )
    result = classify_for_routing("hi")
    assert result.confidence == 1.0


def test_cost_counter_increments(monkeypatch):
    _install_fake_anthropic(
        monkeypatch, text=_VALID_BODY, usage=_FakeUsage(1000, 200)
    )
    classify_for_routing("classify me")
    # 1000/1e6 * $1 + 200/1e6 * $5 = 0.001 + 0.001
    assert classify._cumulative_cost_usd == pytest.approx(0.002)


def test_cost_warns_past_budget(monkeypatch, caplog):
    monkeypatch.setenv("GROVE_TELEMETRY_BUDGET_WARN", "0.001")
    _install_fake_anthropic(
        monkeypatch, text=_VALID_BODY, usage=_FakeUsage(1000, 1000)
    )
    with caplog.at_level(logging.WARNING, logger="grove.classify"):
        classify_for_routing("expensive")
    assert "spend has passed" in caplog.text
