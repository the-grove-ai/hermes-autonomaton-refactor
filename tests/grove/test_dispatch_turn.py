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

    def test_dispatch_turn_returns_generator_result(self):
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

    def test_dispatch_turn_forwards_kwargs_to_generator(self):
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

    def test_dispatcher_round_trip(self):
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
