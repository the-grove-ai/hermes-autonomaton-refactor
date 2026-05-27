"""Tests for Sprint 28 Phase 3 — Dispatcher writes IntentRecords.

Covers the three terminal sites the Dispatcher writes from:

* ``FinalResponse`` → outcome="pending" (Phase 4 finalizes to
  success/correction at next turn start; the Implicit Success Sweep
  finalizes orphans on a future Dispatcher init).
* ``Drop`` disposition → outcome="drop" (terminal).
* Generator exception → outcome="error" (terminal).

Also covers the Implicit Success Sweep at Dispatcher construction, the
per-turn state lifecycle (turn_id monotonic, classification captured,
tools_yielded accumulated), the idempotent-write contract (error
after FinalResponse does NOT double-write), and the AIAgent injection
path that wires ``get_store()`` into the lazy Dispatcher singleton.

The synthetic generator pattern mirrors tests/grove/test_dispatch_turn.py
so this file exercises only the Sprint 28 surface, not LLM behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from grove import intent_store as _intent_store_mod
from grove.classify import ClassificationResult
from grove.dispatcher import Dispatcher
from grove.intent_store import IntentRecord, IntentStore
from grove.intents import FinalResponse, Observation, ToolIntent


# ── Test helpers ──────────────────────────────────────────────────────────


def _synthetic_generator(
    intents_batch: Optional[List[ToolIntent]],
    result: Dict[str, Any],
    *,
    final_text: str = "ok",
):
    """Yield one batch (or nothing) and then a FinalResponse.

    When ``intents_batch`` is empty/None, the generator skips straight
    to FinalResponse — useful for exercising the success terminal with
    no tools yielded.
    """
    def gen():
        if intents_batch:
            obs = yield intents_batch
            assert isinstance(obs, list)
            assert all(isinstance(o, Observation) for o in obs)
        yield FinalResponse(content=final_text)
        return result
    return gen()


def _raising_generator(exc: BaseException):
    """Yield once, then raise on the next send."""
    def gen():
        yield [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        raise exc
        yield  # unreachable; satisfies generator typing
    return gen()


def _bare_agent_with_exec(msgs: List[Dict]):
    """Build a minimal AIAgent stand-in with the state the Dispatcher
    reads at Green-path execution."""
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent._current_assistant_message = {
        "role": "assistant",
        "tool_calls": [
            {"id": "c1", "function": {"name": "t", "arguments": "{}"}}
        ],
    }
    agent._current_messages = msgs
    agent._current_effective_task_id = "task_t"
    agent._current_api_call_count = 3
    agent.session_id = "test-session"
    agent.model = "claude-sonnet-4-6"

    def _stub_execute(asst, messages, task_id, api_n):
        for tc in (asst.get("tool_calls") or []):
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": "stub-result",
            })
    agent._execute_tool_calls = _stub_execute
    return agent


def _patch_classifier_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the zone classifier to return Green for any input.

    The Dispatcher's intent-yield classification fires inside the drive
    loop; this stub keeps the test focused on the IntentRecord wiring
    rather than zone semantics.
    """
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone="green", matched_rule=action, source="test_force_green",
        ),
    )


def _set_current_classification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    intent_class: str = "code_generation",
    register_class: str = "technical",
    complexity_signal: str = "moderate",
    confidence: float = 0.9,
    goal_alignment: Optional[str] = "direct",
) -> ClassificationResult:
    """Pre-populate grove.providers._last_classification so the
    Dispatcher's capture step finds a value to snapshot."""
    classification = ClassificationResult(
        intent_class=intent_class,
        pattern_hash="abc123",
        confidence=confidence,
        register_class=register_class,
        complexity_signal=complexity_signal,
        goal_alignment=goal_alignment,
    )
    from grove import providers as _providers_mod
    monkeypatch.setattr(_providers_mod, "_last_classification", classification)
    return classification


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


# ── Dispatcher construction + sweep ───────────────────────────────────────


class TestDispatcherIntentStoreInit:
    def test_default_intent_store_is_none(self):
        # Legacy / test Dispatchers that pass no kwargs skip the
        # Phase 3 wiring entirely — no sweep, no writes.
        d = Dispatcher()
        assert d._intent_store is None

    def test_intent_store_kwarg_is_held(self, tmp_store: IntentStore):
        d = Dispatcher(intent_store=tmp_store)
        assert d._intent_store is tmp_store

    def test_implicit_success_sweep_runs_at_init(
        self, tmp_path: Path,
    ):
        # Seed a stale pending record (timestamp older than the default
        # 60-min threshold), construct the Dispatcher, verify the sweep
        # finalized it as success.
        store = IntentStore(store_path=tmp_path / "records.jsonl")
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        store.append(IntentRecord(
            timestamp=old_ts,
            session_id="prev-session",
            turn_id="prev-session#1",
            user_message_stem="orphaned turn",
            pattern_hash="ph-prev",
            intent_class="analysis",
            register_class="technical",
            complexity_signal="moderate",
            confidence=0.7,
            outcome="pending",
        ))
        # Construction triggers sweep.
        Dispatcher(intent_store=store)
        latest = list(store.latest_by_turn())
        assert len(latest) == 1
        assert latest[0].turn_id == "prev-session#1"
        assert latest[0].outcome == "success"

    def test_sweep_does_not_run_when_store_is_none(self, tmp_store):
        # The sweep only fires when a store is provided. Construct
        # without the kwarg and verify the underlying file is untouched
        # (we proxy this by pre-seeding then checking it survives).
        tmp_store.append(IntentRecord(
            timestamp=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
            session_id="s",
            turn_id="s#1",
            user_message_stem="m",
            pattern_hash="ph",
            intent_class="conversation",
            register_class="casual",
            complexity_signal="simple",
            confidence=0.5,
            outcome="pending",
        ))
        Dispatcher()  # no intent_store
        # The seeded pending is still pending — nothing swept it.
        latest = list(tmp_store.latest_by_turn())
        assert latest[0].outcome == "pending"


# ── Terminal writes ───────────────────────────────────────────────────────


class TestTerminalFinalResponseWritesPending:
    def test_writes_pending_record_on_final_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch, intent_class="analysis")
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        intents = [ToolIntent(tool_name="read_file", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                intents, {"final_response": "done"}, final_text="done",
            )
        )
        d = Dispatcher(intent_store=tmp_store)
        d.dispatch_turn(agent, user_message="look at the file")

        records = list(tmp_store.records())
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "pending"
        assert rec.intent_class == "analysis"
        assert rec.session_id == "test-session"
        assert rec.turn_id.startswith("test-session#")
        assert rec.user_message_stem == "look at the file"
        assert rec.tools_yielded == ("read_file",)
        assert rec.model_used == "claude-sonnet-4-6"
        assert rec.final_response_chars == 4  # len("done")
        assert rec.api_calls == 3
        assert rec.duration_ms >= 0.0

    def test_classification_captured_includes_goal_alignment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(
            monkeypatch, goal_alignment="direct", confidence=0.95,
        )
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "x"}, final_text="x",
            )
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(
            agent, user_message="ship it",
        )
        rec = next(iter(tmp_store.records()))
        assert rec.goal_alignment == "direct"
        assert rec.confidence == 0.95

    def test_unclassified_turn_uses_sentinel_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # classify_for_routing returned None (Sprint 12 graceful tier).
        # The record still writes with sentinel intent/pattern values so
        # the feed remains complete.
        _patch_classifier_green(monkeypatch)
        from grove import providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_classification", None)
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "x"}, final_text="x",
            )
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(
            agent, user_message="hi",
        )
        rec = next(iter(tmp_store.records()))
        assert rec.intent_class == "unknown"
        assert rec.pattern_hash == "unclassified"
        assert rec.confidence == 0.0
        assert rec.goal_alignment is None


class TestTerminalDropWritesDrop:
    def test_writes_drop_record_on_drop_disposition(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # Force a Red zone classification so the batch halts; inject a
        # Drop disposition so the Dispatcher takes the drop terminal.
        from grove import zones as _zones
        from grove.zones import ZoneResult
        monkeypatch.setattr(
            _zones, "classify",
            lambda action: ZoneResult(
                zone="red", matched_rule="r", source="sovereign",
            ),
        )
        _set_current_classification(monkeypatch)
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "u"})
        )
        d = Dispatcher(
            intent_store=tmp_store,
            sovereign_prompt_handler=lambda halt: "drop",
        )
        d.dispatch_turn(agent, user_message="rm -rf /")

        records = list(tmp_store.records())
        # The sweep may have already finalized nothing (fresh store);
        # only the drop terminal write should be present.
        drops = [r for r in records if r.outcome == "drop"]
        assert len(drops) == 1
        assert drops[0].session_id == "test-session"
        assert drops[0].user_message_stem == "rm -rf /"


class TestTerminalExceptionWritesError:
    def test_writes_error_record_when_generator_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        agent._run_turn_generator = (
            lambda **kw: _raising_generator(RuntimeError("boom"))
        )
        d = Dispatcher(intent_store=tmp_store)
        with pytest.raises(RuntimeError, match="boom"):
            d.dispatch_turn(agent, user_message="trigger error")

        records = list(tmp_store.records())
        errors = [r for r in records if r.outcome == "error"]
        assert len(errors) == 1
        assert errors[0].user_message_stem == "trigger error"

    def test_error_after_final_response_does_not_double_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # The outcome_written flag must short-circuit a second write
        # when an exception fires after FinalResponse already wrote
        # "pending" — the operator should not see two records for one
        # turn with conflicting outcomes.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)

        def gen():
            yield FinalResponse(content="ok")
            raise RuntimeError("post-final boom")

        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: gen()
        d = Dispatcher(intent_store=tmp_store)
        with pytest.raises(RuntimeError, match="post-final boom"):
            d.dispatch_turn(agent, user_message="hi")

        records = list(tmp_store.records())
        # Exactly one record — the pending from FinalResponse. The
        # exception handler's _write_intent_record call short-circuited
        # via outcome_written=True.
        assert len(records) == 1
        assert records[0].outcome == "pending"


# ── Per-turn state lifecycle ──────────────────────────────────────────────


class TestPerTurnStateLifecycle:
    def test_turn_ids_are_monotonic_within_a_dispatcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])

        for i in range(3):
            agent._run_turn_generator = (
                lambda **kw: _synthetic_generator(
                    None, {"final_response": "x"}, final_text="x",
                )
            )
            d.dispatch_turn(agent, user_message=f"turn {i}")

        records = list(tmp_store.records())
        turn_ids = [r.turn_id for r in records]
        assert turn_ids == [
            "test-session#1", "test-session#2", "test-session#3",
        ]

    def test_tools_yielded_accumulates_across_batches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # Multi-batch turn: two ToolIntent yields followed by
        # FinalResponse. The record's tools_yielded captures every
        # tool name across batches.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)

        def gen():
            yield [ToolIntent(tool_name="read_file", arguments={}, call_id="c1")]
            yield [
                ToolIntent(tool_name="search_files", arguments={}, call_id="c2"),
                ToolIntent(tool_name="web_search", arguments={}, call_id="c3"),
            ]
            yield FinalResponse(content="done")

        # Two-batch flow needs the agent's execution state per yield.
        msgs: List[Dict] = []
        agent = _bare_agent_with_exec(msgs)
        agent._run_turn_generator = lambda **kw: gen()
        Dispatcher(intent_store=tmp_store).dispatch_turn(
            agent, user_message="multi-step",
        )
        rec = next(iter(tmp_store.records()))
        assert rec.tools_yielded == (
            "read_file", "search_files", "web_search",
        )


# ── AIAgent integration ──────────────────────────────────────────────────


class TestAgentInjection:
    def test_get_or_create_dispatcher_injects_default_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        # AIAgent's lazy singleton constructs Dispatcher with the
        # module-default store. With the per-test GROVE_HOME isolation
        # in tests/conftest.py, that resolves to a tmp-path store and
        # the production file is untouched.
        from run_agent import AIAgent

        class _StubAgent:
            pass
        stub = _StubAgent()
        stub._dispatcher_singleton = None
        stub._sovereign_prompt_handler = None

        dispatcher = AIAgent._get_or_create_dispatcher(stub)
        assert dispatcher._intent_store is not None
        # The store path lives under the per-test GROVE_HOME tempdir,
        # not the operator's ~/.grove path.
        assert "intent_records.jsonl" in str(dispatcher._intent_store.path)
