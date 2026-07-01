"""R5 — pure per-skill tier resolver (precedence + no-bleed + specialty no-op)."""

from __future__ import annotations

import pytest

from grove.capability import ModelBinding
from grove.skill_binding import resolve_skill_tier


def test_tier_override_routes_to_bound_tier():
    r = resolve_skill_tier(operator_active=False, model_binding=ModelBinding("tier_override", "T2"), turn_tier="T1")
    assert (r.tier, r.reason) == ("T2", "skill_tier_override")


def test_operator_override_wins_over_skill_binding():
    # operator pinned the turn to T3; the skill's T2 binding is ignored.
    r = resolve_skill_tier(operator_active=True, model_binding=ModelBinding("tier_override", "T2"), turn_tier="T3")
    assert (r.tier, r.reason) == ("T3", "operator_override")


def test_specialty_is_validated_but_no_op():
    r = resolve_skill_tier(operator_active=False, model_binding=ModelBinding("specialty"), turn_tier="T1")
    assert (r.tier, r.reason) == ("T1", "skill_specialty_noop")


def test_no_binding_falls_to_turn_default():
    r = resolve_skill_tier(operator_active=False, model_binding=None, turn_tier="T1")
    assert (r.tier, r.reason) == ("T1", "turn_default")


def test_unknown_type_fails_loud():
    with pytest.raises(ValueError, match="unknown model_binding.type"):
        resolve_skill_tier(operator_active=False, model_binding=ModelBinding("mystery"), turn_tier="T1")


def test_no_bleed_two_skills_resolve_independently():
    # Skill A binds T2; skill B has no binding. B resolves to the turn default,
    # NOT A's T2 — each resolves from its own binding (no shared state).
    a = resolve_skill_tier(operator_active=False, model_binding=ModelBinding("tier_override", "T2"), turn_tier="T1")
    b = resolve_skill_tier(operator_active=False, model_binding=None, turn_tier="T1")
    assert a.tier == "T2"
    assert b.tier == "T1"
