"""tier-ux-composition-v1 — the Cognitive Router made operator-visible.

Covers the CLI tier-UX surface: the model-name map, the response-label
tier suffix, the transition line, the per-turn cost line, /why and /tier,
and the session summary. HermesCLI.__init__ is far too heavy for a unit
test, so each test builds a bare instance with object.__new__ and sets
only the tier-UX state the method under test reads.
"""

from __future__ import annotations

import cli
from grove.classify import ClassificationResult
from grove.router import RoutingDecision, TierConfig


def _tier_config(tier="T2", model="claude-sonnet-4-6", provider="anthropic"):
    return TierConfig(
        tier=tier, handler=None, provider=provider, model=model,
        max_tokens=8192, max_latency_ms=None, description="",
    )


def _decision(tier="T2", model="claude-sonnet-4-6", provider="anthropic",
              reason="default", confidence=0.9):
    return RoutingDecision(
        tier=tier,
        tier_config=_tier_config(tier, model, provider),
        reason=reason,
        confidence=confidence,
        pattern_cache_hit=False,
    )


def _cli():
    """A bare HermesCLI with only the tier-UX state set."""
    obj = object.__new__(cli.HermesCLI)
    obj._current_tier = None
    obj._previous_tier = None
    obj._last_routing_decision = None
    obj._last_classification = None
    obj._tier_override = None
    obj._last_turn_input_tokens = 0
    obj._last_turn_output_tokens = 0
    obj._session_tier_stats = {}
    return obj


def _capture_cprint(monkeypatch):
    """Patch cli._cprint and return the list it appends each line to."""
    lines: list[str] = []
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: lines.append(a[0] if a else ""))
    return lines


# --- D6: model display names -------------------------------------------------

def test_model_display_name_known():
    assert cli._model_display_name("claude-sonnet-4-6") == "Sonnet"
    assert cli._model_display_name("claude-opus-4-6") == "Opus"
    assert cli._model_display_name("gemma4") == "Gemma 4"


def test_model_display_name_unknown_passes_through():
    assert cli._model_display_name("some-future-model") == "some-future-model"


# --- D1: tier label ----------------------------------------------------------

def test_tier_label_suffix_with_routing():
    obj = _cli()
    obj._current_tier = "T3"
    obj._last_routing_decision = _decision(tier="T3", model="claude-opus-4-6")
    assert obj._tier_label_suffix() == " [T3 Opus]"


def test_tier_label_suffix_no_routing_is_empty():
    assert _cli()._tier_label_suffix() == ""


# --- transition line ---------------------------------------------------------

def test_transition_line_silent_on_first_turn(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._current_tier = "T2"
    obj._last_routing_decision = _decision()
    obj._render_transition_line()
    assert lines == []


def test_transition_line_silent_on_unchanged_tier(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._previous_tier = "T2"
    obj._current_tier = "T2"
    obj._last_routing_decision = _decision()
    obj._render_transition_line()
    assert lines == []


def test_transition_line_escalation(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._previous_tier = "T2"
    obj._current_tier = "T3"
    obj._last_routing_decision = _decision(
        tier="T3", model="claude-opus-4-6", reason="upward"
    )
    obj._render_transition_line()
    assert len(lines) == 1
    assert "↑" in lines[0]
    assert "Escalating to T3" in lines[0]


def test_transition_line_downward(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._previous_tier = "T2"
    obj._current_tier = "T1"
    obj._last_routing_decision = _decision(
        tier="T1", model="claude-haiku-4-5-20251001", reason="downward"
    )
    obj._render_transition_line()
    assert len(lines) == 1
    assert "↓" in lines[0]
    assert "Routing to T1" in lines[0]


def test_transition_line_silent_on_operator_override(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._previous_tier = "T2"
    obj._current_tier = "T3"
    obj._last_routing_decision = _decision(
        tier="T3", reason="operator_session_override"
    )
    obj._render_transition_line()
    assert lines == []


# --- D2: per-turn cost line --------------------------------------------------

def test_cost_line_local_model_reads_zero(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._last_routing_decision = _decision(
        tier="T2", model="gemma4", provider="ollama"
    )
    obj._render_turn_cost_line(1000, 500)
    assert len(lines) == 1
    assert "local ($0)" in lines[0]
    assert "1,500 tokens" in lines[0]


def test_cost_line_skipped_when_no_tokens(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._last_routing_decision = _decision()
    obj._render_turn_cost_line(0, 0)
    assert lines == []


# --- D5: /tier override ------------------------------------------------------

def test_tier_command_sets_override(monkeypatch):
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: None)
    obj = _cli()
    obj._handle_tier_command("/tier T3")
    assert obj._tier_override == "T3"


def test_tier_command_reset_clears_override(monkeypatch):
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: None)
    obj = _cli()
    obj._tier_override = "T3"
    obj._handle_tier_command("/tier reset")
    assert obj._tier_override is None


def test_tier_command_rejects_t0(monkeypatch):
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: None)
    obj = _cli()
    obj._handle_tier_command("/tier T0")
    assert obj._tier_override is None


def test_tier_command_rejects_unknown_tier(monkeypatch):
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: None)
    obj = _cli()
    obj._handle_tier_command("/tier T9")
    assert obj._tier_override is None


# --- D4: /why ----------------------------------------------------------------

def test_why_command_with_no_decision(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    _cli()._handle_why_command()
    assert any("No routing decision" in ln for ln in lines)


def test_why_command_renders_decision(monkeypatch):
    lines = _capture_cprint(monkeypatch)
    obj = _cli()
    obj._current_tier = "T3"
    obj._last_routing_decision = _decision(
        tier="T3", model="claude-opus-4-6", reason="upward"
    )
    obj._last_classification = ClassificationResult(
        intent_class="planning", pattern_hash="abc", confidence=0.72,
        register_class="strategic", complexity_signal="complex",
    )
    obj._handle_why_command()
    blob = "\n".join(lines)
    assert "T3 (Opus)" in blob
    assert "intent=planning" in blob
    assert "complexity=complex" in blob
    assert "confidence=0.72" in blob
    assert "Reason: upward" in blob


# --- D3: session summary -----------------------------------------------------

def test_accumulate_turn_tallies_per_tier():
    obj = _cli()
    obj._last_routing_decision = _decision(tier="T2")
    obj._accumulate_turn(100, 50)
    obj._accumulate_turn(200, 80)
    obj._last_routing_decision = _decision(tier="T3", model="claude-opus-4-6")
    obj._accumulate_turn(300, 120)
    assert obj._session_tier_stats["T2"]["turns"] == 2
    assert obj._session_tier_stats["T2"]["input"] == 300
    assert obj._session_tier_stats["T3"]["turns"] == 1


def test_session_summary_renders_breakdown(capsys):
    obj = _cli()
    obj._last_routing_decision = _decision(tier="T2")
    obj._accumulate_turn(1000, 500)
    obj._accumulate_turn(1200, 600)
    obj._last_routing_decision = _decision(tier="T3", model="claude-opus-4-6")
    obj._accumulate_turn(2000, 800)
    obj._render_session_summary()
    out = capsys.readouterr().out
    assert "Session summary: 3 turns · 2 tiers used" in out
    assert "T2 Sonnet: 2 turns" in out
    assert "T3 Opus: 1 turn" in out
    assert "Total:" in out


def test_session_summary_empty_is_silent(capsys):
    _cli()._render_session_summary()
    assert capsys.readouterr().out == ""
