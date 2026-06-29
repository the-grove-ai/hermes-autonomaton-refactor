"""write-confinement-v1 — single write-confinement evaluator.

``is_write_allowed(target_path, session_cwd=None)`` is the one gate every
mutating surface calls BEFORE classification. It allows a write iff the
canonicalized target falls in the union of four sources:

  (a) ~/.grove EXCEPT secrets (is_secret_path still walls these)
  (b) declared write_workspaces.yaml (absolute roots, recursive via startswith)
  (c) /tmp + the platform scratch dir
  (d) the live ACP session cwd (dynamic, passed as session_cwd)

Anything outside the union hard-rejects. No silent allow-all on a missing
manifest; no glob (directory roots are recursive boundaries, not patterns);
canonicalization resolves the PARENT for not-yet-existing leaves so a symlinked
parent cannot smuggle a write out of the union.

Test isolation note: pytest ``tmp_path`` lives under the system temp dir, so
source (c) would shadow (a)/(b)/(d). Tests that isolate another source patch
``fs_utils._tmp_roots`` to ``()``; source (c) is tested on its own.
"""

from __future__ import annotations

import logging
import os
import tempfile

import pytest

from grove.utils import fs_utils
from grove.utils.fs_utils import append_write_workspace, is_write_allowed


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp GROVE_HOME with research/ present. Project workspaces live OUTSIDE
    grove (siblings under tmp_path) so source (b)/(d) are genuinely distinct from
    source (a)."""
    grove = tmp_path / "grove"
    grove.mkdir()
    (grove / "research").mkdir()
    monkeypatch.setenv("GROVE_HOME", str(grove))
    fs_utils._write_workspaces_cache.clear()
    return grove


@pytest.fixture
def no_tmp(monkeypatch):
    """Disable source (c) so the source under test is unambiguous."""
    monkeypatch.setattr(fs_utils, "_tmp_roots", lambda: ())


# ── source (a): ~/.grove except secrets ──────────────────────────────────────


def test_grove_nonsecret_allowed(env):
    assert is_write_allowed(str(env / "research" / "foo.md")) is True


def test_grove_secret_refused(env):
    (env / ".env").write_text("SECRET=1\n")
    assert is_write_allowed(str(env / ".env")) is False


def test_write_workspaces_manifest_is_scope_defining(env):
    """Meta-wall: the WRITE allow-list is itself scope-defining (parity with
    workspaces.yaml). If the agent could write it via the generic/shell tools, it
    could grant itself any workspace and confinement would be meaningless."""
    from grove.utils.fs_utils import is_scope_defining

    assert is_scope_defining(str(env / "write_workspaces.yaml")) is True


# ── source (b): declared write_workspaces, recursive ─────────────────────────


def _declare(grove, *paths):
    body = "write_workspaces:\n" + "".join(f"  - path: {p}\n" for p in paths)
    (grove / "write_workspaces.yaml").write_text(body)
    fs_utils._write_workspaces_cache.clear()


def test_declared_root_allowed(env, no_tmp):
    project = env.parent / "projects" / "acme"
    project.mkdir(parents=True)
    _declare(env, project)
    assert is_write_allowed(str(project / "readme.md")) is True


def test_declared_subdirectory_allowed(env, no_tmp):
    """The case single-level glob would FAIL: a deep subpath of a declared root."""
    project = env.parent / "projects" / "acme"
    (project / "deep" / "nested").mkdir(parents=True)
    _declare(env, project)
    assert is_write_allowed(str(project / "deep" / "nested" / "file.txt")) is True


def test_secret_inside_declared_workspace_refused(env, no_tmp):
    project = env.parent / "projects" / "acme"
    project.mkdir(parents=True)
    _declare(env, project)
    assert is_write_allowed(str(project / ".env")) is False


# ── source (c): /tmp + platform scratch ──────────────────────────────────────


def test_tmp_scratch_allowed(env):
    # real _tmp_roots; path is under the system temp but NOT under grove/declared.
    target = os.path.join(tempfile.gettempdir(), "grove-confine-scratch.txt")
    assert is_write_allowed(target) is True


def test_tmp_roots_includes_system_temp():
    assert os.path.realpath(tempfile.gettempdir()) in fs_utils._tmp_roots()


# ── source (d): live ACP session cwd ─────────────────────────────────────────


def test_session_cwd_subpath_allowed(env, no_tmp):
    cwd = env.parent / "ide-project"
    cwd.mkdir()
    assert is_write_allowed(str(cwd / "src" / "main.py"), session_cwd=str(cwd)) is True


def test_session_cwd_none_skips_source_d(env, no_tmp):
    cwd = env.parent / "ide-project"
    cwd.mkdir()
    assert is_write_allowed(str(cwd / "src" / "main.py"), session_cwd=None) is False


# ── outside the union → hard reject ──────────────────────────────────────────


def test_etc_refused(env, no_tmp):
    assert is_write_allowed("/etc/hosts") is False


def test_random_home_refused(env, no_tmp):
    assert is_write_allowed("/home/random/file") is False


# ── symlinked-parent traversal ───────────────────────────────────────────────


def test_symlink_parent_escape_refused(env, no_tmp):
    """A symlink INSIDE a declared root pointing OUT of it must not smuggle a
    write past the boundary: string-prefix matches the declared root, but
    realpath of the parent resolves outside the union → refused."""
    project = env.parent / "projects" / "acme"
    project.mkdir(parents=True)
    outside = env.parent / "outside"
    outside.mkdir()
    _declare(env, project)
    # sanity: a genuine file under the declared root IS allowed
    assert is_write_allowed(str(project / "ok.txt")) is True
    link = project / "link"
    link.symlink_to(outside, target_is_directory=True)
    assert is_write_allowed(str(link / "newfile")) is False


# ── no-silent-path guarantees ────────────────────────────────────────────────


def test_missing_manifest_warns_and_other_sources_work(env, no_tmp, caplog):
    # No write_workspaces.yaml exists. Source (a) must still work …
    assert is_write_allowed(str(env / "research" / "x.md")) is True
    # … and a (b)-reaching path must LOUDLY warn (never silently allow-all).
    with caplog.at_level(logging.WARNING, logger="grove.utils.fs_utils"):
        result = is_write_allowed(str(env.parent / "projects" / "acme" / "f.txt"))
    assert result is False
    assert any("declared workspaces unavailable" in r.message for r in caplog.records)


# ── workspace-grant flow: grant → reload → previously-refused now allowed ─────


def test_grant_then_allowed(env, no_tmp):
    project = env.parent / "projects" / "acme"
    project.mkdir(parents=True)
    target = project / "src" / "main.py"
    assert is_write_allowed(str(target)) is False
    append_write_workspace(str(project), grove_home=str(env))
    assert is_write_allowed(str(target)) is True
