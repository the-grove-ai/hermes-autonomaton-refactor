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

__all__ = ["is_governed_path", "is_scope_defining", "GOVERNED_PATH_MESSAGE"]


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


# ── GRV-001 v2.0 — scope-keyed surface (SHELL governance path ONLY) ───────────
#
# is_scope_defining is DELIBERATELY SEPARATE from is_governed_path above and
# serves a different contract. is_governed_path stays the blanket ~/.grove wall
# for the GENERIC FILE TOOLS (write_file / patch, the agent read/write
# chokepoint, skill_manage) — that contract is unchanged. is_scope_defining
# answers the narrower v2.0 question used ONLY by the shell-effect classifier:
# "would mutating this path redefine the agent's own authority?" It lets the
# shell path treat granted workspaces under ~/.grove as autonomous (GREEN) while
# keeping the scope-defining surfaces sovereign (RED). See grove/shell_effects.py.

# Files (relative to GROVE_HOME) whose mutation redefines the agent's authority,
# plus the operator secrets file (.env): a shell write to .env is RED under the
# v1 blanket today, and scope keying MUST keep it RED — secrets are never an
# autonomous (GREEN) workspace write.
_SCOPE_DEFINING_FILES = frozenset({
    "zones.schema.yaml",
    "routing.config.yaml",
    "prompt.config.yaml",
    ".env",
    os.path.join("dock", "dock.yaml"),
})

# Whole subtrees (relative to GROVE_HOME) that are scope-defining. The live skill
# tree is ~/.grove/skills/<name> (NOT skills/active): the WHOLE skills tree is
# scope-defining for shell writes — skills are authored through skill_manage to
# the .andon quarantine, never a raw terminal write. The capability registry
# governs the agent's executable surface.
_SCOPE_DEFINING_DIR_PREFIXES = (
    "skills",
    "capabilities",
)

# Flattened surface set (files + dir subtrees), relative to GROVE_HOME.
_SCOPE_DEFINING_ENTRIES = tuple(_SCOPE_DEFINING_FILES) + _SCOPE_DEFINING_DIR_PREFIXES


def is_scope_defining(path: object, grove_home: object = None) -> bool:
    """Return ``True`` if *path* resolves to a scope-defining surface inside the
    ``~/.grove`` governance tree — a file or subtree whose mutation would expand
    the agent's own authority (zone schema, routing/prompt config, the dock
    goals, operator secrets, the live skills tree, the capability registry).

    Also ``True`` for the grove root and any ANCESTOR directory of a
    scope-defining surface — mutating a container (``rm -rf ~/.grove``,
    ``rm -rf ~/.grove/dock``) destroys the surface, so it is never autonomous.

    *path* and ``GROVE_HOME`` are realpath-resolved first (collapsing symlinks
    AND ``..``) so a traversal/symlink escape onto a scope-defining surface is
    caught on the resolved target. An unresolvable path fails closed (treated as
    scope-defining) — the wall never errs open.

    GRV-001 v2.0 scope-keyed check for the SHELL path ONLY; generic file tools
    keep the blanket :func:`is_governed_path` wall.
    """
    from hermes_constants import get_hermes_home

    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        grove = os.path.realpath(
            str(grove_home) if grove_home is not None else get_hermes_home()
        )
    except (OSError, ValueError):
        return True  # unresolvable → fail closed (treated as scope-defining)

    if resolved != grove and not resolved.startswith(grove + os.sep):
        return False

    rel = os.path.relpath(resolved, grove)
    # The grove root itself is the ancestor of every scope-defining surface:
    # mutating ~/.grove as a unit (rm -rf ~/.grove, find ~/.grove -delete) is
    # never autonomous.
    if rel == os.curdir:  # "."
        return True
    for entry in _SCOPE_DEFINING_ENTRIES:
        # target IS the surface, or lives inside a scope-defining subtree …
        if rel == entry or rel.startswith(entry + os.sep):
            return True
        # … or target is an ANCESTOR directory of a scope-defining surface (e.g.
        # `dock` is the parent of the scope-defining `dock/dock.yaml`; deleting
        # the parent destroys the surface).
        if entry.startswith(rel + os.sep):
            return True
    return False
