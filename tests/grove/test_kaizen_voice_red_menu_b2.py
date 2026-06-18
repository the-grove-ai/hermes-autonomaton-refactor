"""kaizen-voice Sprint B2 — GRV-005 §VI RED operator-facing menu {Cancel, De-scope}.

Proves the GATE-C2 test matrix for opening the gate-dark RED menu on TTY:

* M1 — the CLI wires ``red_resolution_handler=self._red_resolution_callback``;
  an injected handler is consulted instead of the headless Cancel default, and
  the CLI bridge renders the operator-facing menu end-to-end.
* M2 — the menu offers EXACTLY {Cancel, De-scope}: Cancel → ``red_workflow_cancel``
  terminal; De-scope → ``_build_descope_observations`` re-plan; ``RED_RESOLUTIONS``
  is still ``("cancel", "descoped")`` (no ``operator_runs``); the menu prompt
  renders through ``grove.halt_renderer`` (RED interrupt register, shared fact
  layer), never inline CLI text.
* M3 — ``RED_WORKFLOW_CANCEL`` renders the bespoke Cancel copy, NOT the generic
  ``red_sovereign`` / ``deny_hard`` "requires your approval" text.
* M4 — the red-zone strike machinery is gone (constant + per-turn counter); the
  YELLOW quarantine terminal path is preserved (ANDON-quarantine-preserve); the
  YELLOW four-choice menu is byte-untouched.

The Operator-Runs-It resumable bridge is CUT (GATE-A leg-b re-yield NOT-PROVEN);
it must never appear on this surface.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List

import pytest

from grove.dispatcher import (
    AndonResolutionHalt,
    Dispatcher,
    RED_RESOLUTIONS,
    headless_red_resolution,
)
from grove.intents import ToolIntent
from tests.grove.test_kaizen_voice_red_fork_b1 import (
    _bare_agent,
    _drive,
    _force_zone,
)


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    """Redirect the substrate home to tmp so no test pollutes ~/.grove."""
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


def _force_classify(monkeypatch, *, zone, matched_rule):
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(zone=zone, matched_rule=matched_rule, source="test"),
    )


class _FakeRedHalt:
    """Minimal AndonResolutionHalt-shaped stub: the RED menu reads only
    intents[triggering_index].tool_name / .arguments."""

    def __init__(self, tool_name="terminal", arguments=None):
        self.intents = [ToolIntent(
            tool_name=tool_name, arguments=arguments or {}, call_id="c",
        )]
        self.triggering_index = 0
        self.zone = "red"


# ── M1 — the gate-dark flip: operator-facing handler is consulted ─────────


class TestM1GateFlip:
    def test_injected_red_handler_consulted_not_headless_default(self, monkeypatch):
        """A Dispatcher built with a red_resolution_handler consults IT, not the
        headless Cancel default — the structural seam the CLI flip rides on."""
        seen = {}

        def _operator_menu(halt):
            seen["called"] = True
            return "descoped"

        _force_zone(monkeypatch, "red")
        d = Dispatcher(red_resolution_handler=_operator_menu)
        assert d._red_resolution_handler is _operator_menu
        assert d._red_resolution_handler is not headless_red_resolution
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        # descoped → re-plan (soft); the turn completes rather than terminating.
        _drive(agent, d, intents, {"final_response": "ok"})
        assert seen.get("called") is True

    def test_cli_wires_red_resolution_callback(self):
        """The CLI's Dispatcher construction passes its operator-facing
        _red_resolution_callback as the red_resolution_handler (the flip)."""
        import inspect
        import cli
        assert hasattr(cli.HermesCLI, "_red_resolution_callback")
        src = inspect.getsource(cli.HermesCLI)
        assert "red_resolution_handler=self._red_resolution_callback" in src

    def test_cli_bridge_renders_menu_end_to_end_direct_mode(self, monkeypatch):
        """In direct mode (no prompt_toolkit app) the CLI callback runs
        tty_red_resolution, which renders the halt_renderer RED menu and maps
        the operator's keypress to a RED_RESOLUTIONS value."""
        import cli
        c = object.__new__(cli.HermesCLI)
        c._app = None
        c._app_loop = None
        out = io.StringIO()
        monkeypatch.setattr("sys.stderr", out)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "1")
        result = c._red_resolution_callback(_FakeRedHalt(arguments={"command": "sudo rm -rf /"}))
        assert result == "cancel"
        assert result in RED_RESOLUTIONS
        # The rendered menu reached the operator with both choices.
        assert "[1] Cancel" in out.getvalue()
        assert "[2] De-scope" in out.getvalue()


# ── M2 — menu = EXACTLY {Cancel, De-scope}; mapping + shared-fact render ───


class TestM2RedMenu:
    def test_red_resolutions_unchanged_no_operator_runs(self):
        assert RED_RESOLUTIONS == ("cancel", "descoped")
        assert "operator_runs" not in RED_RESOLUTIONS

    def test_menu_renders_exactly_two_choices_via_halt_renderer(self):
        from grove.halt_renderer import render_red_resolution_prompt
        block = render_red_resolution_prompt("terminal", {"command": "sudo rm -rf /"})
        assert "[1] Cancel" in block
        assert "[2] De-scope" in block
        # No YELLOW four-choice vocabulary, no Operator-Runs-It.
        for forbidden in ("[3]", "[4]", "just this once", "rest of this session",
                          "always", "operator runs", "handle it yourself"):
            assert forbidden.lower() not in block.lower()

    def test_menu_carries_shared_action_fact(self):
        """The fact (WHAT the agent wanted) is the shared action_facts layer,
        byte-identical to what every other surface would render."""
        from grove.action_facts import describe_action_kaizen
        from grove.halt_renderer import render_red_resolution_prompt
        args = {"command": "sudo rm -rf /etc"}
        fact = describe_action_kaizen("terminal", args)
        block = render_red_resolution_prompt("terminal", args)
        assert fact in block

    def test_cancel_maps_to_red_workflow_cancel_terminal(self, monkeypatch):
        from grove.governance_halt import TerminalGovernanceHalt
        _force_zone(monkeypatch, "red")
        d = Dispatcher(red_resolution_handler=lambda h: "cancel")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _drive(agent, d, intents, {"final_response": "x"})
        assert exc.value.context.trigger == "red_workflow_cancel"

    def test_descope_maps_to_descope_observations(self, monkeypatch):
        from grove.halt_event import HaltTrigger, is_feed_worthy
        _force_zone(monkeypatch, "red")
        d = Dispatcher(red_resolution_handler=lambda h: "descoped")
        intent = ToolIntent(
            tool_name="terminal", arguments={"command": "sudo rm -rf /tmp/x"},
            call_id="c1",
        )
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            obs = d._build_descope_observations(_bare_agent([]), halt)
        else:
            raise AssertionError("expected AndonResolutionHalt")
        assert isinstance(obs, list) and obs
        assert d._last_descope_event.trigger is HaltTrigger.OPERATOR_DESCOPED
        assert is_feed_worthy(d._last_descope_event) is True

    def test_tty_red_resolution_choice_mapping(self, monkeypatch):
        from grove.sovereign_prompt_handlers import tty_red_resolution
        cases = {"1": "cancel", "2": "descoped"}
        for key, expected in cases.items():
            monkeypatch.setattr("builtins.input", lambda *a, **k: key)
            assert tty_red_resolution(_FakeRedHalt(), out=io.StringIO()) == expected

    def test_tty_red_resolution_eof_fails_safe_to_cancel(self, monkeypatch):
        from grove.sovereign_prompt_handlers import tty_red_resolution

        def _eof(*a, **k):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        assert tty_red_resolution(_FakeRedHalt(), out=io.StringIO()) == "cancel"


# ── M3 — bespoke Cancel copy, distinct from the generic red branch ─────────


class TestM3CancelCopy:
    def test_red_workflow_cancel_renders_bespoke_copy(self):
        from grove.governance_halt import (
            GovernanceHaltContext,
            TerminalGovernanceHalt,
        )
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="red_workflow_cancel", tool_name="terminal")
        )
        text = halt.surface_text()
        assert text == (
            "Action is structurally prohibited by the autonomaton. "
            "Workflow cancelled."
        )
        # NOT the generic red_sovereign/deny_hard copy or the alternatives footer.
        assert "requires your approval" not in text
        assert "handle the action yourself" not in text

    def test_red_sovereign_copy_unchanged_by_m3(self):
        """M3 is the ONLY copy change: the generic red_sovereign branch must
        still render its prior text (no drift)."""
        from grove.governance_halt import (
            GovernanceHaltContext,
            TerminalGovernanceHalt,
        )
        text = TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="red_sovereign", tool_name="terminal")
        ).surface_text()
        assert "requires your approval and was declined" in text


# ── M4 — strike machinery purged; YELLOW quarantine preserved ──────────────


class TestM4Cleanup:
    def test_strike_limit_constant_removed(self):
        from grove import dispatcher
        assert not hasattr(dispatcher, "_RED_ZONE_STRIKE_LIMIT")

    def test_dispatcher_has_no_strike_counter_attr(self):
        d = Dispatcher()
        assert not hasattr(d, "_current_turn_andon_strikes")

    def test_quarantine_halt_preservation(self, monkeypatch):
        """ANDON-quarantine-preserve: a YELLOW quarantined (.andon) invocation
        the operator declines STILL routes to TerminalGovernanceHalt with the
        ``quarantine`` trigger — the one structural disjunct M4 preserved."""
        from grove.governance_halt import TerminalGovernanceHalt
        _force_classify(monkeypatch, zone="yellow", matched_rule="skill.demo.andon")
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _drive(agent, d, intents, {"final_response": "x"})
        assert exc.value.context.trigger == "quarantine"

    def test_yellow_four_choice_menu_untouched(self, monkeypatch):
        """The YELLOW Sovereign Prompt still renders the four-choice menu and
        maps keys to dispositions — the RED flip touched only the RED field."""
        from grove.sovereign_prompt_handlers import tty_sovereign_prompt

        class _Y:
            def __init__(self):
                self.intents = [ToolIntent(
                    tool_name="terminal", arguments={"command": "git push"}, call_id="c",
                )]
                self.triggering_index = 0
                self.zone = "yellow"
        out = io.StringIO()
        monkeypatch.setattr("builtins.input", lambda *a, **k: "1")
        assert tty_sovereign_prompt(_Y(), out=out) == "once"
        menu = out.getvalue()
        assert "[1] Just this once" in menu
        assert "[4] Not this time" in menu
        # No RED resolution vocabulary leaked onto the YELLOW surface.
        for forbidden in ("cancel", "de-scope", "descope"):
            assert forbidden.lower() not in menu.lower()
