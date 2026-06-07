"""Tests for grove.classify — the T-telemetry classifier (Sprint 12).

All provider calls are mocked — no live T-telemetry calls.
"""

import logging

import pytest

import grove.classify as classify
from grove.classify import (
    ClassificationResult,
    classify_for_routing,
)
from grove.router import TierConfig

_FAKE_RUNTIME = {
    "model": "test-classifier-model",
    "provider": "anthropic",
    "api_key": "test-key",
    "base_url": None,
    "api_mode": "anthropic_messages",
}

_FAKE_TIER_CONFIG = TierConfig(
    tier="T1",
    handler=None,
    provider="anthropic",
    model="test-classifier-model",
    max_tokens=4096,
    max_latency_ms=None,
    description="",
    cost_per_mtok_input=1.0,
    cost_per_mtok_output=5.0,
)

# Sprint 65: the classifier forces the classify_intent tool, so the fake
# returns a tool_use block whose ``input`` is the two-envelope dict — the
# same shape the live API delivers under tool_choice.
_VALID_INPUT = {
    "routing_envelope": {
        "intent_class": "code_generation",
        "register_class": "technical",
        "complexity_signal": "moderate",
        "confidence": 0.85,
    },
    "learning_envelope": {
        "goal_alignment": "direct",
        "is_correction": False,
    },
}

# A captured record of the kwargs the fake client.messages.create received,
# so tests can assert the wire shape (tools, tool_choice, no prefill).
_LAST_CREATE_KWARGS: dict = {}


class _FakeUsage:
    def __init__(self, input_tokens=120, output_tokens=40):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeToolUseBlock:
    def __init__(self, name, value):
        self.type = "tool_use"
        self.name = name
        self.input = value


class _FakeTextBlock:
    def __init__(self, value):
        self.type = "text"
        self.text = value


def _install_fake_anthropic(
    monkeypatch, *, tool_input=None, blocks=None, usage=None, error=None
):
    """Patch anthropic.Anthropic so _call_classifier hits a fake client.

    ``tool_input`` (the common case) makes the fake return one
    ``classify_intent`` tool_use block carrying that dict. ``blocks`` lets
    a test supply an arbitrary content list (e.g. a text-only response, to
    exercise the no-tool_use fallthrough). The kwargs passed to
    ``create`` are recorded in ``_LAST_CREATE_KWARGS``.
    """
    if blocks is None:
        blocks = [_FakeToolUseBlock("classify_intent", tool_input)]

    class _Response:
        def __init__(self):
            self.content = blocks
            self.usage = usage or _FakeUsage()

    class _Messages:
        def create(self, **kwargs):
            _LAST_CREATE_KWARGS.clear()
            _LAST_CREATE_KWARGS.update(kwargs)
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
        classify, "_telemetry_tier_runtime",
        lambda: (dict(_FAKE_RUNTIME), _FAKE_TIER_CONFIG),
    )
    classify._missing_cost_warned = False
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False
    yield
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False


def test_classify_returns_result(monkeypatch):
    _install_fake_anthropic(monkeypatch, tool_input=_VALID_INPUT)
    result = classify_for_routing("Write a function to parse YAML.")
    assert isinstance(result, ClassificationResult)
    assert result.intent_class == "code_generation"
    assert result.register_class == "technical"
    assert result.complexity_signal == "moderate"
    assert result.confidence == 0.85
    assert len(result.pattern_hash) == 64  # SHA-256 hex digest


def test_call_classifier_forces_the_tool(monkeypatch):
    """Sprint 65: the wire call sends the tool + a forcing tool_choice and
    carries NO assistant prefill — the schema is enforced by the API
    contract, not by a prompt instruction."""
    _install_fake_anthropic(monkeypatch, tool_input=_VALID_INPUT)
    classify_for_routing("Write code")
    kwargs = _LAST_CREATE_KWARGS
    assert kwargs["tools"] == [classify._CLASSIFY_TOOL]
    assert kwargs["tool_choice"] == {
        "type": "tool",
        "name": "classify_intent",
    }
    # No prefill: a single user turn, no trailing assistant message.
    assert kwargs["messages"] == [{"role": "user", "content": "Write code"}]


def test_classify_populates_intent_not_none(monkeypatch):
    """The bug this sprint fixes: a tool_use response yields a populated
    intent_class — the {follow_ups: [...]} freeform failure is now
    structurally impossible."""
    _install_fake_anthropic(monkeypatch, tool_input=_VALID_INPUT)
    result = classify_for_routing("Write a parser")
    assert result is not None
    assert result.intent_class is not None
    assert result.intent_class in classify.INTENT_CLASSES


def test_classify_learning_envelope_round_trips(monkeypatch):
    """is_correction and goal_alignment flow from the tool input through
    to the ClassificationResult (Sprint 38 / Sprint 28 consumers)."""
    tool_input = {
        "routing_envelope": {
            "intent_class": "conversation",
            "register_class": "casual",
            "complexity_signal": "simple",
            "confidence": 0.9,
        },
        "learning_envelope": {
            "goal_alignment": "orthogonal",
            "is_correction": True,
        },
    }
    _install_fake_anthropic(monkeypatch, tool_input=tool_input)
    result = classify_for_routing("no, that's not what I meant")
    assert result.is_correction is True
    assert result.goal_alignment == "orthogonal"


def test_classify_none_on_api_error(monkeypatch, caplog):
    _install_fake_anthropic(monkeypatch, error=RuntimeError("api down"))
    with caplog.at_level(logging.ERROR, logger="grove.classify"):
        result = classify_for_routing("anything")
    assert result is None
    assert "classification failed" in caplog.text


def test_classify_none_when_no_tool_use_block(monkeypatch, caplog):
    """If the model somehow returns no classify_intent tool_use block,
    _call_classifier raises loudly and classify_for_routing degrades to
    None (the commanded Sprint 12 D4 fall-through). This should never fire
    under tool_choice forcing, but the diagnostic must not be silent."""
    _install_fake_anthropic(
        monkeypatch, blocks=[_FakeTextBlock("here are some follow-ups")]
    )
    with caplog.at_level(logging.ERROR, logger="grove.classify"):
        result = classify_for_routing("anything")
    assert result is None
    assert "classification failed" in caplog.text
    assert "no classify_intent tool_use block" in caplog.text


def test_classify_empty_message_returns_none(monkeypatch):
    _install_fake_anthropic(monkeypatch, tool_input=_VALID_INPUT)
    assert classify_for_routing("") is None
    assert classify_for_routing("   ") is None
    assert classify_for_routing(None) is None


def test_pattern_hash_deterministic(monkeypatch):
    _install_fake_anthropic(monkeypatch, tool_input=_VALID_INPUT)
    r1 = classify_for_routing("Refactor the parser")
    r2 = classify_for_routing("Refactor the parser")
    assert r1.pattern_hash == r2.pattern_hash


def test_confidence_clamped_to_range(monkeypatch):
    _install_fake_anthropic(
        monkeypatch,
        tool_input={
            "routing_envelope": {
                "intent_class": "analysis",
                "register_class": "technical",
                "complexity_signal": "simple",
                "confidence": 1.7,
            },
            "learning_envelope": {"is_correction": False},
        },
    )
    result = classify_for_routing("hi")
    assert result.confidence == 1.0


def test_cost_counter_increments(monkeypatch):
    _install_fake_anthropic(
        monkeypatch, tool_input=_VALID_INPUT, usage=_FakeUsage(1000, 200)
    )
    classify_for_routing("classify me")
    # 1000/1e6 * $1 + 200/1e6 * $5 = 0.001 + 0.001
    assert classify._cumulative_cost_usd == pytest.approx(0.002)


def test_cost_warns_past_budget(monkeypatch, caplog):
    monkeypatch.setenv("GROVE_TELEMETRY_BUDGET_WARN", "0.001")
    _install_fake_anthropic(
        monkeypatch, tool_input=_VALID_INPUT, usage=_FakeUsage(1000, 1000)
    )
    with caplog.at_level(logging.WARNING, logger="grove.classify"):
        classify_for_routing("expensive")
    assert "spend has passed" in caplog.text


def test_missing_cost_constants_skips_accumulation(monkeypatch, caplog):
    """When the tier_config carries no cost_per_mtok_input/output,
    the spend tracker emits one loud warning and skips accumulation.
    Classification continues; cumulative cost stays at zero."""
    tier_config_no_cost = TierConfig(
        tier="T1",
        handler=None,
        provider="anthropic",
        model="test-classifier-model",
        max_tokens=4096,
        max_latency_ms=None,
        description="",
        cost_per_mtok_input=None,
        cost_per_mtok_output=None,
    )
    monkeypatch.setattr(
        classify, "_telemetry_tier_runtime",
        lambda: (dict(_FAKE_RUNTIME), tier_config_no_cost),
    )
    _install_fake_anthropic(
        monkeypatch, tool_input=_VALID_INPUT, usage=_FakeUsage(1000, 200),
    )
    with caplog.at_level(logging.WARNING, logger="grove.classify"):
        result = classify_for_routing("classify me")
    assert result is not None
    assert classify._cumulative_cost_usd == 0.0
    assert "declares no cost_per_mtok_input/output" in caplog.text


def test_missing_cost_warns_only_once_per_process(monkeypatch, caplog):
    """The fail-loud warning is once-per-process, not once-per-call."""
    tier_config_no_cost = TierConfig(
        tier="T1",
        handler=None,
        provider="anthropic",
        model="test-classifier-model",
        max_tokens=4096,
        max_latency_ms=None,
        description="",
        cost_per_mtok_input=None,
        cost_per_mtok_output=None,
    )
    monkeypatch.setattr(
        classify, "_telemetry_tier_runtime",
        lambda: (dict(_FAKE_RUNTIME), tier_config_no_cost),
    )
    _install_fake_anthropic(
        monkeypatch, tool_input=_VALID_INPUT, usage=_FakeUsage(1000, 200),
    )
    with caplog.at_level(logging.WARNING, logger="grove.classify"):
        classify_for_routing("call one")
        classify_for_routing("call two")
        classify_for_routing("call three")
    assert caplog.text.count("declares no cost_per_mtok_input/output") == 1


def test_cumulative_cost_usd_accessor():
    """cumulative_cost_usd() exposes the module's running T-telemetry spend
    so the CLI session summary can report classification cost."""
    from grove.classify import cumulative_cost_usd

    original = classify._cumulative_cost_usd
    try:
        classify._cumulative_cost_usd = 0.0
        assert cumulative_cost_usd() == 0.0
        classify._cumulative_cost_usd = 0.025
        assert cumulative_cost_usd() == 0.025
    finally:
        classify._cumulative_cost_usd = original
