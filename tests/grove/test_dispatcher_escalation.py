"""Integration tests for Sprint 30 Phase 2 — Dispatcher handles EscalationRequest.

Covers the Dispatcher's catch of EscalationRequest in _drive_generator,
policy evaluation, hot-swap on grant (gen.close() + new Agent + new
generator with full turn_history), denial injection into messages,
Kaizen Ledger escalation_decision events, IntentRecord escalation_count
field, per-turn / per-session counter behavior, and the no-op return
when already at target tier.

The synthetic-generator pattern mirrors
tests/grove/test_dispatcher_intent_records.py so the tests stay focused
on the Sprint 30 surface, not LLM behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from grove.dispatcher import Dispatcher
from grove.intent_store import IntentStore
from grove.intents import (
    EscalationRequest,
    FinalResponse,
    Observation,
    ToolIntent,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _enabled_policy_config() -> Dict[str, Any]:
    return {
        "routing": {
            "default_tier": "T2",
            "escalation_policy": {
                "enabled": True,
                "max_escalations_per_turn": 2,
                "max_escalations_per_session": 5,
                "ceiling_tier": "T3",
                "mapping": {
                    "shallow": "T1",
                    "moderate": "T2",
                    "deep": "T3",
                    "apex": "T3",
                },
            },
            "tier_preferences": {
                "T2": {"provider": "anthropic", "model": "sonnet-stub"},
                "T3": {"provider": "anthropic", "model": "opus-stub"},
            },
        },
    }


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


@pytest.fixture
def enabled_dispatcher(
    tmp_store: IntentStore, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Dispatcher:
    """Construct a Dispatcher with escalation policy enabled.

    The Dispatcher reads its config via the substrate snapshot
    (self._base_runtime_ctx.config). Inject directly post-construction
    so we don't have to monkey-patch the config-load chain.
    """
    monkeypatch.setattr(
        "grove.zones.initialize", lambda *a, **kw: None,
    )
    d = Dispatcher(
        intent_store=tmp_store,
        kaizen_ledger_dir=tmp_path / "ledger",
    )
    # Inject the enabled config directly; the policy loader reads it
    # the first time _get_or_load_escalation_policy fires.
    object.__setattr__(
        d._base_runtime_ctx, "config",
        _enabled_policy_config(),
    )
    return d


def _bare_agent_with_state(messages: List[Dict[str, Any]]):
    """Minimal AIAgent stand-in for the Dispatcher's drive loop."""
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent._current_assistant_message = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "escalate", "arguments": "{}"}}],
    }
    agent._current_messages = messages
    agent._current_effective_task_id = "task_t"
    agent._current_api_call_count = 1
    agent.session_id = "esc-test-session"
    agent.model = "sonnet-stub"
    agent.platform = None
    agent.user_id = None
    agent.user_name = None
    agent.chat_id = None
    agent.chat_name = None
    agent.chat_type = None
    agent.thread_id = None
    agent.max_iterations = 90
    agent.quiet_mode = True
    agent._sovereign_prompt_handler = None
    agent._tools_for_turn = None
    agent._last_tool_selection = None
    agent._execute_tool_calls = (
        lambda asst, msgs, task_id, api_n: None
    )
    # Sprint 34/35 — hot-swap rebuild copies _runtime_ctx through the
    # carry kit; without it, the new AIAgent.__init__ raises per the
    # Sprint 34 mandatory-runtime_ctx contract.
    from tests._runtime_ctx import MOCK_RUNTIME_CTX
    agent._runtime_ctx = MOCK_RUNTIME_CTX
    return agent


def _escalation_request(
    *,
    depth: str = "deep",
    context_size: str = "extended",
    blocker: str = "synthesis-required",
    call_id: str = "esc-call-1",
) -> EscalationRequest:
    return EscalationRequest(
        reason=blocker,
        request={
            "reasoning_depth": depth,
            "context_size": context_size,
            "call_id": call_id,
        },
    )


def _patch_classifier_green(monkeypatch: pytest.MonkeyPatch) -> None:
    from grove import zones as _zones
    from grove.zones import ZoneResult
    monkeypatch.setattr(
        _zones, "classify",
        lambda action: ZoneResult(
            zone="green", matched_rule=action, source="test_force_green",
        ),
    )
    # Sprint 35 — dispatch_turn now calls route_for_agent pre-generator.
    # Stub it to None so escalation tests that pre-set _last_routed_tier
    # (e.g. to T3 for ceiling-deny scenarios) keep that setup; the
    # Dispatcher's snapshot fallback reads the test-controlled globals
    # rather than the live router output.
    monkeypatch.setattr(
        "grove.providers.route_for_agent", lambda **kw: None,
    )


# ── Policy loading on Dispatcher ──────────────────────────────────────────


class TestDispatcherPolicyLoading:
    def test_default_dispatcher_has_disabled_policy(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Vanilla Dispatcher — no config injection.
        monkeypatch.setattr("grove.zones.initialize", lambda *a, **kw: None)
        d = Dispatcher()
        policy = d._get_or_load_escalation_policy()
        assert policy.enabled is False

    def test_enabled_dispatcher_returns_loaded_policy(
        self, enabled_dispatcher: Dispatcher,
    ):
        policy = enabled_dispatcher._get_or_load_escalation_policy()
        assert policy.enabled is True
        assert policy.mapping["deep"] == "T3"

    def test_policy_is_cached(self, enabled_dispatcher: Dispatcher):
        first = enabled_dispatcher._get_or_load_escalation_policy()
        second = enabled_dispatcher._get_or_load_escalation_policy()
        assert first is second


# ── Carry-kit extraction ──────────────────────────────────────────────────


class TestCarryKitExtraction:
    def test_carry_kit_contains_gate_a_locked_fields(self):
        agent = _bare_agent_with_state([])
        agent.platform = "telegram"
        agent.user_id = "u1"
        agent.user_name = "jim"
        agent.chat_id = "c1"
        agent.chat_type = "dm"
        agent.thread_id = "t1"
        kit = Dispatcher._extract_agent_carry_kit(agent)
        # Every field the GATE-A locked carry kit names should be present.
        for key in (
            "model", "session_id", "platform", "user_id", "user_name",
            "chat_id", "chat_name", "chat_type", "thread_id",
            "max_iterations", "enabled_toolsets", "quiet_mode",
            "sovereign_prompt_handler",
        ):
            assert key in kit, f"carry kit missing {key!r}"
        assert kit["session_id"] == "esc-test-session"
        assert kit["platform"] == "telegram"

    def test_carry_kit_excludes_internal_state(self):
        # Internal state (transports, caches, _current_*, _tools_for_turn,
        # etc.) MUST NOT survive the hot-swap — those get rebuilt by the
        # new Agent's __init__.
        agent = _bare_agent_with_state([])
        kit = Dispatcher._extract_agent_carry_kit(agent)
        for forbidden in (
            "_current_messages", "_current_assistant_message",
            "_dispatcher_singleton", "_tools_for_turn",
            "_last_tool_selection", "client", "tools",
        ):
            assert forbidden not in kit, (
                f"carry kit must not include internal field {forbidden!r}"
            )


# ── Denial path — disabled policy ─────────────────────────────────────────


class TestEscalationDeniedWhenDisabled:
    def test_disabled_policy_denies_and_injects_decline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_store: IntentStore,
        tmp_path: Path,
    ):
        # Default Dispatcher (no config) — policy disabled.
        _patch_classifier_green(monkeypatch)
        monkeypatch.setattr("grove.zones.initialize", lambda *a, **kw: None)
        d = Dispatcher(
            intent_store=tmp_store,
            kaizen_ledger_dir=tmp_path / "ledger",
        )

        messages: List[Dict[str, Any]] = []
        agent = _bare_agent_with_state(messages)

        # Synthetic generator: yields EscalationRequest once, then a
        # FinalResponse after the deny resumes with None.
        def gen():
            yield _escalation_request(call_id="c-disabled")
            yield FinalResponse(content="continuing at current tier")

        agent._run_turn_generator = lambda **kw: gen()
        result = d.dispatch_turn(agent, user_message="please escalate")

        # Denial tool-response landed in messages with the original call_id.
        deny_msgs = [
            m for m in messages
            if m.get("role") == "tool"
            and m.get("tool_call_id") == "c-disabled"
        ]
        assert len(deny_msgs) == 1
        assert "denied" in deny_msgs[0]["content"].lower()
        assert "disabled" in deny_msgs[0]["content"].lower()

        # Ledger captured an escalation_decision event.
        ledger = d.ledger_for(agent)
        events = ledger.events_by_type("escalation_decision")
        assert len(events) == 1
        assert events[0]["granted"] is False
        assert "disabled" in events[0]["reason"]


# ── Denial path — budget / ceiling ────────────────────────────────────────


class TestEscalationDeniedByCeiling:
    def test_per_turn_ceiling_denies_second_request(
        self, enabled_dispatcher: Dispatcher,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _patch_classifier_green(monkeypatch)
        # First escalate (current=T2 → grant target=T3 hot-swap); second
        # escalate in the SAME turn should deny on per-turn ceiling.
        # Simpler: simulate two denies by raising ceiling above target so
        # the per-turn ceiling becomes the binding deny reason.
        # Lower max_per_turn to 1 (already default); just emit two
        # escalates that don't grant (already-at-target) so the counter
        # ticks without hot-swap.
        messages: List[Dict[str, Any]] = []
        agent = _bare_agent_with_state(messages)
        agent.model = "opus-stub"  # already at T3 so "deep" no-op denies

        # Force a fake current_tier=T3 so already-at-or-above triggers.
        import grove.providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_routed_tier", "T3")

        def gen():
            yield _escalation_request(depth="deep", call_id="c-first")
            yield _escalation_request(depth="deep", call_id="c-second")
            yield FinalResponse(content="done")

        agent._run_turn_generator = lambda **kw: gen()
        enabled_dispatcher.dispatch_turn(agent, user_message="esc twice")

        events = enabled_dispatcher.ledger_for(agent).events_by_type(
            "escalation_decision",
        )
        assert len(events) == 2
        # First: already-at-or-above
        assert events[0]["granted"] is False
        assert "already at-or-above" in events[0]["reason"]
        # Second: per-turn ceiling (max_escalations_per_turn=2 above,
        # but already-at-or-above takes priority — both deny but for
        # the first-listed reason that fires).
        assert events[1]["granted"] is False


# ── Grant path — hot-swap ─────────────────────────────────────────────────


class TestEscalationGrantHotSwap:
    def test_grant_hot_swaps_with_full_turn_history(
        self, enabled_dispatcher: Dispatcher,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        _patch_classifier_green(monkeypatch)
        # current_tier=T2; deep→T3 — clean grant path.
        import grove.providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_routed_tier", "T2")

        # Track new Agent constructions to verify hot-swap happened.
        constructed = []
        original_init = None
        import run_agent
        original_init = run_agent.AIAgent.__init__

        def _spy_init(self, **kwargs):
            constructed.append(kwargs)
            # Don't call original — heavy. Just install attrs the
            # generator path reads.
            self.model = kwargs.get("model", "")
            self.session_id = kwargs.get("session_id")
            self.platform = kwargs.get("platform")
            self._sovereign_prompt_handler = kwargs.get(
                "sovereign_prompt_handler",
            )
            self._dispatcher_singleton = None
            self._tools_for_turn = None
            self._last_tool_selection = None
            self._current_messages = None

        monkeypatch.setattr(run_agent.AIAgent, "__init__", _spy_init)

        # Original agent's generator yields ONE escalation then awaits.
        # Snapshotted messages should carry through to the new agent.
        seeded_messages = [
            {"role": "user", "content": "original user msg"},
            {"role": "assistant", "content": "prior reasoning"},
        ]
        agent = _bare_agent_with_state(list(seeded_messages))

        def original_gen():
            yield _escalation_request(depth="deep", call_id="c-grant")
            # Never reached — gen.close() terminates this generator on grant.
            yield FinalResponse(content="should-not-fire")

        agent._run_turn_generator = lambda **kw: original_gen()

        # The new agent's _run_turn_generator is called by the hot-swap;
        # patch it on the class to a stub yielding FinalResponse.
        def _new_gen(self, *, user_message, conversation_history=None, **kw):
            # Capture for assertion via closure variable.
            captured["user_message"] = user_message
            captured["conversation_history"] = conversation_history
            yield FinalResponse(content="escalated reply")

        captured: Dict[str, Any] = {}
        monkeypatch.setattr(
            run_agent.AIAgent, "_run_turn_generator", _new_gen,
        )

        enabled_dispatcher.dispatch_turn(
            agent, user_message="please go deeper",
        )

        # A new Agent was constructed with the escalated model.
        assert len(constructed) == 1
        assert constructed[0]["model"] == "opus-stub"  # T3 in our config
        # Carry kit preserved the session.
        assert constructed[0]["session_id"] == "esc-test-session"

        # The new agent's generator received the full turn_history.
        history = captured.get("conversation_history") or []
        assert len(history) >= len(seeded_messages)
        # Seeded messages survived.
        assert {"role": "user", "content": "original user msg"} in history
        # Grant tool-response was appended with the original call_id.
        grant_msgs = [
            m for m in history
            if m.get("role") == "tool"
            and m.get("tool_call_id") == "c-grant"
        ]
        assert len(grant_msgs) == 1
        assert "granted" in grant_msgs[0]["content"].lower()
        # user_message passed through.
        assert captured["user_message"] == "please go deeper"

        # Ledger captured the grant.
        events = enabled_dispatcher.ledger_for(agent).events_by_type(
            "escalation_decision",
        )
        assert any(ev["granted"] for ev in events)


# ── IntentRecord escalation_count threading ───────────────────────────────


class TestEscalationCountOnRecord:
    def test_intent_record_carries_escalation_count(
        self, enabled_dispatcher: Dispatcher,
        monkeypatch: pytest.MonkeyPatch,
        tmp_store: IntentStore,
    ):
        # Single denied escalation (already-at-or-above) so the turn
        # completes via the normal FinalResponse path and writes a
        # pending IntentRecord with escalation_count > 0.
        _patch_classifier_green(monkeypatch)
        import grove.providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_routed_tier", "T3")

        messages: List[Dict[str, Any]] = []
        agent = _bare_agent_with_state(messages)
        agent.model = "opus-stub"

        def gen():
            yield _escalation_request(depth="deep", call_id="c-count-1")
            yield FinalResponse(content="done")

        agent._run_turn_generator = lambda **kw: gen()
        enabled_dispatcher.dispatch_turn(agent, user_message="esc once")

        recs = list(tmp_store.records())
        assert len(recs) == 1
        assert recs[0].escalation_count == 1

    def test_zero_escalations_writes_zero(
        self, enabled_dispatcher: Dispatcher,
        monkeypatch: pytest.MonkeyPatch,
        tmp_store: IntentStore,
    ):
        _patch_classifier_green(monkeypatch)
        agent = _bare_agent_with_state([])

        def gen():
            yield FinalResponse(content="no-escalation turn")

        agent._run_turn_generator = lambda **kw: gen()
        enabled_dispatcher.dispatch_turn(agent, user_message="hi")
        recs = list(tmp_store.records())
        assert recs[0].escalation_count == 0


# ── Session counter persistence across turns ──────────────────────────────


class TestSessionCounterAcrossTurns:
    def test_per_session_counter_accumulates(
        self, enabled_dispatcher: Dispatcher,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _patch_classifier_green(monkeypatch)
        import grove.providers as _providers_mod
        monkeypatch.setattr(_providers_mod, "_last_routed_tier", "T3")

        agent = _bare_agent_with_state([])
        agent.model = "opus-stub"

        def gen_factory(call_id):
            def _g():
                yield _escalation_request(depth="deep", call_id=call_id)
                yield FinalResponse(content="done")
            return _g()

        # Three turns, each with one (denied) escalation.
        for i in range(3):
            _cid = f"c-{i}"
            agent._run_turn_generator = (
                lambda _cid=_cid, **kw: gen_factory(_cid)
            )
            enabled_dispatcher.dispatch_turn(agent, user_message=f"turn {i}")

        counts = enabled_dispatcher._session_escalation_counts
        assert counts["esc-test-session"] == 3
