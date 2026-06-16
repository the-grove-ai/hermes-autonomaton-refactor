"""Filesystem governance helpers — GRV-010 C1b (conformance-substrate-write-v1).

``is_governed_path`` is the single source of truth for "does this path resolve
into the ``~/.grove`` governance tree?" — the structural wall that generic file
tools (``write_file`` / ``patch``) bounce off under Option A (sole-path). The
ONLY governance-config write path is the dedicated ``propose_governance_change``
door, which crosses Stage 04; everything else is blinded to the boundary.

Canonicalization is load-bearing. Both the target and ``GROVE_HOME`` are passed
through ``os.path.realpath`` (collapsing symlinks AND ``..``) BEFORE matching, so
neither a string-prefix trick (``~/.grove-evil``) nor a quarantine escape
(``~/.grove/skills/.andon/../zones.schema.yaml``) can slip past — the escape
collapses to its real target and is matched on the resolved path.

The single allowlisted exception is the agent-authoring quarantine,
``~/.grove/skills/.andon/`` — skills are authored there (still Stage-04-gated for
promotion); nothing else under ``~/.grove`` is writable by a generic file tool.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["is_governed_path", "GOVERNED_PATH_MESSAGE"]


GOVERNED_PATH_MESSAGE = (
    "Governed path: use dedicated governance tools or the .andon quarantine. "
    "Generic file tools cannot write inside ~/.grove (config, the live skills "
    "tree, or the provenance/telemetry feed). Governance config changes go "
    "through propose_governance_change; skills are authored to "
    "~/.grove/skills/.andon/ via skill_manage."
)


def is_governed_path(path: object) -> bool:
    """Return ``True`` if *path* resolves inside the ``~/.grove`` governance tree,
    EXCEPT the one allowlisted quarantine subtree ``~/.grove/skills/.andon/``.

    Resolves *path* and ``GROVE_HOME`` with ``realpath`` first so symlinks and
    ``..`` are collapsed before any prefix comparison. A path that cannot be
    expressed fails closed (treated as governed) — the wall never errs open.
    """
    from hermes_constants import get_hermes_home

    try:
        target = Path(os.path.realpath(os.path.expanduser(str(path))))
        grove_home = Path(os.path.realpath(get_hermes_home()))
    except (OSError, ValueError):
        # Unresolvable → fail closed. A path we cannot canonicalize is never
        # waved through the wall.
        return True

    inside = (target == grove_home) or (grove_home in target.parents)
    if not inside:
        return False

    # The sole exception: the agent-authoring quarantine. Re-validated on the
    # ALREADY-resolved target, so a ``.andon/../<live>`` escape (collapsed by
    # realpath above) does not land here.
    andon = Path(os.path.realpath(grove_home / "skills" / ".andon"))
    allowlisted = (target == andon) or (andon in target.parents)
    return not allowlisted
