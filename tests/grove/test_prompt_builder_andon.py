"""Tests for the Phase 3 ``_build_andon_proposals_section`` integration in
agent/prompt_builder.py — system prompt lists ``~/.grove/skills/.andon/``
proposals so the agent knows what's pending operator review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove import skills as gskills


@pytest.fixture
def fake_grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "fake_home"
    fake.mkdir()
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: fake)
    return fake


def test_section_empty_when_no_andon_dir(fake_grove_home: Path) -> None:
    from agent.prompt_builder import _build_andon_proposals_section
    assert _build_andon_proposals_section() == ""


def test_section_empty_when_andon_dir_empty(fake_grove_home: Path) -> None:
    (fake_grove_home / "skills" / ".andon").mkdir(parents=True)
    from agent.prompt_builder import _build_andon_proposals_section
    assert _build_andon_proposals_section() == ""


def test_section_lists_proposals(fake_grove_home: Path) -> None:
    proposal = """---
name: weekly-team-sync
description: Schedule a recurring weekly team sync.
created_by: autonomaton
zone: yellow
---
# body
"""
    gskills.write_proposal("weekly-team-sync", proposal)

    from agent.prompt_builder import _build_andon_proposals_section
    section = _build_andon_proposals_section()

    assert "Proposed by you, awaiting promotion" in section
    assert "<proposed_skills>" in section
    assert "</proposed_skills>" in section
    assert "weekly-team-sync" in section
    assert "Schedule a recurring weekly team sync" in section
    # Discipline language
    assert "skill.self_promote.*" in section
    assert "red-zone" in section
    assert "hermes andon promote" in section


def test_section_skips_malformed_proposal(fake_grove_home: Path) -> None:
    """A proposal with no frontmatter is silently skipped — not an error."""
    bad = fake_grove_home / "skills" / ".andon" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("just a body, no frontmatter\n")

    good = """---
name: good
description: A valid proposal.
zone: yellow
---
# body
"""
    gskills.write_proposal("good", good)

    from agent.prompt_builder import _build_andon_proposals_section
    section = _build_andon_proposals_section()
    assert "good: A valid proposal." in section
    # Malformed one is omitted
    assert "broken" not in section
