"""write-workspace-grant-flow-v1 — the add_write_workspace grant tool.

When a write is refused, the agent calls add_write_workspace (yellow-zone →
sovereignty prompt). On operator approval the directory is appended to
write_workspaces.yaml (comment-preserving, hot-reload) and the original write
retries. Guards: absolute path only, no secret-laundering.
"""

from __future__ import annotations

import json

import pytest
import yaml

import grove.utils.fs_utils as fs_utils
from grove.utils.fs_utils import is_write_allowed
from tools.workspace_grant_tool import add_write_workspace


@pytest.fixture
def env(tmp_path, monkeypatch):
    grove = tmp_path / "grove"
    grove.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(grove))
    fs_utils._write_workspaces_cache.clear()
    return grove


@pytest.fixture
def no_tmp(monkeypatch):
    """Disable source (c) so a tmp_path-rooted project is granted via (b) only."""
    monkeypatch.setattr(fs_utils, "_tmp_roots", lambda: ())


def _is_error(res):
    try:
        return "error" in json.loads(res)
    except Exception:
        return False


def test_grant_valid_absolute_then_allowed(env, no_tmp):
    project = env.parent / "proj"
    project.mkdir()
    target = project / "src" / "x.py"
    assert is_write_allowed(str(target)) is False  # before grant
    res = add_write_workspace(str(project))
    assert not _is_error(res)
    assert "proj" in res
    assert is_write_allowed(str(target)) is True  # hot-reloaded after grant


def test_grant_relative_refused(env, no_tmp):
    res = add_write_workspace("relative/dir")
    assert _is_error(res)
    assert "absolute" in res.lower()


def test_grant_secret_refused(env, no_tmp):
    # ~/.grove/mcp-tokens is a secret dir anchor → no laundering into the list.
    res = add_write_workspace(str(env / "mcp-tokens"))
    assert _is_error(res)
    assert is_write_allowed(str(env / "mcp-tokens" / "x")) is False


def test_grant_idempotent(env, no_tmp):
    project = env.parent / "proj"
    project.mkdir()
    add_write_workspace(str(project))
    add_write_workspace(str(project))
    data = yaml.safe_load((env / "write_workspaces.yaml").read_text())
    assert len(data["write_workspaces"]) == 1
    assert data["write_workspaces"][0]["path"].endswith("proj")


def test_grant_tool_classifies_yellow(hermetic_grove_home):
    import grove.zones as zones

    zones.initialize()
    assert zones.classify("add_write_workspace").zone == "yellow"


def test_capability_record_admits_tool():
    from grove.capability_registry import load_capabilities

    caps = load_capabilities()
    bound = set()
    for c in caps.values():
        bound.update(c.bindings.tools)
    assert "add_write_workspace" in bound


def test_end_to_end_grant_unblocks_write(env, no_tmp):
    project = env.parent / "clientwork"
    project.mkdir()
    target = project / "report.md"
    assert is_write_allowed(str(target)) is False  # refused
    add_write_workspace(str(project))  # operator-approved grant in production
    assert is_write_allowed(str(target)) is True  # retry now passes
