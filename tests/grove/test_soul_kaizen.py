"""Tests for soul-kaizen-wiring-v1 (Sprint 14) — identity-aware Kaizen.

Identity is mocked; no real ~/.grove, no real Curator LLM calls.
"""

import logging
from types import SimpleNamespace

import pytest

import grove.identity
from grove.identity import IdentityComposition
from grove.kaizen.curator import _compose_identity_preamble
from grove.skills import (
    _alignment_keywords,
    _normalize_soul_alignment,
    _split_goal_lines,
    assess_soul_alignment,
    parse_frontmatter,
    stamp_proposal_frontmatter,
)
from grove.sovereignty import _render_identity_alignment, show_diff

_GOALS_MD = """# Goals

## Active

- Ship grove-autonomaton v0.1.
- Validate the daily-driver experience — run the system as the
  primary cognitive partner.

## Background

[fill in as they crystallize]
"""

_SKILL = "---\nname: demo\ndescription: A demo skill.\n---\n# Demo\nbody"


def _identity(
    *,
    goals=_GOALS_MD,
    refusals=None,
    register="strategic-concise",
    constitution="Sovereignty guardrails.",
    soul="Voice and thinking style.",
    operator="The operator context.",
):
    return IdentityComposition(
        constitution=constitution,
        soul=soul,
        operator=operator,
        goals=goals,
        frontmatter={"register": register, "refusals": refusals or []},
    )


@pytest.fixture
def mock_identity(monkeypatch):
    """Return an installer that patches load_identity with a controlled
    IdentityComposition."""

    def _install(**kwargs):
        comp = _identity(**kwargs)
        monkeypatch.setattr(grove.identity, "load_identity", lambda: comp)
        return comp

    return _install


def _raise_identity_error():
    raise grove.identity.IdentityError("no constitution")


# ----- _compose_identity_preamble (Phase 1) ----------------------------------


def test_preamble_composes_all_identity_layers(mock_identity):
    mock_identity()
    preamble = _compose_identity_preamble()
    assert "<constitution>" in preamble and "<soul>" in preamble
    assert "<operator_context>" in preamble and "<current_goals>" in preamble
    assert "OPERATOR IDENTITY" in preamble
    assert preamble.endswith("\n\n")


def test_preamble_includes_register_instruction(mock_identity):
    mock_identity(register="strategic-concise")
    assert 'register: "strategic-concise"' in _compose_identity_preamble()


def test_preamble_falls_back_when_register_absent(mock_identity):
    mock_identity(register=None)
    assert "declared voice" in _compose_identity_preamble()


def test_preamble_lists_declared_refusals(mock_identity):
    mock_identity(refusals=["never touch production databases"])
    assert "never touch production databases" in _compose_identity_preamble()


def test_preamble_carries_soul_alignment_instructions(mock_identity):
    mock_identity()
    preamble = _compose_identity_preamble()
    for token in ("soul_alignment", "tension_note", "goals_served",
                  "aligned", "neutral", "tension"):
        assert token in preamble


def test_preamble_empty_on_identity_failure(monkeypatch, caplog):
    monkeypatch.setattr(grove.identity, "load_identity", _raise_identity_error)
    with caplog.at_level(logging.WARNING, logger="grove.kaizen.curator"):
        result = _compose_identity_preamble()
    assert result == ""  # PC6: curator runs without identity
    assert "identity unavailable" in caplog.text


# ----- assess_soul_alignment (Phase 1.5) -------------------------------------


def test_assess_aligned_lists_served_goals(mock_identity):
    mock_identity()
    alignment, note, goals = assess_soul_alignment(
        "v0.1-release-checklist",
        "Tracks the tasks to ship grove-autonomaton v0.1.",
    )
    assert alignment == "aligned"
    assert note is None
    assert any("Ship grove-autonomaton" in g for g in goals)


def test_assess_neutral_when_no_overlap(mock_identity):
    mock_identity()
    alignment, note, goals = assess_soul_alignment(
        "json-printer", "Pretty-prints arbitrary JSON blobs."
    )
    assert alignment == "neutral"
    assert note is None
    assert goals == []


def test_assess_tension_on_refusal_overlap(mock_identity):
    mock_identity(refusals=["never deploy to production automatically"])
    alignment, note, _goals = assess_soul_alignment(
        "auto-deployer", "Automatically deploy builds straight to production."
    )
    assert alignment == "tension"
    assert note and "production" in note.lower()


def test_assess_graceful_on_identity_failure(monkeypatch, caplog):
    monkeypatch.setattr(grove.identity, "load_identity", _raise_identity_error)
    with caplog.at_level(logging.WARNING, logger="grove.skills"):
        result = assess_soul_alignment("any-skill", "any description")
    assert result == ("neutral", None, [])
    assert "identity unavailable" in caplog.text


# ----- helpers ---------------------------------------------------------------


def test_split_goal_lines_extracts_and_joins_wraps():
    goals = _split_goal_lines(_GOALS_MD)
    assert "Ship grove-autonomaton v0.1." in goals
    assert any("primary cognitive partner" in g for g in goals)  # wrap joined
    assert all(not g.startswith("[") for g in goals)  # placeholder skipped


def test_split_goal_lines_empty_on_none():
    assert _split_goal_lines(None) == []


def test_alignment_keywords_drops_short_words_and_stopwords():
    keywords = _alignment_keywords("Ship the autonomaton with this system")
    assert "autonomaton" in keywords
    assert not ({"the", "with", "this", "system"} & keywords)


# ----- stamp_proposal_frontmatter / _normalize_soul_alignment ----------------


def test_stamp_writes_all_three_soul_fields():
    fm, _ = parse_frontmatter(
        stamp_proposal_frontmatter(
            _SKILL,
            soul_alignment="aligned",
            tension_note=None,
            goals_served=["Ship v0.1"],
        )
    )
    provenance = fm["provenance"]
    assert provenance["soul_alignment"] == "aligned"
    assert provenance["tension_note"] is None
    assert provenance["goals_served"] == ["Ship v0.1"]


def test_stamp_defaults_neutral_when_unassessed():
    fm, _ = parse_frontmatter(stamp_proposal_frontmatter(_SKILL))
    provenance = fm["provenance"]
    assert provenance["soul_alignment"] == "neutral"
    assert provenance["tension_note"] is None
    assert provenance["goals_served"] == []


def test_normalize_soul_alignment_accepts_valid_tags():
    assert _normalize_soul_alignment("tension") == "tension"
    assert _normalize_soul_alignment("  Aligned ") == "aligned"


def test_normalize_soul_alignment_none_defaults_neutral():
    assert _normalize_soul_alignment(None) == "neutral"


def test_normalize_soul_alignment_invalid_warns_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger="grove.skills"):
        assert _normalize_soul_alignment("super-aligned") == "neutral"
    assert "invalid soul_alignment" in caplog.text


# ----- sovereignty diff display ----------------------------------------------


def test_render_identity_alignment_block():
    stamped = stamp_proposal_frontmatter(
        _SKILL,
        soul_alignment="tension",
        tension_note="conflicts with a declared boundary",
        goals_served=["Ship v0.1"],
    )
    block = _render_identity_alignment(stamped)
    assert "soul_alignment: tension" in block
    assert "conflicts with a declared boundary" in block
    assert "Ship v0.1" in block


def test_render_identity_alignment_empty_for_pre_sprint14_proposal():
    assert _render_identity_alignment(_SKILL) == ""  # no provenance.soul_alignment


def test_show_diff_surfaces_identity_alignment(tmp_path, monkeypatch):
    monkeypatch.setattr("grove.skills.get_hermes_home", lambda: tmp_path)
    from grove.skills import proposal_path

    stamped = stamp_proposal_frontmatter(
        _SKILL, soul_alignment="aligned", goals_served=["Ship v0.1"]
    )
    dest = proposal_path("demo")
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(stamped, encoding="utf-8")

    out = show_diff("demo")
    assert "identity alignment" in out
    assert "soul_alignment: aligned" in out


# ----- _create_skill — both paths --------------------------------------------


def test_create_skill_curator_path_uses_m2_tool_args(tmp_path, monkeypatch):
    """The curator review model assesses; explicit M2 args win, no heuristic."""
    monkeypatch.setattr("grove.skills.get_hermes_home", lambda: tmp_path)
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\nname: curator-umbrella\ndescription: An umbrella skill.\n"
        "---\n# Body\ntext"
    )
    result = _create_skill(
        "curator-umbrella",
        content,
        soul_alignment="aligned",
        tension_note=None,
        goals_served=["Ship grove-autonomaton v0.1"],
    )
    assert result["success"]
    md = (tmp_path / "skills" / ".andon" / "curator-umbrella" / "SKILL.md")
    fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert fm["provenance"]["soul_alignment"] == "aligned"
    assert fm["provenance"]["goals_served"] == ["Ship grove-autonomaton v0.1"]


def test_create_skill_normal_path_uses_heuristic(tmp_path, monkeypatch, mock_identity):
    """A foreground create passes no M2 args — the code assesses heuristically."""
    monkeypatch.setattr("grove.skills.get_hermes_home", lambda: tmp_path)
    mock_identity()
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\nname: ship-tracker\ndescription: Tracks the tasks to ship "
        "grove-autonomaton v0.1.\n---\n# Body\ntext"
    )
    result = _create_skill("ship-tracker", content)
    assert result["success"]
    md = (tmp_path / "skills" / ".andon" / "ship-tracker" / "SKILL.md")
    fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert fm["provenance"]["soul_alignment"] == "aligned"
    assert any("Ship grove" in g for g in fm["provenance"]["goals_served"])


# ----- _create_skill — Sprint 15 extension fields ----------------------------


def test_create_skill_writes_grove_extension_fields(tmp_path, monkeypatch):
    """tier comes from the router, register from soul.md, lineage from the arg."""
    monkeypatch.setattr("grove.skills.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("grove.providers._last_routed_tier", "T3")
    monkeypatch.setattr(
        "grove.identity.load_identity",
        lambda: SimpleNamespace(frontmatter={"register": "strategic-concise"}),
    )
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\nname: tier-register-demo\ndescription: A demo skill.\n"
        "---\n# Body\ntext"
    )
    result = _create_skill(
        "tier-register-demo", content,
        soul_alignment="neutral", tension_note=None, goals_served=[],
        lineage=["calendar-check"],
    )
    assert result["success"]
    md = tmp_path / "skills" / ".andon" / "tier-register-demo" / "SKILL.md"
    fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert fm["tier"] == "T3"
    assert fm["register"] == "strategic-concise"
    assert fm["lineage"] == ["calendar-check"]


def test_create_skill_extension_fields_take_defaults(tmp_path, monkeypatch):
    """No routed tier, no register in soul.md, no lineage arg — defaults apply."""
    monkeypatch.setattr("grove.skills.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("grove.providers._last_routed_tier", None)
    monkeypatch.setattr(
        "grove.identity.load_identity",
        lambda: SimpleNamespace(frontmatter={}),
    )
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\nname: lineage-default-demo\ndescription: A standalone skill.\n"
        "---\n# Body\ntext"
    )
    result = _create_skill(
        "lineage-default-demo", content,
        soul_alignment="neutral", tension_note=None, goals_served=[],
    )
    assert result["success"]
    md = tmp_path / "skills" / ".andon" / "lineage-default-demo" / "SKILL.md"
    fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert fm["lineage"] == []
    assert fm["tier"] is None
    assert fm["register"] is None
