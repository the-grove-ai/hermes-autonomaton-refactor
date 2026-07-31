"""Tests for the version-bump path in scripts/release.py.

``update_version_files()`` is the single place that stamps a new semver into
``pyproject.toml`` (and the version file); a silent break there fails the
weekly release. This module pins that path live. The module-level registry
construction keeps ``tools.registry`` in the release-test import surface
(Sprint 53).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


# Sprint 53 — module-level Dispatcher-style registry for tests.
from tools.registry import ToolRegistry as _Sprint53_TR_top, register_builtin_tools as _Sprint53_RBT_top
_REGISTRY = _Sprint53_TR_top()
_Sprint53_RBT_top(_REGISTRY)


def _load_release_module(monkeypatch, tmp_root: Path):
    """Import scripts/release.py with REPO_ROOT pinned to a temp tree."""
    spec = importlib.util.spec_from_file_location(
        "_release_under_test",
        Path(__file__).resolve().parents[2] / "scripts" / "release.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "REPO_ROOT", tmp_root)
    return module


def test_update_version_files_bumps_pyproject(monkeypatch, tmp_path):
    """update_version_files() is the function release.py actually calls, so it
    must stamp the new semver into pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.13.0"\n', encoding="utf-8"
    )
    version_dir = tmp_path / "hermes_cli"
    version_dir.mkdir()
    (version_dir / "__init__.py").write_text(
        '__version__ = "0.13.0"\n__release_date__ = "2026-05-14"\n',
        encoding="utf-8",
    )

    module = _load_release_module(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "VERSION_FILE", version_dir / "__init__.py")
    monkeypatch.setattr(module, "PYPROJECT_FILE", tmp_path / "pyproject.toml")

    module.update_version_files("0.14.0", "2026-05-21")

    pyproject_text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.14.0"' in pyproject_text
