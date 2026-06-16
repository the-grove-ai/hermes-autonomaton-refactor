"""Sprint 53.2 Phase 1 — quarantine zone rule + skill visibility filtering.

Three guarantees:

* Phase 1b — the ``.andon/`` terminal path classifies YELLOW and is
  ordered ABOVE the broad ``.grove/skills/.*`` GREEN rule (first-match-wins
  in the real repo schema).
* Phase 1c — quarantined skills are excluded from the ACTIVE skills
  system-prompt section and the cache manifest.
* Phase 1d — quarantined skills remain visible in ``skills_list`` but are
  tagged ``[QUARANTINED]`` (flag, don't hide — GATE-A decision 1), and
  active skills take dedup precedence over same-name quarantined drafts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from grove.zones import ZoneClassifier

REPO_SCHEMA = Path(__file__).resolve().parents[2] / "config" / "zones.schema.yaml"


# ── Phase 1b: zone rule ordering ──────────────────────────────────────


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _classify(command: str):
    # GRV-010 C1a — terminal commands are classified by the bashlex-AST effect
    # classifier (grove/shell_effects.py); the regex tool_zones.terminal.rules
    # were removed. The Phase-1b quarantine guarantees are now AST properties.
    from grove.shell_effects import classify_shell_effect
    return classify_shell_effect(command)


def test_quarantined_skill_path_is_yellow(grove_home) -> None:
    """A terminal command running a script under .andon/ halts (yellow): a
    quarantined draft is NOT a promoted skill, so it is not auto-approved."""
    result = _classify(
        f"python3 {grove_home}/skills/.andon/my-skill/scripts/run.py"
    )
    assert result.zone == "yellow"


def test_promoted_skill_path_is_green(grove_home) -> None:
    """A promoted skill path (under ~/.grove/skills, outside .andon/) is green."""
    result = _classify(
        f"python3 {grove_home}/skills/productivity/my-skill/scripts/run.py"
    )
    assert result.zone == "green"


def test_andon_rule_precedes_promoted_rule(grove_home) -> None:
    """Quarantine precedence: a script under skills/.andon/ classifies YELLOW
    even though it is under the skills tree, so a quarantined draft can never
    ride the promoted-skill green path (the .andon check precedes the
    promoted-skill green in grove/shell_effects.py)."""
    result = _classify(f"python3 {grove_home}/skills/.andon/draft/run.py")
    assert result.zone == "yellow"


# ── Phase 1c: active-section + manifest exclusion ─────────────────────


def _write_skill(skill_dir: Path, name: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}.\n---\n\n"
        f"# {name}\n\nStep 1.\n",
        encoding="utf-8",
    )


def test_is_quarantined_path() -> None:
    from agent.prompt_builder import _is_quarantined_path

    assert _is_quarantined_path(Path("/x/.grove/skills/.andon/foo/SKILL.md"))
    assert not _is_quarantined_path(Path("/x/.grove/skills/foo/SKILL.md"))


def test_active_prompt_section_excludes_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent must not see a quarantined SKILL.md as active. The
    quarantined skill may appear ONLY in the 'awaiting promotion' section.

    GRV-009 E6a C3 — the ACTIVE bundled index is now record-driven (the FS scan
    is retired), so the active section carries the migrated records, not a
    temp-dir skill. The quarantine invariant is unchanged: a .andon skill is
    NEVER active; the andon section still reads ~/.grove/skills/.andon."""
    import agent.prompt_builder as pb
    import grove.skills as gskills

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir / ".andon" / "quar-one", "quar-one")

    monkeypatch.setattr(pb, "get_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [skills_dir])
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda: set())
    # The andon section resolves andon_dir() via grove.skills.get_hermes_home.
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: tmp_path)
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)

    prompt = pb.build_skills_system_prompt()

    # Split at the andon section marker; the active portion precedes it.
    marker = "Proposed by you, awaiting promotion"
    active_part = prompt.split(marker)[0]
    assert "apple-notes" in active_part  # a real migrated record IS active
    assert "quar-one" not in active_part
    # Flag, don't hide: the quarantined skill IS surfaced — in the andon
    # section, which only exists when there are pending proposals.
    assert marker in prompt
    assert "quar-one" in prompt


# ── Phase 1d: skills_list flags, doesn't hide ─────────────────────────


def test_skills_list_flags_quarantined(tmp_path: Path) -> None:
    """GRV-009 E6a C3 — bundled active skills come from records; the .andon
    quarantine is still surfaced (flag, don't hide), tagged [QUARANTINED]."""
    from tools.skills_tool import _bundled_skill_entries_from_records, _find_all_skills

    _write_skill(tmp_path / ".andon" / "quar-one", "quar-one")

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skills = _find_all_skills()

    by_name = {s["name"]: s for s in skills}
    # a real migrated record is active and untagged
    a_real = _bundled_skill_entries_from_records()[0]["name"]
    assert a_real in by_name
    assert not by_name[a_real].get("quarantined")
    assert not by_name[a_real]["description"].startswith("[QUARANTINED]")
    # the quarantined draft is visible, tagged, never hidden
    assert "quar-one" in by_name
    quar = by_name["quar-one"]
    assert quar.get("quarantined") is True
    assert quar.get("status") == "[QUARANTINED]"
    assert quar["description"].startswith("[QUARANTINED]")


def test_skill_view_can_load_quarantined(tmp_path: Path) -> None:
    """GATE-A decision 1: quarantined skills remain loadable via skill_view
    (superseding Sprint 06a). The response is tagged quarantined."""
    import json as _json
    from tools.skills_tool import skill_view

    _write_skill(tmp_path / ".andon" / "quar-one", "quar-one")

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        raw = skill_view("quar-one")
    result = _json.loads(raw)
    assert result["success"] is True
    assert result["name"] == "quar-one"
    assert result.get("quarantined") is True
    assert result.get("status") == "[QUARANTINED]"


def test_active_skill_wins_dedup_over_quarantined(tmp_path: Path) -> None:
    """If a name exists both active and quarantined, the active entry wins and
    is NOT tagged quarantined. GRV-009 E6a C3 — the active source is now the
    record set, so a .andon draft sharing a migrated record's name is deduped
    out by the record (which is added first and wins ``seen_names``)."""
    from tools.skills_tool import _bundled_skill_entries_from_records, _find_all_skills

    a_real = _bundled_skill_entries_from_records()[0]["name"]
    _write_skill(tmp_path / ".andon" / a_real, a_real)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skills = _find_all_skills()

    dups = [s for s in skills if s["name"] == a_real]
    assert len(dups) == 1
    assert not dups[0].get("quarantined")
