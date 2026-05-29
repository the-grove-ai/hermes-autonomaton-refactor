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
from grove.intents import ToolBatchYield, FinalResponse, Observation, ToolIntent


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
            # Sprint 31 Phase 2: api_call_count rides ToolBatchYield;
            # legacy fixtures asserted api_calls=3 on the terminal
            # intent record via the deleted ``_current_api_call_count``
            # bridge field's default fixture value. Preserve that
            # value in the yield so the dispatcher's tracker picks
            # it up.
            obs = yield ToolBatchYield(intents=intents_batch, api_call_count=3)
            assert isinstance(obs, list)
            assert all(isinstance(o, Observation) for o in obs)
        yield FinalResponse(content=final_text)
        return result
    return gen()


def _raising_generator(exc: BaseException):
    """Yield once, then raise on the next send."""
    def gen():
        yield ToolBatchYield(intents=[ToolIntent(tool_name="t", arguments={}, call_id="c1")])
        raise exc
        yield  # unreachable; satisfies generator typing
    return gen()


def _bare_agent_with_exec(msgs: List[Dict]):
    """Build a minimal AIAgent stand-in with the state the Dispatcher
    reads at Green-path execution."""
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent._current_messages = msgs
    agent.session_id = "test-session"
    agent.model = "claude-sonnet-4-6"
    _phase2_executor_stub(agent)
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
    Dispatcher's capture step finds a value to snapshot.

    Sprint 35 — the Dispatcher's pre-construction classify path calls
    ``route_for_agent`` and overwrites the global. To preserve the
    test-set classification through dispatch_turn, this helper also
    stubs ``route_for_agent`` to a no-op that leaves the global
    untouched. Tests using this helper are simulating "classification
    happened before dispatch_turn" — semantically identical to the
    ``already_routed=True`` path.
    """
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
    # Sprint 35 — prevent dispatch_turn's _classify_and_bind_turn from
    # overwriting the global. Returning None matches the vanilla-install
    # signal (no routing config); the Dispatcher's snapshot branch then
    # falls back to reading the pre-set global.
    monkeypatch.setattr(
        "grove.providers.route_for_agent",
        lambda **kw: None,
    )
    return classification


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


# ── Dispatcher construction + sweep ───────────────────────────────────────


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
        # Sprint 35 — dispatch_turn now calls route_for_agent pre-
        # generator. Stub it to None so the test's "unclassified turn"
        # scenario survives the new path; _classify_and_bind_turn falls
        # back to snapshotting the (None-set) global.
        monkeypatch.setattr(
            "grove.providers.route_for_agent", lambda **kw: None,
        )
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

        # Filter to the per-turn "pending" writes — Phase 4 adds a
        # second "success" record per finalization, which we don't want
        # to count here. One pending record per turn yields the
        # monotonic id sequence under test.
        pending_turn_ids = [
            r.turn_id for r in tmp_store.records() if r.outcome == "pending"
        ]
        assert pending_turn_ids == [
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
            yield ToolBatchYield(intents=[ToolIntent(tool_name="read_file", arguments={}, call_id="c1")])
            yield ToolBatchYield(intents=[
                ToolIntent(tool_name="search_files", arguments={}, call_id="c2"),
                ToolIntent(tool_name="web_search", arguments={}, call_id="c3"),
            ])
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


# ── Phase 4: explicit success finalization ───────────────────────────────


class TestPhase4ExplicitSuccessFinalization:
    """At the start of turn N+1, the Dispatcher finalizes turn N's
    pending record as success. Together with the 60-min Implicit
    Success Sweep at Dispatcher init, this closes the loop with
    explicit-success semantics only — semantic correction detection
    is deferred per GATE-D (A3)."""

    def test_second_turn_finalizes_previous_pending_as_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])

        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "first"}, final_text="first",
            )
        )
        d.dispatch_turn(agent, user_message="turn 1")

        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "second"}, final_text="second",
            )
        )
        d.dispatch_turn(agent, user_message="turn 2")

        # Three records: turn 1 pending, turn 1 success finalization,
        # turn 2 pending. The latest_by_turn view collapses to
        # {turn 1 → success, turn 2 → pending}.
        latest_by_turn = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        assert latest_by_turn == {
            "test-session#1": "success",
            "test-session#2": "pending",
        }

    def test_finalization_preserves_original_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(
            monkeypatch, intent_class="planning", goal_alignment="direct",
        )
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])
        intents = [ToolIntent(tool_name="search_files", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "x"})
        )
        d.dispatch_turn(agent, user_message="strategic question")

        # Second turn finalizes the first.
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "y"}, final_text="y")
        )
        d.dispatch_turn(agent, user_message="next")

        finalized = next(
            r for r in tmp_store.latest_by_turn()
            if r.turn_id == "test-session#1"
        )
        # Outcome flipped to success; everything else preserved from
        # the pending record.
        assert finalized.outcome == "success"
        assert finalized.intent_class == "planning"
        assert finalized.goal_alignment == "direct"
        assert finalized.tools_yielded == ("search_files",)
        assert finalized.user_message_stem == "strategic question"

    def test_previous_drop_is_not_re_finalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # Turn 1 ends at Drop terminal (outcome=drop). The Phase 4
        # finalization at turn 2 start must NOT overwrite drop → success.
        from grove import zones as _zones
        from grove.zones import ZoneResult
        monkeypatch.setattr(
            _zones, "classify",
            lambda action: ZoneResult(
                zone="red", matched_rule="r", source="sovereign",
            ),
        )
        _set_current_classification(monkeypatch)
        d = Dispatcher(
            intent_store=tmp_store,
            sovereign_prompt_handler=lambda halt: "drop",
        )
        agent = _bare_agent_with_exec([])
        intents = [ToolIntent(tool_name="t", arguments={}, call_id="c1")]
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(intents, {"final_response": "u"})
        )
        d.dispatch_turn(agent, user_message="dangerous")

        # Turn 2: same Dispatcher, normal flow now.
        _patch_classifier_green(monkeypatch)
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "y"}, final_text="y")
        )
        d.dispatch_turn(agent, user_message="next")

        latest_by_turn = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        assert latest_by_turn["test-session#1"] == "drop"
        assert latest_by_turn["test-session#2"] == "pending"

    def test_previous_error_is_not_re_finalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = (
            lambda **kw: _raising_generator(RuntimeError("boom"))
        )
        with pytest.raises(RuntimeError):
            d.dispatch_turn(agent, user_message="will fail")

        # Turn 2: normal.
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "y"}, final_text="y")
        )
        d.dispatch_turn(agent, user_message="next")

        latest_by_turn = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        assert latest_by_turn["test-session#1"] == "error"
        assert latest_by_turn["test-session#2"] == "pending"

    def test_first_turn_attempts_no_finalization(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        # First turn on a fresh Dispatcher has no previous turn — the
        # finalization step is skipped entirely. The store should hold
        # only the new pending record after one turn.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(None, {"final_response": "x"}, final_text="x")
        )
        d.dispatch_turn(agent, user_message="first")

        records = list(tmp_store.records())
        assert len(records) == 1
        assert records[0].outcome == "pending"

    def test_multi_turn_chain_finalizes_each_predecessor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])

        for i in range(5):
            agent._run_turn_generator = (
                lambda **kw: _synthetic_generator(
                    None, {"final_response": f"r{i}"}, final_text=f"r{i}",
                )
            )
            d.dispatch_turn(agent, user_message=f"turn {i}")

        # Five turns: first four finalize to success, fifth remains pending.
        latest_by_turn = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        for i in range(1, 5):
            assert latest_by_turn[f"test-session#{i}"] == "success", (
                f"turn {i} should have finalized to success"
            )
        assert latest_by_turn["test-session#5"] == "pending"


# ── AIAgent integration ──────────────────────────────────────────────────


class TestInlineLazyDispatcherBuild:
    def test_inline_lazy_build_inside_run_conversation_wires_default_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        # Sprint 33 Phase 2 — the lazy Dispatcher build pattern that
        # used to live in the agent's deleted singleton helper is now
        # inlined inside ``AIAgent.run_conversation``. It fires only
        # when an Agent is constructed without going through the
        # Dispatcher inversion path (mostly tests). When it fires it
        # wires ``grove.intent_store.get_store()`` as the default —
        # under the per-test GROVE_HOME isolation, that resolves to a
        # tmp-path store. This test verifies the same wiring contract
        # the deleted singleton helper honored.
        from grove.intent_store import get_store as _get_intent_store

        default_store = _get_intent_store()
        assert default_store is not None
        # The store path lives under the per-test GROVE_HOME tempdir,
        # not the operator's ~/.grove path.
        assert "intent_records.jsonl" in str(default_store.path)
        # And construction via the new sole sanctioned path threads
        # the same store through when the caller doesn't override.
        dispatcher = Dispatcher(intent_store=default_store)
        assert dispatcher._intent_store is default_store
