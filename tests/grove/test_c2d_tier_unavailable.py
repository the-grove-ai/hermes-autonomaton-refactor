"""GRV-010 C2d-1 — tier-unavailable containment (additive, gated).

Detect-Low (gated) / Decide-High:
* run_agent's network-failure gate raises TierUnavailableError ONLY when the
  current tier declares a fallback_tier (else the legacy chain runs unchanged);
* the Dispatcher catches TierUnavailableError and applies the governed policy:
  re-route THROUGH the Cognitive Router at the declared fallback tier (ledger-
  logged), or fail loud via TerminalGovernanceHalt when no valid fallback.

The legacy upstream fallback chain stays LIVE for undeclared tiers (C2d-2
severs it); these tests exercise only the new gated path.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from grove.dispatcher import Dispatcher
from grove.errors import GroveError, TierUnavailableError
from grove.governance_halt import TerminalGovernanceHalt
from grove.intents import FinalResponse, ToolBatchYield, ToolIntent


# ── fixtures / harness ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


def _bare_agent():
    import run_agent
    from grove.tool_executor import ToolResult

    agent = object.__new__(run_agent.AIAgent)
    agent._current_messages = []
    agent.model = "m"
    agent.provider = "p"
    agent.session_id = "c2d_session"

    class _StubExecutor:
        def execute_batch_concurrent(self, ctx):
            return self._run(ctx)

        def execute_batch_sequential(self, ctx):
            return self._run(ctx)

        def _run(self, ctx):
            return [
                ToolResult(
                    intent_id=i.call_id or "", tool_name=i.tool_name,
                    tool_args=dict(i.arguments or {}), success=True, content="ok",
                )
                for i in ctx.intents
            ]

    class _Ctx:
        def __init__(self, intents):
            self.intents = list(intents)

    agent._tool_executor = _StubExecutor()
    agent._build_execution_context_concurrent = lambda intents, t, n: _Ctx(intents)
    agent._build_execution_context_sequential = lambda intents, t, n: _Ctx(intents)
    agent._apply_execution_results_to_messages = lambda r, m, t: None
    agent._executing_tools = False
    return agent


def _raises_gen(exc):
    def gen():
        raise exc
        yield  # pragma: no cover — makes this a generator function
    return gen()


def _success_gen(text="recovered"):
    def gen():
        yield FinalResponse(content=text)
        return {"final_response": text}
    return gen()


def _stub_dispatch_setup(d: Dispatcher, monkeypatch):
    """Neutralize the pre-drive setup so tests focus on the drive + policy."""
    monkeypatch.setattr("grove.dispatcher.pattern_cache_enabled", lambda: False)
    monkeypatch.setattr(d, "_apply_tier_budget", lambda agent, tier: None)
    monkeypatch.setattr(d, "_write_intent_record", lambda agent, **k: None)
    # `already_routed=True` path reads providers.current_classification/current_tier
    monkeypatch.setattr("grove.providers.current_classification", lambda: None)
    monkeypatch.setattr("grove.providers.current_tier", lambda: "T2")


# ══════════════════════════════════════════════════════════════════════
# TierUnavailableError + tier_fallback_for
# ══════════════════════════════════════════════════════════════════════


class TestTierUnavailableError:
    def test_is_grove_error_carrying_tier_provider(self):
        e = TierUnavailableError(
            tier="T2", provider="anthropic", model="claude-sonnet-4-6",
            reason="network_exhausted",
        )
        assert isinstance(e, GroveError)
        assert e.tier == "T2"
        assert e.provider == "anthropic"
        assert e.reason == "network_exhausted"
        assert "T2" in str(e)


class TestTierFallbackFor:
    def test_reads_declared_fallback_tier(self, monkeypatch):
        from grove.router import TierConfig
        import grove.providers as providers
        cfg = TierConfig(
            tier="T2", handler=None, provider="anthropic", model="x",
            max_tokens=8192, max_latency_ms=None, description="",
            fallback_tier="T1",
        )
        monkeypatch.setattr("grove.router.get_tier_config", lambda t: cfg)
        assert providers.tier_fallback_for("T2") == "T1"

    def test_none_when_undeclared(self, monkeypatch):
        from grove.router import TierConfig
        import grove.providers as providers
        cfg = TierConfig(
            tier="T2", handler=None, provider="anthropic", model="x",
            max_tokens=8192, max_latency_ms=None, description="",
        )
        monkeypatch.setattr("grove.router.get_tier_config", lambda t: cfg)
        assert providers.tier_fallback_for("T2") is None

    def test_none_when_router_uninitialized(self, monkeypatch):
        import grove.providers as providers

        def _boom(t):
            raise RuntimeError("router not initialized")

        monkeypatch.setattr("grove.router.get_tier_config", _boom)
        assert providers.tier_fallback_for("T2") is None
        assert providers.tier_fallback_for(None) is None


# ══════════════════════════════════════════════════════════════════════
# (a) run_agent gate — DETECT LOW, GATED
# ══════════════════════════════════════════════════════════════════════


class TestRaiseTierUnavailable:
    """C2d-2 — the gate is dropped: _raise_tier_unavailable raises
    UNCONDITIONALLY on a network-execution failure (declared or not). The
    Dispatcher owns the downshift-vs-halt decision for ALL tiers."""

    def test_raises_regardless_of_declared_fallback(self, monkeypatch):
        agent = _bare_agent()
        agent.provider = "anthropic"
        agent.model = "claude-sonnet-4-6"
        monkeypatch.setattr("grove.providers.current_tier", lambda: "T2")
        with pytest.raises(TierUnavailableError) as exc_info:
            agent._raise_tier_unavailable(reason="network_exhausted")
        assert exc_info.value.tier == "T2"
        assert exc_info.value.provider == "anthropic"
        assert exc_info.value.reason == "network_exhausted"

    def test_raises_even_when_current_tier_unreadable(self, monkeypatch):
        """A config/router problem must not mask the failure — still raise
        (tier=None), just without the tier label."""
        agent = _bare_agent()

        def _boom():
            raise RuntimeError("providers broke")

        monkeypatch.setattr("grove.providers.current_tier", _boom)
        with pytest.raises(TierUnavailableError) as exc_info:
            agent._raise_tier_unavailable(reason="network_exhausted")
        assert exc_info.value.tier is None
        assert exc_info.value.reason == "network_exhausted"


# ══════════════════════════════════════════════════════════════════════
# (b)(c)(d) Dispatcher policy — DECIDE HIGH + SEAM
# ══════════════════════════════════════════════════════════════════════


class TestDispatcherPolicy:
    def test_seam_and_declared_fallback_downshifts_and_reroutes(self, monkeypatch):
        """(b)+(d) — TierUnavailableError bubbles past the drive to the
        Dispatcher (SEAM), which re-routes THROUGH the router at the declared
        fallback tier, ledger-logs tier_fallback, and re-drives to completion."""
        d = Dispatcher()
        _stub_dispatch_setup(d, monkeypatch)
        agent = _bare_agent()

        # First drive raises TierUnavailableError; the re-driven turn succeeds.
        calls = {"n": 0}

        def _gen_factory(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _raises_gen(TierUnavailableError(
                    tier="T2", provider="anthropic", model="m",
                    reason="network_exhausted",
                ))
            return _success_gen("recovered")

        agent._run_turn_generator = _gen_factory

        # Declared fallback T2 → T1.
        monkeypatch.setattr("grove.providers.tier_fallback_for",
                            lambda t: "T1" if t == "T2" else None)

        # Capture the router re-route (explicit_tier pins the fallback tier).
        rerouted = {}

        class _Decision:
            tier = "T1"

        def _route_for_agent(*, message, explicit_model, explicit_tier):
            rerouted["explicit_tier"] = explicit_tier
            return _Decision()

        monkeypatch.setattr("grove.providers.route_for_agent", _route_for_agent)
        monkeypatch.setattr("grove.providers.resolve_tier_to_runtime",
                            lambda tc: {})
        # Don't actually swap clients — just record the bind.
        bound = {}
        monkeypatch.setattr(
            d, "_bind_agent_to_tier",
            lambda agent_, decision, resolver: bound.setdefault("tier", decision.tier),
        )

        # Capture ledger events.
        events: List[Dict[str, Any]] = []

        class _Ledger:
            def record(self, event_type, **fields):
                events.append({"event_type": event_type, **fields})
                return {}

        monkeypatch.setattr(d, "_get_or_create_ledger", lambda agent_: _Ledger())

        result = d.dispatch_turn(agent, user_message="hi", already_routed=True)

        # Re-drove and completed at the fallback tier.
        assert result["final_response"] == "recovered"
        assert calls["n"] == 2  # original + re-driven
        # Re-route went THROUGH the router pinned to the fallback tier.
        assert rerouted["explicit_tier"] == "T1"
        assert bound["tier"] == "T1"
        # tier_fallback ledger event logged.
        fb_events = [e for e in events if e["event_type"] == "tier_fallback"]
        assert len(fb_events) == 1
        assert fb_events[0]["failed_tier"] == "T2"
        assert fb_events[0]["fallback_tier"] == "T1"

    def test_no_fallback_declared_halts_loud(self, monkeypatch):
        """(c) — declared on the failing tier but the fallback is undeclared /
        unavailable → TerminalGovernanceHalt(tier_unavailable). Default is Andon."""
        d = Dispatcher()
        _stub_dispatch_setup(d, monkeypatch)
        agent = _bare_agent()
        agent._run_turn_generator = lambda **kw: _raises_gen(
            TierUnavailableError(tier="T2", provider="anthropic", model="m",
                                 reason="network_exhausted")
        )
        # No fallback resolves for T2.
        monkeypatch.setattr("grove.providers.tier_fallback_for", lambda t: None)
        monkeypatch.setattr(d, "_get_or_create_ledger", lambda agent_: _NullLedger())

        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            d.dispatch_turn(agent, user_message="hi", already_routed=True)
        assert exc_info.value.context.trigger == "tier_unavailable"

    def test_fallback_that_also_fails_eventually_halts(self, monkeypatch):
        """A declared fallback that ALSO raises TierUnavailableError, with no
        further fallback, terminates loud rather than looping forever."""
        d = Dispatcher()
        _stub_dispatch_setup(d, monkeypatch)
        agent = _bare_agent()

        # Every drive raises (T2 fails; the re-routed T1 also fails).
        agent._run_turn_generator = lambda **kw: _raises_gen(
            TierUnavailableError(tier="T2", provider="anthropic", model="m",
                                 reason="network_exhausted")
        )
        # T2 -> T1, but T1 -> (none). The 'used' set prevents re-using T1.
        monkeypatch.setattr("grove.providers.tier_fallback_for",
                            lambda t: "T1" if t == "T2" else None)

        class _Decision:
            tier = "T1"

        monkeypatch.setattr("grove.providers.route_for_agent",
                            lambda **k: _Decision())
        monkeypatch.setattr("grove.providers.resolve_tier_to_runtime", lambda tc: {})
        monkeypatch.setattr(d, "_bind_agent_to_tier", lambda *a, **k: None)
        monkeypatch.setattr(d, "_get_or_create_ledger", lambda agent_: _NullLedger())

        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            d.dispatch_turn(agent, user_message="hi", already_routed=True)
        assert exc_info.value.context.trigger == "tier_unavailable"

    def test_reroute_returns_none_when_router_cannot_resolve(self, monkeypatch):
        """If route_for_agent returns None (vanilla / unknown tier), the
        re-route fails and the turn halts loud — no blind swap."""
        d = Dispatcher()
        _stub_dispatch_setup(d, monkeypatch)
        agent = _bare_agent()
        agent._run_turn_generator = lambda **kw: _raises_gen(
            TierUnavailableError(tier="T2", provider="anthropic", model="m",
                                 reason="network_exhausted")
        )
        monkeypatch.setattr("grove.providers.tier_fallback_for",
                            lambda t: "T1" if t == "T2" else None)
        monkeypatch.setattr("grove.providers.route_for_agent", lambda **k: None)
        monkeypatch.setattr(d, "_get_or_create_ledger", lambda agent_: _NullLedger())

        with pytest.raises(TerminalGovernanceHalt) as exc_info:
            d.dispatch_turn(agent, user_message="hi", already_routed=True)
        assert exc_info.value.context.trigger == "tier_unavailable"


class _NullLedger:
    def record(self, event_type, **fields):
        return {}
