"""Regression test: the google-workspace verbs must register required credential files.

PR #9931 once removed the credential-file declaration, which broke credential
file mounting in Docker/Modal remote backends (#16452). The declaration's home
moved from the skill's ``required_credential_files`` frontmatter to the native
verbs' ``register()`` when the skill was retired (GRV-009 E2 C3); these tests
guard the same capability — detecting/mounting the Google OAuth credentials —
against silently vanishing from its new home.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

_EXPECTED_PATHS = {"google_token.json", "google_client_secret.json"}


class _FakeReg:
    """Minimal registry — register() iterates verbs calling reg.register()."""

    def register(self, **kwargs):
        pass


class TestGoogleWorkspaceCredentialFiles:
    def test_workspace_tool_declares_required_credential_files(self):
        # The declaration's new home: a constant on the verb module (not the
        # retired SKILL.md frontmatter). Guards against the header silently
        # disappearing — the PR #9931 regression.
        from tools.google_workspace_tool import _REQUIRED_CREDENTIAL_FILES

        paths = set(_REQUIRED_CREDENTIAL_FILES)
        assert _EXPECTED_PATHS <= paths, (
            f"Missing entries in _REQUIRED_CREDENTIAL_FILES: {_EXPECTED_PATHS - paths}"
        )

    def test_register_mounts_credentials_when_files_exist(self, tmp_path):
        # Wiring guard: calling the tool's register() must register the
        # credential files for remote-sandbox mounting. If the
        # register_credential_files() call is dropped from register(), this fails.
        hermes_home = tmp_path / ".grove"
        hermes_home.mkdir()
        (hermes_home / "google_token.json").write_text("{}")
        (hermes_home / "google_client_secret.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
        )
        from tools import google_workspace_tool

        clear_credential_files()
        try:
            with patch.dict(os.environ, {"GROVE_HOME": str(hermes_home)}):
                google_workspace_tool.register(_FakeReg())

            container_paths = {m["container_path"] for m in get_credential_file_mounts()}
            assert "/root/.grove/google_token.json" in container_paths
            assert "/root/.grove/google_client_secret.json" in container_paths
        finally:
            clear_credential_files()

    def test_missing_token_is_reported(self, tmp_path):
        """google_token.json absent (first-time setup) — reported as missing,
        client secret still mounts. Tests the declared set feeds the subsystem."""
        hermes_home = tmp_path / ".grove"
        hermes_home.mkdir()
        (hermes_home / "google_client_secret.json").write_text("{}")

        from tools.credential_files import (
            clear_credential_files,
            get_credential_file_mounts,
            register_credential_files,
        )
        from tools.google_workspace_tool import _REQUIRED_CREDENTIAL_FILES

        clear_credential_files()
        try:
            with patch.dict(os.environ, {"GROVE_HOME": str(hermes_home)}):
                missing = register_credential_files(_REQUIRED_CREDENTIAL_FILES)

            assert "google_token.json" in missing
            container_paths = {m["container_path"] for m in get_credential_file_mounts()}
            assert "/root/.grove/google_client_secret.json" in container_paths
            assert "/root/.grove/google_token.json" not in container_paths
        finally:
            clear_credential_files()
