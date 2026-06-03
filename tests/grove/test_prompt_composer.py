"""Tests for grove/prompt/composer.py — _tool_affordances_provider skill extension.

Covers:
  - _load_promoted_skills: directory walking, frontmatter parsing, .andon exclusion,
    error resilience, deduplication, alphabetical sort.
  - _tool_affordances_provider: skills line present/absent, format, test-injectable
    skills_root, no crash on bad registry/empty tools.
"""

from __future__ import annotations

import os
import textwrap
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from grove.prompt.composer import (
    SectionResult,
    _load_promoted_skills,
    _tool_affordances_provider,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_skill(tmp_path, rel_path: str, frontmatter: str, body: str = "# Body\n") -> None:
    """Write a SKILL.md at ``tmp_path / rel_path``."""
    skill_dir = tmp_path / rel_path
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{textwrap.dedent(frontmatter).strip()}\n---\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _make_registry(tools: Dict[str, str]) -> MagicMock:
    """Return a mock registry where get_entry(name).description = tools[name]."""
    registry = MagicMock()

    def _get_entry(name):
        if name not in tools:
            return None
        entry = MagicMock()
        entry.description = tools[name]
        entry.toolset = "core"
        return entry

    registry.get_entry.side_effect = _get_entry
    return registry


def _base_ctx(tmp_path, tools: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Minimal context dict for _tool_affordances_provider."""
    if tools is None:
        tools = {"read_file": "Read a text file with line numbers."}
    return {
        "valid_tool_names": set(tools.keys()),
        "registry": _make_registry(tools),
        "skills_root": str(tmp_path),
    }


# ── _load_promoted_skills ─────────────────────────────────────────────


class TestLoadPromotedSkills:
    def test_returns_name_and_description(self, tmp_path):
        _make_skill(
            tmp_path, "productivity/google-workspace",
            "name: google-workspace\ndescription: Gmail, Calendar, Drive, Docs.",
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        # (name, description, invocations) — these fixture skills have no
        # Usage section, so invocations is empty.
        assert [(r[0], r[1]) for r in results] == [
            ("google-workspace", "Gmail, Calendar, Drive, Docs.")
        ]
        assert results[0][2] == []

    def test_excludes_andon_directory(self, tmp_path):
        _make_skill(
            tmp_path, ".andon/quarantined-skill",
            "name: quarantined\ndescription: Should not appear.",
        )
        _make_skill(
            tmp_path, "apple/imessage",
            "name: imessage\ndescription: Send iMessages.",
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        names = [r[0] for r in results]
        assert "quarantined" not in names
        assert "imessage" in names

    def test_results_sorted_alphabetically(self, tmp_path):
        for name in ("zebra-skill", "alpha-skill", "middle-skill"):
            _make_skill(
                tmp_path, f"cat/{name}",
                f"name: {name}\ndescription: Desc for {name}.",
            )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        names = [r[0] for r in results]
        assert names == sorted(names)

    def test_skips_skill_without_description(self, tmp_path):
        _make_skill(
            tmp_path, "cat/no-desc",
            "name: no-desc\nversion: 1.0.0",
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert results == []

    def test_skips_skill_without_name(self, tmp_path):
        _make_skill(
            tmp_path, "cat/no-name",
            "description: Has desc but no name.",
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert results == []

    def test_skips_file_without_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "cat" / "no-fm"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Just a heading\nNo frontmatter.", encoding="utf-8")
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert results == []

    def test_returns_empty_list_for_nonexistent_root(self, tmp_path):
        missing = str(tmp_path / "does_not_exist")
        results = _load_promoted_skills(skills_root=missing)
        assert results == []

    def test_multiple_skills_across_categories(self, tmp_path):
        _make_skill(tmp_path, "apple/imessage", "name: imessage\ndescription: Send iMessages.")
        _make_skill(tmp_path, "productivity/google-workspace", "name: google-workspace\ndescription: Gmail and Calendar.")
        _make_skill(tmp_path, "research/arxiv", "name: arxiv\ndescription: Search arXiv papers.")
        results = _load_promoted_skills(skills_root=str(tmp_path))
        names = [r[0] for r in results]
        assert "imessage" in names
        assert "google-workspace" in names
        assert "arxiv" in names
        assert len(results) == 3

    def test_strips_quotes_from_description(self, tmp_path):
        _make_skill(
            tmp_path, "cat/quoted",
            'name: quoted\ndescription: "Quoted description value."',
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert [(r[0], r[1]) for r in results] == [("quoted", "Quoted description value.")]

    def test_strips_single_quotes(self, tmp_path):
        _make_skill(
            tmp_path, "cat/single-quoted",
            "name: single-quoted\ndescription: 'Single quoted.'",
        )
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert [(r[0], r[1]) for r in results] == [("single-quoted", "Single quoted.")]

    def test_ignores_directories_without_skill_md(self, tmp_path):
        # directory with other files but no SKILL.md
        other_dir = tmp_path / "cat" / "no-skill"
        other_dir.mkdir(parents=True)
        (other_dir / "README.md").write_text("# Readme", encoding="utf-8")
        _make_skill(tmp_path, "cat/real-skill", "name: real-skill\ndescription: Real.")
        results = _load_promoted_skills(skills_root=str(tmp_path))
        assert len(results) == 1
        assert results[0][0] == "real-skill"

    def test_env_var_fallback(self, tmp_path, monkeypatch):
        """When skills_root is None and HERMES_HOME is set, uses HERMES_HOME/skills."""
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "cat/env-skill", "name: env-skill\ndescription: From env.")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        results = _load_promoted_skills(skills_root=None)
        names = [r[0] for r in results]
        assert "env-skill" in names

    def test_invocation_uses_real_path_and_skill_md_interpreter(self, tmp_path):
        """Usage commands embed the reconstructed on-disk script path and the
        SKILL.md's explicit interpreter (python3.13), not bare python3 — which
        on this machine resolves to a python without the deps (Sprint 56)."""
        body = (
            "## Usage\n\n"
            "Use `/opt/homebrew/bin/python3.13` explicitly on macOS.\n\n"
            "```bash\n"
            'GAPI="python3 ${HOME}/.hermes/skills/cat/gw/scripts/google_api.py"\n'
            "```\n\n"
            "### Calendar\n\n"
            "```bash\n"
            "$GAPI calendar list\n"
            "```\n"
        )
        _make_skill(tmp_path, "cat/gw", "name: gw\ndescription: Google.", body=body)
        results = _load_promoted_skills(skills_root=str(tmp_path))
        invs = dict(results[0][2])
        assert "Calendar" in invs
        cmd = invs["Calendar"]
        # Correct interpreter from the SKILL.md hint (not bare python3).
        assert cmd.startswith("/opt/homebrew/bin/python3.13 ")
        assert "python3 " not in cmd
        # Real on-disk path reconstructed from skill_dir, NOT the SKILL.md's
        # wrong ${HOME}/.hermes shorthand.
        assert cmd.endswith("/cat/gw/scripts/google_api.py calendar list")
        assert ".hermes" not in cmd


# ── _tool_affordances_provider ────────────────────────────────────────


class TestToolAffordancesProvider:
    def test_includes_skills_line_when_skills_present(self, tmp_path):
        _make_skill(tmp_path, "apple/imessage", "name: imessage\ndescription: Send iMessages.")
        ctx = _base_ctx(tmp_path)
        result = _tool_affordances_provider(ctx)
        assert result is not None
        assert "Available skills:" in result.text
        assert "imessage" in result.text
        assert "Send iMessages." in result.text

    def test_skills_line_format(self, tmp_path):
        """Skills section: a header plus one bulleted line per skill, each
        ending with the skill_view-first reminder (Sprint 56 hotfix)."""
        _make_skill(tmp_path, "cat/alpha", "name: alpha\ndescription: Alpha skill.")
        _make_skill(tmp_path, "cat/beta", "name: beta\ndescription: Beta skill.")
        ctx = _base_ctx(tmp_path)
        result = _tool_affordances_provider(ctx)
        assert result is not None
        # Check the exact format
        assert "Available skills:\n" in result.text
        assert "- alpha (Alpha skill.) — call skill_view first" in result.text
        assert "- beta (Beta skill.) — call skill_view first" in result.text

    def test_skills_sorted_alphabetically_in_output(self, tmp_path):
        for name in ("zebra", "aardvark", "mango"):
            _make_skill(tmp_path, f"cat/{name}", f"name: {name}\ndescription: Desc.")
        ctx = _base_ctx(tmp_path)
        result = _tool_affordances_provider(ctx)
        assert result is not None
        idx_a = result.text.index("aardvark")
        idx_m = result.text.index("mango")
        idx_z = result.text.index("zebra")
        assert idx_a < idx_m < idx_z

    def test_no_skills_line_when_no_skills(self, tmp_path):
        """Empty skills_root → no 'Available skills:' line appended."""
        ctx = _base_ctx(tmp_path)  # tmp_path is empty
        result = _tool_affordances_provider(ctx)
        assert result is not None
        assert "Available skills:" not in result.text

    def test_andon_skills_excluded_from_output(self, tmp_path):
        _make_skill(tmp_path, ".andon/secret", "name: secret\ndescription: Quarantined.")
        _make_skill(tmp_path, "cat/public", "name: public\ndescription: Public skill.")
        ctx = _base_ctx(tmp_path)
        result = _tool_affordances_provider(ctx)
        assert result is not None
        assert "secret" not in result.text
        assert "public" in result.text

    def test_returns_none_when_no_valid_tools(self, tmp_path):
        ctx = {
            "valid_tool_names": set(),
            "registry": _make_registry({}),
            "skills_root": str(tmp_path),
        }
        result = _tool_affordances_provider(ctx)
        assert result is None

    def test_returns_none_when_registry_missing(self, tmp_path):
        ctx = {
            "valid_tool_names": {"read_file"},
            "registry": None,
            "skills_root": str(tmp_path),
        }
        result = _tool_affordances_provider(ctx)
        assert result is None

    def test_tools_still_present_with_skills(self, tmp_path):
        """Tool list and skills line must coexist in the output."""
        _make_skill(tmp_path, "cat/a-skill", "name: a-skill\ndescription: A skill.")
        ctx = _base_ctx(tmp_path, tools={"read_file": "Read a file.", "terminal": "Run a command."})
        result = _tool_affordances_provider(ctx)
        assert result is not None
        assert "- read_file:" in result.text
        assert "- terminal:" in result.text
        assert "Available skills:" in result.text

    def test_result_label_is_tool_affordances(self, tmp_path):
        ctx = _base_ctx(tmp_path)
        result = _tool_affordances_provider(ctx)
        assert result is not None
        assert result.label == "tool_affordances"

    def test_skills_line_follows_tools_list(self, tmp_path):
        """'Available skills:' must come after the tool bullet list."""
        _make_skill(tmp_path, "cat/sk", "name: sk\ndescription: A skill desc.")
        ctx = _base_ctx(tmp_path, tools={"clarify": "Ask a question."})
        result = _tool_affordances_provider(ctx)
        assert result is not None
        tools_idx = result.text.index("- clarify:")
        skills_idx = result.text.index("Available skills:")
        assert tools_idx < skills_idx

    def test_no_crash_on_bad_skills_root(self, tmp_path):
        """Nonexistent skills_root must not raise — provider degrades gracefully."""
        ctx = {
            "valid_tool_names": {"read_file"},
            "registry": _make_registry({"read_file": "Read a file."}),
            "skills_root": str(tmp_path / "nonexistent"),
        }
        result = _tool_affordances_provider(ctx)
        # Should still return a result (tools are fine), just no skills line
        assert result is not None
        assert "Available skills:" not in result.text
