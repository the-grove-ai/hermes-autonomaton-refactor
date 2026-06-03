"""Sprint 49 — T0 execution path verification (unit layer).

Deterministic coverage of the T0 Pattern Cache dispatch short-circuit, the
correction-driven auto-demotion, and the operator stats — no live model, no
network. The live integration smoke is ``tests/integration/test_t0_cache.py``
(T21-T23), which is ``@pytest.mark.integration`` and excluded from this gate.

The constraint under test: a T0 hit serves deterministically with NO model
call — the reasoning generator is never driven and the classifier never
fires. Both are wired to raise here, so any inference attempt fails the test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import pytest

from grove.dispatcher import Dispatcher
from grove.pattern_cache import (
    PatternCacheStore,
    CompiledPattern,
    t0_key,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    STATUS_DEMOTED,
)


@pytest.fixture(autouse=True)
def _grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the substrate home so the store, intent records, ledgers and
    proposal queue all land in tmp — never the operator's real ~/.grove."""
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(
    store: PatternCacheStore,
    message: str,
    intent_class: str,
    *,
    cacheable_type: str = "static",
    cached_response: Optional[str] = "cached-answer",
    compiled_invocation: Optional[str] = None,
    status: str = STATUS_ACTIVE,
    hit_count: int = 0,
) -> str:
    key = t0_key(intent_class, message)
    store.upsert(CompiledPattern(
        pattern_id=key, t0_key=key, intent_class=intent_class,
        cacheable_type=cacheable_type, cached_response=cached_response,
        compiled_invocation=compiled_invocation, evidence_hash="e",
        status=status, created_at=_now(), hit_count=hit_count,
    ))
    return key


def _agent(monkeypatch: pytest.MonkeyPatch, *, forbid_generator: bool = True,
           forbid_classifier: bool = True) -> Any:
    """A bare AIAgent shell. By default both the reasoning generator and the
    classifier are armed to raise, so a T0 hit that touched either fails."""
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "s-test"
    agent.model = "claude-sonnet-4-6"
    agent.provider = "anthropic"
    agent.base_url = ""
    if forbid_generator:
        def _boom_gen(**kw):
            raise AssertionError("reasoning generator was driven on a T0 hit")
        agent._run_turn_generator = _boom_gen
    if forbid_classifier:
        import grove.classify as gc
        monkeypatch.setattr(
            gc, "classify_for_routing",
            lambda m: (_ for _ in ()).throw(
                AssertionError("classifier fired on a T0 hit")),
        )
    return agent


def _intent_store(home: Path) -> Any:
    from grove.intent_store import IntentStore
    return IntentStore(home / "intent_records.jsonl")


# ── static hit ────────────────────────────────────────────────────────


def test_t0_static_hit_serves_cached_response(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    _seed(store, "what is the capital of france", "factual_lookup",
          cached_response="Paris.")
    agent = _agent(monkeypatch)
    d = Dispatcher(intent_store=_intent_store(_grove_home))

    result = d.dispatch_turn(
        agent, user_message="What is the capital of France?",
        already_routed=False,
    )

    assert result["final_response"] == "Paris."
    assert result["tier"] == "T0"
    assert result["pattern_cache_hit"] is True
    assert result["api_calls"] == 0
    assert result["estimated_cost_usd"] == 0.0


def test_t0_static_hit_records_hit_and_intent_class(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    key = _seed(store, "ping", "conversation", cached_response="pong")
    agent = _agent(monkeypatch)
    istore = _intent_store(_grove_home)
    d = Dispatcher(intent_store=istore)

    d.dispatch_turn(agent, user_message="ping", already_routed=False)

    # hit recorded
    assert store.get(key).hit_count == 1
    # A4: the intent record is attributed to the pattern's stored intent_class
    records = list(istore.records())
    assert len(records) == 1
    assert records[0].intent_class == "conversation"
    assert records[0].tier_selected == "T0"
    # state set for the next turn's correction check
    assert d._current_turn_t0_pattern_id == key


# ── executable hit ──────────────────────────────────────────────────────


def test_t0_executable_hit_fires_tool(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    _seed(store, "what time is it", "scheduling",
          cacheable_type="executable", cached_response=None,
          compiled_invocation=json.dumps({"tool": "clock", "args": {"tz": "UTC"}}))
    agent = _agent(monkeypatch)
    calls: List[Any] = []

    def _invoke(name, args, task, *a, **k):
        calls.append((name, args))
        return "12:00 UTC"
    agent._invoke_tool = _invoke

    d = Dispatcher(intent_store=_intent_store(_grove_home))
    result = d.dispatch_turn(agent, user_message="what time is it",
                             already_routed=False)

    assert result["final_response"] == "12:00 UTC"
    assert calls == [("clock", {"tz": "UTC"})]


# ── miss ────────────────────────────────────────────────────────────────


def test_t0_miss_falls_through_to_generator(_grove_home, monkeypatch):
    # An empty store → every query misses → the normal generator path runs.
    PatternCacheStore(_grove_home / "pattern_cache.db")
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "s"; agent.model = "m"; agent.provider = "p"
    agent.base_url = ""
    ran = {"gen": False}

    def _gen(**kw):
        ran["gen"] = True

        def g():
            if False:
                yield None
            return {"final_response": "from-agent"}
        return g()
    agent._run_turn_generator = _gen

    d = Dispatcher()
    # already_routed=True so no classifier/network is needed on the miss path
    result = d.dispatch_turn(agent, user_message="an uncached question",
                             already_routed=True)

    assert result["final_response"] == "from-agent"
    assert ran["gen"] is True
    assert d._current_turn_t0_pattern_id is None


# ── kill switch ─────────────────────────────────────────────────────────


def test_kill_switch_skips_t0(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    _seed(store, "ping", "conversation", cached_response="pong")
    # Disable the cache: even with a seeded active pattern, T0 is bypassed.
    monkeypatch.setattr("grove.dispatcher.pattern_cache_enabled", lambda: False)

    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.session_id = "s"; agent.model = "m"; agent.provider = "p"
    agent.base_url = ""
    ran = {"gen": False}

    def _gen(**kw):
        ran["gen"] = True

        def g():
            if False:
                yield None
            return {"final_response": "from-agent"}
        return g()
    agent._run_turn_generator = _gen

    d = Dispatcher()
    result = d.dispatch_turn(agent, user_message="ping", already_routed=True)

    assert ran["gen"] is True
    assert result["final_response"] == "from-agent"


# ── auto-demotion ───────────────────────────────────────────────────────


def _classification(is_correction: bool) -> Any:
    from grove.classify import ClassificationResult
    return ClassificationResult(
        intent_class="factual_lookup", pattern_hash="h", confidence=0.9,
        register_class="technical", complexity_signal="simple",
        goal_alignment=None, is_correction=is_correction,
    )


def test_auto_demotion_on_correction(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    msg = "what is the capital of france"
    key = _seed(store, msg, "factual_lookup", cached_response="Paris.")
    istore = _intent_store(_grove_home)
    d = Dispatcher(intent_store=istore)

    # Turn 1 — T0 hit.
    agent = _agent(monkeypatch)
    d.dispatch_turn(agent, user_message=msg, already_routed=False)
    assert d._current_turn_t0_pattern_id == key

    # Turn 2 — a correction. Different text → miss → normal flow. Provide the
    # classification via the providers global (already_routed=True path).
    import grove.providers as gp
    monkeypatch.setattr(gp, "_last_classification", _classification(True),
                        raising=False)
    agent.session_id = "s-test"
    agent._run_turn_generator = lambda **kw: (
        (lambda: (yield from ()))() or None
    )

    def _gen2(**kw):
        def g():
            if False:
                yield None
            return {"final_response": "corrected"}
        return g()
    agent._run_turn_generator = _gen2

    d.dispatch_turn(agent, user_message="no, that's wrong",
                    already_routed=True)

    # Pattern suspended; no longer served; demotion proposal queued.
    assert store.get(key).status == STATUS_SUSPENDED
    assert store.get_active_for_message(msg) is None
    import grove.eval.proposal_queue as pq
    from grove.eval.proposal_queue import PROPOSAL_TYPE_PATTERN_DEMOTION
    queued = pq.read_all(path=pq.default_queue_path())
    dem = [p for p in queued if p.type == PROPOSAL_TYPE_PATTERN_DEMOTION]
    assert len(dem) == 1
    assert dem[0].payload["pattern_id"] == key
    assert dem[0].payload["trigger"] == "correction_drift"


def test_no_demotion_without_correction(_grove_home, monkeypatch):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    msg = "what is the capital of france"
    key = _seed(store, msg, "factual_lookup", cached_response="Paris.")
    d = Dispatcher(intent_store=_intent_store(_grove_home))

    agent = _agent(monkeypatch)
    d.dispatch_turn(agent, user_message=msg, already_routed=False)

    # Turn 2 — NOT a correction → the pattern stays active.
    import grove.providers as gp
    monkeypatch.setattr(gp, "_last_classification", _classification(False),
                        raising=False)

    def _gen2(**kw):
        def g():
            if False:
                yield None
            return {"final_response": "next"}
        return g()
    agent._run_turn_generator = _gen2
    d.dispatch_turn(agent, user_message="thanks, now do something else",
                    already_routed=True)

    assert store.get(key).status == STATUS_ACTIVE


# ── store-level lifecycle filters ───────────────────────────────────────


def test_get_active_for_message_filters_suspended(_grove_home):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    msg = "ping"
    _seed(store, msg, "conversation", status=STATUS_SUSPENDED)
    assert store.get_active_for_message(msg) is None


def test_get_active_for_message_filters_demoted(_grove_home):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    msg = "ping"
    _seed(store, msg, "conversation", status=STATUS_DEMOTED)
    assert store.get_active_for_message(msg) is None


def test_get_active_for_message_is_intent_agnostic(_grove_home):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    # Promoted under 'factual_lookup'; the lookup doesn't know the intent and
    # still resolves it via the 15-key sweep.
    _seed(store, "what is two plus two", "factual_lookup",
          cached_response="4")
    got = store.get_active_for_message("What is two plus two?")
    assert got is not None and got.cached_response == "4"


def test_get_active_for_message_tiebreak_is_deterministic(_grove_home):
    from grove.classify import INTENT_CLASSES
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    msg = "status report"
    # Same normalized text promoted under two intents → earliest in
    # INTENT_CLASSES order wins, every call.
    _seed(store, msg, "summarization", cached_response="A")
    _seed(store, msg, "retrieval", cached_response="B")
    winner = "retrieval" if INTENT_CLASSES.index("retrieval") < \
        INTENT_CLASSES.index("summarization") else "summarization"
    for _ in range(5):
        got = store.get_active_for_message(msg)
        assert got.intent_class == winner


def test_record_hit_increments(_grove_home):
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    key = _seed(store, "ping", "conversation")
    assert store.record_hit(key) is True
    assert store.record_hit(key) is True
    p = store.get(key)
    assert p.hit_count == 2 and p.last_hit_at is not None
    assert store.record_hit("sha256:nonexistent") is False


# ── stats ───────────────────────────────────────────────────────────────


def test_stats_counts_and_savings(_grove_home, capsys):
    from grove import flywheel_cli as fc
    store = PatternCacheStore(_grove_home / "pattern_cache.db")
    _seed(store, "q1", "factual_lookup", hit_count=7)
    _seed(store, "q2", "scheduling", cacheable_type="executable",
          compiled_invocation="{}", hit_count=3, cached_response=None)
    _seed(store, "q3", "conversation", status=STATUS_DEMOTED, hit_count=99)

    rc = fc.cli_patterns_stats(store=store)
    out = capsys.readouterr().out
    assert rc == 0
    # 2 active (q3 demoted), 3 total, 10 active hits (7+3; demoted excluded)
    assert "Active patterns:      2" in out
    assert "Total patterns:       3" in out
    assert "Total hits (active):  10" in out
    # savings line present and non-zero (repo default T1 cost is declared)
    assert "Estimated savings:    ~$" in out


def test_t1_interaction_cost_reads_config(_grove_home):
    from grove.flywheel_cli import _t1_interaction_cost_usd
    cost = _t1_interaction_cost_usd()
    # Repo default routing.config.yaml declares T1 cost → a positive estimate.
    assert cost is not None and cost > 0
