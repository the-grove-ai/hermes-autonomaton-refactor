"""Tests for the interrupt system.

Run with: python -m pytest tests/test_interrupt.py -v
"""

import queue
import threading
import time
import pytest


# ---------------------------------------------------------------------------
# Unit tests: shared interrupt module
# ---------------------------------------------------------------------------

class TestInterruptModule:
    """Tests for tools/interrupt.py"""

    def test_set_and_check(self):
        from tools.interrupt import set_interrupt, is_interrupted
        set_interrupt(False)
        assert not is_interrupted()

        set_interrupt(True)
        assert is_interrupted()

        set_interrupt(False)
        assert not is_interrupted()

    def test_thread_safety(self):
        """Set from one thread targeting another thread's ident."""
        from tools.interrupt import set_interrupt, is_interrupted, _interrupted_threads, _lock
        set_interrupt(False)
        # Clear any stale thread idents left by prior tests in this worker.
        with _lock:
            _interrupted_threads.clear()

        seen = {"value": False}

        def _checker():
            while not is_interrupted():
                time.sleep(0.01)
            seen["value"] = True

        t = threading.Thread(target=_checker, daemon=True)
        t.start()

        time.sleep(0.05)
        assert not seen["value"]

        # Target the checker thread's ident so it sees the interrupt
        set_interrupt(True, thread_id=t.ident)
        t.join(timeout=1)
        assert seen["value"]

        set_interrupt(False, thread_id=t.ident)


# ---------------------------------------------------------------------------
# Unit tests: pre-tool interrupt check
# ---------------------------------------------------------------------------

class TestPreToolCheck:
    """Verify that _execute_tool_calls skips all tools when interrupted."""

    def test_all_tools_skipped_when_interrupted(self):
        """Mock an interrupted agent and verify no tools execute."""
        from unittest.mock import MagicMock, patch

        # Build a fake assistant_message with 3 tool calls
        tc1 = MagicMock()
        tc1.id = "tc_1"
        tc1.function.name = "terminal"
        tc1.function.arguments = '{"command": "rm -rf /"}'

        tc2 = MagicMock()
        tc2.id = "tc_2"
        tc2.function.name = "terminal"
        tc2.function.arguments = '{"command": "echo hello"}'

        tc3 = MagicMock()
        tc3.id = "tc_3"
        tc3.function.name = "web_search"
        tc3.function.arguments = '{"query": "test"}'

        assistant_msg = MagicMock()
        assistant_msg.tool_calls = [tc1, tc2, tc3]

        messages = []

        # Create a minimal mock agent with _interrupt_requested = True
        agent = MagicMock()
        agent._interrupt_requested = True
        agent.log_prefix = ""
        agent._persist_session = MagicMock()

        # Sprint 31 Phase 2.1: the agent shims are deleted. The
        # pre-flight interrupt-skip behavior under test now lives in
        # grove.tool_executor.ToolExecutor.execute_batch_concurrent
        # (and execute_batch_sequential — symmetric pre-flight check).
        # tests/run_agent/test_concurrent_interrupt.py covers the
        # executor's behavior directly; this test verifies the same
        # observable effect (cancellation messages for every tool)
        # via a minimal executor invocation that mirrors what the
        # dispatcher does in production.
        from grove.intents import ToolIntent
        from grove.tool_executor import (
            ToolExecutor, ExecutionContext, ExecutorConfig,
            ObservabilityCallbacks, SideEffectCallbacks,
        )

        class _AlreadyInterrupted:
            def is_set(self): return True
            def set_for_thread(self, tid): pass
            def clear_for_thread(self, tid): pass

        executor = ToolExecutor()
        intents = [
            ToolIntent(tool_name=tc.function.name, arguments={}, call_id=tc.id)
            for tc in assistant_msg.tool_calls
        ]
        ctx = ExecutionContext(
            intents=intents,
            tool_registry=None,
            callbacks=ObservabilityCallbacks(),
            side_effects=SideEffectCallbacks(invoke_tool=lambda *a, **kw: '{}'),
            interrupt=_AlreadyInterrupted(),
            config=ExecutorConfig(quiet_mode=True),
        )
        results = executor.execute_batch_sequential(ctx)
        for r in results:
            messages.append({
                "role": "tool",
                "name": r.tool_name,
                "content": r.content,
                "tool_call_id": r.intent_id,
            })

        # All 3 should be skipped
        assert len(messages) == 3
        for msg in messages:
            assert msg["role"] == "tool"
            assert "cancelled" in msg["content"].lower() or "interrupted" in msg["content"].lower()

        # No actual tool handlers should have been called
        # (handle_function_call should NOT have been invoked)


# ---------------------------------------------------------------------------
# Unit tests: message combining
# ---------------------------------------------------------------------------

class TestMessageCombining:
    """Verify multiple interrupt messages are joined."""

    def test_cli_interrupt_queue_drain(self):
        """Simulate draining multiple messages from the interrupt queue."""
        q = queue.Queue()
        q.put("Stop!")
        q.put("Don't delete anything")
        q.put("Show me what you were going to delete instead")

        parts = []
        while not q.empty():
            try:
                msg = q.get_nowait()
                if msg:
                    parts.append(msg)
            except queue.Empty:
                break

        combined = "\n".join(parts)
        assert "Stop!" in combined
        assert "Don't delete anything" in combined
        assert "Show me what you were going to delete instead" in combined
        assert combined.count("\n") == 2

    def test_gateway_pending_messages_append(self):
        """Simulate gateway _pending_messages append logic."""
        pending = {}
        key = "agent:main:telegram:dm"

        # First message
        if key in pending:
            pending[key] += "\n" + "Stop!"
        else:
            pending[key] = "Stop!"

        # Second message
        if key in pending:
            pending[key] += "\n" + "Do something else instead"
        else:
            pending[key] = "Do something else instead"

        assert pending[key] == "Stop!\nDo something else instead"


# ---------------------------------------------------------------------------
# Integration tests (require local terminal)
# ---------------------------------------------------------------------------

class TestSIGKILLEscalation:
    """Test that SIGTERM-resistant processes get SIGKILL'd."""

    @pytest.mark.skipif(
        not __import__("shutil").which("bash"),
        reason="Requires bash"
    )
    def test_sigterm_trap_killed_within_2s(self):
        """A process that traps SIGTERM should be SIGKILL'd after 1s grace."""
        from tools.interrupt import set_interrupt
        from tools.environments.local import LocalEnvironment

        set_interrupt(False)
        env = LocalEnvironment(cwd="/tmp", timeout=30)

        # Start execution in a thread, interrupt after 0.5s
        result_holder = {"value": None}

        def _run():
            result_holder["value"] = env.execute(
                "trap '' TERM; sleep 60",
                timeout=30,
            )

        t = threading.Thread(target=_run)
        t.start()

        time.sleep(0.5)
        set_interrupt(True, thread_id=t.ident)

        t.join(timeout=5)
        set_interrupt(False, thread_id=t.ident)

        assert result_holder["value"] is not None
        assert result_holder["value"]["returncode"] == 130
        assert "interrupted" in result_holder["value"]["output"].lower()


# ---------------------------------------------------------------------------
# Manual smoke test checklist (not automated)
# ---------------------------------------------------------------------------

SMOKE_TESTS = """
Manual Smoke Test Checklist:

1. CLI: Run `hermes`, ask it to `sleep 30` in terminal, type "stop" + Enter.
   Expected: command dies within 2s, agent responds to "stop".

2. CLI: Ask it to extract content from 5 URLs, type interrupt mid-way.
   Expected: remaining URLs are skipped, partial results returned.

3. Gateway (Telegram): Send a long task, then send "Stop".
   Expected: agent stops and responds acknowledging the stop.

4. Gateway (Telegram): Send "Stop" then "Do X instead" rapidly.
   Expected: both messages appear as the next prompt (joined by newline).

5. CLI: Start a task that generates 3+ tool calls in one batch.
   Type interrupt during the first tool call.
   Expected: only 1 tool executes, remaining are skipped.
"""
