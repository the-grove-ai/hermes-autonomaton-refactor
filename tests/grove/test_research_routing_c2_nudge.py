"""research-routing-coherence-v1 C2 — intent→skill disclosure nudge.

Covers the shared field-driven match rule
(``agent.prompt_builder.matched_skill_slugs_for_intent``), the composer
provider (``grove.prompt.composer._skill_nudge_provider``), the F3 template-lock
(no record prose reaches the prompt), the F4 boundary sentence, multi-match
rendering, and the additive ``matched_skill_slugs`` telemetry field on the
tool_selection event (``Dispatcher._matched_skill_slugs_for_turn``).

The primary match test drives the REAL record-load path (the real researcher
record, intent_class="research"), not a synthetic record only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.prompt_builder import (
    matched_skill_slugs_for_intent,
    render_skill_nudge_line,
)
from grove.capability import CapabilityKind, LifecycleState
from grove.prompt.composer import _skill_nudge_provider

BOUNDARY = "Do NOT invoke it for simple retrieval"


# ── synthetic record factory (field-driven; monkeypatched into the registry) ──


def _fake_record(
    rec_id: str,
    intents,
    *,
    state=LifecycleState.ACTIVE,
    kind=CapabilityKind.SKILL,
    description: str = "d",
):
    slug = rec_id.rsplit(".", 1)[-1]
    payload = (
        f"---\nname: {slug}\ndescription: {description}\n"
        f"platforms: [linux, macos]\n---\n\nBody.\n"
    )
    return SimpleNamespace(
        id=rec_id,
        kind=kind,
        skill=(SimpleNamespace(category="test") if kind is CapabilityKind.SKILL else None),
        trigger=SimpleNamespace(intents=list(intents)),
        lifecycle=SimpleNamespace(state=state),
        context=SimpleNamespace(payload=payload),
    )


def _patch_caps(monkeypatch, records):
    import grove.capability_registry as reg
    monkeypatch.setattr(reg, "load_capabilities", lambda *a, **k: dict(records))


# ── (1) primary match — REAL researcher record, intent_class="research" ───────


def test_real_researcher_record_matches_research_intent():
    slugs = matched_skill_slugs_for_intent(
        "research", {"invoke_skill"}, set(), disabled=set()
    )
    assert "researcher" in slugs  # real record load, not a stub

    ctx = {
        "intent_class": "research",
        "valid_tool_names": {"invoke_skill"},
        "registry": None,
    }
    result = _skill_nudge_provider(ctx)
    assert result is not None
    assert "`researcher`" in result.text
    assert BOUNDARY in result.text
    assert "invoke_skill" in result.text


# ── (2) no-match arms → line absent ───────────────────────────────────────────


def test_no_intent_class_no_nudge():
    assert matched_skill_slugs_for_intent(None, {"invoke_skill"}, set()) == []
    ctx = {"intent_class": None, "valid_tool_names": {"invoke_skill"}, "registry": None}
    assert _skill_nudge_provider(ctx) is None


def test_empty_trigger_intents_no_match(monkeypatch):
    _patch_caps(monkeypatch, {"skill.test.empty": _fake_record("skill.test.empty", [])})
    assert (
        matched_skill_slugs_for_intent("research", {"invoke_skill"}, set(), disabled=set())
        == []
    )


def test_intent_not_in_trigger_intents_no_match(monkeypatch):
    _patch_caps(
        monkeypatch,
        {"skill.test.other": _fake_record("skill.test.other", ["analysis"])},
    )
    assert (
        matched_skill_slugs_for_intent("research", {"invoke_skill"}, set(), disabled=set())
        == []
    )


def test_non_executable_record_no_match(monkeypatch):
    # A proposed (non-executable) record declaring the matching intent is NEVER
    # nudged toward — same executable authority as C1.
    _patch_caps(
        monkeypatch,
        {
            "skill.test.proposed": _fake_record(
                "skill.test.proposed", ["research"], state=LifecycleState.PROPOSED,
            )
        },
    )
    assert (
        matched_skill_slugs_for_intent("research", {"invoke_skill"}, set(), disabled=set())
        == []
    )


def test_wrong_kind_no_match(monkeypatch):
    _patch_caps(
        monkeypatch,
        {
            "verb.test.thing": _fake_record(
                "verb.test.thing", ["research"], kind=CapabilityKind.VERB,
            )
        },
    )
    assert (
        matched_skill_slugs_for_intent("research", {"invoke_skill"}, set(), disabled=set())
        == []
    )


def test_invoke_skill_absent_no_nudge_line(monkeypatch):
    # The match rule still fires (telemetry side), but the PROMPT line is
    # suppressed when the turn cannot call invoke_skill.
    _patch_caps(
        monkeypatch,
        {"skill.test.match": _fake_record("skill.test.match", ["research"])},
    )
    ctx = {"intent_class": "research", "valid_tool_names": {"read_file"}, "registry": None}
    assert _skill_nudge_provider(ctx) is None


# ── (3) F3 NEGATIVE — record prose can never reach the prompt ──────────────────


def test_f3_record_description_never_reaches_prompt(monkeypatch):
    marker = "ZZ_DISTINCTIVE_MARKER_ZZ"
    _patch_caps(
        monkeypatch,
        {
            "skill.test.marked": _fake_record(
                "skill.test.marked", ["research"], description=f"secret {marker} prose",
            )
        },
    )
    ctx = {"intent_class": "research", "valid_tool_names": {"invoke_skill"}, "registry": None}
    result = _skill_nudge_provider(ctx)
    assert result is not None
    assert "`marked`" in result.text          # the slug DID render
    assert marker not in result.text          # the description did NOT


# ── (4) multi-match → all slugs, one line, same template ──────────────────────


def test_multi_match_one_line_all_slugs(monkeypatch):
    _patch_caps(
        monkeypatch,
        {
            "skill.test.alpha": _fake_record("skill.test.alpha", ["research"]),
            "skill.test.beta": _fake_record("skill.test.beta", ["research", "analysis"]),
        },
    )
    slugs = matched_skill_slugs_for_intent(
        "research", {"invoke_skill"}, set(), disabled=set()
    )
    assert slugs == ["alpha", "beta"]  # sorted, de-duped
    line = render_skill_nudge_line(slugs)
    assert "`alpha`" in line and "`beta`" in line
    assert line.count("\n") == 0  # ONE line, both slugs under the same template
    assert BOUNDARY in line


# ── (5) telemetry — matched_skill_slugs on the tool_selection event ───────────


def test_matched_skill_slugs_for_turn_present_on_match():
    from grove.dispatcher import Dispatcher

    d = object.__new__(Dispatcher)
    d._current_turn_classification = SimpleNamespace(intent_class="research")
    d.registry = None
    agent = SimpleNamespace(valid_tool_names={"invoke_skill"})
    out = d._matched_skill_slugs_for_turn(agent)  # real researcher record
    assert "researcher" in out


def test_matched_skill_slugs_for_turn_empty_on_no_match():
    from grove.dispatcher import Dispatcher

    d = object.__new__(Dispatcher)
    d.registry = None
    agent = SimpleNamespace(valid_tool_names={"invoke_skill"})
    # No classification → empty list (the no-match telemetry value).
    d._current_turn_classification = None
    assert d._matched_skill_slugs_for_turn(agent) == []
