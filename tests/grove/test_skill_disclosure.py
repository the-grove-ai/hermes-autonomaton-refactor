"""GRV-009 E6a C1 — kind=skill resolution + the passive-data wrapper (A8).

Proves the registry-side read path for skills, with NO skills migrated yet:

* the ``<skill_reference_data>`` wrapper + system note (A8 — every skill body
  enters context as passive data that never overrides core directives);
* a synthetic kind=skill record resolves via the registry on PULL (the E5b
  disclosure default), returning its wrapped body;
* the structured, governance-free category fields (lock 2) round-trip and are
  kept OUT of trigger/zone/tier;
* the category-description side-record loader (keyed by category name).

No filesystem scan is touched here — the legacy machinery stays authoritative
until C2 migrates the records and C3 retires the scan.
"""

from __future__ import annotations

import pytest

from grove.capability import (
    Capability,
    CapabilityKind,
    CircuitBreaker,
    Context,
    Disclosure,
    Failure,
    Lifecycle,
    LifecycleState,
    Provenance,
    SkillPresentation,
    Telemetry,
    TierRule,
    TierValidation,
    Trigger,
    Zone,
)
from grove.skill_disclosure import (
    SKILL_REFERENCE_CLOSE,
    SKILL_REFERENCE_NOTE,
    SKILL_REFERENCE_OPEN,
    load_skill_category_descriptions,
    resolve_skill_record,
    wrap_skill_body,
)

_BODY = "# Research workflow\n\nStep 1: search.\nStep 2: synthesize.\n"


def make_skill(**overrides) -> Capability:
    """A fully valid kind=skill record (provenance=migrated, active, pull body)."""
    base = dict(
        id="skill.research.web-research",
        kind=CapabilityKind.SKILL,
        zone=Zone.GREEN,
        # Live skills carry no intent/keyword frontmatter — they ride the
        # always-loaded index, so always=True relaxes the strict trigger rule.
        trigger=Trigger(always=True),
        tier_rule=TierRule(
            eligible=[1, 2, 3],
            preferred=1,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(disclosure=Disclosure.PULL, payload=_BODY),
        lifecycle=Lifecycle(
            state=LifecycleState.ACTIVE, provenance=Provenance.MIGRATED
        ),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category="research"),
    )
    base.update(overrides)
    return Capability(**base)


# ── The passive-data wrapper (A8) ────────────────────────────────────────────


def test_wrap_skill_body_delimits_and_notes():
    wrapped = wrap_skill_body(_BODY)
    assert wrapped.startswith(SKILL_REFERENCE_OPEN)
    assert wrapped.rstrip().endswith(SKILL_REFERENCE_CLOSE)
    assert SKILL_REFERENCE_NOTE in wrapped
    # the original body survives verbatim inside the wrapper
    assert _BODY in wrapped


def test_wrap_skill_body_note_disclaims_instruction_authority():
    # A8 — the note must mark the content informational and subordinate to core
    # directives, so a body that "looks like commands" can't seize the channel.
    note = SKILL_REFERENCE_NOTE.lower()
    assert "never override" in note or "never overrides" in note
    assert "core directive" in note
    assert "passive" in note or "informational" in note


def test_wrap_skill_body_is_deterministic():
    # Byte-stable — the wrapper is part of the C2 golden, so it must not vary.
    assert wrap_skill_body(_BODY) == wrap_skill_body(_BODY)


# ── kind=skill resolution (pull) ─────────────────────────────────────────────


def test_synthetic_skill_record_resolves_pull_wrapped():
    rec = make_skill()
    # the E5b disclosure default is pull — skills ride index-then-skill_view
    assert rec.context.disclosure is Disclosure.PULL
    out = resolve_skill_record(rec)
    assert out == wrap_skill_body(_BODY)
    assert _BODY in out


def test_resolve_rejects_non_skill_record():
    from tests.grove.test_capability import make_valid

    verb = make_valid()  # kind=verb
    with pytest.raises(ValueError, match="not 'skill'"):
        resolve_skill_record(verb)


def test_resolve_rejects_empty_body():
    rec = make_skill(context=Context(disclosure=Disclosure.PULL, payload=""))
    with pytest.raises(ValueError, match="empty"):
        resolve_skill_record(rec)


def test_resolve_rejects_eager_skill_no_silent_path():
    # C1 has no eager skill-body injection path; an eager skill must fail loud,
    # not slip a body into context unwrapped behind a non-existent eager path.
    rec = make_skill(context=Context(disclosure=Disclosure.EAGER, payload=_BODY))
    with pytest.raises(ValueError, match="pull"):
        resolve_skill_record(rec)


# ── structured category fields (lock 2) ──────────────────────────────────────


def test_skill_presentation_round_trips():
    rec = make_skill()
    restored = Capability.from_yaml(rec.to_yaml())
    assert restored == rec
    assert restored.skill is not None
    assert restored.skill.category == "research"


def test_category_is_out_of_trigger_zone_tier():
    rec = make_skill()
    d = rec.to_dict()
    # presentation grouping lives in its own block — never overloaded onto a
    # governance field (the tool_groups anti-pattern lock 2 forbids).
    assert d["skill"] == {"category": "research"}
    assert "category" not in d["trigger"]
    assert "category" not in d["tier_rule"]
    assert "category" not in str(d["zone"])


def test_non_skill_record_emits_no_skill_block():
    from tests.grove.test_capability import make_valid

    verb = make_valid()
    assert "skill" not in verb.to_dict()  # conditional emission — zero blast radius


def test_skill_record_requires_presentation_block():
    with pytest.raises(ValueError, match="skill"):
        make_skill(skill=None)


def test_skill_block_rejected_on_non_skill_kind():
    from tests.grove.test_capability import make_valid

    with pytest.raises(ValueError, match="kind=skill"):
        make_valid(skill=SkillPresentation(category="research"))


def test_skill_category_must_be_non_empty():
    with pytest.raises(ValueError, match="category"):
        make_skill(skill=SkillPresentation(category=""))


# ── category-description side-record (keyed by category name) ─────────────────


def test_load_category_descriptions_reads_mapping(tmp_path):
    p = tmp_path / "skill_categories.yaml"
    p.write_text(
        "version: 1\ncategories:\n  research: Research and synthesis skills.\n"
        "  content: Writing and editorial skills.\n",
        encoding="utf-8",
    )
    out = load_skill_category_descriptions(p)
    assert out == {
        "research": "Research and synthesis skills.",
        "content": "Writing and editorial skills.",
    }


def test_load_category_descriptions_absent_is_empty(tmp_path):
    # An absent side-record is legitimate (no category descriptions declared —
    # mirrors DESCRIPTION.md-absent), NOT a failure to swallow.
    assert load_skill_category_descriptions(tmp_path / "nope.yaml") == {}


def test_load_category_descriptions_malformed_fails_loud(tmp_path):
    p = tmp_path / "skill_categories.yaml"
    p.write_text("version: 1\ncategories:\n  research: [not, a, string]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_skill_category_descriptions(p)
