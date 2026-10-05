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


class TestSubstrateCitationTelemetry:
    """substrate-citation-v1 P3 — the write site records the compounding-curve
    denominator/numerator/epoch from the per-turn agent stash. Compose (hits +
    sig) and the citation appender (rendered) both run AFTER the dispatcher's
    per-turn reset and BEFORE FinalResponse, so the write reads real values."""

    def test_write_site_records_cellar_fields_from_stash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch, intent_class="analysis")
        agent = _bare_agent_with_exec([])

        def _gen(**kw):
            def g():
                # simulate compose (hits+sig) + appender (rendered) firing after
                # the per-turn reset, before the FinalResponse yield.
                agent._cellar_retrieval_hits = 4
                agent._cellar_citations_rendered = 2
                agent._cellar_retrieval_config_sig = "floor=none;k=5;budget=1500"
                yield FinalResponse(content="done")
                return {"final_response": "done"}
            return g()

        agent._run_turn_generator = _gen
        Dispatcher(intent_store=tmp_store).dispatch_turn(
            agent, user_message="draw on the cellar",
        )
        rec = list(tmp_store.records())[0]
        assert rec.cellar_retrieval_hits == 4
        assert rec.cellar_citations_rendered == 2
        assert rec.cellar_retrieval_config_sig == "floor=none;k=5;budget=1500"

    def test_write_site_defaults_when_no_retrieval(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch, intent_class="analysis")
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator(
                None, {"final_response": "x"}, final_text="x",
            )
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="hi")
        rec = list(tmp_store.records())[0]
        assert rec.cellar_retrieval_hits == 0
        assert rec.cellar_citations_rendered == 0
        assert rec.cellar_retrieval_config_sig is None


# ── turn-identity-v1 — ids are unique per SESSION, not per Dispatcher ────────


def _run_turn(dispatcher, agent, text):
    agent._run_turn_generator = (
        lambda **kw: _synthetic_generator(
            None, {"final_response": "x"}, final_text="x",
        )
    )
    dispatcher.dispatch_turn(agent, user_message=text)


@pytest.fixture
def session_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


class TestTurnIdentityAcrossDispatcherRebuilds:
    def test_rebuilt_dispatcher_continues_the_sequence(
        self, monkeypatch, tmp_store, session_db,
    ):
        # The live defect: the gateway builds a NEW Dispatcher per message, the
        # in-memory counter restarted, and every turn was "<session>#1".
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        for i in range(3):
            d = Dispatcher(intent_store=tmp_store, session_db=session_db)
            _run_turn(d, agent, f"turn {i}")

        pending = [r.turn_id for r in tmp_store.records() if r.outcome == "pending"]
        assert pending == ["test-session#1", "test-session#2", "test-session#3"]

    def test_previous_turn_is_closed_across_a_rebuild(
        self, monkeypatch, tmp_store, session_db,
    ):
        # Closure used the previous id held in Dispatcher memory, so a rebuild
        # left the earlier turn "pending" forever.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        for i in range(3):
            d = Dispatcher(intent_store=tmp_store, session_db=session_db)
            _run_turn(d, agent, f"turn {i}")

        latest = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        assert latest == {
            "test-session#1": "success",
            "test-session#2": "success",
            "test-session#3": "pending",      # the live turn; closes on the next
        }

    def test_sessions_keep_separate_sequences(
        self, monkeypatch, tmp_store, session_db,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        a = _bare_agent_with_exec([])
        b = _bare_agent_with_exec([])
        b.session_id = "other-session"
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), a, "a1")
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), b, "b1")
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), a, "a2")
        ids = [r.turn_id for r in tmp_store.records() if r.outcome == "pending"]
        assert ids == ["test-session#1", "other-session#1", "test-session#2"]

    def test_legacy_session_is_seeded_past_its_existing_records(
        self, monkeypatch, tmp_store, session_db,
    ):
        # A session with turns recorded under the old scheme (all "#1") and no
        # stored sequence must not hand out "#1" again.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        for i in range(3):                      # old scheme: no session DB
            _run_turn(Dispatcher(intent_store=tmp_store), agent, f"old {i}")
        old_ids = [r.turn_id for r in tmp_store.records() if r.outcome == "pending"]
        assert old_ids == ["test-session#1"] * 3

        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), agent, "new")
        newest = [r.turn_id for r in tmp_store.records() if r.outcome == "pending"][-1]
        assert newest == "test-session#4"

    def test_no_session_db_keeps_the_in_memory_counter(
        self, monkeypatch, tmp_store,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        d = Dispatcher(intent_store=tmp_store)
        _run_turn(d, agent, "one")
        _run_turn(d, agent, "two")
        ids = [r.turn_id for r in tmp_store.records() if r.outcome == "pending"]
        assert ids == ["test-session#1", "test-session#2"]

    def test_database_fault_is_loud_and_falls_back(
        self, monkeypatch, tmp_store, session_db, caplog,
    ):
        import logging

        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)

        def _boom(session_id, *, seed=0):
            raise RuntimeError("disk full")

        monkeypatch.setattr(session_db, "advance_turn", _boom)
        agent = _bare_agent_with_exec([])
        d = Dispatcher(intent_store=tmp_store, session_db=session_db)
        with caplog.at_level(logging.ERROR, logger="grove.dispatcher"):
            _run_turn(d, agent, "one")
        assert "could not issue a persistent turn sequence" in caplog.text
        assert [r.turn_id for r in tmp_store.records()] == ["test-session#1"]


def test_advance_turn_is_atomic_and_sequential(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db.advance_turn("s") == (1, None)
        assert db.advance_turn("s") == (2, 1)
        assert db.advance_turn("t") == (1, None)
        assert db.advance_turn("u", seed=5) == (6, 5)     # seeded legacy session
        assert db.advance_turn("u", seed=99) == (7, 6)    # seed ignored once stored
    finally:
        db.close()


# ── turn-identity-v1 (unique id) · classifier-failure-reason-v1 ·
#    failed-turn-records-v1 ───────────────────────────────────────────────────


def _early_exit_generator(result):
    """An agent loop that returns a result WITHOUT yielding FinalResponse —
    the shape of every early failure exit in run_agent."""
    def gen():
        return result
        yield  # pragma: no cover — makes this a generator
    return gen()


def _records(store):
    return list(store.records())


class TestUniqueTurnId:
    def test_every_record_carries_a_uuid7_that_survives_finalization(
        self, monkeypatch, tmp_store, session_db,
    ):
        import uuid

        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        for i in range(2):
            _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), agent, f"t{i}")

        recs = _records(tmp_store)
        by_turn = {}
        for r in recs:
            by_turn.setdefault(r.turn_id, set()).add(r.turn_uid)
        # one uid per turn, identical on its pending AND its success record
        assert all(len(uids) == 1 for uids in by_turn.values())
        uids = [next(iter(v)) for v in by_turn.values()]
        assert len(set(uids)) == 2
        assert all(uuid.UUID(u).version == 7 for u in uids)

    def test_tool_call_rows_carry_the_turn_uid(self):
        import inspect

        import run_agent
        from grove import capability_feed

        assert "turn_uid" in capability_feed.FIELDS
        src = inspect.getsource(run_agent.AIAgent._emit_capability_feed_record)
        assert '"turn_uid": getattr(_disp, "_current_turn_uid", None)' in src


class TestClassifierFailureReason:
    def test_failed_classification_is_recorded_with_a_clean_reason(
        self, monkeypatch, tmp_store,
    ):
        from grove import classify as _classify_mod
        from grove import providers as _providers

        _patch_classifier_green(monkeypatch)
        monkeypatch.setattr(_providers, "_last_classification", None, raising=False)
        monkeypatch.setattr(
            _classify_mod, "last_classification_failure",
            lambda: ("no_tool_call", "RuntimeError"),
        )
        d = Dispatcher(intent_store=tmp_store)
        _run_turn(d, _bare_agent_with_exec([]), "hello")

        rec = _records(tmp_store)[-1]
        assert rec.intent_class == "unknown" and rec.confidence == 0.0
        assert rec.classification_status == "failed"
        assert rec.classification_failure == "no_tool_call: RuntimeError"

    def test_successful_classification_is_ok(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        _run_turn(Dispatcher(intent_store=tmp_store), _bare_agent_with_exec([]), "hi")
        rec = _records(tmp_store)[-1]
        assert rec.classification_status == "ok"
        assert rec.classification_failure is None

    def test_real_low_confidence_unknown_is_not_a_failure(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(
            monkeypatch, intent_class="unknown", confidence=0.3,
        )
        _run_turn(Dispatcher(intent_store=tmp_store), _bare_agent_with_exec([]), "hmm")
        rec = _records(tmp_store)[-1]
        assert rec.intent_class == "unknown"
        assert rec.classification_status == "ok"

    def test_classify_captures_a_cleaned_failure_not_the_raw_text(self, monkeypatch):
        from grove import classify as _classify_mod

        class _Leaky(Exception):
            status_code = 401

        def _boom():
            raise _Leaky("Authorization: Bearer sk-live-SECRET-123 rejected")

        monkeypatch.setattr(_classify_mod, "_telemetry_tier_runtime", _boom)
        assert _classify_mod.classify_for_routing("anything") is None
        kind, summary = _classify_mod.last_classification_failure()
        assert kind == "auth_error"
        assert summary == "_Leaky (HTTP 401)"
        assert "SECRET" not in summary and "Bearer" not in summary
        # ...and it resets on the next successful-path call
        monkeypatch.setattr(_classify_mod, "_telemetry_tier_runtime", lambda: (_ for _ in ()).throw(ValueError("x")))
        _classify_mod.classify_for_routing("")          # empty message: skipped
        assert _classify_mod.last_classification_failure() is None


class TestFailedTurnsAreRecorded:
    def test_early_exit_result_writes_an_error_record(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _early_exit_generator({
            "messages": [], "completed": False, "api_calls": 3,
            "error": "Invalid API response after 3 retries: token=sk-SECRET",
        })
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="go")

        recs = _records(tmp_store)
        assert len(recs) == 1
        assert recs[0].outcome == "error"
        assert recs[0].failure_kind == "retries_exhausted"
        assert "SECRET" not in (recs[0].failure_summary or "")

    def test_interrupted_result_is_recorded_as_interrupted(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _early_exit_generator(
            {"interrupted": True, "completed": False}
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="go")
        rec = _records(tmp_store)[-1]
        assert rec.outcome == "interrupted" and rec.failure_kind == "interrupted"

    def test_loop_that_raises_still_writes_a_record(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)

        class _ApiDown(Exception):
            status_code = 503

        def _raising():
            def gen():
                raise _ApiDown("upstream said: X-Api-Key=abc123 invalid")
                yield  # pragma: no cover
            return gen()

        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _raising()
        with pytest.raises(_ApiDown):
            Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="go")
        recs = _records(tmp_store)
        assert len(recs) == 1
        assert recs[0].outcome == "error"
        assert recs[0].failure_kind == "provider_error"
        assert recs[0].failure_summary == "_ApiDown (HTTP 503)"

    def test_failure_before_the_turn_has_an_identity_is_recorded(
        self, monkeypatch, tmp_store, session_db,
    ):
        # Anything raised BEFORE the inner try (here: the session broadcast at
        # the very top of the turn) used to leave no record at all.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        d = Dispatcher(intent_store=tmp_store, session_db=session_db)
        _run_turn(d, agent, "first")                       # turn #1, fine

        def _boom(*a, **kw):
            raise RuntimeError("session broadcast failed")

        monkeypatch.setattr(d, "broadcast_session_id", _boom)
        with pytest.raises(RuntimeError):
            d.dispatch_turn(agent, user_message="second")

        errors = [r for r in _records(tmp_store) if r.outcome == "error"]
        assert len(errors) == 1
        assert errors[0].turn_id == "test-session#2"       # its OWN id, not #1
        assert errors[0].failure_kind == "exception"
        assert errors[0].user_message_stem == "second"

    def test_empty_response_is_an_error_not_a_pending_success(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _synthetic_generator(
            None, {"final_response": "(empty)"}, final_text="(empty)",
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="go")
        rec = _records(tmp_store)[-1]
        assert rec.outcome == "error" and rec.failure_kind == "empty_response"

    def test_normal_turn_writes_exactly_one_record_and_no_failure_fields(
        self, monkeypatch, tmp_store,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        _run_turn(Dispatcher(intent_store=tmp_store), _bare_agent_with_exec([]), "hi")
        recs = _records(tmp_store)
        assert [r.outcome for r in recs] == ["pending"]
        assert recs[0].failure_kind is None and recs[0].failure_summary is None


class TestClosureNeverOverwritesAFailure:
    def test_next_turn_does_not_turn_an_error_into_success(
        self, monkeypatch, tmp_store, session_db,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _early_exit_generator(
            {"completed": False, "error": "Invalid API response after 3 retries"}
        )
        Dispatcher(intent_store=tmp_store, session_db=session_db).dispatch_turn(
            agent, user_message="fails",
        )
        # a brand-new Dispatcher handles the next message and closes the previous turn
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), agent, "next")

        latest = {r.turn_id: r.outcome for r in tmp_store.latest_by_turn()}
        assert latest["test-session#1"] == "error"         # untouched
        assert latest["test-session#2"] == "pending"
        assert [r.outcome for r in _records(tmp_store) if r.turn_id == "test-session#1"] == ["error"]

    @pytest.mark.parametrize("outcome", ["error", "interrupted", "governance_terminated"])
    def test_stale_sweep_leaves_failures_alone(self, tmp_store, outcome):
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        tmp_store.append(IntentRecord(
            timestamp=old, session_id="s", turn_id="s#1", user_message_stem="x",
            pattern_hash="h", intent_class="conversation", register_class="casual",
            complexity_signal="simple", confidence=0.9, outcome=outcome,
        ))
        assert tmp_store.sweep_stale_pending() == 0
        assert [r.outcome for r in tmp_store.records()] == [outcome]


# ── stage-summary-v1 — every turn carries all five stages ────────────────────

_STAGES = ("telemetry", "recognition", "compilation", "approval", "execution")


class TestStageSummary:
    def test_plain_answer_turn_has_all_five_stages(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch, intent_class="conversation",
                                    complexity_signal="simple", confidence=0.95)
        agent = _bare_agent_with_exec([])
        agent.platform = "telegram"
        _run_turn(Dispatcher(intent_store=tmp_store), agent, "hello")

        rec = _records(tmp_store)[-1]
        st = rec.stages
        # Records are serialized with sorted keys (stable for hashing), so the
        # five stages are asserted as a set, not in pipeline order.
        assert set(st) == set(_STAGES)
        assert st["telemetry"]["turn_uid"] == rec.turn_uid
        assert st["telemetry"]["ordinal"] == rec.turn_id
        assert st["telemetry"]["surface"] == "telegram"
        assert st["recognition"] == {
            "status": "ok", "failure": None, "intent_class": "conversation",
            "complexity": "simple", "confidence": 0.95,
        }
        # No tool calls: the Approval stage says so instead of being absent.
        assert st["approval"]["verdict"] == "green_pass_no_tool_calls"
        assert st["approval"]["tool_calls_by_zone"] == {}
        assert st["execution"]["mode"] == "response_only"
        assert st["execution"]["tools_run"] == 0
        # outcome/failure are NOT duplicated inside the block
        assert "outcome" not in st["execution"] and "failure_kind" not in st["execution"]

    def test_tool_turn_records_the_actual_zone_verdict_and_rule(
        self, monkeypatch, tmp_store,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _synthetic_generator(
            [ToolIntent(tool_name="read_file", arguments={}, call_id="c1"),
             ToolIntent(tool_name="web_search", arguments={}, call_id="c2")],
            {"final_response": "done"}, final_text="done",
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="look")

        st = _records(tmp_store)[-1].stages
        assert st["approval"]["verdict"] == "all_green"
        assert st["approval"]["tool_calls_by_zone"] == {"green": 2}
        assert [v["tool"] for v in st["approval"]["verdicts"]] == ["read_file", "web_search"]
        assert all(v["zone"] == "green" and v["source"] == "test_force_green"
                   for v in st["approval"]["verdicts"])
        assert st["execution"]["mode"] == "tools" and st["execution"]["tools_run"] == 2
        assert st["execution"]["model_calls"] == 3

    def test_token_usage_is_the_turns_delta_not_the_session_total(
        self, monkeypatch, tmp_store,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent.session_input_tokens = 1000        # carried in from earlier turns
        agent.session_output_tokens = 50
        agent.session_cache_read_tokens = 900

        def _gen(**kw):
            def gen():
                agent.session_input_tokens += 120
                agent.session_output_tokens += 30
                agent.session_cache_read_tokens += 100
                agent._turn_retries = getattr(agent, "_turn_retries", 0) + 2
                yield FinalResponse(content="ok")
                return {"final_response": "ok"}
            return gen()

        agent._run_turn_generator = _gen
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="q")
        ex = _records(tmp_store)[-1].stages["execution"]
        assert ex["tokens"] == {"input": 120, "output": 30, "cache_read": 100}
        assert ex["retries"] == 2

    def test_failed_turn_still_carries_all_five_stages(self, monkeypatch, tmp_store):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        agent._run_turn_generator = lambda **kw: _early_exit_generator(
            {"completed": False, "error": "Invalid API response after 3 retries"}
        )
        Dispatcher(intent_store=tmp_store).dispatch_turn(agent, user_message="go")
        rec = _records(tmp_store)[-1]
        assert rec.outcome == "error"
        assert set(rec.stages) == set(_STAGES)

    def test_failed_classification_shows_in_the_recognition_stage(
        self, monkeypatch, tmp_store,
    ):
        from grove import classify as _classify_mod
        from grove import providers as _providers

        _patch_classifier_green(monkeypatch)
        monkeypatch.setattr(_providers, "_last_classification", None, raising=False)
        monkeypatch.setattr(_classify_mod, "last_classification_failure",
                            lambda: ("timeout", "APITimeoutError"))
        _run_turn(Dispatcher(intent_store=tmp_store), _bare_agent_with_exec([]), "hi")
        rg = _records(tmp_store)[-1].stages["recognition"]
        assert rg["status"] == "failed" and rg["failure"] == "timeout: APITimeoutError"

    def test_summary_is_copied_unchanged_onto_the_finalization_record(
        self, monkeypatch, tmp_store, session_db,
    ):
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        agent = _bare_agent_with_exec([])
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), agent, "one")
        _run_turn(Dispatcher(intent_store=tmp_store, session_db=session_db), agent, "two")
        first = [r for r in _records(tmp_store) if r.turn_id == "test-session#1"]
        assert [r.outcome for r in first] == ["pending", "success"]
        assert first[0].stages == first[1].stages

    def test_ledger_observer_feeds_the_approval_stage(self, monkeypatch, tmp_store, tmp_path):
        from grove.kaizen_ledger import KaizenLedger

        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        d = Dispatcher(intent_store=tmp_store)
        agent = _bare_agent_with_exec([])

        def _gen(**kw):
            def gen():
                ledger = d._get_or_create_ledger(agent)
                ledger.record("grant_execution", grant_id="grant-abc", scope="calendar_create",
                              auth_type="standing_capability")
                yield FinalResponse(content="ok")
                return {"final_response": "ok"}
            return gen()

        agent._run_turn_generator = _gen
        d.dispatch_turn(agent, user_message="q")
        ap = _records(tmp_store)[-1].stages["approval"]
        assert ap["grants"] == [{"grant_id": "grant-abc", "scope": "calendar_create",
                                 "auth_type": "standing_capability"}]

    def test_observer_fault_never_fails_the_ledger_write(self, tmp_path, caplog):
        import logging

        from grove.kaizen_ledger import KaizenLedger

        ledger = KaizenLedger(session_id="s", ledger_dir=tmp_path)

        def _bad(event):
            raise RuntimeError("observer down")

        ledger.observer = _bad
        with caplog.at_level(logging.WARNING, logger="grove.kaizen_ledger"):
            ledger.record("final_response", content_length=1, metadata={})
        assert len(list(ledger.events())) == 1
        assert "event observer failed" in caplog.text


# ── shell-stamping-v1 — the tool-call row carries the verdict that governed it ──


class _FeedSpy:
    def __init__(self):
        self.rows = []

    def utc_now_iso(self):
        return "2026-10-05T00:00:00+00:00"

    def enqueue(self, row):
        self.rows.append(row)


def _emit_row(disp, tool_name, call_id):
    import run_agent

    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "s1"
    agent._dispatcher_singleton = disp
    spy = _FeedSpy()
    agent._emit_capability_feed_record(tool_name, "ok", 1.0, spy, tool_call_id=call_id)
    return spy.rows[0]


class TestShellStamping:
    def _classify(self, intents):
        d = Dispatcher()
        d._begin_stage_capture(_bare_agent_with_exec([]))
        try:
            d._classify_intents_batch_and_halt_or_raise(intents)
        except Exception:
            pass  # a Yellow/Red verdict halts the batch; the verdict is still kept
        return d

    def test_shell_row_carries_the_command_verdict_not_the_tool_name_zone(self):
        from grove import capability_feed
        from grove import dispatch as _grove_dispatch
        from grove.zones import classify as _static

        cmd = "rm -rf /etc/hosts"
        intent = ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="call_sh")
        real = _grove_dispatch.classify_command(cmd, tool_id="terminal")
        d = self._classify([intent])

        row = _emit_row(d, "terminal", "call_sh")
        assert set(row) == set(capability_feed.FIELDS)
        assert row["zone"] == real.zone
        assert row["zone_rule"] == real.matched_rule
        assert row["zone_source"] == real.source
        # the point of the stamp: this is the command's verdict, which differs
        # from what the bare tool name classifies as
        assert real.zone != "green"
        assert (row["zone"], row["zone_rule"]) != (
            _static("terminal").zone, _static("terminal").matched_rule,
        )

    def test_two_shell_calls_in_one_batch_each_keep_their_own_verdict(self):
        from grove import dispatch as _grove_dispatch

        a, b = "ls -la", "rm -rf /etc/hosts"
        d = self._classify([
            ToolIntent(tool_name="terminal", arguments={"command": a}, call_id="c_a"),
            ToolIntent(tool_name="terminal", arguments={"command": b}, call_id="c_b"),
        ])
        ra = _emit_row(d, "terminal", "c_a")
        rb = _emit_row(d, "terminal", "c_b")
        assert ra["zone"] == _grove_dispatch.classify_command(a, tool_id="terminal").zone
        assert rb["zone"] == _grove_dispatch.classify_command(b, tool_id="terminal").zone
        assert ra["zone_rule"] == _grove_dispatch.classify_command(a, tool_id="terminal").matched_rule
        assert rb["zone_rule"] == _grove_dispatch.classify_command(b, tool_id="terminal").matched_rule

    def test_row_without_a_recorded_verdict_says_so(self):
        from grove.zones import classify as _static

        d = Dispatcher()
        d._begin_stage_capture(_bare_agent_with_exec([]))
        row = _emit_row(d, "read_file", "never_classified")
        assert row["zone"] == _static("read_file").zone
        assert row["zone_rule"] is None
        assert row["zone_source"] == "tool_name_static"

    def test_verdicts_do_not_leak_into_the_next_turn(self):
        d = self._classify([
            ToolIntent(tool_name="terminal", arguments={"command": "ls"}, call_id="c1"),
        ])
        assert d.zone_verdict_for_call("c1") is not None
        d._begin_stage_capture(_bare_agent_with_exec([]))
        assert d.zone_verdict_for_call("c1") is None

    def test_agent_without_a_dispatcher_still_emits(self):
        row = _emit_row(None, "read_file", "c1")
        assert row["zone_source"] == "tool_name_static"
