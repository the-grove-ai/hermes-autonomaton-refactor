"""execute-code-meta-surface-containment-v1 Phase-2 Change 3 — attempt-stamp.

  * field-schema of the escalated_write_attempt stamp (7 fields, aligned to
    containment_violation + IntentRecord conventions);
  * RED path: stamp filed at STORE time (_store_pending_red_proposal);
  * YELLOW path: stamp filed BEFORE the executor — present even when _execute_fn
    raises (stamp-before-exec ordering).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.governance_halt import TerminalGovernanceHalt
from grove.intents import FinalResponse, ToolBatchYield, ToolIntent
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    import grove.red_pending_store as rps
    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture(autouse=True)
def _capture_queue_writes(monkeypatch):
    from grove.eval import proposal_queue as pq
    monkeypatch.setattr(pq, "append", lambda p: None)
    yield


@pytest.fixture
def cap_ledger(monkeypatch):
    """Capture every KaizenLedger.record call the dispatcher makes."""
    from grove import kaizen_ledger as kl
    events: list[tuple[str, dict]] = []
    orig = kl.KaizenLedger.record

    def _cap(self, event_type: str, **fields: Any):
        events.append((event_type, fields))
        return orig(self, event_type, **fields)

    monkeypatch.setattr(kl.KaizenLedger, "record", _cap)
    return events


class _CapLedger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, **fields: Any) -> None:
        self.events.append((event_type, fields))


class _FakeGen:
    def send(self, obs: Any) -> Any:
        return obs


def _term(cmd: str) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="c1")


# ── field-schema ─────────────────────────────────────────────────────────────

class TestStampFieldSchema:
    _FIELDS = {"actor", "surface", "write_target", "write_class",
               "pattern_key", "resolution", "grant_id"}

    def test_unresolved_writer_target_is_dynamic(self):
        d = Dispatcher()
        cap = _CapLedger()
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:git:argv:ab")
        d._emit_shell_write_attempt_stamp(
            _term("git reset --hard"), zr, "store_pending_approval", cap
        )
        assert len(cap.events) == 1
        ev, fields = cap.events[0]
        assert ev == "escalated_write_attempt"
        assert set(fields) == self._FIELDS
        assert fields["actor"] == "agent"
        assert fields["write_target"] == "UNRESOLVED_DYNAMIC"
        assert fields["write_class"] == "UNRESOLVED_WRITER"
        assert fields["resolution"] == "store_pending_approval"
        assert fields["grant_id"] is None

    def test_bucket2_target_is_command(self):
        d = Dispatcher()
        cap = _CapLedger()
        zr = SimpleNamespace(zone="yellow", pattern_key="cmd:echo:argv:cd")
        d._emit_shell_write_attempt_stamp(_term("echo x > /tmp/f"), zr, "once", cap)
        ev, fields = cap.events[0]
        assert fields["write_target"] == "echo x > /tmp/f"
        assert fields["write_class"] == "cmd"
        assert fields["resolution"] == "once"

    def test_non_shell_intent_is_noop(self):
        d = Dispatcher()
        cap = _CapLedger()
        zr = SimpleNamespace(zone="yellow", pattern_key="x")
        d._emit_shell_write_attempt_stamp(
            ToolIntent(tool_name="write_file", arguments={}, call_id="c"),
            zr, "once", cap,
        )
        assert cap.events == []

    def test_none_ledger_is_noop(self):
        d = Dispatcher()
        zr = SimpleNamespace(zone="red", pattern_key="UNRESOLVED_WRITER:x")
        # must not raise
        d._emit_shell_write_attempt_stamp(_term("git x"), zr, "r", None)


# ── RED path: stamp at store time ────────────────────────────────────────────

class TestRedStampAtStoreTime:
    def _to_halt(self, d: Dispatcher, intent: ToolIntent) -> AndonResolutionHalt:
        try:
            d._classify_intents_batch_and_halt_or_raise([intent])
        except AndonResolutionHalt as halt:
            return halt
        raise AssertionError("expected AndonResolutionHalt")

    def test_red_unresolved_writer_stamped_at_store(self):
        d = Dispatcher()  # reachable → STORE_PENDING
        halt = self._to_halt(d, _term("git reset --hard origin/main"))
        cap = _CapLedger()
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt, ledger=cap)
        stamps = [f for e, f in cap.events if e == "escalated_write_attempt"]
        assert len(stamps) == 1
        assert stamps[0]["resolution"] == "store_pending_approval"
        assert stamps[0]["write_target"] == "UNRESOLVED_DYNAMIC"
        assert len(d._red_pending_store) == 1  # stored (stamp is pre-store-return)


# ── YELLOW path: stamp BEFORE the executor (survives an executor raise) ───────

class TestYellowStampBeforeExec:
    def _agent(self, msgs):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._current_messages = msgs
        agent.model = "m"
        agent.provider = "p"
        agent.api_key = ""
        agent.base_url = ""
        agent.session_id = "test_stamp_before_exec"
        agent._exec_called = False

        class _RaisingExecutor:
            def execute_batch_concurrent(self, ctx):
                return self._run(ctx)

            def execute_batch_sequential(self, ctx):
                return self._run(ctx)

            def _run(self, ctx):
                agent._exec_called = True
                raise RuntimeError("executor blew up AFTER the stamp")

        class _Ctx:
            def __init__(self, intents):
                self.intents = list(intents)

        agent._tool_executor = _RaisingExecutor()
        agent._build_execution_context_concurrent = lambda i, t, n: _Ctx(i)
        agent._build_execution_context_sequential = lambda i, t, n: _Ctx(i)
        agent._apply_execution_results_to_messages = lambda r, m, t: None
        agent._executing_tools = False
        return agent

    def test_stamp_present_even_when_executor_raises(self, cap_ledger):
        # A yellow terminal write, approved "once", whose executor RAISES: the
        # attempt-stamp must already be in the ledger (filed before _execute_fn).
        agent = self._agent([])
        intents = [_term("echo x > /tmp/scratch.txt")]  # bucket-2 YELLOW

        def gen():
            yield ToolBatchYield(intents=intents)
            yield FinalResponse(content="done")
            return {"final_response": "done"}

        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(sovereign_prompt_handler=lambda halt: "once")  # approve

        with pytest.raises(RuntimeError):
            d.dispatch_turn(agent, user_message="hi")

        assert agent._exec_called is True  # the executor DID run (and raised)
        stamps = [f for e, f in cap_ledger if e == "escalated_write_attempt"]
        assert len(stamps) >= 1
        assert stamps[0]["resolution"] == "once"
        assert stamps[0]["actor"] == "agent"
