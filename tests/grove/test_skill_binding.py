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


# ── aux-model-bindings-v1: type=model is fleet-only — Mylo path refuses ───────


def _sentinel_andon_halts():
    """All andon_halt events filed under cli-* sentinel sessions this test."""
    import json

    from grove.kaizen_ledger import default_ledger_dir

    events = []
    for f in sorted(default_ledger_dir().glob("cli-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("event_type") == "andon_halt":
                events.append(ev)
    return events


def _assert_refusal(skill_name):
    """Shared body: refuse + file + continue, detail names the skill verbatim."""
    binding = ModelBinding(type="model", model="pin-org/pin-model")
    r = resolve_skill_tier(
        operator_active=False, model_binding=binding, turn_tier="T1",
        skill_name=skill_name,
    )
    # No raise (caller fires on failed invoke_skill attempts — a turn must
    # never detonate), no rebind: the turn default is preserved.
    assert (r.tier, r.reason) == ("T1", "model_binding_mylo_refusal")
    halts = _sentinel_andon_halts()
    assert len(halts) == 1  # exactly one filing
    ev = halts[0]
    assert ev["source"] == "skill_binding"
    assert ev["check"] == "model_binding_mylo_refusal"
    assert repr(skill_name) in ev["detail"]
    assert "fleet-only" in ev["detail"]
    assert "pin-org/pin-model" in ev["detail"]


def test_model_pin_on_mylo_path_refuses_files_and_continues(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="grove.skill_binding"):
        _assert_refusal("forge-jobsearch")
    assert "forge-jobsearch" in caplog.text  # warning names the skill


def test_model_pin_refusal_identical_for_traversal_shaped_name():
    # Phase 0 finding: invoke_skill("fleet/<name>") traverses the nested skills
    # dir and skill_record_for_name resolves the slashed form to the same fleet
    # record — the refusal must behave identically on that shape.
    _assert_refusal("fleet/forge-jobsearch")


def test_tier_override_still_resolves_after_model_branch():
    # The shipped rebind path is untouched by the refusal branch.
    r = resolve_skill_tier(
        operator_active=False,
        model_binding=ModelBinding("tier_override", "T2"),
        turn_tier="T1",
        skill_name="any-skill",
    )
    assert (r.tier, r.reason) == ("T2", "skill_tier_override")
    assert _sentinel_andon_halts() == []  # no spurious filing
