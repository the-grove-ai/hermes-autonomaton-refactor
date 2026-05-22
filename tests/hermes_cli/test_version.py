"""Version surface — the importable package version is the v0.1.0 release.

Sprint 19 (v0.1-release-prep-v1) bumped the fork from the upstream
v0.14.0 fork-point to the Grove v0.1.0 release.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_importable_version_is_0_1_0():
    """hermes_cli.__version__ is the single source of truth read by
    --version, the HTTP User-Agent strings, and the model catalog."""
    from hermes_cli import __version__

    assert __version__ == "0.1.0"


def test_release_date_is_the_v0_1_0_date():
    """__release_date__ pairs with __version__ in the --version display."""
    from hermes_cli import __release_date__

    assert __release_date__ == "2026.5.22"


def test_pyproject_version_matches_package_version():
    """pyproject.toml and the importable package version agree on 0.1.0."""
    import hermes_cli

    repo_root = Path(hermes_cli.__file__).resolve().parent.parent
    pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(pyproject_text)

    assert parsed["project"]["version"] == hermes_cli.__version__ == "0.1.0"
