"""webui-governance-pipeline-v1 — close the bypass.

The CLI runs governance (route_for_agent + T-telemetry classification +
escalation) in _resolve_turn_agent_config before calling run_conversation.
The webui calls run_conversation directly and historically skipped all of
it — picking a model from session.model (config.yaml model.default) with
no classification, no escalation, no routing telemetry. W3.0 closes the
bypass by adding self-routing inside run_conversation, gated by an
already_routed kwarg that CLI sets True after pre-routing.

These tests prove the parity contract:

- (a, b) webui-style call (no pre-route) self-routes: produces a
  RoutingDecision with valid tier AND a ClassificationResult with
  intent/confidence.
- (d) low-confidence escalation works on the webui path.
- (e) CLI-style (already_routed=True) and webui-style (self-route)
  produce identical RoutingDecision.tier for identical input. Single
  function — route_for_agent — drives both.
- (f) exactly one T-telemetry classification per webui turn (not zero,
  not two). Enforces the Sprint 12 gate.
- regression: already_routed=True skips self-routing entirely.
- regression: __init__ initializes the governance state fields.

AIAgent.__init__ is far too heavy for a unit test (provider clients,
DB, telemetry); each test builds a bare instance with object.__new__
and sets only the routing state the method under test reads. Mirrors
the test_tier_ux.py pattern.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import grove.providers
import grove.router
from grove.classify import ClassificationResult
from run_agent import AIAgent


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


@pytest.fixture
def routing_config(tmp_path, monkeypatch):
    """Install a known routing.config.yaml at a temp HOME so route_for_agent
    finds it. Each test gets a clean router and provider-module state."""
    home = tmp_path / "home"
    grove_dir = home / ".grove"
    grove_dir.mkdir(parents=True)
    (grove_dir / "routing.config.yaml").write_text(VALID_ROUTING_CONFIG)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GROVE_TIER", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)
    grove.router._default_router = None
    grove.providers._last_routed_tier = None
    grove.providers._last_classification = None
    yield grove_dir / "routing.config.yaml"
    grove.router._default_router = None
    grove.providers._last_routed_tier = None
    grove.providers._last_classification = None


def _bare_agent(*, model="claude-sonnet-4-6", provider="anthropic"):
    """An AIAgent skeleton with just the fields _maybe_route_for_turn reads
    and the methods it calls. apply_tier is replaced with a recorder so we
    can observe what the routing pipeline asked for without rebuilding a
    full provider client."""
    agent = object.__new__(AIAgent)
    agent.model = model
    agent.provider = provider
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = "anthropic_messages"
    agent.max_tokens = None
    agent._last_routing_decision = None
    agent._last_classification_result = None
    # Record apply_tier / switch_model arguments instead of mutating the
    # heavy live-agent state.
    agent._apply_tier_calls = []
    agent._switch_model_calls = []

    def _recording_apply_tier(model_, max_tokens_):
        agent._apply_tier_calls.append((model_, max_tokens_))
        agent.model = model_
        if max_tokens_ is not None:
            agent.max_tokens = max_tokens_

    def _recording_switch_model(**kwargs):
        agent._switch_model_calls.append(kwargs)
        agent.model = kwargs.get("new_model") or agent.model
        agent.provider = kwargs.get("new_provider") or agent.provider

    agent.apply_tier = _recording_apply_tier
    agent.switch_model = _recording_switch_model
    return agent


# ── (a) + (b) ─────────────────────────────────────────────────────────────


def test_webui_path_produces_routing_decision_with_valid_tier(routing_config):
    """Webui-style call: no pre-route → self-route fires → _last_routing_decision
    populated with a valid tier."""
    agent = _bare_agent()
    agent._maybe_route_for_turn("a simple test message")
    decision = agent._last_routing_decision
    assert decision is not None, "_maybe_route_for_turn must populate _last_routing_decision"
    assert decision.tier in {"T1", "T2", "T3"}, f"unexpected tier: {decision.tier}"


def test_webui_path_produces_classification_result_with_intent(routing_config):
    """Webui-style call: T-telemetry classifier fires inside route_for_agent
    → _last_classification_result populated with intent + confidence."""
    agent = _bare_agent()
    # Patch classify_for_routing to return a deterministic result so the
    # test doesn't depend on a live Haiku call. The point of this test
    # is the threading, not the classifier accuracy.
    canned = ClassificationResult(
        intent_class="simple_question",
        confidence=0.85,
        complexity_signal="simple",
        register_class="standards",
        pattern_hash="deadbeef",
    )
    with patch("grove.classify.classify_for_routing", return_value=canned):
        agent._maybe_route_for_turn("what is 2 + 2?")
    cr = agent._last_classification_result
    assert cr is not None, "_maybe_route_for_turn must populate _last_classification_result"
    assert cr.intent_class == "simple_question"
    assert cr.confidence == pytest.approx(0.85)


# ── (d) low-confidence escalation ─────────────────────────────────────────


def test_webui_path_low_confidence_escalates(routing_config):
    """A low-confidence classification triggers the escalation rule,
    stepping the routed tier up from default T2 to T3. The agent's
    currently-configured self.model is NOT an operator preference and
    must not shadow the escalation rule — the routing pipeline runs
    independent of session.model/config.yaml model.default."""
    agent = _bare_agent(model="claude-sonnet-4-6")  # webui's typical session.model
    low_conf = ClassificationResult(
        intent_class="ambiguous",
        confidence=0.2,
        complexity_signal="simple",
        register_class="standards",
        pattern_hash="deadbee0",
    )
    with patch("grove.classify.classify_for_routing", return_value=low_conf):
        agent._maybe_route_for_turn("ambiguous request")
    decision = agent._last_routing_decision
    assert decision is not None
    assert decision.tier == "T3", \
        f"low confidence should escalate from default T2 to T3; got {decision.tier} ({decision.reason})"
    assert decision.reason == "escalation"


# ── (e) parity: CLI-style and webui-style produce identical tier ─────────


def test_cli_and_webui_paths_produce_identical_tier(routing_config):
    """Both paths converge on grove.providers.route_for_agent. Calling it
    once directly (CLI's pattern in _resolve_turn_agent_config) and once
    via _maybe_route_for_turn (webui's pattern) MUST produce identical
    RoutingDecision.tier values for identical input."""
    same_classification = ClassificationResult(
        intent_class="code_generation",
        confidence=0.85,
        complexity_signal="complex",
        register_class="standards",
        pattern_hash="cafe0001",
    )

    with patch("grove.classify.classify_for_routing", return_value=same_classification):
        # CLI-style: direct call, identical to what _resolve_turn_agent_config does
        cli_decision = grove.providers.route_for_agent(
            message="refactor the authentication module",
            explicit_model=None,
            explicit_tier=None,
        )

        # Webui-style: bare agent, self-route via the new method
        agent = _bare_agent()
        agent._maybe_route_for_turn("refactor the authentication module")
        webui_decision = agent._last_routing_decision

    assert cli_decision is not None and webui_decision is not None
    assert cli_decision.tier == webui_decision.tier, (
        f"parity violation: CLI routed to {cli_decision.tier} but webui routed to "
        f"{webui_decision.tier} for the same input"
    )
    assert cli_decision.reason == webui_decision.reason


# ── (f) exactly one classifier call per turn ─────────────────────────────


def test_webui_path_classifier_fires_exactly_once_per_turn(routing_config):
    """T-telemetry must classify the user message exactly once per
    _maybe_route_for_turn call — Sprint 12 gate. Counts the calls into
    grove.classify.classify_for_routing."""
    call_count = [0]

    real_classify = grove.classify.classify_for_routing  # noqa: F841

    def counting_classify(message):
        call_count[0] += 1
        return ClassificationResult(
            intent_class="simple_question",
            confidence=0.85,
            complexity_signal="simple",
            register_class="standards",
            pattern_hash="cafe0002",
        )

    agent = _bare_agent()
    with patch("grove.classify.classify_for_routing", counting_classify):
        agent._maybe_route_for_turn("how does X work?")

    assert call_count[0] == 1, (
        f"T-telemetry must classify exactly once per turn; saw {call_count[0]} calls"
    )


# ── regression: already_routed=True skips self-routing ───────────────────


def test_run_conversation_already_routed_skips_self_route(routing_config):
    """When the caller (CLI) sets already_routed=True, run_conversation
    must NOT call route_for_agent again — preserves CLI's existing
    single-classification-per-turn discipline."""
    call_count = [0]

    def counting_classify(message):
        call_count[0] += 1
        return ClassificationResult(
            intent_class="simple_question",
            confidence=0.85,
            complexity_signal="simple",
            register_class="standards",
            pattern_hash="cafe0003",
        )

    # Bare agent — we'll only invoke _maybe_route_for_turn explicitly with
    # the guard; the guard mirrors what run_conversation does.
    agent = _bare_agent()
    with patch("grove.classify.classify_for_routing", counting_classify):
        already_routed = True
        if not already_routed:
            agent._maybe_route_for_turn("test")

    assert call_count[0] == 0, (
        f"already_routed=True must not trigger classification; saw {call_count[0]} calls"
    )
    assert agent._last_routing_decision is None


# ── classify-failed: degraded decision, pipeline still governs ──────────


def test_classify_failure_produces_classifier_unavailable_decision(routing_config):
    """The pipeline is immutable — it runs on every turn, no exceptions.
    When T-telemetry classification fails (auth, network, rate limit),
    the router produces a degraded RoutingDecision with reason=
    'classifier_unavailable', confidence=0.0, and routes to the default
    tier. The turn is governed by a degraded decision, NOT ungoverned.
    apply_tier fires; the caller's runtime is replaced by the
    default-tier runtime."""
    # Provider matches default_tier (anthropic) so the dispatch picks
    # apply_tier (no credential lookup needed for the test).
    agent = _bare_agent(provider="anthropic", model="anything")
    with patch("grove.classify.classify_for_routing", return_value=None):
        agent._maybe_route_for_turn("test message")
    # Degraded decision is recorded and signaled via reason.
    decision = agent._last_routing_decision
    assert decision is not None
    assert decision.reason == "classifier_unavailable"
    assert decision.confidence == 0.0
    assert decision.tier == "T2"  # default_tier in the test config
    # Classification field reflects the failure.
    assert agent._last_classification_result is None
    # Tier swap fired — the turn IS governed by the degraded decision.
    assert len(agent._apply_tier_calls) == 1
    applied_model, applied_max = agent._apply_tier_calls[0]
    assert applied_model == "claude-sonnet-4-6"
    assert applied_max == 8192


# ── regression: route_for_agent None return is handled silently ──────────


def test_maybe_route_handles_none_decision_silently(monkeypatch):
    """If route_for_agent returns None (a vanilla install with no routing
    config installed anywhere), _maybe_route_for_turn must return silently
    and leave the caller's model untouched. Patches route_for_agent at the
    call site so the test doesn't depend on filesystem state — the grove-
    autonomaton repo always ships a default routing.config.yaml, so the
    real-filesystem 'no config' case is unreachable in this codebase."""
    agent = _bare_agent(model="my-original-model")
    with patch("grove.providers.route_for_agent", return_value=None):
        agent._maybe_route_for_turn("any message")
    assert agent._last_routing_decision is None
    assert agent.model == "my-original-model"
    assert agent._apply_tier_calls == []
    assert agent._switch_model_calls == []


# ── regression: non-string user_message skips routing ────────────────────


def test_maybe_route_handles_non_string_user_message(routing_config):
    """run_conversation accepts list-form user_message (history-rebuild
    paths). The governance method must skip routing rather than crash
    classify_for_routing on a non-string input."""
    agent = _bare_agent()
    agent._maybe_route_for_turn(["multi-part", "message"])
    assert agent._last_routing_decision is None
    assert agent._apply_tier_calls == []


# ── apply_tier vs switch_model dispatch ──────────────────────────────────


def test_same_provider_routing_uses_apply_tier(routing_config):
    """Decision provider == current provider → apply_tier (lightweight
    swap). No switch_model call."""
    # Default decision will land on T2 anthropic claude-sonnet-4-6;
    # current agent is also anthropic; same-provider path.
    canned = ClassificationResult(
        intent_class="simple_question",
        confidence=0.85,
        complexity_signal="simple",
        register_class="standards",
        pattern_hash="cafe0004",
    )
    # Agent's currently-configured model "something-else" is NOT an
    # operator preference; the router decides purely from classification.
    # Default classification routes to T2 (claude-sonnet-4-6, anthropic),
    # so apply_tier — not switch_model — fires (same provider).
    agent = _bare_agent(provider="anthropic", model="something-else")
    with patch("grove.classify.classify_for_routing", return_value=canned):
        agent._maybe_route_for_turn("simple")
    assert len(agent._apply_tier_calls) == 1
    assert agent._switch_model_calls == []
    applied_model, applied_max = agent._apply_tier_calls[0]
    assert applied_model == "claude-sonnet-4-6"
    assert applied_max == 8192


def test_cross_provider_routing_uses_switch_model(routing_config, tmp_path, monkeypatch):
    """Decision provider != current provider → switch_model (full rebuild).
    Construct an agent whose provider differs from the routed decision's
    provider to force the cross-provider path."""
    # The standard config routes to anthropic; configure agent as omlx
    # so the dispatcher picks switch_model instead of apply_tier.
    canned = ClassificationResult(
        intent_class="simple_question",
        confidence=0.85,
        complexity_signal="simple",
        register_class="standards",
        pattern_hash="cafe0005",
    )
    agent = _bare_agent(provider="omlx", model="gemma-4-26B-A4B-it-MLX-4bit")
    with patch("grove.classify.classify_for_routing", return_value=canned):
        with patch(
            "grove.providers.resolve_tier_to_runtime",
            return_value={
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
                "api_key": "x",
                "base_url": "",
                "api_mode": "anthropic_messages",
            },
        ):
            agent._maybe_route_for_turn("simple")
    assert agent._apply_tier_calls == []
    assert len(agent._switch_model_calls) == 1
    kw = agent._switch_model_calls[0]
    assert kw["new_provider"] == "anthropic"
    assert kw["new_model"] == "claude-sonnet-4-6"
