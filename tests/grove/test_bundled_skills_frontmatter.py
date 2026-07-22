"""Every bundled skill carries Grove frontmatter (Sprint 17).

reference-skills-curation-v1 retrofitted created_by / zone / tier onto all
bundled skills. This test keeps that a standing invariant — a new bundled
skill added without Grove frontmatter fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.skills import parse_frontmatter

# guard-set-self-declaring: this whole module is a defect-class guard suite.
pytestmark = pytest.mark.guard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_SKILLS = sorted(
    p
    for d in ("skills", "optional-skills")
    for p in (_REPO_ROOT / d).rglob("SKILL.md")
)


def test_bundled_skills_discovered():
    assert _BUNDLED_SKILLS, (
        "no bundled SKILL.md files found under skills/ or optional-skills/"
    )


@pytest.mark.parametrize(
    "skill_md",
    _BUNDLED_SKILLS,
    ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_bundled_skill_has_grove_frontmatter(skill_md):
    """Each bundled SKILL.md parses and carries created_by / zone / tier."""
    fm, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    for field in ("created_by", "zone", "tier"):
        assert field in fm, (
            f"{skill_md.name}: missing Grove frontmatter field {field!r}"
        )
