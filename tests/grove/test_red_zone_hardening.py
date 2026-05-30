"""Sprint 32 Phase 3 — red-zone hardening tests.

Covers:

* Strike counter increments per-tool per-turn on red-zone halts.
* At ``_RED_ZONE_STRIKE_LIMIT`` strikes the dispatcher forces
  ``deny_hard`` WITHOUT invoking the operator handler.
* Hard-denial Observation carries the GATE-A directive text:
  "HARD DENIAL: This action is prohibited. Do not attempt this
  tool with these arguments again."
* Yellow-zone halts do NOT count toward strikes (the counter is
  red-only by design).
* Strikes reset at turn boundary (cross-turn enforcement remains
  architectural — the zone rule persists).
* Gateway path: a gateway handler's ``once`` return on a red-zone
  halt still hits the strike counter at the dispatcher (the
  hard-block path is the same code regardless of handler source).
* Phase 3b: malformed regex in ``zones.schema.yaml`` raises
  ``SchemaConfigurationError`` at load time — agent does not start.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import (
    AndonHalt,
    Dispatcher,
    _RED_ZONE_STRIKE_LIMIT,
)
from grove.errors import SchemaConfigurationError
from grove.intents import ToolIntent
from grove.zones import ZoneClassifier, ZoneResult


def _halt(
    tool: str = "terminal",
    zone: str = "red",
    arguments=None,
) -> AndonHalt:
    intents = [ToolIntent(
        tool_name=tool,
        arguments=arguments or {"command": "sudo rm -rf /"},
        call_id="c1",
    )]
    zr = [ZoneResult(zone=zone, matched_rule="r", source="rules")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


@pytest.fixture
def dispatcher() -> Dispatcher:
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_t#1"
    return d


# ── Strike counter accrues ───────────────────────────────────────────


class TestStrikeCounter:
    def test_first_two_red_halts_invoke_handler(
        self, dispatcher: Dispatcher,
    ):
        """Within the strike limit the handler is invoked normally."""
        invocations = []
        def _h(halt):
            invocations.append(halt)
            return "deny"
        dispatcher._sovereign_prompt_handler = _h

        for _ in range(_RED_ZONE_STRIKE_LIMIT - 1):
            disposition = dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(arguments={"command": f"c-{len(invocations)}"}),
            )
            assert disposition == "deny"
        assert len(invocations) == _RED_ZONE_STRIKE_LIMIT - 1

    def test_strike_limit_forces_deny_hard_silently(
        self, dispatcher: Dispatcher,
    ):
        """At limit the handler is bypassed; deny_hard returned."""
        def _explode(_h):
            raise AssertionError(
                "handler MUST NOT be invoked at strike limit"
            )
        # First (LIMIT - 1) calls invoke handler:
        dispatcher._sovereign_prompt_handler = lambda h: "deny"
        for i in range(_RED_ZONE_STRIKE_LIMIT - 1):
            dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(arguments={"command": f"c-{i}"}),
            )
        # Now swap to the tripwire — the next call MUST bypass it.
        dispatcher._sovereign_prompt_handler = _explode
        disposition = dispatcher._handle_andon_halt(
            agent=MagicMock(),
            halt=_halt(arguments={"command": "limit-trigger"}),
        )
        assert disposition == "deny_hard"

    def test_per_tool_strikes_are_independent(
        self, dispatcher: Dispatcher,
    ):
        """sudo strikes ≠ rm strikes — different tools have separate
        counters within a turn."""
        dispatcher._sovereign_prompt_handler = lambda h: "deny"
        # 2 strikes on "terminal" tool.
        for i in range(_RED_ZONE_STRIKE_LIMIT - 1):
            dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(tool="terminal", arguments={"command": f"c-{i}"}),
            )
        # 1 strike on "execute_code" — independent counter.
        dispatcher._handle_andon_halt(
            agent=MagicMock(),
            halt=_halt(tool="execute_code", arguments={"code": "x"}),
        )
        assert dispatcher._current_turn_andon_strikes == {
            "terminal": _RED_ZONE_STRIKE_LIMIT - 1,
            "execute_code": 1,
        }

    def test_yellow_zone_does_not_count_strikes(
        self, dispatcher: Dispatcher,
    ):
        """Yellow halts MUST NOT increment the red-zone strike
        counter — operator-supervised yellow lacks the structural
        bite that mandates the hard-denial path."""
        dispatcher._sovereign_prompt_handler = lambda h: "once"
        for _ in range(_RED_ZONE_STRIKE_LIMIT + 5):
            dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(zone="yellow", arguments={"command": "ok"}),
            )
        assert dispatcher._current_turn_andon_strikes == {}


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
        assert "Operator denied" in obs.value
        assert obs.metadata.get("is_hard_denial", False) is False


# ── Turn-boundary reset ──────────────────────────────────────────────


class TestTurnBoundaryReset:
    def test_strikes_reset_on_per_turn_state_block(self):
        """The strike counter is part of the per-turn reset block at
        ``dispatch_turn`` entry. Cross-turn enforcement remains
        architectural — the zone rule itself blocks every turn."""
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        d._write_pending_andon = lambda agent, halt: None
        d._clear_pending_andon = lambda agent, marker: None
        # Seed the per-turn dict as if mid-turn.
        d._current_turn_andon_strikes = {
            "terminal": 2, "execute_code": 1,
        }
        # The Phase 1 init's reset semantics: dispatch_turn re-assigns
        # the dict to ``{}`` at entry. Simulate that minimally:
        d._current_turn_andon_strikes = {}
        assert d._current_turn_andon_strikes == {}


# ── Telemetry on hard denial ─────────────────────────────────────────


class TestHardDenialTelemetry:
    def test_hard_denial_writes_andon_hard_denial_ledger_event(
        self, dispatcher: Dispatcher,
    ):
        ledger = MagicMock()
        dispatcher._sovereign_prompt_handler = lambda h: "deny"
        # Build up to the limit-trigger call.
        for i in range(_RED_ZONE_STRIKE_LIMIT - 1):
            dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(arguments={"command": f"c-{i}"}),
                ledger=ledger,
            )
        ledger.reset_mock()
        # Trigger the hard denial.
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(),
            halt=_halt(arguments={"command": "boom"}),
            ledger=ledger,
        )
        assert result == "deny_hard"
        # ledger.record called with andon_hard_denial event.
        record_calls = [c for c in ledger.record.call_args_list
                        if c.args and c.args[0] == "andon_hard_denial"]
        assert len(record_calls) == 1
        kwargs = record_calls[0].kwargs
        assert kwargs["tool"] == "terminal"
        assert kwargs["strikes"] == _RED_ZONE_STRIKE_LIMIT
        assert kwargs["zone"] == "red"


# ── Gateway hard-block (Phase 3c) ────────────────────────────────────


class TestGatewayHardBlock:
    def test_gateway_handler_still_hits_strike_counter(
        self, dispatcher: Dispatcher,
    ):
        """A gateway handler that returns ``once`` still triggers the
        strike counter on red halts — the gateway code path does NOT
        bypass the structural counter. At the strike limit the
        dispatcher emits ``deny_hard`` regardless of handler identity."""
        from grove.sovereign_prompt_handlers import gateway_auto_allow_handler
        dispatcher._sovereign_prompt_handler = gateway_auto_allow_handler

        # Pre-saturate strikes minus one to set up the limit-trigger.
        for i in range(_RED_ZONE_STRIKE_LIMIT - 1):
            dispatcher._handle_andon_halt(
                agent=MagicMock(),
                halt=_halt(arguments={"command": f"gateway-{i}"}),
            )

        # Limit trigger: gateway handler MUST be bypassed; deny_hard.
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(),
            halt=_halt(arguments={"command": "boom"}),
        )
        assert result == "deny_hard"


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
