"""Tests for the Phase 1 wiring in tools/skill_manager_tool.py.

Verifies that agent-created skills (via `_create_skill`) land in
``~/.grove/skills/.andon/`` instead of the active dir, with Grove
proposal frontmatter stamped and scan verdict recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove import skills as gskills


@pytest.fixture
def fake_grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect grove.skills + tools.skill_manager_tool + agent.skill_utils to a tmp home."""
    fake = tmp_path / "fake_home"
    fake.mkdir()
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: fake)
    # tools.skill_manager_tool captured SKILLS_DIR at import time.
    import tools.skill_manager_tool as smt
    monkeypatch.setattr(smt, "SKILLS_DIR", fake / "skills")
    monkeypatch.setattr(smt, "GROVE_HOME", fake)
    # _find_skill walks get_all_skills_dirs() from agent.skill_utils.
    import agent.skill_utils as skill_utils
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [fake / "skills"])
    return fake


def test_create_skill_lands_in_andon(fake_grove_home: Path) -> None:
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\n"
        "name: weekly-team-sync\n"
        "description: Schedule a recurring weekly team sync.\n"
        "---\n"
        "# Weekly team sync\n\nBody.\n"
    )
    result = _create_skill("weekly-team-sync", content)

    assert result["success"] is True
    assert result["quarantined"] is True
    assert result["zone"] == "yellow"

    proposal = fake_grove_home / "skills" / ".andon" / "weekly-team-sync"
    assert proposal.exists()
    # Active dir does NOT have the skill.
    assert not (fake_grove_home / "skills" / "weekly-team-sync").exists()


def test_create_skill_stamps_grove_frontmatter(fake_grove_home: Path) -> None:
    from tools.skill_manager_tool import _create_skill

    content = (
        "---\nname: backup-photos\ndescription: Nightly backup.\n---\n# x\n"
    )
    _create_skill("backup-photos", content)

    skill_md = fake_grove_home / "skills" / ".andon" / "backup-photos" / "SKILL.md"
    fm, _ = gskills.parse_frontmatter(skill_md.read_text())
    assert fm["created_by"] == "autonomaton"
    assert fm["zone"] == "yellow"
    assert "proposed_at" in fm
    assert "provenance" in fm
    assert fm["provenance"]["created_by"] == "autonomaton"
    assert fm["provenance"]["scan_verdict"] in {"safe", "caution", "dangerous", "unknown"}


def test_create_skill_message_points_at_andon_cli(fake_grove_home: Path) -> None:
    from tools.skill_manager_tool import _create_skill

    content = "---\nname: w\ndescription: x\n---\n# w\n\nDo the thing.\n"
    result = _create_skill("w", content)
    msg = result["message"]
    assert "hermes andon list" in msg
    assert "hermes andon diff w" in msg
    assert "hermes andon promote w" in msg


def test_create_skill_collision_with_active(fake_grove_home: Path) -> None:
    """Reproposing into .andon/ is fine; colliding with an active skill is not."""
    from tools.skill_manager_tool import _create_skill

    # Pre-create an active skill of the same name.
    active = fake_grove_home / "skills" / "existing"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("---\nname: existing\ndescription: y\n---\n# already active\n")

    content = "---\nname: existing\ndescription: x\n---\n# new\n\nDo it.\n"
    result = _create_skill("existing", content)
    assert result["success"] is False
    assert "already exists" in result["error"]
    assert "hermes andon revoke existing" in result["error"]


def test_create_skill_reproposal_overwrites_andon(fake_grove_home: Path) -> None:
    """A second proposal of the same name overwrites the prior proposal."""
    from tools.skill_manager_tool import _create_skill

    content_v1 = "---\nname: w\ndescription: v1\n---\n# v1\n"
    content_v2 = "---\nname: w\ndescription: v2\n---\n# v2\n"
    r1 = _create_skill("w", content_v1)
    assert r1["success"] is True
    r2 = _create_skill("w", content_v2)
    assert r2["success"] is True

    skill_md = fake_grove_home / "skills" / ".andon" / "w" / "SKILL.md"
    fm, body = gskills.parse_frontmatter(skill_md.read_text())
    assert fm["description"] == "v2"
    assert "# v2" in body


def test_install_policy_agent_created_is_andon() -> None:
    """INSTALL_POLICY agent-created row should be all 'andon' (Sprint 06a)."""
    from tools.skills_guard import INSTALL_POLICY
    assert INSTALL_POLICY["agent-created"] == ("andon", "andon", "andon")
