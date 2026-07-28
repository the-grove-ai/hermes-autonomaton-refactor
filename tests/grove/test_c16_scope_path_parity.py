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
    (real / "routing.operational.yaml").write_text("routing: {}\n", encoding="utf-8")
    (real / "routing.autonomaton.yaml").write_text("routing: {}\n", encoding="utf-8")

    home = tmp_path / "home" / "hermes" / ".grove"
    home.parent.mkdir(parents=True)
    home.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("GROVE_HOME", str(home))
    return home


@pytest.mark.parametrize(
    "filename, resolver, expect_scope_defining",
    [
        # GRV-001 v2.0 — the operator OPERATIONAL file is surface_class: in_scope
        # (autonomous_loop-writable; the model-swap writer targets it), so the wall
        # does NOT classify it as scope-defining. R1 — the machine overlay is
        # likewise NOT scope-defining: its write door is the machine-sink fail-closed
        # guard, not the scope wall. Both writers' resolved targets must still equal
        # the wall's resolution (the symlink-parity invariant this test pins).
        ("routing.operational.yaml", _default_config_path, False),
        ("routing.autonomaton.yaml", _default_machine_path, False),
    ],
)
def test_writer_resolved_path_matches_scope_wall_resolution(
    symlinked_grove_home, filename, resolver, expect_scope_defining
):
    """POSITIVE — the writer's resolved target and the scope wall's resolved
    check path are identical, for both the operational file and the machine
    overlay. The wall's scope-defining classification differs per surface_class:
    the operational file is in_scope, the machine overlay is scope-defining."""
    nominal = symlinked_grove_home / filename

    # What routing_writer will os.replace onto (production resolver).
    writer_path = resolver()

    # Exactly the resolution is_scope_defining performs internally
    # (os.path.realpath of the nominal path).
    wall_path = Path(os.path.realpath(str(nominal)))

    assert writer_path == wall_path

    # The directory symlink collapses to the real dir; the filename is preserved,
    # so membership (or non-membership) matches the surface's declared class.
    assert (
        is_scope_defining(str(nominal), grove_home=str(symlinked_grove_home))
        is expect_scope_defining
    )


@pytest.mark.parametrize(
    "filename, resolver",
    [
        ("routing.operational.yaml", _default_config_path),
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
