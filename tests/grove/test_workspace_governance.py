"""workspace-governance-unification-v1 — positive-allowlist FS governance.

A single ``is_granted_workspace`` allowlist (declared in
``$GROVE_HOME/workspaces.yaml``) unifies the three enforcement planes:
generic file tools (write_file / read_file), the agent FS chokepoint, and the
shell classifier. FAIL-CLOSED: only explicitly granted paths are autonomous;
everything else under ~/.grove stays walled — including substrate, secrets, and
tokens that the prior v2 blanket-GREEN complement left writable on the shell.
"""

from __future__ import annotations

import json

import pytest

from grove.utils.fs_utils import (
    is_governed_path,
    is_granted_workspace,
    is_scope_defining,
)


@pytest.fixture
def grove(tmp_path, monkeypatch):
    """Materialize a tmp GROVE_HOME with research/ + notes/ granted, plus the
    substrate/secret files that must stay walled."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research" / "existing.md").write_text("seed\n")
    (tmp_path / "memory" / "records.jsonl").write_text("{}\n")
    (tmp_path / "intent_records.jsonl").write_text("{}\n")
    (tmp_path / "google_token.json").write_text("{}\n")
    (tmp_path / "config.yaml").write_text("x: 1\n")
    (tmp_path / "zones.schema.yaml").write_text("x\n")
    (tmp_path / "workspaces.yaml").write_text(
        "granted_workspaces:\n  - path: research/\n  - path: notes/\n"
    )
    return tmp_path


# ── is_granted_workspace allowlist semantics (SPEC 1-8) ──────────────────────


class TestIsGrantedWorkspace:
    def test_1_research_is_granted(self, grove):
        assert is_granted_workspace(grove / "research" / "doc.md") is True

    def test_2_notes_is_granted(self, grove):
        assert is_granted_workspace(grove / "notes" / "todo.md") is True

    def test_3_memory_not_granted(self, grove):
        assert is_granted_workspace(grove / "memory" / "records.jsonl") is False

    def test_4_token_not_granted(self, grove):
        assert is_granted_workspace(grove / "google_token.json") is False

    def test_5_config_not_granted(self, grove):
        assert is_granted_workspace(grove / "config.yaml") is False

    def test_6_missing_manifest_grants_nothing(self, tmp_path, monkeypatch):
        # Fresh GROVE_HOME with NO workspaces.yaml → fail-closed.
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        (tmp_path / "research").mkdir()
        assert is_granted_workspace(tmp_path / "research" / "x.md") is False

    def test_7_malformed_manifest_grants_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        (tmp_path / "research").mkdir()
        (tmp_path / "workspaces.yaml").write_text("][ this : is : not : yaml\n")
        assert is_granted_workspace(tmp_path / "research" / "x.md") is False

    def test_8_traversal_to_secret_not_granted(self, grove):
        # ../research/../../.env collapses (realpath) off the granted prefix.
        escape = grove / "research" / ".." / ".." / ".env"
        assert is_granted_workspace(escape) is False

    def test_boundary_prefix_no_false_grant(self, grove):
        # `research/` must NOT grant a sibling whose name merely starts with it.
        (grove / "research-evil").mkdir()
        assert is_granted_workspace(grove / "research-evil" / "x.md") is False

    def test_scope_defining_never_granted_even_if_listed(self, tmp_path, monkeypatch):
        # Defense-in-depth: a fat-fingered grant of a scope-defining container
        # cannot widen authority.
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        (tmp_path / "skills" / "x").mkdir(parents=True)
        (tmp_path / "skills" / "x" / "SKILL.md").write_text("x\n")
        (tmp_path / "workspaces.yaml").write_text(
            "granted_workspaces:\n  - path: skills/\n"
        )
        assert is_granted_workspace(tmp_path / "skills" / "x" / "SKILL.md") is False


# ── Generic file tools (SPEC 9-13) ───────────────────────────────────────────


class TestFileTools:
    @pytest.fixture(autouse=True)
    def _neutralize_sensitive_check(self, monkeypatch):
        # macOS pytest tmp dirs live under /private/var (a _check_sensitive_path
        # prefix) which would fire first; neutralize so the governed/allowlist
        # gate is the operative one (mirrors test_c1b_substrate_write).
        monkeypatch.setattr(
            "tools.file_tools._check_sensitive_path", lambda *a, **k: None,
        )

    def test_9_write_file_granted_workspace_succeeds(self, grove):
        from tools.file_tools import write_file_tool
        raw = write_file_tool(str(grove / "research" / "test.md"), "hello\nworld\n")
        result = json.loads(raw)
        assert not result.get("error"), result
        assert (grove / "research" / "test.md").read_text() == "hello\nworld\n"

    def test_10_write_file_substrate_refused(self, grove):
        from tools.file_tools import write_file_tool
        result = json.loads(
            write_file_tool(str(grove / "intent_records.jsonl"), "{}\n")
        )
        assert result.get("error") and "Governed path" in result["error"]

    def test_11_write_file_token_refused(self, grove):
        from tools.file_tools import write_file_tool
        result = json.loads(
            write_file_tool(str(grove / "google_token.json"), "{}\n")
        )
        assert result.get("error") and "Governed path" in result["error"]

    def test_12_read_file_granted_workspace_succeeds(self, grove):
        from tools.file_tools import read_file_tool
        result = json.loads(read_file_tool(str(grove / "research" / "existing.md")))
        assert not result.get("error"), result

    def test_13_read_file_token_refused(self, grove):
        from tools.file_tools import read_file_tool
        result = json.loads(read_file_tool(str(grove / "google_token.json")))
        assert result.get("error") and "Governed path" in result["error"]


# ── Shell classifier (SPEC 14-16, 18) ────────────────────────────────────────


class TestShellClassifier:
    def test_14_shell_token_write_is_red(self, grove):
        # THE SECURITY FIX: was GREEN under v2's blanket complement.
        from grove.shell_effects import classify_shell_effect
        assert classify_shell_effect(
            f"echo x > {grove / 'google_token.json'}"
        ).zone == "red"

    def test_15_shell_research_write_is_green(self, grove):
        from grove.shell_effects import classify_shell_effect
        assert classify_shell_effect(
            f"echo x > {grove / 'research' / 'test.md'}"
        ).zone == "green"

    def test_16_shell_substrate_write_is_red(self, grove):
        from grove.shell_effects import classify_shell_effect
        assert classify_shell_effect(
            f"echo x > {grove / 'memory' / 'records.jsonl'}"
        ).zone == "red"

    def test_config_write_is_red(self, grove):
        from grove.shell_effects import classify_shell_effect
        assert classify_shell_effect(
            f"echo x > {grove / 'config.yaml'}"
        ).zone == "red"

    def test_18_heredoc_opacity_fires_before_workspace(self, grove):
        # Opacity is about content visibility, not path authorization: a heredoc
        # into a GRANTED workspace is still RED (opacity check precedes the
        # workspace check in _classify_node).
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect(
            f"cat > {grove / 'research' / 'x.md'} << EOF\ndata\nEOF"
        )
        assert zr.zone == "red"


# ── Invariants (SPEC 17, 19, 20) ─────────────────────────────────────────────


class TestInvariants:
    def test_17_workspaces_manifest_is_scope_defining(self, grove):
        assert is_scope_defining(grove / "workspaces.yaml") is True

    def test_19_is_governed_path_unchanged_for_granted(self, grove):
        # The blanket wall itself is UNCHANGED — a granted path is still
        # "governed"; the allowlist overrides only at the call sites.
        assert is_governed_path(grove / "research" / "doc.md") is True
        assert is_granted_workspace(grove / "research" / "doc.md") is True

    def test_20_write_file_still_walls_scope_defining(self, grove, monkeypatch):
        # Regression mirror of C1b: a scope-defining target stays refused.
        monkeypatch.setattr(
            "tools.file_tools._check_sensitive_path", lambda *a, **k: None,
        )
        from tools.file_tools import write_file_tool
        result = json.loads(
            write_file_tool(str(grove / "zones.schema.yaml"), "x\n")
        )
        assert result.get("error") and "Governed path" in result["error"]
