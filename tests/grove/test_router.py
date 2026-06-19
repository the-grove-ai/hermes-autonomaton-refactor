"""Tests for grove.router — the Cognitive Router config loader."""

import logging
from pathlib import Path

import pytest

from grove.router import CognitiveRouter, RoutingDecision, TierConfig

VALID_CONFIG = """\
routing:
  schema_version: 1
  default_tier: T2
  tier_preferences:
    T0:
      handler: pattern_cache
      description: Deterministic recall.
      max_latency_ms: 50
    T1:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      description: Cheap cognition.
      max_tokens: 4096
    T2:
      provider: anthropic
      model: claude-sonnet-4-6
      description: Premium cognition.
      max_tokens: 8192
    T3:
      provider: anthropic
      model: claude-opus-4-6
      description: Apex cognition.
      max_tokens: 16384
  escalation:
    threshold: 0.6
    description: Confidence dial.
  telemetry:
    tier: T1
    description: Scoring tier.
"""


def _write(tmp_path: Path, text: str = VALID_CONFIG) -> Path:
    cfg = tmp_path / "routing.config.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_t1_get_tier_config_returns_sonnet(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    t2 = router.get_tier_config("T2")
    assert isinstance(t2, TierConfig)
    assert t2.tier == "T2"
    assert t2.provider == "anthropic"
    assert t2.model == "claude-sonnet-4-6"
    assert t2.max_tokens == 8192


def test_t2_t0_returns_pattern_cache_handler(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    t0 = router.get_tier_config("T0")
    assert t0.handler == "pattern_cache"
    assert t0.provider is None
    assert t0.model is None
    assert t0.max_latency_ms == 50


def test_t3_default_tier(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    assert router.get_default_tier() == "T2"


def test_t4_escalation_threshold(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    threshold = router.get_escalation_threshold()
    assert threshold == 0.6
    assert isinstance(threshold, float)


def test_t5_model_independence_swap(tmp_path):
    """Principle 7: swap T1 to a local provider by config edit alone."""
    swapped = VALID_CONFIG.replace(
        "      provider: anthropic\n      model: claude-haiku-4-5-20251001",
        "      provider: ollama\n      model: gemma-4",
    )
    router = CognitiveRouter(_write(tmp_path, swapped))
    t1 = router.get_tier_config("T1")
    assert t1.provider == "ollama"
    assert t1.model == "gemma-4"


def test_t6_reload_picks_up_valid_change(tmp_path):
    cfg = _write(tmp_path)
    router = CognitiveRouter(cfg)
    assert router.get_default_tier() == "T2"
    cfg.write_text(
        VALID_CONFIG.replace("default_tier: T2", "default_tier: T3"),
        encoding="utf-8",
    )
    router.reload()
    assert router.get_default_tier() == "T3"


def test_t7_reload_invalid_keeps_last_good(tmp_path, caplog):
    cfg = _write(tmp_path)
    router = CognitiveRouter(cfg)
    cfg.write_text("routing: {unclosed", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        router.reload()
    assert router.get_default_tier() == "T2"
    assert router.get_tier_config("T2").model == "claude-sonnet-4-6"
    assert any("reload failed" in r.message for r in caplog.records)


def test_t8_missing_config_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        CognitiveRouter(tmp_path / "does-not-exist.yaml")


def test_t9_unknown_tier_raises_keyerror(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    with pytest.raises(KeyError):
        router.get_tier_config("T9")


def test_t10_bad_schema_version_raises_valueerror(tmp_path):
    bad = VALID_CONFIG.replace("schema_version: 1", "schema_version: 2")
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


# ----- Sprint 11: route() ------------------------------------------------------

ZONE_OVERRIDE_CONFIG = VALID_CONFIG.replace(
    "  tier_preferences:",
    "  zone_overrides:\n    red: T3\n  tier_preferences:",
)


def test_route_default_returns_t2(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route()
    assert isinstance(d, RoutingDecision)
    assert d.tier == "T2"
    # No classifier inputs — the pipeline still routes to default tier
    # but tags the decision as degraded (W3.0) so classifier outages
    # are observable in telemetry.
    assert d.reason == "classifier_unavailable"
    assert d.confidence == 0.0
    assert d.tier_config.model == "claude-sonnet-4-6"
    assert d.pattern_cache_hit is False


def test_route_operator_tier_overrides(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_tier="T3")
    assert d.tier == "T3"
    assert d.reason == "operator_override"
    assert d.tier_config.model == "claude-opus-4-6"


def test_route_operator_model_preference(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_model="claude-opus-4-6")
    assert d.tier == "T3"
    assert d.reason == "operator_model_preference"
    assert d.tier_config.model == "claude-opus-4-6"


def test_route_operator_model_untiered(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_model="some-unlisted-model")
    assert d.tier == "T2"  # runs in the default tier's slot
    assert d.reason == "operator_model_untiered"
    assert d.tier_config.model == "some-unlisted-model"
    assert d.tier_config.provider == "anthropic"  # default tier's provider


def test_route_operator_tier_beats_operator_model(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(operator_tier="T1", operator_model="claude-opus-4-6")
    assert d.tier == "T1"
    assert d.reason == "operator_override"


def test_route_model_to_tier(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    assert router.model_to_tier("claude-sonnet-4-6") == "T2"
    assert router.model_to_tier("claude-opus-4-6") == "T3"
    assert router.model_to_tier("not-a-bound-model") is None


def test_route_zone_override(tmp_path):
    router = CognitiveRouter(_write(tmp_path, ZONE_OVERRIDE_CONFIG))
    d = router.route(zone="red")
    assert d.tier == "T3"
    assert d.reason == "zone_override"
    d_green = router.route(zone="green")
    assert d_green.tier == "T2"
    # Zone passes through (no override for green) and lands on default
    # tier; with no classifier inputs the decision is tagged degraded.
    assert d_green.reason == "classifier_unavailable"


def test_route_escalation_on_low_confidence(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    d = router.route(confidence=0.4)  # below threshold 0.6
    assert d.tier == "T3"  # T2 default escalated one step
    assert d.reason == "escalation"
    d_ok = router.route(confidence=0.9)  # above threshold
    assert d_ok.tier == "T2"
    assert d_ok.reason == "default"


def test_route_t0_pattern_cache_always_miss(tmp_path):
    router = CognitiveRouter(_write(tmp_path))
    for kwargs in ({}, {"operator_tier": "T3"}, {"confidence": 0.4}):
        assert router.route(**kwargs).pattern_cache_hit is False


def test_route_circuit_breaker_threshold_zero(tmp_path):
    cfg = VALID_CONFIG.replace("threshold: 0.6", "threshold: 0.0")
    router = CognitiveRouter(_write(tmp_path, cfg))
    d = router.route(confidence=0.01)  # would escalate if enabled
    assert d.tier == "T2"
    assert d.reason == "default"  # escalation disabled at threshold 0.0


# ----- Sprint 14.1: declarative routing rules ---------------------------------

RULES_CONFIG = VALID_CONFIG.replace(
    "  telemetry:",
    """\
  routing_rules:
    downward:
      enabled: true
      match:
        complexity: simple
        min_confidence: 0.85
      target_tier: T1
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
  telemetry:""",
)


def test_route_upward_to_t3(tmp_path):
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="complex", intent="planning", confidence=0.9)
    assert d.tier == "T3"
    assert d.reason == "upward"


def test_route_upward_matches_novel_complexity(tmp_path):
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="novel", intent="debugging", confidence=0.8)
    assert d.tier == "T3"
    assert d.reason == "upward"


def test_route_upward_needs_both_complexity_and_intent(tmp_path):
    """A complex request with a non-matching intent does not go up."""
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="complex", intent="conversation", confidence=0.9)
    assert d.tier == "T2"
    assert d.reason == "default"


def test_route_downward_to_t1(tmp_path):
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="simple", intent="conversation", confidence=0.95)
    assert d.tier == "T1"
    assert d.reason == "downward"


def test_route_downward_needs_min_confidence(tmp_path):
    """Simple work below the confidence floor does not go down."""
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="simple", confidence=0.7)
    assert d.tier == "T2"
    assert d.reason == "default"


def test_route_downward_disabled_holds_default(tmp_path):
    disabled = RULES_CONFIG.replace(
        "    downward:\n      enabled: true",
        "    downward:\n      enabled: false",
    )
    router = CognitiveRouter(_write(tmp_path, disabled))
    d = router.route(complexity_signal="simple", confidence=0.95)
    assert d.tier == "T2"
    assert d.reason == "default"


def test_route_escalation_rule_steps_up(tmp_path):
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(confidence=0.4)
    assert d.tier == "T3"  # default T2 stepped up one rung
    assert d.reason == "escalation"


def test_route_first_matching_rule_wins(tmp_path):
    """upward is evaluated before escalation — a request matching both
    resolves as upward."""
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(complexity_signal="complex", intent="planning", confidence=0.4)
    assert d.tier == "T3"
    assert d.reason == "upward"  # not "escalation", though 0.4 < 0.6 too


def test_route_rules_skipped_without_classification(tmp_path):
    """No classification signals — every rule abstains, the pipeline
    routes to the default tier and tags the decision as degraded so
    classifier outages are observable downstream (W3.0)."""
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route()
    assert d.tier == "T2"
    assert d.reason == "classifier_unavailable"
    assert d.confidence == 0.0


def test_route_operator_override_beats_rules(tmp_path):
    router = CognitiveRouter(_write(tmp_path, RULES_CONFIG))
    d = router.route(
        operator_tier="T1", complexity_signal="complex",
        intent="planning", confidence=0.9,
    )
    assert d.tier == "T1"
    assert d.reason == "operator_override"


def test_route_escalation_max_confidence_overrides_threshold(tmp_path):
    """routing_rules.escalation.match.max_confidence wins over the
    top-level escalation.threshold."""
    cfg = RULES_CONFIG.replace("max_confidence: 0.6", "max_confidence: 0.3")
    router = CognitiveRouter(_write(tmp_path, cfg))
    # 0.5 < 0.6 (top-level) but >= 0.3 (the rule) — must NOT escalate.
    assert router.route(confidence=0.5).reason == "default"
    assert router.route(confidence=0.2).reason == "escalation"


def test_route_downward_missing_target_tier_raises(tmp_path):
    bad = RULES_CONFIG.replace("      target_tier: T1\n", "")
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


def test_route_escalation_bad_action_raises(tmp_path):
    bad = RULES_CONFIG.replace("action: step_up", "action: teleport")
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


def test_route_rule_unknown_target_tier_raises(tmp_path):
    bad = RULES_CONFIG.replace("target_tier: T1", "target_tier: T9")
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


# --- Sprint 20: local-tier-binding MVP config (T2 bound to local Gemma 4) ---
# The daily-driver shape: T1 Haiku classify, T2 Gemma 4 local, T3 Opus apex.
# downward is disabled, so simple work holds on the local T2 tier.
MVP_CONFIG = RULES_CONFIG.replace(
    "    T2:\n      provider: anthropic\n      model: claude-sonnet-4-6",
    "    T2:\n      provider: ollama\n      model: gemma4",
).replace(
    "    downward:\n      enabled: true",
    "    downward:\n      enabled: false",
)


def test_route_mvp_simple_stays_on_local_t2(tmp_path):
    """Simple, high-confidence work holds on T2 — the local Gemma 4 tier."""
    router = CognitiveRouter(_write(tmp_path, MVP_CONFIG))
    d = router.route(complexity_signal="simple", intent="factual_retrieval",
                     confidence=0.95)
    assert d.tier == "T2"
    assert d.reason == "default"
    assert d.tier_config.provider == "ollama"
    assert d.tier_config.model == "gemma4"


def test_route_mvp_complex_escalates_to_opus_t3(tmp_path):
    """Complex planning work escalates to T3 — the cloud apex model."""
    router = CognitiveRouter(_write(tmp_path, MVP_CONFIG))
    d = router.route(complexity_signal="complex", intent="planning", confidence=0.9)
    assert d.tier == "T3"
    assert d.reason == "upward"
    assert d.tier_config.model == "claude-opus-4-6"


def test_route_mvp_tier_swap_gemma_opus_gemma_per_turn(tmp_path):
    """Per-turn routing: one turn on local Gemma 4, the next on Opus, then
    back. route() carries no state — each turn is decided fresh, so the
    binding is never sticky across a swap."""
    router = CognitiveRouter(_write(tmp_path, MVP_CONFIG))
    simple = dict(complexity_signal="simple", intent="factual_retrieval",
                  confidence=0.95)
    hard = dict(complexity_signal="complex", intent="planning", confidence=0.9)
    models = [
        router.route(**simple).tier_config.model,
        router.route(**hard).tier_config.model,
        router.route(**simple).tier_config.model,
    ]
    assert models == ["gemma4", "claude-opus-4-6", "gemma4"]


# ----- declarative-routing-rules-v1: arbitrary names, config-key order -------
# GRV-001 Invariant I — the rule NAME is no longer locked in code: every
# routing_rules key parses as a rule, in config (insertion) order, and a
# malformed rule / unknown key fails loud naming the offender.


def _rules_cfg(rules_yaml: str) -> str:
    """VALID_CONFIG (default_tier T2, tiers T0–T3) with a routing_rules block
    spliced in above telemetry. ``rules_yaml`` is the 4-space-indented body."""
    return VALID_CONFIG.replace(
        "  telemetry:", "  routing_rules:\n" + rules_yaml + "  telemetry:",
    )


def _router_from(tmp_path: Path, name: str, text: str) -> CognitiveRouter:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return CognitiveRouter(p)


NOVEL_NAMES_CONFIG = _rules_cfg(
    """\
    premium_coding:
      enabled: true
      match:
        complexity: [simple, moderate, complex]
        intents: [code_generation, debugging]
      target_tier: T2
    apex_coding:
      enabled: true
      match:
        complexity: [novel]
        intents: [code_generation, debugging]
      target_tier: T3
"""
)


def test_novel_named_rule_parses_and_routes(tmp_path):
    """The core fix: a rule with a name outside the legacy tuple parses and
    evaluates, and its reason IS the novel name (passthrough-safe)."""
    router = CognitiveRouter(_write(tmp_path, NOVEL_NAMES_CONFIG))
    d = router.route(complexity_signal="complex", intent="debugging", confidence=0.9)
    assert d.tier == "T2"
    assert d.reason == "premium_coding"
    d2 = router.route(complexity_signal="novel", intent="code_generation", confidence=0.9)
    assert d2.tier == "T3"
    assert d2.reason == "apex_coding"


def test_unknown_rule_level_key_raises_naming_offender(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      targt_tier: T2
      match:
        intents: [research]
"""
    )
    with pytest.raises(ValueError, match=r"premium.*targt_tier|targt_tier"):
        CognitiveRouter(_write(tmp_path, bad))


def test_unknown_match_key_raises_naming_offender(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: T2
      match:
        complexty: simple
"""
    )
    with pytest.raises(ValueError, match=r"complexty"):
        CognitiveRouter(_write(tmp_path, bad))


def test_unknown_match_key_urgency_raises(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: T2
      match:
        urgency: high
"""
    )
    with pytest.raises(ValueError, match=r"urgency"):
        CognitiveRouter(_write(tmp_path, bad))


def test_non_mapping_rule_spec_raises(tmp_path):
    bad = _rules_cfg("    premium: T2\n")
    with pytest.raises(ValueError, match=r"premium.*mapping"):
        CognitiveRouter(_write(tmp_path, bad))


def test_non_string_target_tier_raises(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: 2
      match:
        intents: [research]
"""
    )
    with pytest.raises(ValueError, match=r"target_tier"):
        CognitiveRouter(_write(tmp_path, bad))


def test_non_mapping_match_raises(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: T2
      match: simple
"""
    )
    with pytest.raises(ValueError, match=r"match must be a mapping"):
        CognitiveRouter(_write(tmp_path, bad))


def test_bad_complexity_type_raises(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: T2
      match:
        complexity:
          nested: bad
"""
    )
    with pytest.raises(ValueError):
        CognitiveRouter(_write(tmp_path, bad))


def test_bad_confidence_type_raises(tmp_path):
    bad = _rules_cfg(
        """\
    premium:
      enabled: true
      target_tier: T2
      match:
        min_confidence: high
"""
    )
    with pytest.raises(ValueError, match=r"min_confidence"):
        CognitiveRouter(_write(tmp_path, bad))


def test_escalation_is_positional_first_wins(tmp_path):
    """A DECLARED escalation evaluates at its config position. Listed FIRST,
    it wins over a later target rule on a turn matching both."""
    esc_first = _rules_cfg(
        """\
    escalation:
      enabled: true
      match:
        max_confidence: 0.6
      action: step_up
    upward:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
"""
    )
    router = CognitiveRouter(_write(tmp_path, esc_first))
    d = router.route(complexity_signal="complex", intent="planning", confidence=0.4)
    assert d.reason == "escalation"  # not "upward" — escalation is first
    assert d.tier == "T3"  # default T2 stepped up one rung


def test_escalation_position_flips_winner(tmp_path):
    """Same overlapping low-confidence turn; only escalation's position
    relative to upward differs. Position decides the winner."""
    esc_first = _rules_cfg(
        """\
    escalation:
      enabled: true
      match:
        max_confidence: 0.6
      action: step_up
    upward:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
"""
    )
    esc_last = _rules_cfg(
        """\
    upward:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
    escalation:
      enabled: true
      match:
        max_confidence: 0.6
      action: step_up
"""
    )
    turn = dict(complexity_signal="complex", intent="planning", confidence=0.4)
    r_first = _router_from(tmp_path, "esc_first.yaml", esc_first)
    r_last = _router_from(tmp_path, "esc_last.yaml", esc_last)
    assert r_first.route(**turn).reason == "escalation"
    assert r_last.route(**turn).reason == "upward"


def test_synthesized_escalation_when_absent_appended_last(tmp_path):
    """A config with routing_rules but NO escalation key still steps up on
    low confidence — the step_up is synthesized from escalation.threshold and
    appended last."""
    cfg = _rules_cfg(
        """\
    upward:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
"""
    )
    router = CognitiveRouter(_write(tmp_path, cfg))
    d = router.route(confidence=0.4)  # below top-level threshold 0.6
    assert d.reason == "escalation"
    assert d.tier == "T3"  # default T2 + 1


def test_eval_order_is_config_key_order(tmp_path):
    """Two rules with overlapping match; first in config wins. Reordering
    them flips the winner — eval order follows config-key order."""
    a_first = _rules_cfg(
        """\
    rule_a:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T2
    rule_b:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
"""
    )
    b_first = _rules_cfg(
        """\
    rule_b:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T3
    rule_a:
      enabled: true
      match:
        complexity: [complex]
        intents: [planning]
      target_tier: T2
"""
    )
    turn = dict(complexity_signal="complex", intent="planning", confidence=0.9)
    ra = _router_from(tmp_path, "a.yaml", a_first)
    rb = _router_from(tmp_path, "b.yaml", b_first)
    assert (ra.route(**turn).reason, ra.route(**turn).tier) == ("rule_a", "T2")
    assert (rb.route(**turn).reason, rb.route(**turn).tier) == ("rule_b", "T3")


def test_real_repo_config_routes_unchanged(tmp_path):
    """Regression: the live repo config (upward_moderate, upward, escalation)
    parses and routes identically under the generalized parser."""
    repo_cfg = Path(__file__).resolve().parents[2] / "config" / "routing.config.yaml"
    router = CognitiveRouter(repo_cfg)
    # upward_moderate: moderate knowledge work → T2
    assert router.route(
        complexity_signal="moderate", intent="research", confidence=0.9
    ).tier == "T2"
    # upward: complex/novel knowledge work → T3
    assert router.route(
        complexity_signal="complex", intent="planning", confidence=0.9
    ).tier == "T3"
    # daily driver → default T1
    assert router.route(
        complexity_signal="simple", intent="conversation", confidence=0.95
    ).tier == "T1"


def test_malformed_rule_reload_keeps_prior_config(tmp_path, caplog):
    """All-or-nothing swap (#14): a malformed rule on reload is rejected and
    the prior loaded config stays intact."""
    p = tmp_path / "routing.config.yaml"
    p.write_text(RULES_CONFIG, encoding="utf-8")
    router = CognitiveRouter(p)
    before = router.route(complexity_signal="complex", intent="planning", confidence=0.9)
    assert before.reason == "upward"
    # Inject an unknown match key and reload — must reject, keep last-good.
    p.write_text(
        RULES_CONFIG.replace(
            "        complexity: [complex, novel]",
            "        complexity: [complex, novel]\n        bogus: x",
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        router.reload()
    after = router.route(complexity_signal="complex", intent="planning", confidence=0.9)
    assert after.reason == "upward"  # prior config survived the rejected reload
