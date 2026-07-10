"""kaizen-voice Sprint B1 — GRV-005 §VI RED hard-fork plumbing.

Proves the GATE-C2 test matrix for the RED workflow-resolution fork:

* zone-typed exception raised per zone (RED → AndonResolutionHalt,
  YELLOW → AndonPermissionHalt);
* strict zone-disposition coupling fails loud (ValueError) on cross-
  contamination, both directions;
* RED minting never fires on BOTH the drive-loop path AND the
  classify_and_mint (RPC/plugin) path;
* the parallel ``red_resolution`` telemetry preserves the dropped
  ``andon_disposition`` volume for RED;
* Cancel carries the distinct ``red_workflow_cancel`` provenance;
  De-scoped carries the feed-worthy OPERATOR_DESCOPED state;
* the folded YELLOW Sovereign Prompt text is byte-identical and carries
  NO RED branch;
* the shared action-fact layer (grove.action_facts) is single-sourced
  and byte-identical wherever it is imported;
* a RED resolution is reachable only via the headless hook / default
  fallback — never an operator-facing menu (B2).

GATE-DARK: B1 is PLUMBING ONLY. There is no operator-facing RED menu;
RED routes mechanically to the headless red-resolution handler.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List

import pytest

from grove.dispatcher import (
    AndonHalt,
    AndonPermissionHalt,
    AndonResolutionHalt,
    Dispatcher,
    RED_RESOLUTIONS,
    headless_red_resolution,
)
from grove.intents import ToolIntent
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests.grove.test_dispatch_turn import (
    _phase2_executor_stub,
    _synthetic_generator,
)


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    """Redirect the substrate home to tmp so no test pollutes ~/.grove."""
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


def _force_zone(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone=zone, matched_rule=f"forced_{zone}", source="test",
        ),
    )


def _bare_agent(msgs: List[Dict]) -> Any:
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent._current_messages = msgs
    agent.model = "claude-sonnet-4-6"
    agent.provider = "anthropic"
    _phase2_executor_stub(agent)
    return agent


def _drive(agent: Any, dispatcher: Dispatcher, intents: List[ToolIntent], result):
    agent._run_turn_generator = (
        lambda **kw: _synthetic_generator(intents, result)
    )
    return dispatcher.dispatch_turn(agent, user_message="hi")


# ── 1. Zone-typed exception per zone (fork-early at the raise) ────────────


class TestForkEarlyTypedException:
    def test_red_raises_resolution_halt(self, monkeypatch):
        _force_zone(monkeypatch, "red")
        d = Dispatcher()
        intent = ToolIntent(tool_name="write_file", arguments={}, call_id="c1")
        with pytest.raises(AndonResolutionHalt) as exc:
            d._classify_intents_batch_and_halt_or_raise([intent])
        # Subclass of the base so existing `except AndonHalt` still catches it.
        assert isinstance(exc.value, AndonHalt)
        assert not isinstance(exc.value, AndonPermissionHalt)
        assert exc.value.zone == "red"

    def test_yellow_raises_permission_halt(self, monkeypatch):
        _force_zone(monkeypatch, "yellow")
        d = Dispatcher()
        intent = ToolIntent(tool_name="write_file", arguments={}, call_id="c1")
        with pytest.raises(AndonPermissionHalt) as exc:
            d._classify_intents_batch_and_halt_or_raise([intent])
        assert isinstance(exc.value, AndonHalt)
        assert not isinstance(exc.value, AndonResolutionHalt)
        assert exc.value.zone == "yellow"


# ── 2. Strict zone-disposition coupling (fail-loud, both directions) ──────


class TestStrictCoupling:
    def test_red_halt_rejects_yellow_disposition(self, monkeypatch):
        # A RED halt whose resolution handler returns a YELLOW disposition is
        # cross-contamination → ValueError.
        _force_zone(monkeypatch, "red")
        # Phase A: an UNREACHABLE surface consults the handler (reachable would
        # store-pend, never touching the handler). The cross-contamination
        # ValueError only fires when the handler IS consulted.
        d = Dispatcher(
            red_resolution_handler=lambda halt: "once",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(ValueError, match="admissible RED resolutions"):
            _drive(agent, d, intents, {"final_response": "x"})

    def test_yellow_halt_rejects_red_resolution(self, monkeypatch):
        # A YELLOW halt whose sovereign prompt returns a RED resolution is
        # cross-contamination → ValueError.
        _force_zone(monkeypatch, "yellow")
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "cancel")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(ValueError, match="RED resolution"):
            _drive(agent, d, intents, {"final_response": "x"})

    def test_red_resolution_handler_unknown_value_fails_loud(self, monkeypatch):
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the handler is consulted (reachable store-pends).
        d = Dispatcher(
            red_resolution_handler=lambda halt: "bogus",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(ValueError, match="admissible RED resolutions"):
            _drive(agent, d, intents, {"final_response": "x"})


# ── 3. RED mint never fires — BOTH paths ──────────────────────────────────


class TestRedMintNeverFires:
    def test_drive_loop_cancel_never_executes(self, monkeypatch):
        from grove.governance_halt import TerminalGovernanceHalt
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the cancel handler (headless default) is
        # consulted and terminates (reachable would store-pend instead).
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)  # default headless → cancel
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt):
            _drive(agent, d, intents, {"final_response": "x"})
        # Executor never reached ⇒ the green-path batch mint (1621) never fired.
        assert agent._exec_called is False

    def test_drive_loop_descope_never_executes(self, monkeypatch):
        _force_zone(monkeypatch, "red")
        d = Dispatcher(red_resolution_handler=lambda halt: "descoped")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        _drive(agent, d, intents, {"final_response": "re-planned"})
        # De-scope drops + re-plans straight to FinalResponse — no execution,
        # so the green-path batch mint never fired.
        assert agent._exec_called is False

    def test_classify_and_mint_red_never_mints(self, monkeypatch):
        # The second consumer / gate 2608 — RED returns (False, ...) fail-closed
        # with NO mint.
        _force_zone(monkeypatch, "red")
        d = Dispatcher()
        minted: List[Any] = []
        monkeypatch.setattr(
            d._approval_gate, "mint", lambda *a, **k: minted.append(a),
        )
        allowed, message = d.classify_and_mint("write_file", {"x": 1})
        assert allowed is False
        assert "no token minted" in message
        assert minted == []  # gate 2608 never fired for RED

    def test_classify_and_mint_yellow_covered_still_mints(self, monkeypatch):
        # Sanity counter-case: a YELLOW-covered effect DOES mint — the fork did
        # not break the existing green/yellow-covered fast path.
        _force_zone(monkeypatch, "yellow")
        d = Dispatcher()
        minted: List[Any] = []
        monkeypatch.setattr(
            d._approval_gate, "mint", lambda *a, **k: minted.append(a),
        )
        allowed, _ = d.classify_and_mint("write_file", {"x": 1}, yellow_covered=True)
        assert allowed is True
        assert len(minted) == 1


# ── 4. red_resolution telemetry volume preserved ──────────────────────────


class TestRedResolutionTelemetry:
    def test_descope_records_red_resolution_not_disposition(self, monkeypatch):
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the descoped handler is consulted and records a
        # descoped red_resolution (reachable would record store_pending_approval).
        d = Dispatcher(
            red_resolution_handler=lambda halt: "descoped",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        _drive(agent, d, intents, {"final_response": "ok"})
        ledger = d.ledger_for(agent)
        red_events = ledger.events_by_type("red_resolution")
        disp_events = ledger.events_by_type("andon_disposition")
        # Volume preserved: one red_resolution where a YELLOW halt would have
        # emitted one andon_disposition — and NO andon_disposition for RED.
        assert len(red_events) == 1
        assert red_events[0]["resolution"] == "descoped"
        assert red_events[0]["zone"] == "red"
        assert disp_events == []
        # andon_halt still fires for both zones.
        assert len(ledger.events_by_type("andon_halt")) == 1

    def test_cancel_records_red_resolution(self, monkeypatch):
        from grove.governance_halt import TerminalGovernanceHalt
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the headless cancel default is consulted and
        # records a cancel red_resolution (reachable would store-pend).
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)  # default headless cancel
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt):
            _drive(agent, d, intents, {"final_response": "x"})
        ledger = d.ledger_for(agent)
        red_events = ledger.events_by_type("red_resolution")
        assert len(red_events) == 1
        assert red_events[0]["resolution"] == "cancel"
        assert ledger.events_by_type("andon_disposition") == []


# ── 5. Cancel provenance + De-scoped feed-worthiness ──────────────────────


class TestResolutionSemantics:
    def test_cancel_carries_red_workflow_cancel_provenance(self, monkeypatch):
        from grove.governance_halt import TerminalGovernanceHalt
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the cancel handler is consulted (reachable
        # store-pends). Cancel still carries red_workflow_cancel provenance.
        d = Dispatcher(
            red_resolution_handler=lambda halt: "cancel",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt) as exc:
            _drive(agent, d, intents, {"final_response": "x"})
        # DISTINCT provenance from red_sovereign.
        assert exc.value.context.trigger == "red_workflow_cancel"

    def test_descope_event_is_feed_worthy(self, monkeypatch):
        from grove.halt_event import HaltTrigger, is_feed_worthy
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the descoped handler is consulted (reachable
        # store-pends) and the feed-worthy OPERATOR_DESCOPED event is emitted.
        d = Dispatcher(
            red_resolution_handler=lambda halt: "descoped",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        _drive(agent, d, intents, {"final_response": "ok"})
        event = d._last_descope_event
        assert event.trigger is HaltTrigger.OPERATOR_DESCOPED
        assert event.capabilities.can_descope is True
        # The Sprint-A rule surfaces it on the permanent feed (steering decision).
        assert is_feed_worthy(event) is True

    def test_descope_computes_within_authority_alternative(self, monkeypatch):
        # descope_command strips the privilege wrapper; the de-scoped event
        # carries the within-authority form.
        _force_zone(monkeypatch, "red")
        # Phase A: UNREACHABLE so the descoped handler is consulted (reachable
        # store-pends) and the within-authority alternative is computed.
        d = Dispatcher(
            red_resolution_handler=lambda halt: "descoped",
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        agent = _bare_agent([])
        intents = [
            ToolIntent(
                tool_name="terminal",
                arguments={"command": "sudo apt install ripgrep"},
                call_id="c1",
            )
        ]
        _drive(agent, d, intents, {"final_response": "ok"})
        assert d._last_descope_event.what_halted.summary == "apt install ripgrep"


# ── 6. Headless-only reachability (no operator-facing RED menu) ───────────


class TestHeadlessOnly:
    def test_default_handler_is_headless_cancel(self):
        d = Dispatcher()
        assert d._red_resolution_handler is headless_red_resolution
        assert headless_red_resolution(halt=None) == "cancel"

    def test_red_never_consults_sovereign_prompt(self, monkeypatch):
        # Inject a sovereign_prompt_handler that EXPLODES if called — a RED halt
        # must not touch the four-choice prompt.
        from grove.governance_halt import TerminalGovernanceHalt

        def _exploding_prompt(halt):
            raise AssertionError("RED must not consult the sovereign prompt")

        _force_zone(monkeypatch, "red")
        d = Dispatcher(sovereign_prompt_handler=_exploding_prompt)
        # Phase A: force UNREACHABLE via the fleet platform so the RED path
        # consults the headless cancel default and terminates. Reachability keys
        # on platform here (not the sovereign handler), so the exploding sovereign
        # prompt is still never touched — the invariant this test guards.
        d._platform = "fleet"
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="write_file", arguments={}, call_id="c1")]
        with pytest.raises(TerminalGovernanceHalt):
            _drive(agent, d, intents, {"final_response": "x"})


# ── 7. Cancel + De-scope resolution fns unit-tested headless ──────────────


class TestResolutionFnsHeadless:
    def _red_halt(self, monkeypatch, command=None):
        _force_zone(monkeypatch, "red")
        # Phase A: an UNREACHABLE dispatcher so _resolve_red_halt consults the
        # red_resolution_handler (cancel/descope) rather than store-pending — the
        # headless resolution mechanic these unit tests exercise.
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        args = {"command": command} if command is not None else {}
        intent = ToolIntent(tool_name="terminal", arguments=args, call_id="c1")
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            return d, halt
        raise AssertionError("expected AndonResolutionHalt")

    def test_resolve_red_halt_cancel_raises_terminal(self, monkeypatch):
        from grove.governance_halt import TerminalGovernanceHalt
        d, halt = self._red_halt(monkeypatch)
        d._red_resolution_handler = lambda h: "cancel"
        with pytest.raises(TerminalGovernanceHalt) as exc:
            d._resolve_red_halt(_bare_agent([]), gen=None, halt=halt, ledger=None)
        assert exc.value.context.trigger == "red_workflow_cancel"

    def test_resolve_red_halt_descope_re_plans(self, monkeypatch):
        d, halt = self._red_halt(monkeypatch)
        d._red_resolution_handler = lambda h: "descoped"

        sent = {}

        class _FakeGen:
            def send(self, observations):
                sent["observations"] = observations
                return "resumed"

        out = d._resolve_red_halt(
            _bare_agent([]), gen=_FakeGen(), halt=halt, ledger=None,
        )
        # De-scope drops + re-plans: returns the generator's next yield, and the
        # re-plan observations were sent back.
        assert out == "resumed"
        assert isinstance(sent["observations"], list)

    def test_build_descope_observations_feed_worthy(self, monkeypatch):
        from grove.halt_event import HaltTrigger, is_feed_worthy
        d, halt = self._red_halt(monkeypatch, command="sudo rm -rf /tmp/x")
        obs = d._build_descope_observations(_bare_agent([]), halt)
        assert isinstance(obs, list) and obs
        assert d._last_descope_event.trigger is HaltTrigger.OPERATOR_DESCOPED
        assert is_feed_worthy(d._last_descope_event) is True


# ── 8. YELLOW fold byte-identical + NO RED branch ─────────────────────────


class _FakeHalt:
    """Minimal AndonHalt-shaped stub for the TTY prompt (it reads only
    intents[triggering_index].tool_name / .arguments)."""

    def __init__(self, tool_name, arguments, zone):
        self.intents = [ToolIntent(tool_name=tool_name, arguments=arguments, call_id="c")]
        self.triggering_index = 0
        self.zone = zone


class TestYellowFoldByteIdentical:
    # H2 (grant-mint-unification-v1): the [3] line now names the store an
    # Always writes; the B1 eight-print SHAPE (blank / header / blank /
    # choices / trailing blank via a single print) is what this pins.
    def _legacy_block(self, description: str) -> str:
        buf = io.StringIO()
        print(file=buf)
        print(
            f"I'd like to {description}. This one's your call before I go ahead.",
            file=buf,
        )
        print(file=buf)
        print("  [1] Just this once", file=buf)
        print("  [2] For the rest of this session", file=buf)
        print("  [3] Always (zone rule) — I'll remember it", file=buf)
        print("  [4] Not this time", file=buf)
        print(file=buf)
        return buf.getvalue()

    def test_render_matches_legacy_eight_print_sequence(self):
        from grove.action_facts import describe_action_kaizen
        from grove.halt_renderer import render_yellow_sovereign_prompt
        args = {"command": "rm -rf /tmp/junk"}
        desc = describe_action_kaizen("terminal", args)
        block = render_yellow_sovereign_prompt(
            "terminal", args, always_store="zone rule",
        )
        # A single print(block) reproduces the prior eight-print output exactly.
        nbuf = io.StringIO()
        print(block, file=nbuf)
        assert nbuf.getvalue() == self._legacy_block(desc)

    def test_tty_prompt_byte_identical_and_no_red_branch(self, monkeypatch):
        from grove.sovereign_prompt_handlers import tty_sovereign_prompt
        args = {"command": "git push origin main"}
        # Same menu regardless of zone — there is NO RED branch on this surface.
        monkeypatch.setattr("builtins.input", lambda *a, **k: "4")
        yellow_out = io.StringIO()
        red_out = io.StringIO()
        d_yellow = tty_sovereign_prompt(_FakeHalt("terminal", args, "yellow"), out=yellow_out)
        d_red = tty_sovereign_prompt(_FakeHalt("terminal", args, "red"), out=red_out)
        assert d_yellow == "deny" and d_red == "deny"  # input "4" → deny
        # Byte-identical menu text across zones: the surface has no RED branch.
        assert yellow_out.getvalue() == red_out.getvalue()
        assert "[1] Just this once" in yellow_out.getvalue()
        # No RED-resolution vocabulary leaked onto the four-choice surface.
        for forbidden in ("cancel", "de-scope", "descope", "Operator Runs"):
            assert forbidden.lower() not in yellow_out.getvalue().lower()


# ── 9. Shared fact layer single-sourced + byte-identical ──────────────────


class TestSharedFactLayer:
    def test_fact_formatter_single_sourced(self):
        from grove.action_facts import describe_action_kaizen as d_src
        from grove.halt_renderer import describe_action_kaizen as d_halt
        from grove.sovereign_prompt_handlers import describe_action_kaizen as d_sph
        # One definition, imported in three places — not three copies.
        assert d_src is d_halt is d_sph

    def test_fact_byte_identical_across_surfaces(self):
        # The fact the RED/halt surface would render and the fact the proposal
        # surface imports are byte-identical (shared layer, isolated tone).
        from grove.action_facts import describe_action_kaizen
        from grove.halt_renderer import render_yellow_sovereign_prompt
        cases = [
            ("terminal", {"command": "brew install ripgrep"}),
            ("write_file", {"path": "/tmp/out.txt"}),
            ("mcp_notion_notion_fetch", {"id": "abc"}),
            ("execute_code", {"code": "print(1)"}),
        ]
        for tool, args in cases:
            fact = describe_action_kaizen(tool, args)
            # The fact appears verbatim inside the folded YELLOW surface.
            assert f"I'd like to {fact}." in render_yellow_sovereign_prompt(tool, args)
