"""Tests for grove.classify — Sprint 28 Phase 2 two-envelope extension.

Covers the goal_alignment classification, the two-envelope JSON parser,
goals.md runtime integration, and the structural verification that the
routing-envelope fields produce identical routing decisions regardless
of which envelope shape (legacy flat vs new two-envelope) the response
arrives in.

All Anthropic calls are mocked. The two-envelope contract is the new
production shape; the legacy flat shape stays parseable defensively so
in-flight responses mid-transition don't crash routing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import grove.classify as classify
from grove.classify import (
    GOAL_ALIGNMENT_VALUES,
    ClassificationResult,
    _build_classification_system_prompt,
    _parse_classification,
    classify_for_routing,
)


_FAKE_RUNTIME = {
    "model": "claude-haiku-4-5-20251001",
    "provider": "anthropic",
    "api_key": "test-key",
    "base_url": None,
    "api_mode": "anthropic_messages",
}


def _two_envelope_body(
    *,
    intent_class: str = "code_generation",
    register_class: str = "technical",
    complexity_signal: str = "moderate",
    confidence: float = 0.85,
    goal_alignment: str = "direct",
) -> str:
    """The raw body the parser receives after the prefilled "{".

    The classifier prefills assistant content with "{" so the response
    starts the JSON object; the parser re-joins the prefill before
    calling json.loads. Test bodies follow the same shape.
    """
    obj = {
        "routing_envelope": {
            "intent_class": intent_class,
            "register_class": register_class,
            "complexity_signal": complexity_signal,
            "confidence": confidence,
        },
        "learning_envelope": {
            "goal_alignment": goal_alignment,
        },
    }
    serialized = json.dumps(obj)
    # The fake omits the leading "{" because the real response continues
    # from the prefill.
    return serialized[1:]


class _FakeUsage:
    def __init__(self, input_tokens=120, output_tokens=40):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _install_fake_anthropic(monkeypatch, *, text=None, usage=None):
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
            return _Response()

    class _Anthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr("anthropic.Anthropic", _Anthropic)


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch):
    monkeypatch.setattr(
        classify, "_telemetry_tier_runtime", lambda: dict(_FAKE_RUNTIME)
    )
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False
    yield
    classify._cumulative_cost_usd = 0.0
    classify._budget_warned = False


@pytest.fixture
def isolated_goals_path(monkeypatch, tmp_path: Path):
    """Redirect _goals_path() at a tmp file so tests control goals content."""
    p = tmp_path / "goals.md"
    monkeypatch.setattr(classify, "_goals_path", lambda: p)
    return p


# ── ClassificationResult schema extension ─────────────────────────────────


class TestClassificationResultExtension:
    def test_goal_alignment_defaults_none(self):
        # Backward compat — constructors that don't pass goal_alignment
        # still produce a valid result.
        r = ClassificationResult(
            intent_class="conversation",
            pattern_hash="abc",
            confidence=0.9,
            register_class="casual",
            complexity_signal="simple",
        )
        assert r.goal_alignment is None

    def test_goal_alignment_accepts_explicit_value(self):
        r = ClassificationResult(
            intent_class="planning",
            pattern_hash="abc",
            confidence=0.7,
            register_class="strategic",
            complexity_signal="complex",
            goal_alignment="direct",
        )
        assert r.goal_alignment == "direct"


# ── _build_classification_system_prompt ───────────────────────────────────


class TestSystemPromptBuilder:
    def test_includes_two_envelope_structure(self):
        prompt = _build_classification_system_prompt("Goal A\nGoal B")
        assert "routing_envelope" in prompt
        assert "learning_envelope" in prompt
        # The routing fields stay together so the model treats them as
        # one envelope; the learning field stays separate.
        routing_pos = prompt.index("routing_envelope")
        learning_pos = prompt.index("learning_envelope")
        assert routing_pos < learning_pos

    def test_embeds_goals_content_when_present(self):
        prompt = _build_classification_system_prompt("Ship v0.1\nValidate UX")
        assert "Ship v0.1" in prompt
        assert "Validate UX" in prompt

    def test_names_empty_case_explicitly(self):
        # When goals.md is missing/empty the prompt explicitly tells the
        # model what to return — no hallucinated goals, no missing
        # goal_alignment field.
        prompt = _build_classification_system_prompt("")
        assert "no_goals_set" in prompt
        assert "no goals set" in prompt.lower()

    def test_lists_all_goal_alignment_values(self):
        prompt = _build_classification_system_prompt("anything")
        for value in GOAL_ALIGNMENT_VALUES:
            assert value in prompt


# ── _read_goals_content (graceful tier) ───────────────────────────────────


class TestReadGoalsContent:
    def test_returns_file_contents_when_present(self, isolated_goals_path):
        isolated_goals_path.write_text("Ship v0.1\n", encoding="utf-8")
        assert classify._read_goals_content() == "Ship v0.1\n"

    def test_returns_empty_string_when_missing(self, isolated_goals_path):
        # File doesn't exist — graceful tier per the goals.md template
        # comment "a missing goals.md is fine".
        assert not isolated_goals_path.exists()
        assert classify._read_goals_content() == ""

    def test_returns_empty_string_when_file_unreadable(
        self, monkeypatch, isolated_goals_path, caplog,
    ):
        # An OSError other than FileNotFoundError degrades the same
        # way — log debug, return empty, classification continues.
        isolated_goals_path.write_text("real", encoding="utf-8")
        def _broken_read(*a, **kw):
            raise PermissionError("no read access")
        monkeypatch.setattr(Path, "read_text", _broken_read)
        with caplog.at_level(logging.DEBUG, logger="grove.classify"):
            assert classify._read_goals_content() == ""


# ── _parse_classification — two-envelope ──────────────────────────────────


class TestParseClassificationTwoEnvelope:
    def test_parses_two_envelope_response(self):
        body = _two_envelope_body(
            intent_class="debugging",
            confidence=0.7,
            goal_alignment="indirect",
        )
        parsed = _parse_classification("{" + body)
        assert parsed["intent_class"] == "debugging"
        assert parsed["confidence"] == 0.7
        assert parsed["goal_alignment"] == "indirect"

    @pytest.mark.parametrize("value", list(GOAL_ALIGNMENT_VALUES))
    def test_accepts_each_valid_goal_alignment(self, value: str):
        body = _two_envelope_body(goal_alignment=value)
        parsed = _parse_classification("{" + body)
        assert parsed["goal_alignment"] == value

    def test_unknown_goal_alignment_drops_to_none(self, caplog):
        # Defensive: a value outside the closed set is logged at debug
        # and dropped to None rather than failing the whole
        # classification — routing must not depend on the learning
        # envelope's correctness.
        body = _two_envelope_body(goal_alignment="ratified")
        with caplog.at_level(logging.DEBUG, logger="grove.classify"):
            parsed = _parse_classification("{" + body)
        assert parsed["goal_alignment"] is None
        # Routing fields still valid.
        assert parsed["intent_class"] == "code_generation"
        assert parsed["confidence"] == 0.85

    def test_missing_learning_envelope_yields_none(self):
        body = json.dumps({
            "routing_envelope": {
                "intent_class": "analysis",
                "register_class": "strategic",
                "complexity_signal": "complex",
                "confidence": 0.6,
            },
        })[1:]
        parsed = _parse_classification("{" + body)
        assert parsed["goal_alignment"] is None
        assert parsed["intent_class"] == "analysis"

    def test_missing_goal_alignment_field_yields_none(self):
        body = json.dumps({
            "routing_envelope": {
                "intent_class": "analysis",
                "register_class": "strategic",
                "complexity_signal": "complex",
                "confidence": 0.6,
            },
            "learning_envelope": {},
        })[1:]
        parsed = _parse_classification("{" + body)
        assert parsed["goal_alignment"] is None


# ── _parse_classification — legacy flat (backward compat) ─────────────────


class TestParseClassificationLegacyFlat:
    """The legacy flat-shape response (Sprint 12 contract) must continue
    parsing. Defensive against in-flight responses mid-prompt-transition
    and to keep the pre-Phase-2 test bodies passing without rewrite."""

    def test_parses_flat_response_routing_only(self):
        body = json.dumps({
            "intent_class": "code_generation",
            "register_class": "technical",
            "complexity_signal": "moderate",
            "confidence": 0.9,
        })[1:]
        parsed = _parse_classification("{" + body)
        assert parsed["intent_class"] == "code_generation"
        assert parsed["confidence"] == 0.9
        # No learning envelope in the flat shape → goal_alignment None.
        assert parsed["goal_alignment"] is None


# ── classify_for_routing — end-to-end with goal_alignment ─────────────────


class TestClassifyForRoutingWithGoals:
    def test_populates_goal_alignment_on_result(
        self, monkeypatch, isolated_goals_path,
    ):
        isolated_goals_path.write_text(
            "Ship grove-autonomaton v0.1\n", encoding="utf-8",
        )
        _install_fake_anthropic(
            monkeypatch, text=_two_envelope_body(goal_alignment="direct"),
        )
        result = classify_for_routing("Let's wire the Dispatcher.")
        assert result is not None
        assert result.goal_alignment == "direct"

    def test_no_goals_set_when_goals_missing(
        self, monkeypatch, isolated_goals_path,
    ):
        # File doesn't exist. The model (mocked to return "no_goals_set")
        # is what we'd expect a real Haiku to return given the empty-
        # case prompt — the test confirms the value plumbs through.
        assert not isolated_goals_path.exists()
        _install_fake_anthropic(
            monkeypatch,
            text=_two_envelope_body(goal_alignment="no_goals_set"),
        )
        result = classify_for_routing("anything")
        assert result.goal_alignment == "no_goals_set"


# ── Structural verification: routing fields identical across envelope shapes


class TestRoutingFieldsConsistency:
    """The operator's GATE-A guidance: monitor routing telemetry for
    confidence shifts during Phase 2. The live monitoring is operational;
    the structural verification here proves the parser produces
    identical routing-field results whether the response arrives in the
    new two-envelope shape or the legacy flat shape.
    """

    def test_routing_fields_identical_across_shapes(self):
        flat = json.dumps({
            "intent_class": "planning",
            "register_class": "strategic",
            "complexity_signal": "complex",
            "confidence": 0.72,
        })[1:]
        two_envelope = _two_envelope_body(
            intent_class="planning",
            register_class="strategic",
            complexity_signal="complex",
            confidence=0.72,
            goal_alignment="direct",
        )
        flat_parsed = _parse_classification("{" + flat)
        env_parsed = _parse_classification("{" + two_envelope)
        for key in (
            "intent_class", "register_class",
            "complexity_signal", "confidence",
        ):
            assert flat_parsed[key] == env_parsed[key], (
                f"routing field {key!r} drifted between shapes: "
                f"flat={flat_parsed[key]!r} env={env_parsed[key]!r}"
            )
