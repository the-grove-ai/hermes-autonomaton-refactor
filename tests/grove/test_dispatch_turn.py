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

    def test_red_intent_halts_batch_and_routes_through_drop_disposition(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Phase 5: AndonHalt is caught and routed through the Sovereign
        # Prompt. With a "drop" disposition injected, the dispatcher
        # closes the generator, flushes volatile state, and returns a
        # drop result. The executor is NEVER called.
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
        # Inject a Drop disposition handler so the test is deterministic
        # (the default TTY handler would call input() and block).
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "drop")
        result = d.dispatch_turn(agent, user_message="hi")
        assert agent._exec_called is False
        assert result["turn_exit_reason"] == "andon_drop"
        assert result["andon_disposition"]["disposition"] == "drop"
        assert result["andon_disposition"]["zone"] == "red"
        assert result["andon_disposition"]["matched_rule"] == "test_red_rule"
        assert result["completed"] is False
        # § IX(3): volatile state flushed on Drop.
        assert result["messages"] == []

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
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "drop")
        result = d.dispatch_turn(agent, user_message="hi")
        assert agent._exec_called is False
        assert result["andon_disposition"]["zone"] == "yellow"
        assert result["andon_disposition"]["disposition"] == "drop"

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
        # Capture the AndonHalt to verify per-batch context. Inject a
        # disposition handler that records the halt then returns "drop".
        captured_halt = {}

        def _capturing_prompt(halt):
            captured_halt["halt"] = halt
            return "drop"

        d = Dispatcher(sovereign_prompt_handler=_capturing_prompt)
        result = d.dispatch_turn(agent, user_message="hi")
        # The halt the prompt saw carried both zone results so Phase 5
        # UX can show the full batch context.
        halt = captured_halt["halt"]
        assert halt.triggering_index == 1
        assert halt.intents[1].tool_name == "write"
        assert len(halt.zone_results) == 2
        assert halt.zone_results[0].zone == "green"
        assert halt.zone_results[1].zone == "red"
        # Result reflects the Drop disposition for the whole batch.
        assert result["andon_disposition"]["disposition"] == "drop"
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

    def test_drop_disposition_closes_generator_cleanly(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When Drop fires, the dispatch_turn outer `finally` runs
        # gen.close(). The generator's own finally block clears
        # self._current_* and raises through any open contexts. The
        # GeneratorExit propagates cleanly because the A6 audit
        # confirmed zero bare-except / except-BaseException sites in
        # the legacy code path.
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
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "drop")
        d.dispatch_turn(agent, user_message="hi")
        # The generator's finally block ran — confirming gen.close() was
        # invoked (either by the disposition flow's drop branch or by
        # dispatch_turn's outer finally; both are guarantees).
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


class TestPhase5SkipDisposition:
    """Skip disposition: Dispatcher injects denial Observations and appends
    paired denial tool messages so the LLM context stays consistent. The
    generator resumes; the Agent re-reasons or pivots."""

    def _bare_agent(self, msgs):
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
        agent.model = "m"
        agent.provider = "p"
        agent.session_id = "test_skip_session"
        return agent

    def test_skip_injects_denial_observations(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Capture the Observations that get sent back to the generator;
        # Skip flow must yield denial Observations with success=False.
        _force_red(monkeypatch)
        # Redirect pending_andon dir to tmp so we don't pollute ~/.grove.
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        captured_send: dict = {}

        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]

        def gen():
            received = yield intents
            captured_send["observations"] = received
            yield FinalResponse(content="after_skip")
            return {"final_response": "after_skip"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "skip")
        result = d.dispatch_turn(agent, user_message="hi")

        # The generator received Observations with success=False
        observations = captured_send["observations"]
        assert len(observations) == 1
        assert observations[0].success is False
        assert observations[0].intent_id == "c1"
        assert "skipped" in observations[0].value.lower()
        assert observations[0].metadata.get("disposition") == "skip"
        # Generator resumed and completed
        assert result["final_response"] == "after_skip"
        # Executor was NOT called (the batch was skipped, not executed)
        assert agent._exec_called is False

    def test_skip_appends_denial_tool_messages_for_llm_consistency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Every assistant tool_call needs a paired tool message in the
        # context; otherwise the next LLM call errors. Skip must
        # append denial tool messages with the right tool_call_ids.
        _force_red(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        intents = [
            ToolIntent(tool_name="a", arguments={}, call_id="c1"),
            ToolIntent(tool_name="b", arguments={}, call_id="c2"),
        ]

        def gen():
            yield intents
            yield FinalResponse(content="ok")
            return {"final_response": "ok"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "skip")
        d.dispatch_turn(agent, user_message="hi")

        # Two denial tool messages were appended, one per intent
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
        # All denial messages mention "skipped"
        assert all("skipped" in m["content"].lower() for m in tool_msgs)


class TestPhase5DropDisposition:
    """Drop disposition: Dispatcher closes the generator (raising
    GeneratorExit at the yield); volatile turn state is flushed;
    persistent state stays at its pre-turn snapshot per § IX(3)."""

    def _bare_agent(self, msgs):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_assistant_message = {"role": "assistant"}
        agent._current_messages = msgs
        agent._current_effective_task_id = "t"
        agent._current_api_call_count = 1
        agent._exec_called = False
        agent._execute_tool_calls = (
            lambda asst, messages, task_id, api_n:
            setattr(agent, "_exec_called", True)
        )
        agent.model = "m"
        agent.provider = "p"
        agent.session_id = "test_drop_session"
        return agent

    def test_drop_flushes_volatile_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _force_red(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "unreachable"})
        )
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "drop")
        result = d.dispatch_turn(agent, user_message="hi")

        # § IX(3): volatile messages flushed
        assert result["messages"] == []
        # The Drop result carries explicit disposition metadata
        assert result["andon_disposition"]["disposition"] == "drop"
        assert result["turn_exit_reason"] == "andon_drop"
        # Executor never ran
        assert agent._exec_called is False

    def test_drop_does_not_swallow_generator_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # A6 mitigation: gen.close() must propagate GeneratorExit
        # cleanly through the generator's body. If a try/except
        # Exception block in the body swallowed it, the generator
        # would leak state. This test wraps the yield in `except
        # Exception` (which DOES NOT catch GeneratorExit per Python 3)
        # to prove the propagation works.
        _force_red(monkeypatch)
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

        msgs = []
        agent = self._bare_agent(msgs)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        cleanup_ran = {"flag": False}

        def gen():
            try:
                try:
                    yield intents
                except Exception:
                    # This MUST NOT catch GeneratorExit per Python 3
                    # exception hierarchy. If it did, cleanup_ran would
                    # not fire because the except block would swallow
                    # the close() and the generator would continue.
                    pass
                yield FinalResponse(content="unreachable")
            finally:
                cleanup_ran["flag"] = True
            return {"final_response": "unreachable"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "drop")
        d.dispatch_turn(agent, user_message="hi")
        # The generator's outer finally ran, proving GeneratorExit
        # propagated through the inner except Exception.
        assert cleanup_ran["flag"] is True


class TestPhase5PendingAndonMarker:
    """D3 process-restart resilience: pending_andon markers persist a
    structural trail of paused turns so a killed-mid-prompt process
    can be acknowledged on restart."""

    def test_marker_written_before_prompt_and_cleared_after(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _force_red(monkeypatch)
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
            assert payload["halt"]["zone"] == "red"
            assert len(payload["intents"]) == 1
            return "drop"

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
        # leave a stale trail.
        _force_red(monkeypatch)
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

    def test_default_prompt_returns_skip_on_skip_input(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        # Stub builtins.input to return "skip"
        monkeypatch.setattr("builtins.input", lambda prompt="": "skip")
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "skip"

    def test_default_prompt_returns_drop_on_drop_input(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        monkeypatch.setattr("builtins.input", lambda prompt="": "2")
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "drop"

    def test_default_prompt_drops_on_eof(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # EOFError → safest default is Drop (don't auto-skip).
        from grove.dispatcher import AndonHalt, _default_sovereign_prompt
        from grove.zones import ZoneResult

        def _eof(prompt=""):
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        intents = [ToolIntent(tool_name="x", arguments={}, call_id="c1")]
        zr = [ZoneResult(zone="red", matched_rule="r", source="s")]
        halt = AndonHalt(intents=intents, zone_results=zr, triggering_index=0)
        assert _default_sovereign_prompt(halt) == "drop"
