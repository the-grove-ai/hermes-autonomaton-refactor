"""Sprint 32 Phase 3 — red-zone hardening tests (kaizen-voice B2 trim).

Covers:

* The hard-denial Observation shape produced by
  ``_build_skip_observations(hard=True)`` — the GATE-A directive text plus
  the ``is_hard_denial`` / ``disposition`` metadata.
* The soft-deny Observation keeps its original decline-to-run wording.

(zones-v2-scope-keying P2 retired the v1 regex/hierarchical-rule schema and
its ``SchemaConfigurationError`` load-time validation, so the former Phase 3b
``TestRegexFailHard`` suite — which exercised ``check_pattern_safety`` and
``classify_command_string`` on a v1 ``tool_zones.<tool>.rules`` schema — was
deleted. Shell command classification is now EFFECT-based via
``grove.shell_effects.classify_shell_effect``; v2 schema-load validation is
covered in the zones module's own tests.)

kaizen-voice Sprint B2 removed the red-zone STRIKE COUNTER and its tests:
post-§VI a RED halt is an ``AndonResolutionHalt`` resolved upstream by
``_resolve_red_halt`` (the §VI fork), so it never reaches
``_handle_andon_halt`` — the per-turn strike counter that lived there was
inert dead code. RED drive-loop / classify_and_mint behavior is covered by
``test_kaizen_voice_red_fork_b1.py``; the operator-facing RED menu by
``test_kaizen_voice_red_menu_b2.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher
from grove.intents import ToolIntent


@pytest.fixture
def dispatcher() -> Dispatcher:
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_t#1"
    return d


# ── Hard-denial Observation shape ────────────────────────────────────


class TestHardDenialObservation:
    def test_hard_observation_carries_directive_text(
        self, dispatcher: Dispatcher,
    ):
        intents = [ToolIntent(
            tool_name="terminal",
            arguments={"command": "sudo rm -rf /"},
            call_id="c1",
        )]
        observations = dispatcher._build_skip_observations(
            agent=MagicMock(), intents=intents, hard=True,
        )
        assert len(observations) == 1
        obs = observations[0]
        # The exact directive text the operator locked at GATE-A.
        assert obs.value.startswith("HARD DENIAL: This action is prohibited.")
        assert "Do not attempt this tool with these arguments again." in obs.value
        assert "terminal" in obs.value  # carries the tool name for the LLM
        # The metadata marker so the agent can detect "do not retry"
        # without parsing the text.
        assert obs.metadata["is_hard_denial"] is True
        assert obs.metadata["disposition"] == "deny_hard"

    def test_soft_observation_uses_original_skip_text(
        self, dispatcher: Dispatcher,
    ):
        """The Phase 1 ``deny`` (= v1.0 skip) path keeps its
        original Observation text — only ``deny_hard`` upgrades
        to the directive phrasing."""
        intents = [ToolIntent(
            tool_name="terminal", arguments={}, call_id="c1",
        )]
        observations = dispatcher._build_skip_observations(
            agent=MagicMock(), intents=intents,  # hard=False default
        )
        obs = observations[0]
        assert "HARD DENIAL" not in obs.value
        # Sprint 57 — operator-friendly wording (no governance vocab); the
        # soft-deny still reads as a decline-to-run, disposition stays "deny".
        assert "declined to run" in obs.value
        assert "Andon" not in obs.value
        assert obs.metadata.get("disposition") == "deny"
        assert obs.metadata.get("is_hard_denial", False) is False
