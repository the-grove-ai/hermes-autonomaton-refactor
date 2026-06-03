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


def _classify(command: str):
    cls = ZoneClassifier(REPO_SCHEMA)
    return cls.classify_command_string(
        command, "command.execute.python3", tool_id="terminal",
    )


def test_quarantined_skill_path_is_yellow() -> None:
    """A terminal command running a script under .andon/ halts (yellow)."""
    result = _classify(
        "python3 /Users/op/.grove/skills/.andon/my-skill/scripts/run.py"
    )
    assert result.zone == "yellow"
    assert ".andon" in result.matched_rule


def test_promoted_skill_path_is_green() -> None:
    """A promoted skill path (outside .andon/) passes green."""
    result = _classify(
        "python3 /Users/op/.grove/skills/productivity/my-skill/scripts/run.py"
    )
    assert result.zone == "green"
    assert ".andon" not in result.matched_rule


def test_andon_rule_precedes_promoted_rule() -> None:
    """First-match-wins: the .andon yellow rule must sit ABOVE the broad
    .grove/skills/.* green rule, else quarantined skills would pass green."""
    import yaml

    schema = yaml.safe_load(REPO_SCHEMA.read_text(encoding="utf-8"))
    rules = schema["tool_zones"]["terminal"]["rules"]
    patterns = [r["match_pattern"] for r in rules]
    andon_idx = next(i for i, p in enumerate(patterns) if r".andon" in p)
    promoted_idx = next(
        i for i, p in enumerate(patterns)
        if p == r".*\.grove/skills/.*"
    )
    assert andon_idx < promoted_idx
    assert rules[andon_idx]["zone"] == "yellow"


# ── Phase 1c: active-section + manifest exclusion ─────────────────────


def _write_skill(skill_dir: Path, name: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}.\n---\n\n"
        f"# {name}\n\nStep 1.\n",
        encoding="utf-8",
    )


def test_manifest_excludes_quarantined(tmp_path: Path) -> None:
    from agent.prompt_builder import _build_skills_manifest

    _write_skill(tmp_path / "productivity" / "active-one", "active-one")
    _write_skill(tmp_path / ".andon" / "quar-one", "quar-one")

    manifest = _build_skills_manifest(tmp_path)
    keys = list(manifest.keys())
    assert any("active-one" in k for k in keys)
    assert not any(".andon" in k for k in keys)


def test_is_quarantined_path() -> None:
    from agent.prompt_builder import _is_quarantined_path

    assert _is_quarantined_path(Path("/x/.grove/skills/.andon/foo/SKILL.md"))
    assert not _is_quarantined_path(Path("/x/.grove/skills/foo/SKILL.md"))


def test_active_prompt_section_excludes_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent must not see a quarantined SKILL.md as active. The
    quarantined skill may appear ONLY in the 'awaiting promotion' section."""
    import agent.prompt_builder as pb
    import grove.skills as gskills

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir / "productivity" / "active-one", "active-one")
    _write_skill(skills_dir / ".andon" / "quar-one", "quar-one")

    monkeypatch.setattr(pb, "get_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [skills_dir])
    monkeypatch.setattr(
        pb, "_skills_prompt_snapshot_path", lambda: tmp_path / "snap.json"
    )
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda: set())
    # The andon section resolves andon_dir() via grove.skills.get_hermes_home.
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: tmp_path)
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)

    prompt = pb.build_skills_system_prompt()

    # Split at the andon section marker; the active portion precedes it.
    marker = "Proposed by you, awaiting promotion"
    active_part = prompt.split(marker)[0]
    assert "active-one" in active_part
    assert "quar-one" not in active_part
    # Flag, don't hide: the quarantined skill IS surfaced — in the andon
    # section, which only exists when there are pending proposals.
    assert marker in prompt
    assert "quar-one" in prompt


# ── Phase 1d: skills_list flags, doesn't hide ─────────────────────────


def test_skills_list_flags_quarantined(tmp_path: Path) -> None:
    from tools.skills_tool import _find_all_skills

    _write_skill(tmp_path / "productivity" / "active-one", "active-one")
    _write_skill(tmp_path / ".andon" / "quar-one", "quar-one")

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skills = _find_all_skills()

    by_name = {s["name"]: s for s in skills}
    assert "active-one" in by_name
    assert "quar-one" in by_name  # visible, not hidden

    active = by_name["active-one"]
    assert not active.get("quarantined")
    assert not active["description"].startswith("[QUARANTINED]")

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
    """If a name exists both active and quarantined, the active (promoted)
    entry wins and is NOT tagged quarantined."""
    from tools.skills_tool import _find_all_skills

    _write_skill(tmp_path / "productivity" / "dup", "dup")
    _write_skill(tmp_path / ".andon" / "dup", "dup")

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        skills = _find_all_skills()

    dups = [s for s in skills if s["name"] == "dup"]
    assert len(dups) == 1
    assert not dups[0].get("quarantined")
