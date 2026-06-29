"""write-confinement-v1 Phase 3 — shell classifier confinement.

A shell WRITE target outside ~/.grove is no longer a soft YELLOW: it routes
through ``is_write_allowed``. A target inside the write allow-list (declared
workspaces / tmp) stays YELLOW (operator-approvable); anything else hard-rejects
RED. session_cwd is NOT threadable to the shell classifier (the shell surface is
not an IDE/ACP surface) — see the Phase 3 Andon; source (d) does not apply here.
"""

from __future__ import annotations

import grove.utils.fs_utils as fs_utils
from grove.shell_effects import classify_shell_effect


def test_shell_redirect_outside_union_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    r = classify_shell_effect("echo hi > /home/nonexistent-grove/x.txt")
    assert r.zone == "red"


def test_shell_rm_outside_union_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    r = classify_shell_effect("rm /home/nonexistent-grove/x.txt")
    assert r.zone == "red"


def test_shell_write_to_write_workspaces_manifest_is_red(tmp_path, monkeypatch):
    """Meta-wall on the shell surface: a shell write to the WRITE allow-list
    manifest hard-rejects (the agent cannot grant itself workspaces)."""
    grove = tmp_path / "grove"
    grove.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(grove))
    r = classify_shell_effect(f"echo x > {grove}/write_workspaces.yaml")
    assert r.zone == "red"


def test_shell_redirect_tmp_stays_yellow(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "grove"))
    (tmp_path / "grove").mkdir()
    r = classify_shell_effect(f"echo hi > {tmp_path}/scratch.txt")
    assert r.zone == "yellow"


def test_shell_redirect_declared_workspace_stays_yellow(tmp_path, monkeypatch):
    grove = tmp_path / "grove"
    grove.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(grove))
    project = tmp_path / "project"
    project.mkdir()
    (grove / "write_workspaces.yaml").write_text(
        f"write_workspaces:\n  - path: {project}\n"
    )
    fs_utils._write_workspaces_cache.clear()
    r = classify_shell_effect(f"echo hi > {project}/out.txt")
    assert r.zone == "yellow"
