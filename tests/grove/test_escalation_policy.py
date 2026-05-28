"""Tests for grove.escalation_policy — Sprint 30 Phase 2 policy logic.

Covers the EscalationPolicy / EscalationDecision dataclasses, the
load_escalation_policy reader (default + present + malformed config),
and evaluate_escalation rule ordering (disabled / per-turn ceiling /
per-session budget / unknown depth / ceiling tier exceeded / already
at-or-above target / granted).
"""

from __future__ import annotations

import pytest

from grove.escalation_policy import (
    EscalationDecision,
    EscalationPolicy,
    evaluate_escalation,
    load_escalation_policy,
)


# ── EscalationPolicy defaults ─────────────────────────────────────────────


class TestPolicyDefaults:
    def test_default_policy_is_disabled(self):
        p = EscalationPolicy()
        assert p.enabled is False
        assert p.max_escalations_per_turn == 1
        assert p.max_escalations_per_session == 5
        assert p.ceiling_tier == "T3"

    def test_default_mapping_covers_four_depths(self):
        p = EscalationPolicy()
        assert p.mapping == {
            "shallow": "T1",
            "moderate": "T2",
            "deep": "T3",
            "apex": "T3",
        }

    def test_resolved_tier_maps_known_depth(self):
        p = EscalationPolicy()
        assert p.resolved_tier("deep") == "T3"
        assert p.resolved_tier("shallow") == "T1"

    def test_resolved_tier_returns_none_for_unknown_depth(self):
        p = EscalationPolicy()
        assert p.resolved_tier("yolo") is None
        assert p.resolved_tier(None) is None
        assert p.resolved_tier("") is None


# ── load_escalation_policy ────────────────────────────────────────────────


class TestLoadEscalationPolicy:
    def test_missing_routing_block_defaults_to_disabled(self):
        # Vanilla install — no routing config at all.
        p = load_escalation_policy({})
        assert p.enabled is False

    def test_missing_escalation_block_defaults_to_disabled(self):
        # Routing config present but no escalation_policy key.
        p = load_escalation_policy({"routing": {"default_tier": "T2"}})
        assert p.enabled is False

    def test_malformed_escalation_block_defaults_to_disabled(self):
        # esc_policy is not a dict — should NOT raise.
        p = load_escalation_policy({
            "routing": {"escalation_policy": "broken"}
        })
        assert p.enabled is False

    def test_loads_enabled_with_custom_values(self):
        p = load_escalation_policy({
            "routing": {
                "escalation_policy": {
                    "enabled": True,
                    "max_escalations_per_turn": 2,
                    "max_escalations_per_session": 10,
                    "ceiling_tier": "T2",
                    "mapping": {"shallow": "T0", "deep": "T2"},
                },
            },
        })
        assert p.enabled is True
        assert p.max_escalations_per_turn == 2
        assert p.max_escalations_per_session == 10
        assert p.ceiling_tier == "T2"
        assert p.mapping == {"shallow": "T0", "deep": "T2"}

    def test_malformed_mapping_falls_back_to_default(self):
        # mapping is not a dict — silently restore defaults.
        p = load_escalation_policy({
            "routing": {
                "escalation_policy": {
                    "enabled": True,
                    "mapping": "broken",
                },
            },
        })
        assert p.mapping["deep"] == "T3"


# ── evaluate_escalation — denial paths ────────────────────────────────────


class TestEvaluateDenialPaths:
    def test_disabled_policy_denies(self):
        p = EscalationPolicy(enabled=False)
        d = evaluate_escalation(
            policy=p,
            current_tier="T2",
            requested_depth="deep",
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=0,
        )
        assert d.granted is False
        assert "disabled" in d.reason

    def test_per_turn_ceiling_denies(self):
        p = EscalationPolicy(enabled=True, max_escalations_per_turn=1)
        d = evaluate_escalation(
            policy=p,
            current_tier="T2",
            requested_depth="deep",
            requested_context="normal",
            turn_escalations_so_far=1,  # already hit the ceiling
            session_escalations_so_far=0,
        )
        assert d.granted is False
        assert "per-turn ceiling" in d.reason

    def test_per_session_budget_denies(self):
        p = EscalationPolicy(enabled=True, max_escalations_per_session=3)
        d = evaluate_escalation(
            policy=p,
            current_tier="T2",
            requested_depth="deep",
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=3,
        )
        assert d.granted is False
        assert "per-session budget" in d.reason

    def test_unknown_depth_denies(self):
        p = EscalationPolicy(enabled=True)
        d = evaluate_escalation(
            policy=p,
            current_tier="T2",
            requested_depth="yolo",
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=0,
        )
        assert d.granted is False
        assert "not in policy mapping" in d.reason

    def test_ceiling_exceeded_denies(self):
        # Policy ceiling T2 + request maps to T3 — deny.
        p = EscalationPolicy(enabled=True, ceiling_tier="T2")
        d = evaluate_escalation(
            policy=p,
            current_tier="T1",
            requested_depth="deep",  # → T3 by default
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=0,
        )
        assert d.granted is False
        assert "exceeds policy ceiling" in d.reason

    def test_already_at_target_denies_with_no_op(self):
        # Agent at T3 asks for deep (→T3) — no escalation needed.
        p = EscalationPolicy(enabled=True)
        d = evaluate_escalation(
            policy=p,
            current_tier="T3",
            requested_depth="deep",
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=0,
        )
        assert d.granted is False
        assert "already at-or-above" in d.reason
        # target_tier still populated for ledger visibility.
        assert d.target_tier == "T3"


# ── evaluate_escalation — grant path ──────────────────────────────────────


class TestEvaluateGrantPath:
    def test_grant_with_target_tier(self):
        p = EscalationPolicy(enabled=True)
        d = evaluate_escalation(
            policy=p,
            current_tier="T2",
            requested_depth="deep",
            requested_context="extended",
            turn_escalations_so_far=0,
            session_escalations_so_far=2,  # under budget
        )
        assert d.granted is True
        assert d.target_tier == "T3"
        assert d.current_tier == "T2"
        assert "deep → T3" in d.reason
        assert "extended" in d.reason

    def test_grant_when_no_current_tier_known(self):
        # Vanilla install with no router — current_tier=None should
        # still permit grants. (The escalation policy operates above
        # routing in the loaded-config dependency graph.)
        p = EscalationPolicy(enabled=True)
        d = evaluate_escalation(
            policy=p,
            current_tier=None,
            requested_depth="moderate",
            requested_context="normal",
            turn_escalations_so_far=0,
            session_escalations_so_far=0,
        )
        assert d.granted is True
        assert d.target_tier == "T2"


# ── EscalationDecision dataclass ──────────────────────────────────────────


class TestEscalationDecisionShape:
    def test_is_frozen(self):
        d = EscalationDecision(granted=True, reason="r")
        with pytest.raises((AttributeError, Exception)):
            d.granted = False

    def test_target_tier_optional_on_denial(self):
        d = EscalationDecision(granted=False, reason="nope")
        assert d.target_tier is None
