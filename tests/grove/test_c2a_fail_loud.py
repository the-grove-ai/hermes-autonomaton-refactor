"""GRV-010 C2a — fail-loud core (B15) tests.

A STRUCTURAL governed denial must TERMINATE the agent's autonomous turn and
surface to the operator — never improvise a workaround. An ordinary Yellow
operator decline ("not now") stays collaborative. C2a draws that line with a
terminal control-flow signal, :class:`grove.governance_halt.TerminalGovernanceHalt`,
raised at two boundaries:

  * Level 1 — the pre-execution deny fork (``grove.dispatcher`` deny fork):
    RED-sovereign / deny_hard / quarantined-.andon → terminate; Yellow → soft.
  * Level 2 — the executor (``grove.tool_executor``): a GroveError (in practice
    GovernanceError, the dispatch crypto-lock) is re-raised AS the terminal
    halt instead of degrading to a recoverable "Error executing tool..." string.

The signal subclasses BaseException so it propagates uncaught past the
``except Exception`` catch-alls; it is NOT resumable (cf. OperatorInputRequired).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from grove.dispatcher import Dispatcher
from grove.errors import GovernanceError
from grove.governance_halt import (
    TERMINAL_TRIGGERS,
    GovernanceHaltContext,
    TerminalGovernanceHalt,
    terminal_halt_result,
)
from grove.intents import FinalResponse, ToolBatchYield, ToolIntent
from grove.operator_input import OperatorInputRequired, PendingOperatorRequest
from grove.tool_executor import (
    ExecutionContext,
    ExecutorConfig,
    ObservabilityCallbacks,
    SideEffectCallbacks,
    ToolExecutor,
)


# ══════════════════════════════════════════════════════════════════════
# Module-level: the terminal signal + result shape
# ══════════════════════════════════════════════════════════════════════


class TestTerminalSignal:
    def test_is_baseexception_not_exception(self):
        """Catch-all-proof: a BaseException slips past `except Exception`,
        mirroring OperatorInputRequired's discipline."""
        assert issubclass(TerminalGovernanceHalt, BaseException)
        assert not issubclass(TerminalGovernanceHalt, Exception)

    def test_not_caught_by_except_exception(self):
        halt = TerminalGovernanceHalt(GovernanceHaltContext(trigger="deny_hard"))
        caught_by_exception = False
        propagated = False
        try:
            try:
                raise halt
            except Exception:  # noqa: BLE001
                caught_by_exception = True
        except TerminalGovernanceHalt:
            propagated = True
        assert caught_by_exception is False
        assert propagated is True

    def test_terminal_triggers_set(self):
        # GRV-010 C2d added "tier_unavailable" — a model-availability halt.
        # GRV-005 §VI (kaizen-voice B1) added "red_workflow_cancel" — the
        # operator aborting a structurally-blocked RED workflow (distinct
        # provenance from red_sovereign).
        assert set(TERMINAL_TRIGGERS) == {
            "red_sovereign", "deny_hard", "quarantine", "governance_error",
            "tier_unavailable", "red_workflow_cancel",
        }

    def test_terminal_halt_result_shape(self):
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(
                trigger="red_sovereign", tool_name="terminal", zone="red",
            )
        )
        r = terminal_halt_result(halt)
        assert r["failed"] is True
        assert r["completed"] is True
        assert r["governance_terminated"] is True
        assert r["governance_trigger"] == "red_sovereign"
        # Declarative policy honored at the halt path.
        assert r["failure_fallback"] == "halt_and_surface"
        # Terminal, NOT resumable — no store-and-resume field.
        assert "awaiting_operator" not in r
        assert isinstance(r["final_response"], str) and r["final_response"]

    def test_surface_text_is_operator_register_no_governance_vocab(self):
        """The operator-facing text must not parrot governance internals."""
        for trig in ("red_sovereign", "deny_hard", "quarantine", "governance_error"):
            text = TerminalGovernanceHalt(
                GovernanceHaltContext(trigger=trig, tool_name="terminal")
            ).surface_text()
            low = text.lower()
            assert "andon" not in low
            assert "zone" not in low
            assert "sovereign" not in low
            # Offers the Kaizen disposition (cancel / handle / different approach).
            assert "cancel" in low

    def test_distinct_from_operator_input_required(self):
        """Reusing OperatorInputRequired would make the gateway RESUME; the two
        must be distinct types so terminal != resumable."""
        assert TerminalGovernanceHalt is not OperatorInputRequired
        assert not issubclass(TerminalGovernanceHalt, OperatorInputRequired)


# ══════════════════════════════════════════════════════════════════════
# Level 2 — executor: GroveError re-raised AS TerminalGovernanceHalt
# ══════════════════════════════════════════════════════════════════════


class _NoInterrupt:
    def is_set(self) -> bool:
        return False

    def set_for_thread(self, tid: int) -> None:
        pass

    def clear_for_thread(self, tid: int) -> None:
        pass


def _ctx(invoke_tool) -> ExecutionContext:
    intents = [ToolIntent(tool_name="terminal", arguments={"command": "x"}, call_id="c1")]
    return ExecutionContext(
        intents=intents,
        tool_registry=None,
        callbacks=ObservabilityCallbacks(),
        side_effects=SideEffectCallbacks(invoke_tool=invoke_tool),
        interrupt=_NoInterrupt(),
        config=ExecutorConfig(),
        effective_task_id="t",
        api_call_count=0,
    )


class TestLevel2Executor:
    def test_sequential_governance_error_becomes_terminal(self):
        def _boom(*a, **k):
            raise GovernanceError("unapproved tool dispatch (no Stage-04 token)")

        ex = ToolExecutor()
        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            ex.execute_batch_sequential(_ctx(_boom))
        assert exc_info.value.context.trigger == "governance_error"
        assert exc_info.value.context.tool_name == "terminal"

    def test_concurrent_governance_error_becomes_terminal(self):
        """Worker-thread GroveError is captured and re-raised on the main
        thread after the pool joins — not swallowed in the Future."""
        def _boom(*a, **k):
            raise GovernanceError("unapproved tool dispatch (no Stage-04 token)")

        ex = ToolExecutor()
        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            ex.execute_batch_concurrent(_ctx(_boom))
        assert exc_info.value.context.trigger == "governance_error"

    def test_sequential_plain_exception_still_degrades_to_string(self):
        """A non-structural tool error keeps the recoverable observation path —
        only GroveError terminalizes. (Don't blanket-terminalize.)"""
        def _boom(*a, **k):
            raise ValueError("ordinary bad args")

        ex = ToolExecutor()
        results = ex.execute_batch_sequential(_ctx(_boom))
        assert len(results) == 1
        # Degraded to a recoverable observation string — NOT terminalized.
        assert "Error executing tool" in str(results[0].content)

    def test_concurrent_plain_exception_still_degrades_to_string(self):
        def _boom(*a, **k):
            raise RuntimeError("transient")

        ex = ToolExecutor()
        results = ex.execute_batch_concurrent(_ctx(_boom))
        assert len(results) == 1
        # Degraded to a recoverable observation string — NOT terminalized.
        assert "Error executing tool" in str(results[0].content)

    def test_sequential_operator_input_required_still_propagates(self):
        """Regression: the resumable store-and-resume signal must still
        propagate unchanged (NOT converted to a terminal halt)."""
        pending = PendingOperatorRequest(
            kind="clarify",
            prompt_text="which file?",
            original_user_message="edit it",
            created_at=0.0,
            timeout_at=0.0,
        )

        def _yield(*a, **k):
            raise OperatorInputRequired(pending)

        ex = ToolExecutor()
        with pytest.raises(OperatorInputRequired):
            ex.execute_batch_sequential(_ctx(_yield))


# ══════════════════════════════════════════════════════════════════════
# Level 1 — deny fork: structural denial terminates; Yellow continues
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


def _bare_agent(msgs: List[Dict]):
    import run_agent
    from grove.tool_executor import ToolResult

    agent = object.__new__(run_agent.AIAgent)
    agent._current_messages = msgs
    agent.model = "m"
    agent.provider = "p"
    agent.session_id = "c2a_session"
    agent._exec_called = False

    class _StubExecutor:
        def execute_batch_concurrent(self, ctx):
            return self._run(ctx)

        def execute_batch_sequential(self, ctx):
            return self._run(ctx)

        def _run(self, ctx):
            agent._exec_called = True
            return [
                ToolResult(
                    intent_id=i.call_id or "",
                    tool_name=i.tool_name,
                    tool_args=dict(i.arguments or {}),
                    success=True,
                    content="ok",
                )
                for i in ctx.intents
            ]

    class _Ctx:
        def __init__(self, intents):
            self.intents = list(intents)

    agent._tool_executor = _StubExecutor()
    agent._build_execution_context_concurrent = lambda intents, task, n: _Ctx(intents)
    agent._build_execution_context_sequential = lambda intents, task, n: _Ctx(intents)

    def _apply(results, messages, task_id):
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.intent_id, "content": r.content})

    agent._apply_execution_results_to_messages = _apply
    agent._executing_tools = False
    return agent


def _gen_one_batch(intents, final="done"):
    def gen():
        yield ToolBatchYield(intents=intents)
        yield FinalResponse(content=final)
        return {"final_response": final}
    return gen()


def _force_classify(monkeypatch, *, zone, matched_rule):
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(zone=zone, matched_rule=matched_rule, source="test"),
    )


class TestLevel1DenyFork:
    def test_red_deny_terminates(self, monkeypatch):
        # GRV-005 §VI (kaizen-voice B1) supersedes the C2a red-deny path: a RED
        # halt is now a workflow RESOLUTION, not a disposition — it never consults
        # the four-choice sovereign prompt. With no operator-facing RED menu in
        # B1 (GATE-DARK), the default headless resolution is Cancel, terminating
        # the turn via the DISTINCT provenance trigger ``red_workflow_cancel``
        # (not ``red_sovereign``). The structural-termination guarantee is intact.
        _force_classify(monkeypatch, zone="red", matched_rule="forced_red")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
        agent._run_turn_generator = lambda **kw: _gen_one_batch(intents)
        # The sovereign handler is injected to prove RED does NOT consult it.
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            d.dispatch_turn(agent, user_message="hi")
        assert exc_info.value.context.trigger == "red_workflow_cancel"
        assert agent._exec_called is False

    # NOTE (kaizen-voice B2): the former ``test_deny_hard_terminates`` was
    # removed. ``deny_hard`` was produced ONLY by the red-zone strike counter in
    # ``_handle_andon_halt`` (Sprint 32 Phase 3a). B2 purged that inert counter
    # (RED never reaches ``_handle_andon_halt`` post-§VI fork) and the deny
    # branch's ``deny_hard`` disjunct, so ``deny_hard`` is no longer a reachable
    # disposition in the dispatch path. The ``deny_hard`` HaltTrigger / renderer
    # branch still exist and are covered by ``test_kaizen_voice_halt_event.py``.

    def test_quarantine_deny_terminates(self, monkeypatch):
        # A quarantined .andon invocation: matched_rule carries ".andon".
        # Even at Yellow zone, declining it is structural (don't run unapproved
        # code) → terminal with the quarantine trigger.
        _force_classify(
            monkeypatch, zone="yellow", matched_rule="skill.quarantine.andon",
        )
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
        agent._run_turn_generator = lambda **kw: _gen_one_batch(intents)
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            d.dispatch_turn(agent, user_message="hi")
        assert exc_info.value.context.trigger == "quarantine"

    def test_yellow_deny_continues_collaboratively(self, monkeypatch):
        """An ordinary Yellow decline keeps the soft-observation path: the turn
        continues, no terminal halt, no resume."""
        _force_classify(monkeypatch, zone="yellow", matched_rule="forced_yellow")
        msgs: List[Dict] = []
        agent = _bare_agent(msgs)
        intents = [ToolIntent(tool_name="memory", arguments={}, call_id="c1")]
        agent._run_turn_generator = lambda **kw: _gen_one_batch(intents, final="recovered")
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        result = d.dispatch_turn(agent, user_message="hi")
        assert result["final_response"] == "recovered"
        # A denial tool message was injected (collaborative), turn not terminated.
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert tool_msgs and "declined" in tool_msgs[0]["content"].lower()


class TestDispatchTurnOutcome:
    def test_terminal_halt_records_governance_terminated_outcome(self, monkeypatch):
        """dispatch_turn labels the ledger outcome distinctly (not 'error') and
        re-raises the terminal signal to the surface."""
        _force_classify(monkeypatch, zone="red", matched_rule="forced_red")
        agent = _bare_agent([])
        intents = [ToolIntent(tool_name="terminal", arguments={}, call_id="c1")]
        agent._run_turn_generator = lambda **kw: _gen_one_batch(intents)
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        outcomes = []
        orig = d._write_intent_record
        monkeypatch.setattr(
            d, "_write_intent_record",
            lambda agent_, outcome=None, **k: outcomes.append(outcome),
        )
        with pytest.raises(TerminalGovernanceHalt):
            d.dispatch_turn(agent, user_message="hi")
        assert "governance_terminated" in outcomes
        assert "error" not in outcomes
