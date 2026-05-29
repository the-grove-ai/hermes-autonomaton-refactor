"""Sprint 35 — classify-before-construct. Dispatcher classifies + binds
the routed tier to the pre-built Agent shell BEFORE the generator runs.

These tests assert the Phase 1 contract:

* ``Dispatcher.dispatch_turn`` calls ``route_for_agent`` and binds the
  Agent's tier via ``_bind_agent_to_tier`` BEFORE invoking
  ``_run_turn_generator``. THE timing contract Sprint 35 introduces.
* ``self._current_turn_classification`` is populated BEFORE the
  generator's first send — Sprint 28 IntentRecord terminal writes and
  Sprint 29 tool filter both see the classification.
* Sprint 30.1 ``escalation_decision`` ledger event fires from the
  Dispatcher's pre-construction path (moved out of ``_drive_generator``).
* ``_bind_agent_to_tier`` picks ``apply_tier`` for same-provider routes
  and ``switch_model`` for cross-provider routes — same selection rule
  the deleted ``_maybe_route_for_turn`` used.
* The ``already_routed=True`` gate (CLI pre-routing, Sprint 30 hot-swap)
  short-circuits the classification call.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher


# ── Lightweight stand-ins (avoid importing the heavyweight TierConfig/RoutingDecision) ─


@dataclass
class _StubTierConfig:
    tier: str
    provider: str
    model: str
    max_tokens: int | None


@dataclass
class _StubRoutingDecision:
    tier: str
    tier_config: _StubTierConfig
    reason: str
    confidence: float | None
    pattern_cache_hit: bool


# ── _bind_agent_to_tier (the helper Sprint 35 relocates from the Agent) ──


class TestBindAgentToTier:
    def test_same_provider_uses_apply_tier(self):
        agent = MagicMock()
        agent.provider = "anthropic"
        decision = _StubRoutingDecision(
            tier="T2",
            tier_config=_StubTierConfig(
                tier="T2", provider="anthropic",
                model="claude-sonnet-4-6", max_tokens=8192,
            ),
            reason="default",
            confidence=0.9,
            pattern_cache_hit=False,
        )
        Dispatcher._bind_agent_to_tier(agent, decision, lambda cfg: {})
        agent.apply_tier.assert_called_once_with("claude-sonnet-4-6", 8192)
        agent.switch_model.assert_not_called()

    def test_cross_provider_uses_switch_model(self):
        agent = MagicMock()
        agent.provider = "anthropic"
        decision = _StubRoutingDecision(
            tier="T3",
            tier_config=_StubTierConfig(
                tier="T3", provider="openai",
                model="o4", max_tokens=16384,
            ),
            reason="step_up",
            confidence=0.4,
            pattern_cache_hit=False,
        )

        def fake_resolver(cfg):
            return {
                "model": cfg.model,
                "provider": cfg.provider,
                "api_key": "sk-test",
                "base_url": "https://api.openai.com/v1",
                "api_mode": "chat_completions",
            }
        Dispatcher._bind_agent_to_tier(agent, decision, fake_resolver)
        agent.switch_model.assert_called_once_with(
            new_model="o4",
            new_provider="openai",
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            api_mode="chat_completions",
        )
        assert agent.max_tokens == 16384
        agent.apply_tier.assert_not_called()

    def test_empty_current_provider_treated_as_same_provider(self):
        # Fixture pattern: tests replace self.client wholesale and leave
        # provider empty. apply_tier is the safe choice — same as the
        # pre-Sprint-35 logic.
        agent = MagicMock()
        agent.provider = ""
        decision = _StubRoutingDecision(
            tier="T2",
            tier_config=_StubTierConfig(
                tier="T2", provider="anthropic",
                model="some-model", max_tokens=None,
            ),
            reason="default",
            confidence=0.9,
            pattern_cache_hit=False,
        )
        Dispatcher._bind_agent_to_tier(agent, decision, lambda cfg: {})
        agent.apply_tier.assert_called_once_with("some-model", None)
        agent.switch_model.assert_not_called()


# ── Pre-construction classification timing (THE Sprint 35 contract) ─────


class TestPreConstructionTiming:
    def _stub_route(self, monkeypatch, *, decision=None, classification=None, pre_route=None):
        """Wire up grove.providers so route_for_agent returns ``decision``
        and the module globals reflect ``classification`` + ``pre_route``.
        """
        import grove.providers as providers_mod

        def fake_route_for_agent(*, message, explicit_model=None, explicit_tier=None, tier_source=None):
            providers_mod._last_classification = classification
            providers_mod._last_pre_route_decision = pre_route
            return decision

        monkeypatch.setattr(
            "grove.providers.route_for_agent", fake_route_for_agent,
        )
        # The Dispatcher imports current_classification + current_pre_route_decision
        # from grove.providers inside _classify_and_bind_turn; their
        # behavior reads the (monkeypatched-by-fake_route_for_agent)
        # module globals.

    def test_classification_fires_before_generator_runs(
        self, dispatcher_with_session, mock_classification_result, monkeypatch,
    ):
        # THE timing contract under test. Before Sprint 35: classification
        # fired during the generator's first send(None). After: it fires
        # in dispatch_turn BEFORE _run_turn_generator is invoked.
        d = dispatcher_with_session
        d.open_session(session_id="s_timing")
        d._session_row_created = True

        order = []

        decision = _StubRoutingDecision(
            tier="T2",
            tier_config=_StubTierConfig(
                tier="T2", provider="anthropic",
                model="claude-sonnet-4-6", max_tokens=8192,
            ),
            reason="default",
            confidence=0.85,
            pattern_cache_hit=False,
        )

        def fake_route(*, message, explicit_model=None, explicit_tier=None, tier_source=None):
            import grove.providers as providers_mod
            providers_mod._last_classification = mock_classification_result
            providers_mod._last_pre_route_decision = None
            order.append("route_for_agent")
            return decision

        monkeypatch.setattr("grove.providers.route_for_agent", fake_route)

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_timing"
        agent.model = "claude-haiku-4-5"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            order.append("_run_turn_generator")
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        d.dispatch_turn(agent, "what is two plus two?")

        # THE timing assertion: route_for_agent fired before the
        # generator was even instantiated.
        assert order == ["route_for_agent", "_run_turn_generator"]

    def test_current_turn_classification_populated_pre_generator(
        self, dispatcher_with_session, mock_classification_result, monkeypatch,
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s_cap")
        d._session_row_created = True

        captured_at_generator_start = {}
        decision = _StubRoutingDecision(
            tier="T2",
            tier_config=_StubTierConfig(
                tier="T2", provider="anthropic",
                model="claude-sonnet-4-6", max_tokens=8192,
            ),
            reason="default", confidence=0.85, pattern_cache_hit=False,
        )

        def fake_route(*, message, **_):
            import grove.providers as providers_mod
            providers_mod._last_classification = mock_classification_result
            providers_mod._last_pre_route_decision = None
            return decision
        monkeypatch.setattr("grove.providers.route_for_agent", fake_route)

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_cap"
        agent.model = "claude-haiku-4-5"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            # By the time the generator runs, the Dispatcher's
            # classification capture must already be populated.
            captured_at_generator_start["classification"] = (
                d._current_turn_classification
            )
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        d.dispatch_turn(agent, "hello")
        assert (
            captured_at_generator_start["classification"]
            is mock_classification_result
        )

    def test_already_routed_short_circuits_classification(
        self, dispatcher_with_session, monkeypatch,
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s_already")
        d._session_row_created = True

        route_calls = []
        monkeypatch.setattr(
            "grove.providers.route_for_agent",
            lambda **kw: route_calls.append(kw) or None,
        )

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_already"
        agent.model = "claude-sonnet-4-6"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        d.dispatch_turn(agent, "hello", already_routed=True)
        # already_routed=True short-circuits Sprint 35's pre-route call.
        assert route_calls == []

    def test_non_string_user_message_skips_classification(
        self, dispatcher_with_session, monkeypatch,
    ):
        # Mirrors the pre-Sprint-35 _maybe_route_for_turn guard: the
        # T-telemetry classifier requires text input.
        d = dispatcher_with_session
        d.open_session(session_id="s_nonstr")
        d._session_row_created = True

        route_calls = []
        monkeypatch.setattr(
            "grove.providers.route_for_agent",
            lambda **kw: route_calls.append(kw) or None,
        )

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_nonstr"
        agent.model = "claude-sonnet-4-6"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        # Non-string user_message — e.g. a list-form history reconstruction
        d.dispatch_turn(agent, ["historical", "message", "list"])
        assert route_calls == []


# ── Sprint 30.1 pre-route ledger event moves to pre-construction ─────


class TestPreRouteLedgerEventMoved:
    def test_pre_route_ledger_event_emitted_from_dispatch_turn(
        self, dispatcher_with_session, mock_classification_result, monkeypatch,
    ):
        d = dispatcher_with_session
        d.open_session(session_id="s_preroute")
        d._session_row_created = True

        decision = _StubRoutingDecision(
            tier="T3",
            tier_config=_StubTierConfig(
                tier="T3", provider="anthropic",
                model="claude-opus-4-7", max_tokens=16384,
            ),
            reason="pre_route_escalation",
            confidence=0.4,
            pattern_cache_hit=False,
        )
        pre_route_payload = {
            "current_tier": "T2",
            "target_tier": "T3",
            "complexity_signal": "complex",
            "confidence": 0.4,
        }

        def fake_route(*, message, **_):
            import grove.providers as providers_mod
            providers_mod._last_classification = mock_classification_result
            providers_mod._last_pre_route_decision = pre_route_payload
            return decision
        monkeypatch.setattr("grove.providers.route_for_agent", fake_route)

        ledger_calls = []

        class _CapturingLedger:
            def record(self, *args, **kwargs):
                ledger_calls.append((args, kwargs))
        monkeypatch.setattr(
            d, "_get_or_create_ledger",
            lambda *a, **k: _CapturingLedger(),
        )

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_preroute"
        agent.model = "claude-sonnet-4-6"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        d.dispatch_turn(agent, "do a complex thing")

        # The pre-route ledger event was emitted from
        # _classify_and_bind_turn (pre-generator), not from
        # _drive_generator's post-first-send capture.
        pre_route_events = [
            (a, kw) for (a, kw) in ledger_calls
            if a and a[0] == "escalation_decision"
            and kw.get("source") == "pre_route"
        ]
        assert len(pre_route_events) == 1
        kwargs = pre_route_events[0][1]
        assert kwargs["target_tier"] == "T3"
        assert kwargs["current_tier"] == "T2"
        assert kwargs["complexity_signal"] == "complex"


# ── Vanilla install / classifier-unavailable graceful degrade ─────────


class TestGracefulDegrade:
    def test_no_routing_config_returns_silently(
        self, dispatcher_with_session, monkeypatch,
    ):
        # route_for_agent returns None on vanilla install. The
        # Dispatcher must NOT bind a tier or capture a classification.
        d = dispatcher_with_session
        d.open_session(session_id="s_vanilla")
        d._session_row_created = True

        monkeypatch.setattr(
            "grove.providers.route_for_agent",
            lambda **kw: None,
        )
        # Clear any stale module-global classification from prior tests
        # in the worker so the snapshot fallback reads a clean None.
        import grove.providers as _providers
        monkeypatch.setattr(_providers, "_last_classification", None)

        agent = MagicMock()
        agent.provider = "anthropic"
        agent.session_id = "s_vanilla"
        agent.model = "claude-sonnet-4-6"
        agent._session_init_model_config = None

        def fake_run_turn(*args, **kwargs):
            def _gen():
                from grove.intents import FinalResponse
                yield FinalResponse(content="ok")
            return _gen()
        agent._run_turn_generator = fake_run_turn

        d.dispatch_turn(agent, "hello")
        agent.apply_tier.assert_not_called()
        agent.switch_model.assert_not_called()
        # _current_turn_classification was reset to None at dispatch_turn
        # entry and stays None on the vanilla path.
        assert d._current_turn_classification is None
