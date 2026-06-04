"""Sprint 57 — gateway noise cleanup verification (T26-T27).

T26: grove_agent_help carries the post-halt discipline directive and does not
     teach the agent to predict/narrate governance internals.
T27: the agent-context halt-message path emits no governance vocabulary the
     agent could parrot to the operator (Andon / sovereignty / zone-as-class).

These exercise the REAL message builders — the strings the agent actually
reads as Observation values and tool-result errors — not mocks of them.
"""

from __future__ import annotations

from types import SimpleNamespace

from unittest.mock import MagicMock

from agent.prompt_builder import GROVE_AGENT_HELP_GUIDANCE


# ── T26: post-halt prompt discipline ──────────────────────────────────


def test_T26_grove_agent_help_has_post_halt_directive():
    g = GROVE_AGENT_HELP_GUIDANCE
    # The directive is present and concrete.
    assert "paused or denied by the system" in g
    assert "do not explain the internal mechanism" in g
    # A2 — it tells the agent to ACT, not just announce, and to keep going.
    assert "do not merely announce" in g
    assert "Paused is not failed" in g
    # Operator-facing approval phrasing uses "needs your approval" (NOT the
    # Sprint-55-forbidden "requires approval").
    assert "needs your approval" in g
    assert "requires approval" not in g


def test_T26_grove_agent_help_no_prediction_language():
    """The post-halt directive NAMES governance terms only to forbid them —
    that is correct. What must stay absent is language that teaches the agent
    to PREDICT or CLASSIFY governance outcomes (the Sprint 55 guarantee)."""
    g = GROVE_AGENT_HELP_GUIDANCE.lower()
    forbidden_prediction = [
        "yellow zone", "red zone", "green zone", "auto-allow", "auto allow",
        "requires approval", "will be allowed", "will prompt you", "zone rule",
        "disposition", "green/yellow/red",
    ]
    leaked = [t for t in forbidden_prediction if t in g]
    assert not leaked, f"grove_agent_help leaks prediction language: {leaked}"


# ── T27: the agent-context halt-message path is clean ─────────────────


def _assert_no_governance_vocab(text: str, *, where: str) -> None:
    """No governance IMPLEMENTATION vocab the agent could echo. The config
    file path ``zones.schema.yaml`` is a legitimate operator reference and is
    explicitly allowed — only zone-as-classification language is banned."""
    low = text.lower()
    banned = ["andon", "sovereignty", "sovereign zone", "red-zone",
              "red zone", "yellow zone", "zone violation", "dispatcher halted"]
    hits = [t for t in banned if t in low]
    assert not hits, f"{where} leaks governance vocab {hits}: {text!r}"


def test_T27_soft_deny_observation_is_clean():
    from grove.dispatcher import Dispatcher
    from grove.intents import ToolIntent

    d = Dispatcher()
    intents = [ToolIntent(tool_name="execute_code", arguments={}, call_id="c1")]
    obs = d._build_skip_observations(agent=MagicMock(), intents=intents)[0]
    _assert_no_governance_vocab(obs.value, where="soft-deny Observation.value")
    # The tool name itself may appear (it's the operator's own request), but
    # the framing must be a plain decline-to-run.
    assert "declined to run" in obs.value


def test_T27_hard_deny_observation_is_clean():
    from grove.dispatcher import Dispatcher
    from grove.intents import ToolIntent

    d = Dispatcher()
    intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
    obs = d._build_skip_observations(
        agent=MagicMock(), intents=intents, hard=True,
    )[0]
    _assert_no_governance_vocab(obs.value, where="hard-deny Observation.value")


def test_T27_render_red_surface_is_clean():
    from grove.dispatch import render_red_surface

    zr = SimpleNamespace(matched_rule="command.execute.sudo")
    surface = render_red_surface("sudo apt install foo", zr)
    _assert_no_governance_vocab(surface, where="render_red_surface")
    # Butler structure preserved.
    assert "That's in your direct control — here's how." in surface
    assert "the system paused at this protected action" in surface
    # The config-file reference is allowed and still present.
    assert "zones.schema.yaml" in surface


def test_T27_red_zone_guard_message_is_clean():
    """The terminal guard's red-zone message reaches the agent as a tool
    error (terminal_tool error= field) — it must be clean."""
    from tools.approval import check_all_command_guards

    result = check_all_command_guards("sudo apt install foo", env_type="local")
    assert result["approved"] is False
    _assert_no_governance_vocab(result["message"], where="red-zone guard message")
