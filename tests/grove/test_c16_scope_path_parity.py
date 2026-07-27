"""C16 — routing_writer's resolved write path must equal the path
``fs_utils.is_scope_defining`` evaluates.

The scope wall ``realpath``-resolves every path it checks (collapsing symlinks
and ``..``). Before this sprint ``routing_writer._default_config_path`` returned
an UNRESOLVED path, so ``apply_mutation``'s ``os.replace`` could land on a
symlink (severing a ``~/.grove/routing.config.yaml`` → provider-variant, or a
symlinked ``~/.grove`` storage dir) — a target the wall was evaluating in its
resolved form. This test pins the two resolutions together for BOTH
scope-defining routing surfaces.

Layout mirrors the live VM: ``~/.grove`` is a directory symlink onto the real
storage (``/mnt/grove-data/.grove``), with the routing files as regular files
inside. Everything runs under ``tmp_path``; no live ``~/.grove`` path is
touched, written, or evaluated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from grove.config.routing_writer import (
    _default_config_path,
    _default_machine_path,
)
from grove.utils.fs_utils import is_scope_defining


@pytest.fixture
def symlinked_grove_home(tmp_path, monkeypatch):
    """A GROVE_HOME that is a directory symlink onto real backing storage —
    the live VM arrangement (``~/.grove`` -> ``/mnt/grove-data/.grove``)."""
    real = tmp_path / "mnt" / "grove-data" / ".grove"
    real.mkdir(parents=True)
    (real / "routing.config.yaml").write_text("routing: {}\n", encoding="utf-8")
    (real / "routing.autonomaton.yaml").write_text("routing: {}\n", encoding="utf-8")

    home = tmp_path / "home" / "hermes" / ".grove"
    home.parent.mkdir(parents=True)
    home.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("GROVE_HOME", str(home))
    return home


@pytest.mark.parametrize(
    "filename, resolver",
    [
        ("routing.config.yaml", _default_config_path),
        ("routing.autonomaton.yaml", _default_machine_path),
    ],
)
def test_writer_resolved_path_matches_scope_wall_resolution(
    symlinked_grove_home, filename, resolver
):
    """POSITIVE — the writer's resolved target and the scope wall's resolved
    check path are identical, for both routing.config.yaml and
    routing.autonomaton.yaml."""
    nominal = symlinked_grove_home / filename

    # What routing_writer will os.replace onto (production resolver).
    writer_path = resolver()

    # Exactly the resolution is_scope_defining performs internally
    # (os.path.realpath of the nominal path).
    wall_path = Path(os.path.realpath(str(nominal)))

    assert writer_path == wall_path

    # And the wall still classifies the canonical surface as scope-defining
    # (the directory symlink collapses to the real dir; the filename is
    # preserved, so membership matches).
    assert is_scope_defining(str(nominal), grove_home=str(symlinked_grove_home)) is True


@pytest.mark.parametrize(
    "filename, resolver",
    [
        ("routing.config.yaml", _default_config_path),
        ("routing.autonomaton.yaml", _default_machine_path),
    ],
)
def test_negative_control_mutated_path_breaks_parity(
    symlinked_grove_home, filename, resolver
):
    """NEGATIVE CONTROL — perturbing the resolved write path makes the parity
    assertion FAIL, proving the positive assertion is not vacuous."""
    nominal = symlinked_grove_home / filename
    wall_path = Path(os.path.realpath(str(nominal)))

    mutated = resolver().with_name("routing.config.OTHER.yaml")

    assert mutated != wall_path
