"""Sprint 32 Phase 3 — red-zone hardening tests (kaizen-voice B2 trim).

Covers:

* The hard-denial Observation shape produced by
  ``_build_skip_observations(hard=True)`` — the GATE-A directive text plus
  the ``is_hard_denial`` / ``disposition`` metadata.
* The soft-deny Observation keeps its original decline-to-run wording.
* Phase 3b: malformed regex in ``zones.schema.yaml`` raises
  ``SchemaConfigurationError`` at load time — agent does not start.

kaizen-voice Sprint B2 removed the red-zone STRIKE COUNTER and its tests:
post-§VI a RED halt is an ``AndonResolutionHalt`` resolved upstream by
``_resolve_red_halt`` (the §VI fork), so it never reaches
``_handle_andon_halt`` — the per-turn strike counter that lived there was
inert dead code. RED drive-loop / classify_and_mint behavior is covered by
``test_kaizen_voice_red_fork_b1.py``; the operator-facing RED menu by
``test_kaizen_voice_red_menu_b2.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher
from grove.errors import SchemaConfigurationError
from grove.intents import ToolIntent
from grove.zones import ZoneClassifier


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


# ── Phase 3b — regex fail-hard at schema load ────────────────────────


class TestRegexFailHard:
    def test_malformed_pattern_raises_schema_configuration_error(
        self, tmp_path: Path,
    ):
        """A rule whose pattern fails ``check_pattern_safety`` MUST
        raise ``SchemaConfigurationError`` at load time. The agent
        does not start with malformed governance."""
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text("""
schema_version: 1
zones:
  green:
    auto_approve: []
  yellow:
    proposes: []
  red:
    sovereign: []
tool_zones:
  terminal:
    default_zone: yellow
    rules:
      - match_pattern: ".*"
        zone: green
        reason: "catch-all rejected by safety check"
""")
        with pytest.raises(SchemaConfigurationError) as exc_info:
            ZoneClassifier(schema)
        message = str(exc_info.value)
        # Message MUST name the offending tool and the failure reason.
        assert "terminal" in message
        assert "rules[0]" in message
        assert "match_pattern" in message

    def test_invalid_zone_value_raises(self, tmp_path: Path):
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text("""
schema_version: 1
zones:
  green:
    auto_approve: []
  yellow:
    proposes: []
  red:
    sovereign: []
tool_zones:
  terminal:
    default_zone: yellow
    rules:
      - match_pattern: "^sudo"
        zone: invalid_zone
        reason: ""
""")
        with pytest.raises(SchemaConfigurationError) as exc_info:
            ZoneClassifier(schema)
        assert "invalid_zone" in str(exc_info.value)

    def test_non_dict_rule_entry_raises(self, tmp_path: Path):
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text("""
schema_version: 1
zones:
  green:
    auto_approve: []
  yellow:
    proposes: []
  red:
    sovereign: []
tool_zones:
  terminal:
    default_zone: yellow
    rules:
      - "not a mapping"
""")
        with pytest.raises(SchemaConfigurationError):
            ZoneClassifier(schema)

    def test_valid_schema_loads_successfully(self, tmp_path: Path):
        """Sanity check — a well-formed schema MUST still load
        cleanly (no false-positive on a good rule)."""
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text("""
schema_version: 1
zones:
  green:
    auto_approve: []
  yellow:
    proposes: []
  red:
    sovereign: []
tool_zones:
  terminal:
    default_zone: yellow
    rules:
      - match_pattern: "^sudo\\\\s+.*"
        zone: red
        reason: "Privilege escalation requires sovereign approval."
""")
        # Should not raise.
        clf = ZoneClassifier(schema)
        result = clf.classify_command_string(
            command="sudo apt-get update",
            action="command.terminal",
            tool_id="terminal",
        )
        assert result.zone == "red"
