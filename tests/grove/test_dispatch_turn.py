"""Tests for the Sprint 26 Phase 3 generator-shaped turn dispatch.

Covers ``AIAgent._extract_tool_intents``, the in-process
``run_conversation`` wrapper's drive loop, and
``Dispatcher.dispatch_turn``. Each test constructs a synthetic
generator (not a real LLM turn) so we can verify the drive logic
without depending on a live model API.

These tests focus on the contract the Phase 3 inversion introduces:
the Agent yields intents per GRV-005 § IV; the consumer (legacy
wrapper or Dispatcher) executes and sends ``Observation`` back. The
extensive behavior of the actual LLM loop is left to the existing
integration tests that exercise ``run_conversation`` against mocked
provider clients.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext
from grove.intents import ToolBatchYield, FinalResponse, Observation, ToolIntent
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests._runtime_ctx import MOCK_RUNTIME_CTX


# Sprint 26 Phase 6 — every test in this file constructs Dispatchers
# which now eagerly create per-session Kaizen Ledger files. Redirect
# the substrate home to tmp so no test pollutes ~/.grove/.kaizen_ledger.



@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


# ── _extract_tool_intents ─────────────────────────────────────────────────


def _phase2_executor_stub(agent):
    """Sprint 31 Phase 2 migration: provide the minimum agent surface
    the dispatcher's new direct-executor path expects.

    The legacy Phase 1 tests stubbed ``agent._execute_tool_calls`` as
    a no-op lambda. Phase 2 routes the dispatcher through
    ``agent._tool_executor.execute_batch_concurrent/sequential`` plus
    ``agent._build_execution_context_*`` and
    ``agent._apply_execution_results_to_messages``. This helper
    wires all four with stubs that mimic the prior legacy stub's
    observable effect: append one tool message per intent and
    surface execution via ``agent._exec_called``.
    """
    from grove.tool_executor import ToolResult

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
                    content="stub-result",
                )
                for i in ctx.intents
            ]

    class _MinimalCtx:
        def __init__(self, intents):
            self.intents = list(intents)

    agent._tool_executor = _StubExecutor()
    agent._build_execution_context_concurrent = (
        lambda intents, task, n: _MinimalCtx(intents)
    )
    agent._build_execution_context_sequential = (
        lambda intents, task, n: _MinimalCtx(intents)
    )

    def _apply(results, messages, task_id):
        for r in results:
            messages.append({
                "role": "tool",
                "tool_call_id": r.intent_id,
                "content": r.content,
            })

    agent._apply_execution_results_to_messages = _apply
    agent._executing_tools = False
    return agent


class TestExtractToolIntents:
    """The helper that converts an LLM's assistant_message tool_calls
    into a list of ``ToolIntent`` instances. Handles both dict-style
    and object-style assistant messages since both shapes appear in
    the codebase's LLM provider adapters."""

    def _bare_agent(self):
        import run_agent
        return object.__new__(run_agent.AIAgent)

    def test_empty_message_returns_empty_list(self):
        agent = self._bare_agent()
        assert agent._extract_tool_intents({"tool_calls": []}) == []
        assert agent._extract_tool_intents({}) == []

    def test_dict_form_single_tool(self):
        agent = self._bare_agent()
        msg = {
            "tool_calls": [
                {
                    "id": "call_001",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command": "ls"}',
                    },
                }
            ]
        }
        intents = agent._extract_tool_intents(msg)
        assert len(intents) == 1
        assert isinstance(intents[0], ToolIntent)
        assert intents[0].tool_name == "terminal"
        assert intents[0].arguments == {"command": "ls"}
        assert intents[0].call_id == "call_001"

    def test_dict_form_multiple_tools(self):
        agent = self._bare_agent()
        msg = {
            "tool_calls": [
                {"id": "a", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "b", "function": {"name": "t2", "arguments": '{"x": 1}'}},
            ]
        }
        intents = agent._extract_tool_intents(msg)
        assert [i.call_id for i in intents] == ["a", "b"]
        assert [i.tool_name for i in intents] == ["t1", "t2"]
        assert intents[1].arguments == {"x": 1}

    def test_object_form(self):
        agent = self._bare_agent()
        # Simulate the SDK-object shape (attr-based access)
        class _Fn:
            name = "memory"
            arguments = '{"key": "k", "value": "v"}'
        class _TC:
            id = "call_999"
            function = _Fn()
        class _Msg:
            tool_calls = [_TC()]
        intents = agent._extract_tool_intents(_Msg())
        assert len(intents) == 1
        assert intents[0].tool_name == "memory"
        assert intents[0].arguments == {"key": "k", "value": "v"}
        assert intents[0].call_id == "call_999"

    def test_malformed_arguments_default_to_empty(self):
        # Bad JSON arguments → empty dict, no exception propagation.
        agent = self._bare_agent()
        msg = {
            "tool_calls": [
                {"id": "x", "function": {"name": "t", "arguments": "not-json"}}
            ]
        }
        intents = agent._extract_tool_intents(msg)
        assert intents[0].arguments == {}

    def test_no_tool_calls_attr_returns_empty(self):
        # Object-style with no tool_calls attribute
        agent = self._bare_agent()
        class _Msg:
            pass
        assert agent._extract_tool_intents(_Msg()) == []


# ── Synthetic generator-drive tests ───────────────────────────────────────


def _synthetic_generator(intents_batch: List[ToolIntent], result: Dict[str, Any]):
    """Build a synthetic _run_turn_generator that yields one batch of
    ToolIntents, then a FinalResponse, then returns the result dict.

    Used by the wrapper/Dispatcher drive tests to verify the drive loop
    semantics without exercising a real LLM turn.
    """
    def gen():
        observations = yield ToolBatchYield(intents=intents_batch)
        # Assert the consumer sent us back a list of Observations
        assert isinstance(observations, list)
        assert all(isinstance(o, Observation) for o in observations)
        yield FinalResponse(content=str(result.get("final_response") or ""))
        return result
    return gen()


class TestPhase7RunConversationDelegation:
    """Phase 7: AIAgent.run_conversation no longer drives the generator
    in-process. It delegates to a lazily-created Dispatcher singleton
    held on the Agent. The previous in-process drive loop was deleted;
    all execution now flows through Dispatcher.dispatch_turn under
    GRV-005 § II/III authority."""

    def _bare_agent_with_state(self, msgs: List[Dict]):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._runtime_ctx = MOCK_RUNTIME_CTX
        agent._current_messages = msgs
        agent.session_id = "phase7_delegation_session"
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        _phase2_executor_stub(agent)
        return agent

    def test_run_conversation_creates_dispatcher_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The Agent lazily creates a Dispatcher on first call. The
        # same instance is reused on subsequent calls.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        import run_agent
        run_agent.AIAgent.run_conversation(agent, user_message="hi")
        first_dispatcher = agent._dispatcher_singleton
        assert first_dispatcher is not None

        # Second call reuses the same dispatcher
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        run_agent.AIAgent.run_conversation(agent, user_message="hi again")
        assert agent._dispatcher_singleton is first_dispatcher

    def test_run_conversation_returns_dispatcher_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The delegation returns whatever dispatch_turn returns.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        expected = {"final_response": "delegated-ok"}
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, expected)
        )
        import run_agent
        result = run_agent.AIAgent.run_conversation(agent, user_message="hi")
        assert result == expected

    def test_run_conversation_routes_through_dispatcher_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Phase 7 verification: the wrapper goes through the Dispatcher's
        # classification gate, so a Red-zoned tool halts the turn (where the
        # pre-Phase-7 wrapper would have executed).
        # GRV-005 §VI (kaizen-voice B1): a RED halt is a workflow RESOLUTION, not
        # a disposition — it never consults the four-choice sovereign prompt. With
        # no operator-facing RED menu in B1 (GATE-DARK), the default headless
        # resolution is Cancel, which terminates the turn via
        # TerminalGovernanceHalt with the DISTINCT provenance trigger
        # ``red_workflow_cancel`` (not ``red_sovereign``). The terminal signal
        # propagates uncaught through run_conversation to the surface.
        from grove.governance_halt import TerminalGovernanceHalt
        _force_red(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "u"})
        )
        # red-action-store-pending-v1 Phase A: RED store-pending is now
        # reachability-gated. This test exercises the CANCEL/terminal mechanic, so
        # it runs on an UNREACHABLE surface (``non_interactive_deny_handler``
        # installed as the sovereign handler → ``_is_operator_reachable()`` False).
        # The handler is still injected to prove RED does NOT consult it — the
        # red_resolution_handler default (headless Cancel) governs instead.
        agent._dispatcher_singleton = Dispatcher(
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        import run_agent
        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            run_agent.AIAgent.run_conversation(agent, user_message="hi")
        assert exc_info.value.context.trigger == "red_workflow_cancel"
        # Executor was never reached — the turn terminated, no improvisation.
        assert agent._exec_called is False


# ── Dispatcher.dispatch_turn ──────────────────────────────────────────────


def _patch_classifier_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Phase 4 zone classifier to return Green for any input.

    Used by Phase 3 dispatch tests that don't care about classification
    — they exercise the drive loop, not the zone gate. Phase 4 tests
    use real classification or explicit fixture rules.
    """
    from grove import zones as _zones
    from grove.zones import ZoneResult

    def _always_green(action):
        return ZoneResult(
            zone="green", matched_rule=action, source="test_force_green",
        )

    monkeypatch.setattr(_zones, "classify", _always_green)


class TestDispatcherDispatchTurn:
    """``Dispatcher.dispatch_turn(agent, user_message, **kwargs)`` is
    the GRV-005-conformant turn entry point. Returns the same shape
    the legacy wrapper does; in Phase 3 MVP both paths delegate
    execution to ``agent._execute_tool_calls``."""

    def _bare_agent_with_state(self, msgs: List[Dict]):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_messages = msgs
        agent.session_id = "test-session"
        agent.model = "claude-sonnet-4-6"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        _phase2_executor_stub(agent)
        return agent

    def test_dispatch_turn_returns_generator_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "dispatched"})
        )
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hello")
        assert result == {"final_response": "dispatched"}
        assert agent._exec_called is True

    def test_dispatch_turn_forwards_kwargs_to_generator(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        captured = {}

        def gen_factory(**kw):
            captured.update(kw)
            return _synthetic_generator(intents, {"final_response": "ok"})

        agent._run_turn_generator = gen_factory
        d = Dispatcher()
        d.dispatch_turn(
            agent,
            user_message="hi",
            task_id="task_X",
            already_routed=True,
        )
        assert captured["user_message"] == "hi"
        assert captured["task_id"] == "task_X"
        # Sprint 35 — ``already_routed`` is consumed by the Dispatcher's
        # pre-construction classify path and popped before forwarding
        # to the generator. The generator no longer accepts the kwarg
        # (``_maybe_route_for_turn`` is deleted).
        assert "already_routed" not in captured


# ── Protocol round-trip ───────────────────────────────────────────────────


class TestProtocolRoundTrip:
    """End-to-end shape check: the synthetic generator asserts the
    consumer sends back ``List[Observation]`` for each yielded
    ``List[ToolIntent]``. If the wrapper or Dispatcher fails to
    package observations correctly, the generator's internal asserts
    raise and the test fails."""

    def test_wrapper_round_trip(self, monkeypatch: pytest.MonkeyPatch):
        # Phase 7: the wrapper delegates to Dispatcher.dispatch_turn,
        # which means classification fires. With synthetic intents this
        # would default-yellow halt — force green to exercise the
        # round-trip protocol shape.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._runtime_ctx = MOCK_RUNTIME_CTX
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
        }
        agent._current_messages = msgs
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent.session_id = "round_trip_session"
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""

        def _exec(asst, messages, task_id, api_n):
            for tc in (asst.get("tool_calls") or []):
                messages.append({
                    "role": "tool", "tool_call_id": tc.get("id"),
                    "content": "x",
                })
        agent._execute_tool_calls = _exec
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        result = run_agent.AIAgent.run_conversation(agent, user_message="hi")
        # No assertion failure in the synthetic generator means the
        # delegation sent List[Observation] back correctly.
        assert result["final_response"] == "ok"

    def test_dispatcher_round_trip(self, monkeypatch: pytest.MonkeyPatch):
        # Dispatcher path: Phase 4 runs zone classification. Force the
        # classifier green for this round-trip test so we exercise the
        # drive loop, not the zone gate (the gate has its own tests).
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
        }
        agent._current_messages = msgs
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.model = "m"
        agent.provider = "p"
        agent.api_key = ""
        agent.base_url = ""

        def _exec(asst, messages, task_id, api_n):
            for tc in (asst.get("tool_calls") or []):
                messages.append({
                    "role": "tool", "tool_call_id": tc.get("id"),
                    "content": "x",
                })
        agent._execute_tool_calls = _exec
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hi")
        assert result["final_response"] == "ok"


# ── Phase 4 — tool-zone classification at intent-yield ─────────────────────


class TestPhase4ZoneClassification:
    """The Dispatcher classifies every ToolIntent in the yielded batch
    before executing. Per the D6 lock, the batch is the disposition
    unit — a single Yellow/Red intent halts the whole batch via
    AndonHalt. Phase 4 fires the gate; Phase 5 adds Skip/Drop UX."""

    def _bare_agent_for_batch(self, msgs: List[Dict]):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_messages = msgs
        agent.model = "claude-sonnet-4-6"
        agent.provider = "anthropic"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        _phase2_executor_stub(agent)
        return agent

    def test_green_batch_executes_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [ToolIntent(tool_name="memory", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hi")
        # No halt: execution proceeded and the generator completed.
        assert "andon_halt" not in result
        assert agent._exec_called is True
        assert result["final_response"] == "ok"

    def test_yellow_intent_also_halts_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Per Phase 4 scope, BOTH Yellow and Red halt the batch.
        # Phase 5 differentiates dispositions via operator choice; this
        # test injects "drop" to verify the gate fires for Yellow.
        from grove import zones as _zones
        from grove.zones import ZoneResult

        monkeypatch.setattr(
            _zones, "classify",
            lambda action: ZoneResult(
                zone="yellow", matched_rule="y", source="test",
            ),
        )
        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [ToolIntent(tool_name="memory", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "unreachable"})
        )
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        d.dispatch_turn(agent, user_message="hi")
        # Halt fired → executor never invoked on the halted batch
        # (deny injects denial Observations and the agent continues).
        assert agent._exec_called is False

    def test_first_red_in_mixed_batch_halts_whole_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # D6 lock: per-batch scoping. Green + Red batch → whole batch halts.
        # The triggering intent is the first non-Green one.
        from grove import zones as _zones
        from grove.zones import ZoneResult

        def _by_tool(action: str):
            # Synthetic: read → green, write → red. The classifier
            # receives the bare tool_name (no ``tool.`` prefix) per
            # zones.schema.yaml::tool_zones convention.
            if action == "read":
                return ZoneResult(zone="green", matched_rule=action, source="t")
            if action == "write":
                return ZoneResult(zone="red", matched_rule=action, source="t")
            return ZoneResult(zone="green", matched_rule=action, source="t")
        monkeypatch.setattr(_zones, "classify", _by_tool)

        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [
            ToolIntent(tool_name="read", arguments={}, call_id="c1"),
            ToolIntent(tool_name="write", arguments={}, call_id="c2"),
        ]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "unreachable"})
        )
        # Capture the AndonResolutionHalt to verify per-batch context. GRV-005
        # §VI: a RED batch is resolved via the red_resolution_handler (NOT the
        # four-choice sovereign prompt). Inject a handler that records the halt
        # then resolves to Cancel.
        captured_halt = {}

        def _capturing_resolution(halt):
            captured_halt["halt"] = halt
            return "cancel"

        # §VI — Cancel resolves the structurally-blocked RED batch by terminating
        # the turn via TerminalGovernanceHalt(red_workflow_cancel).
        # red-action-store-pending-v1 Phase A: RED store-pending is now
        # reachability-gated; the ``_capturing_resolution`` red_resolution_handler
        # only fires on an UNREACHABLE surface. Install
        # ``non_interactive_deny_handler`` so ``_is_operator_reachable()`` is False,
        # preserving the batch-halt context assertions and terminal-cancel path.
        from grove.governance_halt import TerminalGovernanceHalt
        d = Dispatcher(
            red_resolution_handler=_capturing_resolution,
            sovereign_prompt_handler=non_interactive_deny_handler,
        )
        with pytest.raises(TerminalGovernanceHalt):
            d.dispatch_turn(agent, user_message="hi")
        # The halt the prompt saw carried both zone results so Phase 5
        # UX can show the full batch context.
        halt = captured_halt["halt"]
        assert halt.triggering_index == 1
        assert halt.intents[1].tool_name == "write"
        assert len(halt.zone_results) == 2
        assert halt.zone_results[0].zone == "green"
        assert halt.zone_results[1].zone == "red"
        # deny disposition for the whole batch — executor bypassed.
        assert agent._exec_called is False

    def test_terminal_command_routes_through_command_classifier(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Terminal-style intents with a 'command' arg go through the
        # Sprint 06a command_to_action + Sprint 22 hierarchical
        # classifier, not the generic classify(action) path.
        from grove import dispatch as _gd
        from grove.zones import ZoneResult

        recorded: dict = {}

        def _stub_classify_command(command, env_type="local", *, tool_id=None):
            recorded["command"] = command
            recorded["tool_id"] = tool_id
            return ZoneResult(
                zone="green", matched_rule="command.execute.echo",
                source="test_command_route",
            )
        monkeypatch.setattr(_gd, "classify_command", _stub_classify_command)

        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [
            ToolIntent(
                tool_name="terminal",
                arguments={"command": "echo hi"},
                call_id="c1",
            )
        ]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="hi")
        # Confirm the command-classifier was the one consulted.
        assert recorded["command"] == "echo hi"
        assert recorded["tool_id"] == "terminal"

class TestAndonHaltException:
    """The AndonHalt exception carries the full batch context Phase 5
    will need to build the Sovereign Prompt."""

    def test_andon_halt_exposes_triggering_metadata(self):
        from grove.dispatcher import AndonHalt
        from grove.zones import ZoneResult

        intents = [
            ToolIntent(tool_name="a", arguments={}, call_id="c0"),
            ToolIntent(tool_name="b", arguments={"x": 1}, call_id="c1"),
        ]
        zone_results = [
            ZoneResult(zone="green", matched_rule="g", source="auto_approve"),
            ZoneResult(
                zone="red", matched_rule="b-rule", source="sovereign",
                reason="hard-coded denial",
            ),
        ]
        halt = AndonHalt(intents=intents, zone_results=zone_results,
                         triggering_index=1)
        assert halt.zone == "red"
        assert halt.matched_rule == "b-rule"
        assert halt.source == "sovereign"
        assert halt.reason == "hard-coded denial"
        assert halt.triggering_index == 1
        # Message includes tool name + zone for log diagnostics
        assert "'b'" in str(halt) or "b" in str(halt)
        assert "red" in str(halt)


# ── Phase 5 — Mid-execution Andon disposition ─────────────────────────────


def _force_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every classify() call to return Red."""
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone="red", matched_rule="forced_red", source="test",
        ),
    )


def _force_yellow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every classify() call to return Yellow.

    GRV-010 C2a — a Yellow operator decline stays collaborative: the soft
    denial Observation is injected and the turn continues. (A RED decline now
    terminates the turn via TerminalGovernanceHalt — see test_c2a_fail_loud.py.)
    The soft-deny-path tests below use Yellow to exercise the collaborative
    branch that survives C2a.
    """
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone="yellow", matched_rule="forced_yellow", source="test",
        ),
    )


class TestPhase5SkipDisposition:
    """Skip disposition: Dispatcher injects denial Observations and appends
    paired denial tool messages so the LLM context stays consistent. The
    generator resumes; the Agent re-reasons or pivots."""

    def _bare_agent(self, msgs):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_messages = msgs
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "test_skip_session"
        _phase2_executor_stub(agent)
        return agent

    def test_skip_injects_denial_observations(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Capture the Observations that get sent back to the generator;
        # a Yellow soft-deny must yield denial Observations with success=False
        # and continue the turn (GRV-010 C2a: collaborative Yellow path).
        _force_yellow(monkeypatch)
        # Redirect pending_andon dir to tmp so we don't pollute ~/.grove.
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        captured_send: dict = {}

        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]

        def gen():
            received = yield ToolBatchYield(intents=intents)
            captured_send["observations"] = received
            yield FinalResponse(content="after_skip")
            return {"final_response": "after_skip"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        result = d.dispatch_turn(agent, user_message="hi")

        # The generator received Observations with success=False
        observations = captured_send["observations"]
        assert len(observations) == 1
        assert observations[0].success is False
        assert observations[0].intent_id == "c1"
        # Sprint 57 — operator-friendly wording, no governance vocab.
        assert "declined" in observations[0].value.lower()
        assert "andon" not in observations[0].value.lower()
        assert observations[0].metadata.get("disposition") == "deny"
        # Generator resumed and completed
        assert result["final_response"] == "after_skip"
        # Executor was NOT called (the batch was denied, not executed)
        assert agent._exec_called is False

    def test_skip_appends_denial_tool_messages_for_llm_consistency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Every assistant tool_call needs a paired tool message in the
        # context; otherwise the next LLM call errors. A Yellow soft-deny must
        # append denial tool messages with the right tool_call_ids and continue.
        _force_yellow(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        intents = [
            ToolIntent(tool_name="a", arguments={}, call_id="c1"),
            ToolIntent(tool_name="b", arguments={}, call_id="c2"),
        ]

        def gen():
            yield ToolBatchYield(intents=intents)
            yield FinalResponse(content="ok")
            return {"final_response": "ok"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        d.dispatch_turn(agent, user_message="hi")

        # Two denial tool messages were appended, one per intent
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
        # All denial messages read as a decline-to-run (Sprint 57 wording).
        assert all("declined" in m["content"].lower() for m in tool_msgs)


class TestPhase5PendingAndonMarker:
    """D3 process-restart resilience: pending_andon markers persist a
    structural trail of paused turns so a killed-mid-prompt process
    can be acknowledged on restart."""

    def test_marker_written_before_prompt_and_cleared_after(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # GRV-010 C2a — use Yellow so the turn completes normally (a RED decline
        # now terminates via TerminalGovernanceHalt). The pending_andon marker
        # is zone-agnostic; this test covers its write/clear lifecycle.
        _force_yellow(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = []
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._execute_tool_calls = lambda *a, **k: None
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "marker_test_session_123"

        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "u"})
        )

        # During the prompt, the marker file must exist; after disposition
        # completes, the marker must be cleared. The prompt handler
        # asserts the marker presence as a side effect.
        marker_dir = tmp_path / ".pending_andon"
        marker_path = marker_dir / "marker_test_session_123.json"

        def _checking_prompt(halt):
            # Marker must exist at this point
            assert marker_path.exists(), (
                f"pending_andon marker missing during prompt: {marker_path}"
            )
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            assert payload["session_id"] == "marker_test_session_123"
            assert payload["halt"]["zone"] == "yellow"
            assert len(payload["intents"]) == 1
            return "deny"

        import json
        d = Dispatcher(sovereign_prompt_handler=_checking_prompt)
        d.dispatch_turn(agent, user_message="hi")
        # Marker cleared after disposition completed
        assert not marker_path.exists()

    def test_marker_cleared_even_if_prompt_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The clear runs in a finally — if the prompt handler raises,
        # the marker should still be cleared so a buggy handler doesn't
        # leave a stale trail. GRV-005 §VI: the pending_andon marker is a
        # YELLOW disposition-path concern (written/cleared inside
        # _handle_andon_halt); a RED halt is now a workflow resolution that
        # never reaches that path, so this finally-clear test exercises YELLOW.
        _force_yellow(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = []
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._execute_tool_calls = lambda *a, **k: None
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "buggy_prompt_session"

        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "u"})
        )

        def _buggy_prompt(halt):
            raise RuntimeError("simulated prompt crash")

        marker_path = tmp_path / ".pending_andon" / "buggy_prompt_session.json"
        d = Dispatcher(sovereign_prompt_handler=_buggy_prompt)
        with pytest.raises(RuntimeError, match="simulated prompt crash"):
            d.dispatch_turn(agent, user_message="hi")
        # Marker cleared by the finally block even though the prompt raised
        assert not marker_path.exists()

    def test_check_pending_andon_surfaces_existing_markers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Operator starting fresh sees any markers left from a prior
        # killed session.
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        marker_dir = tmp_path / ".pending_andon"
        marker_dir.mkdir(parents=True, exist_ok=True)
        import json
        (marker_dir / "prior_session.json").write_text(
            json.dumps({"session_id": "prior_session", "halt": {"zone": "red"}}),
            encoding="utf-8",
        )
        markers = Dispatcher.check_pending_andon()
        assert len(markers) == 1
        assert markers[0]["session_id"] == "prior_session"
        assert "_marker_path" in markers[0]

    def test_check_pending_andon_empty_when_no_markers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        assert Dispatcher.check_pending_andon() == []


class TestPhase5SovereignPromptDefault:
    """The default TTY Sovereign Prompt is the fallback when no handler
    is injected. These tests verify its structure without actually
    requiring TTY input."""

    def test_default_prompt_returns_deny_on_deny_input(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Sprint 32 v1.1: the typed alias "deny" resolves to the
        # deny disposition (v1.0 "skip" semantic).
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        monkeypatch.setattr("builtins.input", lambda prompt="": "deny")
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "deny"

    def test_default_prompt_returns_once_on_choice_one(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Sprint 32 v1.1: "1" maps to the once disposition (execute).
        # v1.0 "drop" is no longer offered through the prompt; the
        # legacy alias remains accepted at the dispatcher level for
        # test fixtures that exercise the drop pipeline directly.
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        monkeypatch.setattr("builtins.input", lambda prompt="": "1")
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "once"

    def test_default_prompt_denies_on_eof(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Sprint 32 v1.1: EOFError → safest default is Deny (fail-
        # safe; absence-of-input means "block this action"). v1.0
        # defaulted to Drop.
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        def _eof(prompt=""):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "deny"


# ── Phase 6 — Kaizen Ledger wiring + Tier Override ────────────────────────


class TestPhase6KaizenLedgerWiring:
    """The Dispatcher writes structured events to a per-session Kaizen
    Ledger at every observable turn moment, per GRV-005 § IX(4)'s
    foreground/background split. The Agent's reasoning loop never
    reads from the ledger (no mid-stream injection)."""

    def _bare_agent(self, msgs):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = msgs
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._execute_tool_calls = lambda *a, **k: None
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "phase6_session"
        return agent

    def test_green_batch_no_longer_records_tool_batch_executed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # GRV-009 E3 C4 — tool_batch_executed is RETIRED; the unified capability
        # feed (per-invocation at AIAgent._invoke_tool) is sole-path for
        # invocation usage. The dispatcher emits no such ledger event anymore.
        # final_response is still recorded.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent(msgs)
        intents = [
            ToolIntent(tool_name="x", arguments={}, call_id="c1"),
            ToolIntent(tool_name="y", arguments={}, call_id="c2"),
        ]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="hi")

        ledger = d.ledger_for(agent)
        assert ledger is not None
        assert ledger.events_by_type("tool_batch_executed") == []  # retired (C4)
        # Final response still recorded.
        final_events = ledger.events_by_type("final_response")
        assert len(final_events) == 1
        assert final_events[0]["content_length"] == len("ok")

    def test_andon_halt_skip_records_disposition_then_executes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Yellow + soft-deny produces: andon_halt, andon_disposition(deny),
        # final_response (the generator continues past the collaborative deny).
        # GRV-010 C2a: a RED deny would instead terminate the turn — this test
        # covers the surviving Yellow recover-and-continue path.
        _force_yellow(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent(msgs)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "recovered"})
        )
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "deny")
        d.dispatch_turn(agent, user_message="hi")

        ledger = d.ledger_for(agent)
        types = [e["event_type"] for e in ledger.events()]
        # No tool_batch_executed (skip bypassed execution); no
        # turn_dropped (skip is a recovery flow, not a drop).
        assert "andon_halt" in types
        assert "andon_disposition" in types
        assert "tool_batch_executed" not in types
        assert "turn_dropped" not in types
        assert "final_response" in types
        assert ledger.events_by_type("andon_disposition")[0]["disposition"] == "deny"

    def test_approved_disposition_records_provenance_ledger_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # C0 (conformance-disarm-seal-v1) — provenance acceptance. An
        # operator-APPROVED disposition ("once" — exactly what the web
        # store-and-resume handler returns on the replay turn once the
        # operator's grant is consumed) MUST be recorded as a durable,
        # timestamped andon_disposition ledger entry. No execution proceeds
        # past Stage 04 without a logged, operator-approved verdict.
        from grove import zones as _zones
        from grove.zones import ZoneResult
        monkeypatch.setattr(
            _zones, "classify",
            lambda action: ZoneResult(zone="yellow", matched_rule="y", source="test"),
        )
        msgs: List[Dict] = []
        agent = self._bare_agent(msgs)
        _phase2_executor_stub(agent)  # yellow→once falls through to execute
        intents = [ToolIntent(tool_name="memory", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "done"})
        )
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "once")
        d.dispatch_turn(agent, user_message="hi")

        ledger = d.ledger_for(agent)
        disp = ledger.events_by_type("andon_disposition")
        assert len(disp) == 1
        entry = disp[0]
        assert entry["disposition"] == "once"         # the operator's grant
        assert entry["triggering_tool"] == "memory"   # under which action
        assert entry["timestamp"]                      # when (UTC, durable)
        # The approved action then executed (Green-path fall-through).
        assert agent._exec_called is True

    def test_ledger_persists_across_turns_in_same_session(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Two dispatch_turn calls for the same session_id append to the
        # same ledger file.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent(msgs)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="turn 1")
        d.dispatch_turn(agent, user_message="turn 2")

        ledger = d.ledger_for(agent)
        # GRV-009 E3 C4 — tool_batch_executed retired; use final_response (one
        # per turn, still emitted) to prove cross-turn ledger persistence.
        final_events = ledger.events_by_type("final_response")
        # Two turns = two final_response events on the same ledger
        assert len(final_events) == 2

    def test_ledger_isolated_per_session(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Concurrent sessions get distinct ledgers; events don't leak.
        _patch_classifier_green(monkeypatch)
        d = Dispatcher()

        for sid in ("session_a", "session_b"):
            msgs: List[Dict] = []
            agent = self._bare_agent(msgs)
            agent.session_id = sid
            intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
            agent._run_turn_generator = (
                lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
            )
            d.dispatch_turn(agent, user_message=f"hi from {sid}")

        led_a = d.ledger_for("session_a")
        led_b = d.ledger_for("session_b")
        assert led_a is not None and led_b is not None
        assert led_a is not led_b
        assert led_a.session_id == "session_a"
        assert led_b.session_id == "session_b"
        assert led_a.path != led_b.path

    def test_no_mid_stream_injection_into_agent_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Per § IX(4) "No Mid-Stream Injection": ledger writes never
        # leak into agent._current_messages.
        _patch_classifier_green(monkeypatch)
        msgs: List[Dict] = []
        agent = self._bare_agent(msgs)

        def _exec(asst, messages, task_id, api_n):
            for tc in (asst.get("tool_calls") or []):
                messages.append({
                    "role": "tool", "tool_call_id": tc.get("id"),
                    "content": "result",
                })
        agent._execute_tool_calls = _exec
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}],
        }

        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="hi")

        # The agent's messages list MUST contain only tool execution
        # responses, never any ledger metadata (latency_ms, batch_size,
        # zone classifications, etc.).
        for msg in msgs:
            assert "latency_ms" not in str(msg)
            assert "event_type" not in str(msg)


class TestPhase6TierOverride:
    """Tier Override pathway per § IX(4) — process-scoped per-session
    state the Dispatcher exposes for Sprint 27's escalation handler
    and operator-facing slash commands."""

    def test_override_tier_stores_per_session(self):
        d = Dispatcher()
        d.override_tier("session_a", "T3", reason="apex tier needed")
        d.override_tier("session_b", "T2", reason="cheaper tier requested")
        assert d.get_tier_override("session_a") == "T3"
        assert d.get_tier_override("session_b") == "T2"

    def test_get_tier_override_returns_none_when_unset(self):
        d = Dispatcher()
        assert d.get_tier_override("session_unset") is None

    def test_override_tier_writes_ledger_entry(self):
        d = Dispatcher()
        d.override_tier("session_x", "T3", reason="user requested apex")
        ledger = d.ledger_for("session_x")
        assert ledger is not None
        overrides = ledger.events_by_type("tier_override")
        assert len(overrides) == 1
        assert overrides[0]["target_tier"] == "T3"
        assert overrides[0]["reason"] == "user requested apex"

    def test_override_tier_accepts_agent_or_session_id(self):
        import run_agent
        d = Dispatcher()
        agent = object.__new__(run_agent.AIAgent)
        agent.session_id = "from_agent_session"
        d.override_tier(agent, "T3", reason="via agent")
        assert d.get_tier_override(agent) == "T3"
        assert d.get_tier_override("from_agent_session") == "T3"


# ── Phase 7 — env broadcast + acknowledge_pending_andon ──────────────────


class TestPhase7BroadcastSessionId:
    """Sprint 26 Phase 7 — GROVE_SESSION_ID env-write authority moved
    from AIAgent to Dispatcher.broadcast_session_id per GRV-005 § II/III.
    The Agent declares; the Dispatcher writes."""

    def test_broadcast_session_id_writes_env(self, monkeypatch: pytest.MonkeyPatch):
        # Start with the env unset to verify the broadcast writes it.
        monkeypatch.delenv("GROVE_SESSION_ID", raising=False)
        Dispatcher.broadcast_session_id("test_session_123")
        import os
        assert os.environ.get("GROVE_SESSION_ID") == "test_session_123"

    def test_dispatch_turn_broadcasts_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When dispatch_turn runs, GROVE_SESSION_ID is set to the
        # agent's session_id. This replaces the deleted AIAgent.__init__
        # write site (Phase 1a TODO swept).
        _patch_classifier_green(monkeypatch)
        monkeypatch.delenv("GROVE_SESSION_ID", raising=False)
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = []
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._execute_tool_calls = lambda *a, **k: None
        agent.model = "m"
        agent.provider = "p"
        # switch_model dereferences self.api_key/self.base_url on the governed
        # tier-bind (run_agent.py:3852-3853); object.__new__ skips __init__.
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "phase7_env_test"
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="hi")
        import os
        assert os.environ.get("GROVE_SESSION_ID") == "phase7_env_test"


class TestPhase7AcknowledgePendingAndon:
    """Sprint 26 Phase 7 — startup recovery hook. Acknowledges and
    discards any pending_andon markers left from prior sessions, per
    the operator-locked Option 1 (discard with notice)."""

    def test_acknowledge_returns_empty_when_no_markers(self):
        # tmp_path autouse-redirected; no markers present.
        result = Dispatcher.acknowledge_pending_andon()
        assert result == []

    def test_acknowledge_surfaces_and_deletes_markers(self, tmp_path: Path):
        # Pre-create two pending_andon markers, then acknowledge.
        marker_dir = tmp_path / ".pending_andon"
        marker_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        (marker_dir / "session_a.json").write_text(
            _json.dumps({"session_id": "a", "halt": {"zone": "red"}}),
            encoding="utf-8",
        )
        (marker_dir / "session_b.json").write_text(
            _json.dumps({"session_id": "b", "halt": {"zone": "yellow"}}),
            encoding="utf-8",
        )
        notices: List[Dict[str, Any]] = []
        result = Dispatcher.acknowledge_pending_andon(
            notice_callback=lambda m: notices.append(m),
        )
        # All markers surfaced via callback
        assert len(notices) == 2
        sids = {n["session_id"] for n in notices}
        assert sids == {"a", "b"}
        # Result mirrors what was acknowledged
        assert len(result) == 2
        # Markers deleted from disk (Option 1: Discard with notice)
        assert list(marker_dir.glob("*.json")) == []

    def test_acknowledge_notice_callback_exception_doesnt_block_cleanup(
        self, tmp_path: Path,
    ):
        # A buggy notice callback shouldn't prevent the marker from
        # being deleted — clean up the file anyway.
        marker_dir = tmp_path / ".pending_andon"
        marker_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        marker_path = marker_dir / "session_buggy.json"
        marker_path.write_text(
            _json.dumps({"session_id": "buggy", "halt": {"zone": "red"}}),
            encoding="utf-8",
        )

        def _crashing(marker):
            raise RuntimeError("notice callback exploded")

        # Should not raise — the callback failure is swallowed
        Dispatcher.acknowledge_pending_andon(notice_callback=_crashing)
        # Marker still deleted despite callback failure
        assert not marker_path.exists()

    def test_acknowledge_works_without_callback(self, tmp_path: Path):
        # No callback → markers silently acknowledged and deleted.
        marker_dir = tmp_path / ".pending_andon"
        marker_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        (marker_dir / "silent_session.json").write_text(
            _json.dumps({"session_id": "silent", "halt": {"zone": "red"}}),
            encoding="utf-8",
        )
        result = Dispatcher.acknowledge_pending_andon()
        assert len(result) == 1
        assert list(marker_dir.glob("*.json")) == []
