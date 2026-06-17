"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _hermes_home_path() -> Path:
    """Resolve the active GROVE_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.grove"))


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    hermes_home = _hermes_home_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            str(hermes_home / ".env"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved GROVE_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("GROVE_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_root = get_safe_write_root()
    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
        return True

    return False


def get_read_block_error(path: str) -> Optional[str]:
    """Return an error message when a read targets internal Hermes cache files."""
    resolved = Path(path).expanduser().resolve()
    hermes_home = _hermes_home_path().resolve()
    blocked_dirs = [
        hermes_home / "skills" / ".hub" / "index-cache",
        hermes_home / "skills" / ".hub",
    ]
    for blocked in blocked_dirs:
        try:
            resolved.relative_to(blocked)
        except ValueError:
            continue
        return (
            f"Access denied: {path} is an internal Hermes cache file "
            "and cannot be read directly to prevent prompt injection. "
            "Use the skills_list or skill_view tools instead."
        )
    return None


# ---------------------------------------------------------------------------
# GRV-010 C3b — substrate-altitude agent FS guard.
#
# The single source of truth for "may a GENERIC AGENT file operation touch this
# path?" — kept here so ALL agent FS policy lives in one module. Both helpers
# delegate the boundary decision to ``grove.utils.fs_utils.is_governed_path``,
# which realpath-canonicalizes (collapsing symlinks AND ``..``) before matching
# and allowlists the ``~/.grove/skills/.andon/`` authoring quarantine.
#
# These guards apply ONLY to the agent file-op surface (the ShellFileOperations
# write/move/delete chokepoint, the read_file tool, and the Copilot ACP shim).
# Internal system loaders and the sanctioned governance/skill doors call the raw
# Python primitives directly and are NOT routed through here — by design.
# ---------------------------------------------------------------------------

GOVERNED_READ_MESSAGE = (
    "Governed path: generic file tools cannot read inside ~/.grove "
    "(governance config, operator secrets, or the live skills tree). Use "
    "skill_view / skills_list for skill content; governance config and secrets "
    "are operator-only. The ~/.grove/skills/.andon/ authoring quarantine "
    "remains readable."
)


def reject_governed_agent_write(path: object) -> None:
    """Raise ``PermissionError`` when an agent file-op targets the governed
    ``~/.grove`` tree (``.andon`` quarantine allowlisted).

    Realpath-resolved at consume — call this immediately before the raw FS
    write/move/delete so a symlinked or ``..``-traversed destination is
    canonicalized before the check. The raised ``PermissionError`` is already
    classified as an expected write denial by the file tools, so callers turn
    it into a clean tool error.
    """
    from grove.utils.fs_utils import GOVERNED_PATH_MESSAGE, is_governed_path

    if is_governed_path(path):
        raise PermissionError(GOVERNED_PATH_MESSAGE)


def reject_governed_agent_read(path: object) -> Optional[str]:
    """Return an error message when a generic agent read targets the governed
    ``~/.grove`` tree (``.andon`` quarantine allowlisted), else ``None``.

    Mirrors :func:`get_read_block_error`'s return-string contract so the
    read_file tool can surface it as a tool error. Realpath-resolved via
    ``is_governed_path``.
    """
    from grove.utils.fs_utils import is_governed_path

    if is_governed_path(path):
        return GOVERNED_READ_MESSAGE
    return None
