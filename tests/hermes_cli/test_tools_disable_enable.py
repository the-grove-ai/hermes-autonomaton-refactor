"""Tests for hermes tools disable/enable/list command (backend)."""
from argparse import Namespace
from unittest.mock import patch

import pytest

from hermes_cli.tools_config import tools_disable_enable_command
from toolsets import UnknownToolsetError


# ── Built-in toolset disable ────────────────────────────────────────────────


class TestToolsDisableBuiltin:

    def test_disable_removes_toolset_from_platform(self):
        config = {"platform_toolsets": {"cli": ["web", "memory", "terminal"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="disable", names=["web"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "memory" in saved["platform_toolsets"]["cli"]

    def test_disable_multiple_toolsets(self):
        # "memory" toolset key retired (retrieval-ambient-class-v1 P1) — use
        # "todo" as the second real toolset in the multi-disable exercise.
        config = {"platform_toolsets": {"cli": ["web", "todo", "terminal"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="disable", names=["web", "todo"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "todo" not in saved["platform_toolsets"]["cli"]
        assert "terminal" in saved["platform_toolsets"]["cli"]

    def test_disable_already_absent_is_idempotent(self):
        config = {"platform_toolsets": {"cli": ["memory"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="disable", names=["web"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]


# ── Built-in toolset enable ─────────────────────────────────────────────────


class TestToolsEnableBuiltin:

    def test_enable_adds_toolset_to_platform(self):
        config = {"platform_toolsets": {"cli": ["memory"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="enable", names=["web"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert "web" in saved["platform_toolsets"]["cli"]

    def test_enable_already_present_is_idempotent(self):
        config = {"platform_toolsets": {"cli": ["web"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="enable", names=["web"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert saved["platform_toolsets"]["cli"].count("web") == 1


# ── MCP tool disable ────────────────────────────────────────────────────────


class TestToolsDisableMcp:

    def test_disable_adds_to_exclude_list(self):
        config = {"mcp_servers": {"github": {"command": "npx"}}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["github:create_issue"], platform="cli")
            )
        saved = mock_save.call_args[0][0]
        assert "create_issue" in saved["mcp_servers"]["github"]["tools"]["exclude"]

    def test_disable_already_excluded_is_idempotent(self):
        config = {"mcp_servers": {"github": {"tools": {"exclude": ["create_issue"]}}}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["github:create_issue"], platform="cli")
            )
        saved = mock_save.call_args[0][0]
        assert saved["mcp_servers"]["github"]["tools"]["exclude"].count("create_issue") == 1

    def test_disable_unknown_server_prints_error(self, capsys):
        config = {"mcp_servers": {}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config"):
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["unknown:tool"], platform="cli")
            )
        out = capsys.readouterr().out
        assert "MCP server 'unknown' not found in config" in out


# ── MCP tool enable ──────────────────────────────────────────────────────────


class TestToolsEnableMcp:

    def test_enable_removes_from_exclude_list(self):
        config = {"mcp_servers": {"github": {"tools": {"exclude": ["create_issue", "delete_branch"]}}}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(
                Namespace(tools_action="enable", names=["github:create_issue"], platform="cli")
            )
        saved = mock_save.call_args[0][0]
        assert "create_issue" not in saved["mcp_servers"]["github"]["tools"]["exclude"]
        assert "delete_branch" in saved["mcp_servers"]["github"]["tools"]["exclude"]


# ── Mixed targets ────────────────────────────────────────────────────────────


class TestToolsMixedTargets:

    def test_disable_builtin_and_mcp_together(self):
        config = {
            "platform_toolsets": {"cli": ["web", "memory"]},
            "mcp_servers": {"github": {"command": "npx"}},
        }
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(
                tools_action="disable",
                names=["web", "github:create_issue"],
                platform="cli",
            ))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "create_issue" in saved["mcp_servers"]["github"]["tools"]["exclude"]

    def test_builtin_toggle_does_not_persist_implicit_mcp_defaults(self):
        config = {
            "platform_toolsets": {"cli": ["web", "memory"]},
            "mcp_servers": {"exa": {"url": "https://mcp.exa.ai/mcp"}},
        }
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(
                tools_action="disable",
                names=["web"],
                platform="cli",
            ))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "memory" in saved["platform_toolsets"]["cli"]
        assert "exa" not in saved["platform_toolsets"]["cli"]


# ── List output ──────────────────────────────────────────────────────────────


class TestToolsList:

    def test_list_shows_enabled_toolsets(self, capsys):
        # "memory" toolset key retired (retrieval-ambient-class-v1 P1).
        config = {"platform_toolsets": {"cli": ["web", "todo"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config):
            tools_disable_enable_command(Namespace(tools_action="list", platform="cli"))
        out = capsys.readouterr().out
        assert "web" in out
        assert "todo" in out

    def test_list_shows_mcp_excluded_tools(self, capsys):
        config = {
            "mcp_servers": {"github": {"tools": {"exclude": ["create_issue"]}}},
        }
        with patch("hermes_cli.tools_config.load_config", return_value=config):
            tools_disable_enable_command(Namespace(tools_action="list", platform="cli"))
        out = capsys.readouterr().out
        assert "github" in out
        assert "create_issue" in out


# ── Validation ───────────────────────────────────────────────────────────────


class TestToolsValidation:

    def test_unknown_platform_prints_error(self, capsys):
        config = {}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config"):
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["web"], platform="invalid_platform")
            )
        out = capsys.readouterr().out
        assert "Unknown platform 'invalid_platform'" in out

    def test_unknown_toolset_raises_fail_loud(self):
        # GRV-009 E5 C-SEAM4 — warn-and-continue is retired: an unknown toolset
        # aborts the command with diagnostics (bad name + known set).
        config = {"platform_toolsets": {"cli": ["web"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config"):
            with pytest.raises(UnknownToolsetError) as ei:
                tools_disable_enable_command(
                    Namespace(tools_action="disable", names=["nonexistent_toolset"], platform="cli")
                )
        assert "nonexistent_toolset" in str(ei.value)

    def test_unknown_toolset_does_not_corrupt_config(self):
        # Fail-loud is atomic: the raise precedes save, so the config is never
        # written on a typo'd toolset name.
        config = {"platform_toolsets": {"cli": ["web", "memory"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            with pytest.raises(UnknownToolsetError):
                tools_disable_enable_command(
                    Namespace(tools_action="disable", names=["nonexistent_toolset"], platform="cli")
                )
        mock_save.assert_not_called()

    def test_mixed_valid_and_invalid_aborts_whole_command(self):
        # No partial apply: a single unknown name aborts the batch — the valid
        # 'web' is NOT removed, config untouched (the deliberate C-SEAM4 change).
        config = {"platform_toolsets": {"cli": ["web", "memory"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            with pytest.raises(UnknownToolsetError):
                tools_disable_enable_command(
                    Namespace(tools_action="disable", names=["web", "bad_toolset"], platform="cli")
                )
        mock_save.assert_not_called()
