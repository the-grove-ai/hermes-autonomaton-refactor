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
from grove.intents import FinalResponse, Observation, ToolIntent


# ── _extract_tool_intents ─────────────────────────────────────────────────


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
        observations = yield intents_batch
        # Assert the consumer sent us back a list of Observations
        assert isinstance(observations, list)
        assert all(isinstance(o, Observation) for o in observations)
        yield FinalResponse(content=str(result.get("final_response") or ""))
        return result
    return gen()


class TestRunConversationWrapper:
    """The in-process wrapper drives _run_turn_generator. Verifies the
    drive loop returns StopIteration.value as the legacy result dict and
    invokes _execute_tool_calls on yielded batches."""

    def _bare_agent_with_state(self, msgs: List[Dict]):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = msgs
        agent._current_effective_task_id = "task_t"
        agent._current_api_call_count = 1
        # Stub _execute_tool_calls so we can assert it was called and
        # observe its mutation of messages (mirrors the legacy contract).
        agent._execute_tool_calls_called_with = None

        def _stub_execute(asst, messages, task_id, api_n):
            agent._execute_tool_calls_called_with = {
                "asst": asst, "messages": messages,
                "task_id": task_id, "api_n": api_n,
            }
            # Mirror the real method's side effect: append tool messages
            # for each tool_call in asst.
            tool_calls = (asst.get("tool_calls") if isinstance(asst, dict) else []) or []
            for tc in tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": f"result-for-{tc.get('id', '')}",
                })
        agent._execute_tool_calls = _stub_execute
        # Stub _run_turn_generator to return the synthetic generator.
        return agent

    def test_wrapper_returns_stop_iteration_value(self, monkeypatch: pytest.MonkeyPatch):
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
        }
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        expected_result = {"final_response": "ok", "messages": msgs}
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, expected_result)
        )
        import run_agent
        # Call the real wrapper method by binding
        result = run_agent.AIAgent.run_conversation(agent, user_message="hi")
        assert result == expected_result

    def test_wrapper_executes_yielded_batch_via_execute_tool_calls(self):
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
        }
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        import run_agent
        run_agent.AIAgent.run_conversation(agent, user_message="hi")
        # The stub recorded that _execute_tool_calls was called with
        # the stashed state.
        assert agent._execute_tool_calls_called_with is not None
        assert agent._execute_tool_calls_called_with["task_id"] == "task_t"
        # The stub appended a tool message for c1.
        assert any(
            m.get("tool_call_id") == "c1" for m in msgs
        )

    def test_wrapper_builds_observations_from_appended_tool_messages(self):
        # The wrapper packages an Observation for each intent whose
        # call_id matches a freshly-appended tool message. The
        # synthetic generator's assertion (intents → Observations
        # round-trip) verifies the packaging shape.
        msgs: List[Dict] = []
        agent = self._bare_agent_with_state(msgs)
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "t1", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "t2", "arguments": "{}"}},
            ],
        }
        intents = [
            ToolIntent(tool_name="t1", arguments={}, call_id="c1"),
            ToolIntent(tool_name="t2", arguments={}, call_id="c2"),
        ]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "ok"})
        )
        import run_agent
        run_agent.AIAgent.run_conversation(agent, user_message="hi")
        # The stub appended one tool message per intent; the wrapper
        # should have built two Observations matching them by call_id.
        tool_msg_ids = [
            m.get("tool_call_id") for m in msgs if m.get("role") == "tool"
        ]
        assert "c1" in tool_msg_ids
        assert "c2" in tool_msg_ids


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
        agent._current_assistant_message = {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
        }
        agent._current_messages = msgs
        agent._current_effective_task_id = "task_t"
        agent._current_api_call_count = 1
        agent._exec_called = False

        def _stub_execute(asst, messages, task_id, api_n):
            agent._exec_called = True
            for tc in (asst.get("tool_calls") or []):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": "dispatcher-result",
                })
        agent._execute_tool_calls = _stub_execute
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
        assert captured["already_routed"] is True


# ── Protocol round-trip ───────────────────────────────────────────────────


class TestProtocolRoundTrip:
    """End-to-end shape check: the synthetic generator asserts the
    consumer sends back ``List[Observation]`` for each yielded
    ``List[ToolIntent]``. If the wrapper or Dispatcher fails to
    package observations correctly, the generator's internal asserts
    raise and the test fails."""

    def test_wrapper_round_trip(self):
        # The legacy wrapper does NOT run zone classification (per
        # Phase 3/4 design: classification is added to the Dispatcher's
        # path; the wrapper preserves legacy unzoned behavior via
        # _execute_tool_calls). So no classifier patch is needed here.
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
        # wrapper sent List[Observation] back correctly.
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
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = msgs
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._exec_called = False

        def _exec(asst, messages, task_id, api_n):
            agent._exec_called = True
        agent._execute_tool_calls = _exec
        agent.model = "claude-sonnet-4-6"
        agent.provider = "anthropic"
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

    def test_red_intent_halts_batch_without_executing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Patch classifier to flag this specific batch as Red.
        from grove import zones as _zones
        from grove.zones import ZoneResult

        def _red(action):
            return ZoneResult(
                zone="red", matched_rule="test_red_rule",
                source="test_force_red",
            )
        monkeypatch.setattr(_zones, "classify", _red)

        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [ToolIntent(tool_name="memory", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "unreachable"})
        )
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hi")
        # Halt: execution was bypassed; result carries halt metadata.
        assert agent._exec_called is False
        assert "andon_halt" in result
        assert result["andon_halt"]["zone"] == "red"
        assert result["andon_halt"]["matched_rule"] == "test_red_rule"
        assert result["completed"] is False
        assert result["turn_exit_reason"] == "andon_halt"

    def test_yellow_intent_also_halts_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Per Phase 4 scope, BOTH Yellow and Red halt the batch.
        # Phase 5 differentiates dispositions (Red blocks; Yellow asks).
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
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hi")
        assert agent._exec_called is False
        assert result["andon_halt"]["zone"] == "yellow"

    def test_first_red_in_mixed_batch_halts_whole_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # D6 lock: per-batch scoping. Green + Red batch → whole batch halts.
        # The triggering intent is the first non-Green one.
        from grove import zones as _zones
        from grove.zones import ZoneResult

        def _by_tool(action: str):
            # Synthetic: tool.read → green, tool.write → red
            if action == "tool.read":
                return ZoneResult(zone="green", matched_rule=action, source="t")
            if action == "tool.write":
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
        d = Dispatcher()
        result = d.dispatch_turn(agent, user_message="hi")
        # Halt fires; triggering intent is the Red one at index 1.
        assert result["andon_halt"]["zone"] == "red"
        assert result["andon_halt"]["triggering_index"] == 1
        assert result["andon_halt"]["triggering_intent"]["tool_name"] == "write"
        # The full batch zone results are surfaced for Phase 5's UX:
        assert len(result["andon_halt"]["zone_results"]) == 2
        assert result["andon_halt"]["zone_results"][0]["zone"] == "green"
        assert result["andon_halt"]["zone_results"][1]["zone"] == "red"
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

    def test_andon_halt_closes_generator_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When AndonHalt fires, the Dispatcher must close() the generator
        # so its finally blocks run (clears agent._current_* stash).
        # Phase 5 replaces close() with disposition routing.
        from grove import zones as _zones
        from grove.zones import ZoneResult

        monkeypatch.setattr(
            _zones, "classify",
            lambda action: ZoneResult(
                zone="red", matched_rule="r", source="test",
            ),
        )
        msgs: List[Dict] = []
        agent = self._bare_agent_for_batch(msgs)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        finally_ran = {"flag": False}

        def gen():
            try:
                yield intents
                yield FinalResponse(content="unreachable")
            finally:
                finally_ran["flag"] = True
            return {"final_response": "unreachable"}
        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher()
        d.dispatch_turn(agent, user_message="hi")
        # The generator's finally block ran — confirming close() was called.
        assert finally_ran["flag"] is True


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
