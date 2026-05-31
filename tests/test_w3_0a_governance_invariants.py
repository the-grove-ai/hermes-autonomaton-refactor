"""W3.0a governance-integrity-audit-v1 — invariant tests.

These tests verify INVARIANTS, not behavior. Invariants break when
someone wires a shortcut. Behavior tests pass when shortcuts exist.

Architecture is the guarantee; policy is the promise. The pipeline
is immutable — it runs on every turn, no exceptions.

INSTRUMENTATION BOUNDARIES
==========================

Per W3.0a SPEC: tests MUST instrument the OUTPUT, not mock the
checkpoint being tested. Mocking route() to test that route() is
called is circular.

Two boundaries:

1. Telemetry emission boundary
   ``grove.telemetry.log_routing_decision`` is called from inside
   ``grove.providers.route_for_agent``. Patching it lets us count
   route_for_agent invocations per turn (I3, I6) without mocking
   route() itself.

2. Provider boundary
   For I2 we check the agent's ``self.model`` after governance
   fires — that field is what every API-call path reads. If the
   RoutingDecision says T2/Sonnet but self.model is still
   "claude-opus-4-7", routing was advisory not binding (I2 fail).

TEST ISOLATION
==============

A ``_governance_reset`` autouse fixture wipes:
  - GROVE_TIER / GROVE_INFERENCE_MODEL env vars
  - grove.router._default_router singleton (forces re-init)
  - grove.providers._last_routed_tier module global
  - grove.providers._last_classification module global

A model-preference contaminating one test's environment must NOT
silently make another test pass (false-green discipline).
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import grove.providers
import grove.router
import grove.classify
from grove.classify import ClassificationResult
from run_agent import AIAgent

# Sprint 52 GATE-B (grove-autonomaton fork) — same rationale as
# tests/test_w3_0_governance_pipeline.py. These invariant tests verify
# the legacy in-Agent ``_maybe_route_for_turn`` call stack that
# Sprint 35 deleted (routing moved to
# ``grove.dispatcher.Dispatcher._classify_and_bind_turn``). The
# governance invariants themselves still hold; they are exercised by
# the Dispatcher-side test suite at ``tests/grove/``. Marked rather
# than rewritten per GATE-B Override #1.
pytestmark = pytest.mark.skip(
    reason="grove-autonomaton: legacy routing replaced by Dispatcher, see Sprint 35",
)


# ── shared fixtures ────────────────────────────────────────────────


VALID_ROUTING_CONFIG = """\
routing:
  schema_version: 1
  default_tier: T2
  tier_preferences:
    T0:
      handler: pattern_cache
      description: Pattern cache.
      max_latency_ms: 50
    T1:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      description: Cheap.
      max_tokens: 4096
    T2:
      provider: anthropic
      model: claude-sonnet-4-6
      description: Premium.
      max_tokens: 8192
    T3:
      provider: anthropic
      model: claude-opus-4-6
      description: Apex.
      max_tokens: 16384
  escalation:
    threshold: 0.6
    description: Confidence dial.
  routing_rules:
    upward:
      enabled: true
      match:
        complexity: [complex, novel]
        intents: [planning, analysis, code_generation, debugging]
      target_tier: T3
    escalation:
      enabled: true
      match:
        max_confidence: 0.6
      action: step_up
  telemetry:
    tier: T1
    description: Scoring tier.
"""


@pytest.fixture(autouse=True)
def _governance_reset(monkeypatch):
    """Hard-reset all governance singletons + env between tests so a
    bleeding GROVE_TIER or cached _last_classification cannot make a
    later test silently pass."""
    monkeypatch.delenv("GROVE_TIER", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)
    grove.router._default_router = None
    grove.providers._last_routed_tier = None
    grove.providers._last_classification = None
    yield
    grove.router._default_router = None
    grove.providers._last_routed_tier = None
    grove.providers._last_classification = None


@pytest.fixture
def routing_config(tmp_path, monkeypatch):
    """Install a known routing.config.yaml at a temp HOME so the
    router resolves to this config rather than the repo default."""
    home = tmp_path / "home"
    grove_dir = home / ".grove"
    grove_dir.mkdir(parents=True)
    (grove_dir / "routing.config.yaml").write_text(VALID_ROUTING_CONFIG)
    monkeypatch.setenv("HOME", str(home))
    grove.router._default_router = None
    return grove_dir / "routing.config.yaml"


def _bare_agent(*, model="", provider="anthropic"):
    """An AIAgent skeleton with just the fields _maybe_route_for_turn
    reads and the methods it calls. apply_tier / switch_model record
    their arguments so tests can inspect what was applied."""
    agent = object.__new__(AIAgent)
    agent.model = model
    agent.provider = provider
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = "anthropic_messages"
    agent.max_tokens = None
    agent._last_routing_decision = None
    agent._last_classification_result = None
    agent._apply_tier_calls = []
    agent._switch_model_calls = []

    def _record_apply_tier(model_, max_tokens_):
        agent._apply_tier_calls.append((model_, max_tokens_))
        agent.model = model_
        if max_tokens_ is not None:
            agent.max_tokens = max_tokens_

    def _record_switch_model(**kwargs):
        agent._switch_model_calls.append(kwargs)
        agent.model = kwargs.get("new_model") or agent.model
        agent.provider = kwargs.get("new_provider") or agent.provider

    agent.apply_tier = _record_apply_tier
    agent.switch_model = _record_switch_model
    return agent


def _canned_classification(
    *, intent="simple_question", confidence=0.85, complexity="simple"
):
    return ClassificationResult(
        intent_class=intent,
        pattern_hash="dead0001",
        confidence=confidence,
        register_class="standards",
        complexity_signal=complexity,
    )


# ────────────────────────────────────────────────────────────────────
# I1: NO UNGOVERNED PATH TO MODEL API
# ────────────────────────────────────────────────────────────────────


class TestI1NoUngovernedPath:
    """Every code path from user input to an API call passes through
    route_for_agent. No exceptions. The telemetry boundary
    (log_routing_decision) is the proof — it fires exactly when
    route_for_agent runs, regardless of caller."""

    def test_webui_style_call_hits_route_for_agent(self, routing_config):
        """A webui-style call (no pre-route, no already_routed)
        MUST cause route_for_agent to fire — proven by the
        telemetry emission."""
        agent = _bare_agent()
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            agent._maybe_route_for_turn("what is 2 plus 2?")
        assert log_route.called, (
            "I1 violated: webui-style call did not reach route_for_agent "
            "— ungoverned path to model API"
        )
        assert log_route.call_count == 1

    def test_classifier_failure_still_governs_turn(self, routing_config):
        """The pipeline is immutable. Classifier failure produces a
        degraded RoutingDecision (reason='classifier_unavailable',
        confidence=0.0). 'Preserve caller's model on classifier
        failure' is a bypass, NOT a fix. Verify the routing
        telemetry still fires AND the tier swap still applies."""
        agent = _bare_agent(provider="anthropic")
        # Mock ONLY the classifier's network call (not route() itself).
        with patch(
            "grove.classify.classify_for_routing", return_value=None
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            agent._maybe_route_for_turn("anything")
        assert log_route.called, (
            "I1 (degraded path) violated: classifier failure caused "
            "governance to be skipped — silent degradation bypass"
        )
        decision = agent._last_routing_decision
        assert decision is not None, (
            "I1: classifier failure must still produce a degraded "
            "RoutingDecision, not no decision"
        )
        assert decision.reason == "classifier_unavailable", (
            f"I1: degraded decision must carry reason='classifier_unavailable' "
            f"(got {decision.reason!r}); without that signal, classifier "
            f"outages are invisible to telemetry consumers"
        )
        assert decision.confidence == 0.0, (
            "I1: degraded decision must carry confidence=0.0"
        )
        # The turn IS governed — tier swap fires per the degraded decision.
        assert len(agent._apply_tier_calls) == 1, (
            "I1: degraded decision must still be APPLIED (tier swap), "
            "not skipped. Skipping is the bypass."
        )


# ────────────────────────────────────────────────────────────────────
# I2: ROUTING DECISION GOVERNS ACTUAL SELECTION
# ────────────────────────────────────────────────────────────────────


class TestI2RoutingDecisionGoverns:
    """The model string in the API call MATCHES RoutingDecision.model.
    Not 'a RoutingDecision was constructed.' The decision must be
    BINDING, not advisory."""

    def test_routed_model_replaces_agent_model(self, routing_config):
        """After _maybe_route_for_turn, agent.model MUST equal the
        RoutingDecision.tier_config.model. If self.model still holds
        the agent's original config-default value, routing was
        advisory, which is a bypass."""
        agent = _bare_agent(provider="anthropic", model="claude-opus-4-7")
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ):
            agent._maybe_route_for_turn("simple question")
        decision = agent._last_routing_decision
        assert decision is not None
        assert agent.model == decision.tier_config.model, (
            f"I2 violated: agent.model={agent.model!r} but routing "
            f"decided {decision.tier_config.model!r}. RoutingDecision "
            f"is advisory, not binding."
        )

    def test_routed_model_takes_effect_under_classifier_failure(
        self, routing_config
    ):
        """Even with classifier failed, the degraded routing decision
        is BINDING. agent.model after the swap must match the
        default tier's model (T2 in this config)."""
        agent = _bare_agent(provider="anthropic", model="claude-opus-4-7")
        with patch("grove.classify.classify_for_routing", return_value=None):
            agent._maybe_route_for_turn("anything")
        decision = agent._last_routing_decision
        assert decision is not None
        # default_tier in VALID_ROUTING_CONFIG is T2 (Sonnet)
        assert agent.model == "claude-sonnet-4-6", (
            f"I2 (degraded): agent.model={agent.model!r} did not adopt "
            f"the degraded decision's tier_config.model "
            f"(claude-sonnet-4-6). Degraded decision is advisory."
        )


# ────────────────────────────────────────────────────────────────────
# I3: CLASSIFICATION RESULT EMITTED
# ────────────────────────────────────────────────────────────────────


class TestI3ClassificationEmitted:
    """Every routing decision emits log_routing_decision. The
    telemetry event carries the classification fields (when a
    classifier result was available). Verified at the telemetry
    emission boundary, NOT the provider boundary."""

    def test_classification_enriches_routing_decision_event(self, routing_config):
        """When classify_for_routing returns a real ClassificationResult,
        the routing_decision event includes intent_class / confidence /
        complexity_signal / register_class / pattern_hash fields.
        log_routing_decision is keyword-only — verify by kwargs."""
        canned = _canned_classification(
            intent="code_generation", confidence=0.78, complexity="complex"
        )
        with patch(
            "grove.classify.classify_for_routing", return_value=canned
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            grove.providers.route_for_agent(message="refactor X")
        assert log_route.called
        kwargs = log_route.call_args.kwargs
        assert kwargs.get("intent_class") == "code_generation", (
            f"I3 violated: routing_decision event missing classification "
            f"intent_class (got {kwargs.get('intent_class')!r}); "
            f"classification fired but was not emitted in telemetry."
        )
        assert kwargs.get("complexity_signal") == "complex", (
            "I3: routing_decision event missing complexity_signal"
        )
        assert kwargs.get("pattern_hash") == "dead0001", (
            "I3: routing_decision event missing pattern_hash"
        )
        assert kwargs.get("register_class") == "standards", (
            "I3: routing_decision event missing register_class"
        )

    def test_routing_event_includes_decision_fields(self, routing_config):
        """Independent of classification, every routing_decision event
        carries the routing fields: tier, reason, model. These are the
        primary outputs of route() and the operator's view of governance."""
        with patch(
            "grove.classify.classify_for_routing", return_value=None
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            grove.providers.route_for_agent(message="anything")
        assert log_route.called
        kwargs = log_route.call_args.kwargs
        assert kwargs.get("tier") in {"T1", "T2", "T3"}
        assert kwargs.get("reason") == "classifier_unavailable", (
            "I3 (degraded): classifier failure must emit "
            "reason='classifier_unavailable' so outages are observable"
        )
        assert kwargs.get("model") == "claude-sonnet-4-6"


# ────────────────────────────────────────────────────────────────────
# I4: ZONE CHECKS UNSUPPRESSIBLE
# ────────────────────────────────────────────────────────────────────


class TestI4ZoneChecksUnsuppressible:
    """No try/except, no fallback, no silent-degradation path wraps
    zone classification on tool actions. If the zone check errors,
    the action does not proceed.

    The PROOF: patch grove.dispatch.classify_command to raise.
    check_all_command_guards must NOT return approved=True. It must
    return approved=False with a signal that the classifier failed.
    """

    def test_classifier_exception_blocks_action(self, monkeypatch):
        """Zone classifier raises → action is BLOCKED, not allowed
        through a legacy approval path."""
        from tools import approval

        # Make the function reach the zone check by setting the
        # benign command + cli env_type that bypasses hardline +
        # sudo guards and avoids GROVE_YOLO_MODE.
        monkeypatch.setenv("GROVE_INTERACTIVE", "1")
        monkeypatch.delenv("GROVE_YOLO_MODE", raising=False)
        monkeypatch.delenv("GROVE_EXEC_ASK", raising=False)

        def _boom(*a, **k):
            raise RuntimeError("zone schema failed to load")

        # Patch the symbol at its import location inside approval.py
        # (the function does `from grove.dispatch import classify_command`
        # inside its body, so we patch at the source module).
        monkeypatch.setattr(
            "grove.dispatch.classify_command", _boom, raising=True
        )

        result = approval.check_all_command_guards(
            command="ls -la",  # benign command — would normally pass legacy approval
            env_type="cli",
        )
        assert result.get("approved") is False, (
            "I4 violated: zone classifier exception did NOT block the "
            "action. The legacy approval flow let it through — that "
            "is the silent-degradation bypass. Fix: replace the "
            "fall-through with a fail-loud block."
        )
        # The block should identify the classifier failure so operators
        # can diagnose, not just deny generically.
        assert result.get("classifier_failed") is True, (
            "I4: block result must signal classifier_failed=True so "
            "operators can distinguish a classifier outage from a "
            "policy denial"
        )
        msg = (result.get("message") or "").lower()
        assert "classifier" in msg or "zone" in msg, (
            f"I4: block message must be actionable for the operator "
            f"(reference 'classifier' or 'zone'); got {result.get('message')!r}"
        )


# ────────────────────────────────────────────────────────────────────
# I5: SOVEREIGNTY GATE NON-BYPASSABLE
# ────────────────────────────────────────────────────────────────────


class TestI5SovereigntyGateNonBypassable:
    """When a Red-zone action fires, execution HALTS. No downstream
    code runs the action anyway. The gate is not advisory."""

    def test_red_zone_non_interactive_blocks(self, monkeypatch):
        """A Red zone classification in non-interactive context (no
        CLI/gateway/ask flags) returns approved=False with
        sovereign_red=True. The caller cannot ignore this and run."""
        from tools import approval
        from grove.zones import ZoneResult

        monkeypatch.delenv("GROVE_INTERACTIVE", raising=False)
        monkeypatch.delenv("GROVE_YOLO_MODE", raising=False)
        monkeypatch.delenv("GROVE_EXEC_ASK", raising=False)
        monkeypatch.delenv("GROVE_ZONE_STRICT", raising=False)

        red = ZoneResult(
            zone="red",
            matched_rule="test_red_rule",
            source="tool_zones",
        )
        monkeypatch.setattr(
            "grove.dispatch.classify_command",
            lambda *_a, **_k: red,
            raising=True,
        )

        result = approval.check_all_command_guards(
            command="rm -rf /tmp/some_path",
            env_type="cli",
        )
        assert result.get("approved") is False, (
            "I5 violated: Red-zone action was approved. The gate is "
            "advisory, not binding."
        )
        assert result.get("zone_classified") == "red"
        assert result.get("sovereign_red") is True


# ────────────────────────────────────────────────────────────────────
# I6: EXACTLY-ONCE CLASSIFICATION
# ────────────────────────────────────────────────────────────────────


class TestI6ExactlyOnceClassification:
    """Each turn produces exactly one T-telemetry classifier call.
    Not zero (skipped). Not two (double-fire from CLI pre-route +
    AIAgent self-route). Verified at both the telemetry emission
    boundary AND the classifier-invocation boundary."""

    def test_self_route_fires_classifier_exactly_once(self, routing_config):
        """A webui-style call (no pre-route, no already_routed): the
        classifier fires exactly once via _maybe_route_for_turn."""
        agent = _bare_agent()
        classifier_calls = []

        def _counting(msg):
            classifier_calls.append(msg)
            return _canned_classification()

        with patch("grove.classify.classify_for_routing", _counting):
            agent._maybe_route_for_turn("test message")
        assert len(classifier_calls) == 1, (
            f"I6 violated: classifier fired {len(classifier_calls)} times "
            f"on a single _maybe_route_for_turn call (expected 1)"
        )

    def test_already_routed_signal_prevents_double_classification(self):
        """Simulates the CLI pattern: CLI pre-routes (one classifier
        call via _resolve_turn_agent_config), then calls
        run_conversation. If the CLI passes already_routed=True,
        _maybe_route_for_turn is skipped — no second classifier call."""
        agent = _bare_agent()
        classifier_calls = []

        def _counting(msg):
            classifier_calls.append(msg)
            return _canned_classification()

        # The already_routed gate is structural — checked by the
        # caller (run_conversation) before invoking
        # _maybe_route_for_turn. Mimic the run_conversation entry
        # logic to prove the gate's intent.
        already_routed = True
        with patch("grove.classify.classify_for_routing", _counting):
            if not already_routed:
                agent._maybe_route_for_turn("test message")
        assert classifier_calls == [], (
            "I6 (gated) violated: already_routed=True did not prevent "
            "_maybe_route_for_turn from firing"
        )

    def test_telemetry_emission_count_matches_classifier_count(
        self, routing_config
    ):
        """The number of routing_decision events emitted MUST equal
        the number of route_for_agent invocations (the canonical
        single-call point per turn). One in, one out."""
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            grove.providers.route_for_agent(message="test")
            grove.providers.route_for_agent(message="test 2")
            grove.providers.route_for_agent(message="test 3")
        assert log_route.call_count == 3, (
            f"I6 (parity): 3 route_for_agent calls but only "
            f"{log_route.call_count} routing_decision events. "
            f"Telemetry is being dropped."
        )


# ────────────────────────────────────────────────────────────────────
# I7: OPERATOR PREFERENCE FEEDS ROUTER, NEVER BYPASSES IT
# ────────────────────────────────────────────────────────────────────


class TestI7OperatorPreferenceFeedsRouter:
    """A non-empty model / --model / operator_model is an INPUT to
    route(). It is resolved to a tier WITHIN the pipeline. It never
    causes the pipeline to be skipped. Tests must exercise routing
    WITH a model preference set and verify a RoutingDecision is
    still produced."""

    def test_operator_model_input_still_produces_routing_decision(
        self, routing_config
    ):
        """explicit_model='something-else' is INPUT to the router.
        A RoutingDecision MUST be produced (not skipped). If routing
        is skipped because the operator preference 'wins', that's
        the bypass W3.0a forbids."""
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            decision = grove.providers.route_for_agent(
                message="some message",
                explicit_model="something-else",
            )
        assert decision is not None, (
            "I7 violated: explicit_model caused route_for_agent to "
            "skip routing entirely. Operator preference is INPUT to "
            "the router, not a bypass."
        )
        assert log_route.called, (
            "I7: routing_decision telemetry must fire even when "
            "operator_model is set"
        )

    def test_operator_model_matching_a_tier_resolves_to_that_tier(
        self, routing_config
    ):
        """If the operator preference matches a tier's model, the
        router routes to that tier with reason='operator_model_preference'
        — pipeline-internal resolution, not bypass."""
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ):
            decision = grove.providers.route_for_agent(
                message="anything",
                explicit_model="claude-sonnet-4-6",
            )
        assert decision is not None
        assert decision.tier == "T2"
        assert decision.reason == "operator_model_preference"

    def test_operator_model_untiered_keeps_model_at_default_tier(
        self, routing_config
    ):
        """An operator model that matches no tier resolves WITHIN
        the pipeline to reason='operator_model_untiered' on the
        default tier — not by skipping routing."""
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ):
            decision = grove.providers.route_for_agent(
                message="anything",
                explicit_model="custom/unknown-model-name",
            )
        assert decision is not None
        assert decision.reason == "operator_model_untiered"

    def test_webui_agent_with_non_empty_model_still_routes(self, routing_config):
        """The W3.0a-key test the SPEC calls out specifically: a
        webui agent constructed with model='claude-opus-4-7' (a
        config.yaml default, NOT operator intent) routes via the
        pipeline. self.model is NOT passed as explicit_model
        (W3.0 fix) so it never shadows the routing decision.
        RoutingDecision is produced; tier swap applies."""
        agent = _bare_agent(provider="anthropic", model="claude-opus-4-7")
        with patch(
            "grove.classify.classify_for_routing",
            return_value=_canned_classification(),
        ), patch(
            "grove.providers.log_routing_decision"
        ) as log_route:
            agent._maybe_route_for_turn("anything")
        assert log_route.called, (
            "I7 (W3.0a-key): agent.model='claude-opus-4-7' caused "
            "routing to be skipped. This is the operator-preference "
            "bypass that W3.0's Andon corrected."
        )
        assert agent._last_routing_decision is not None
        # Tier swap fired
        assert len(agent._apply_tier_calls) == 1
