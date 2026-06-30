"""Filesystem governance helpers — GRV-010 C1b (conformance-substrate-write-v1).

``is_governed_path`` is the single source of truth for "does this path resolve
into the ``~/.grove`` governance tree?" Post secrets-only-wall-v1 the generic
file tools (``write_file`` / ``patch``) wall on ``is_secret_path`` (operator
secrets + system paths), not on this check — which now backs
``skill_manager_tool``'s ``~/.grove`` boundary. The dedicated
``propose_governance_change`` door, which crosses Stage 04, remains the
sanctioned writer of governance config.

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

import fnmatch
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "is_governed_path",
    "is_scope_defining",
    "is_granted_workspace",
    "is_secret_path",
    "is_write_allowed",
    "append_write_workspace",
    "write_refused_message",
    "GOVERNED_PATH_MESSAGE",
]


GOVERNED_PATH_MESSAGE = (
    "This path is protected: it holds operator secrets (credentials, tokens, "
    "keys) or is a sensitive system path. It cannot be written, and no approval "
    "makes that safe. Everything else — including all of ~/.grove and your "
    "project files — is writable through the normal flow. Do not attempt "
    "alternative write methods."
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
# serves a different contract. Post secrets-only-wall-v1, is_governed_path no
# longer walls the generic file tools — write_file / patch and the agent
# read/write chokepoint wall on is_secret_path (secrets + system paths only).
# is_governed_path now backs skill_manager_tool's ~/.grove boundary check
# (.andon allowlisted). is_scope_defining
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
    # workspace-governance-unification-v1 — the META-WALL: the workspace grant
    # manifest is itself scope-defining. If the agent could write it, it could
    # grant itself unlimited GREEN zones, so it is never an autonomous write.
    "workspaces.yaml",
    # write-confinement-v1 — the WRITE allow-list manifest is the same meta-wall:
    # if the agent could write it via the generic/shell tools it could grant
    # itself any write workspace, so it is never an autonomous write (the
    # operator-approved grant flow applies it through a sanctioned door instead).
    "write_workspaces.yaml",
    # GRV-001 Grant Token model — standing grant manifest is scope-defining.
    # The agent cannot write its own grants; only the operator can create or
    # revoke standing grants via authenticated grant management commands.
    "grants.yaml",
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

    GRV-001 v2.0 scope-keyed check for the SHELL path ONLY; post
    secrets-only-wall-v1 the generic file tools wall on
    :func:`is_secret_path` (secrets + system paths), not on this check.
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


# ── workspace-governance-unification-v1 — positive allowlist (ALL FS planes) ──
#
# is_granted_workspace is the POSITIVE allowlist consulted by the generic file
# tools (write_file / read_file), the agent FS chokepoint, AND the shell
# classifier. Only paths the operator explicitly grants in
# ``$GROVE_HOME/workspaces.yaml`` are autonomous workspaces; everything else
# under ~/.grove stays walled. FAIL-CLOSED on every axis: a missing or malformed
# manifest grants NOTHING, a path outside the grove tree is not a workspace, and
# a scope-defining surface is NEVER grantable even if the manifest lists it
# (defense-in-depth — a fat-fingered grant cannot widen the agent's authority).
# The manifest itself is in _SCOPE_DEFINING_FILES (the meta-wall).

_WORKSPACES_MANIFEST = "workspaces.yaml"
# {grove_realpath: (manifest_mtime_ns, frozenset(granted_relpaths))}. The
# resolver runs per write-target per command, so the parsed manifest is cached
# by mtime to avoid a YAML load on every FS check (A1). A stat is cheap; the
# parse only re-runs when the manifest actually changes.
_granted_cache: dict = {}


def _load_granted_workspaces(grove: str) -> frozenset:
    """Return the granted workspace relpaths (trailing slash stripped), cached by
    manifest mtime. Fail-closed: a missing manifest or any parse error yields the
    empty set (nothing granted)."""
    ws_path = os.path.join(grove, _WORKSPACES_MANIFEST)
    try:
        mtime = os.stat(ws_path).st_mtime_ns
    except OSError:
        return frozenset()  # no manifest → nothing granted (not cached: a later
        #                     create must be seen on the next call)
    cached = _granted_cache.get(grove)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        import yaml

        with open(ws_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        granted = frozenset(
            rel
            for w in data.get("granted_workspaces", [])
            if (rel := str(w.get("path", "")).strip().rstrip("/"))
        )
    except Exception:
        granted = frozenset()  # malformed → fail closed
    _granted_cache[grove] = (mtime, granted)
    return granted


def is_granted_workspace(path: object, grove_home: object = None) -> bool:
    """Return ``True`` iff *path* resolves into an operator-granted workspace
    under ``$GROVE_HOME`` (declared in ``workspaces.yaml``).

    Positive allowlist, FAIL-CLOSED: a missing/malformed manifest → ``False``; a
    path outside the grove tree → ``False``; a scope-defining surface → ``False``
    even if the manifest mistakenly lists it (defense-in-depth). *path* and
    ``GROVE_HOME`` are realpath-resolved first (collapsing symlinks AND ``..``)
    so a traversal/symlink escape is matched on the resolved target. Prefix
    matching is component-boundary safe — ``research/`` grants ``research/x`` but
    never ``research-evil/x``.
    """
    from hermes_constants import get_hermes_home

    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        grove = os.path.realpath(
            str(grove_home) if grove_home is not None else get_hermes_home()
        )
    except (OSError, ValueError):
        return False  # unresolvable → not granted

    if resolved != grove and not resolved.startswith(grove + os.sep):
        return False  # outside the grove tree

    # Defense-in-depth: a scope-defining surface is never an autonomous
    # workspace, even if workspaces.yaml mistakenly lists its container.
    if is_scope_defining(resolved, grove):
        return False

    rel = os.path.relpath(resolved, grove)
    for prefix in _load_granted_workspaces(grove):
        if rel == prefix or rel.startswith(prefix + os.sep):
            return True
    return False


# ── secrets-only-wall-v1 (Hotfix 3) — deny-list, NOT confinement ─────────────
#
# Rationalized model: the agent does legitimate file work ANYWHERE (project
# files, /tmp, IDE/ACP surfaces) — there is NO in-bounds sandbox. file_safety's
# sole job is a DENY-LIST: refuse (a) sensitive SYSTEM roots and (b) secret files
# WHEREVER they live. ~/.grove is deliberately NOT a sensitive root — it is the
# operator's brain, the most readable place; only its secrets (.env, mcp-tokens/,
# …) are walled, by the same anchors that apply everywhere.
#
# realpath canonicalizes the target FIRST, so a `..` traversal or a symlink that
# resolves onto a secret / sensitive root is matched on the REAL destination —
# that is the SOLE traversal guard now that the in-bounds check is gone.

# Sensitive SYSTEM roots — absolute-prefix block. Deliberately NOT ~/.grove.
_SENSITIVE_ROOTS = (
    "/etc",
    "/var/log",
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
)

# Directory anchors — a path with a component equal to one of these is inside a
# secret store; matched anywhere (incl. ~/.grove). NOT a substring glob, so the
# document cache (doc_*_secret.bin) and a "debugging-mcp-credentials" skill are
# NOT wrongly walled.
_SECRET_DIR_ANCHORS = (
    "mcp-tokens",
    "pairing",
    "secrets",
    ".credentials",
)

# Path-suffix anchors — the basename alone ("config") is too generic, so match
# the trailing path segment.
_SECRET_PATH_SUFFIXES = (
    os.path.join(".git", "config"),
)

# File-glob anchors — matched against the BASENAME via fnmatch (apply anywhere).
_SECRET_FILE_GLOBS = (
    ".env*",
    "auth.json",
    "credentials.json",
    "google_client_secret.json",
    "google_token.json",
    "google_token.json.bak*",
    "application_default_credentials.json",
    "*service_account*.json",
    "channel_directory.json",
    "gateway_state.json",
    ".npmrc",
    "pip.conf",
    "*.pem",
    "*.key",
    "id_rsa*",
)


def is_secret_path(path: object, grove_home: object = None) -> bool:
    """Return ``True`` if reading OR writing *path* must be refused — the SINGLE
    file_safety wall (secrets-only-wall-v1, deny-list model).

    NO sandbox / in-bounds confinement: the agent does legitimate work on project
    files, ``/tmp``, and IDE surfaces. This refuses only (a) sensitive SYSTEM
    roots (``/etc``, ``~/.ssh``, …) and (b) secret files/dirs
    (credentials/tokens/keys) WHEREVER they live — including inside ``~/.grove``
    (so ``~/.grove`` stays broadly readable while ITS secrets stay walled).

    ``realpath`` canonicalizes the target FIRST: a ``..`` traversal or a symlink
    that resolves onto a secret or a sensitive root is matched on the REAL
    destination — the sole traversal guard now that in-bounds is gone.
    Unresolvable → fail closed.

    ``grove_home`` is accepted for signature stability but unused — the deny-list
    is absolute, not grove-relative.
    """
    try:
        target = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return True  # unresolvable → fail closed

    # (a) sensitive SYSTEM roots — absolute-prefix block on the resolved target.
    for root in _SENSITIVE_ROOTS:
        try:
            r = os.path.realpath(os.path.expanduser(root))
        except (OSError, ValueError):
            return True  # a configured root we cannot resolve → fail closed
        if target == r or target.startswith(r + os.sep):
            return True

    # (b) secret anchors — apply everywhere (dir component, path suffix, basename).
    parts = target.split(os.sep)
    if any(d in parts for d in _SECRET_DIR_ANCHORS):
        return True
    for suffix in _SECRET_PATH_SUFFIXES:
        if target == suffix or target.endswith(os.sep + suffix):
            return True
    base = os.path.basename(target)
    return any(fnmatch.fnmatch(base, g) for g in _SECRET_FILE_GLOBS)


# ── write-confinement-v1 — single write-confinement evaluator ────────────────
#
# is_write_allowed is the ONE gate every mutating surface (write_file / patch /
# delete / move, the shell classifier, the ACP write shim) calls BEFORE
# classification. Reads stay on the secrets-only deny-list (is_secret_path);
# WRITES move to a positive ALLOW-list — so enumeration drift only ever affects
# reads (recoverable), never widens the irreversible verb. A write is allowed iff
# the canonicalized target lands in the union of four sources:
#
#   (a) ~/.grove EXCEPT secrets (is_secret_path still walls these)
#   (b) declared write_workspaces.yaml — absolute directory roots, recursive
#   (c) /tmp + the platform scratch dir
#   (d) the live ACP session cwd (dynamic, passed by the caller)
#
# Anything outside the union hard-rejects. FAIL-LOUD, NEVER fail-open: a missing
# or malformed manifest WARNS and grants nothing (it never silently allows all),
# and an unresolvable target is refused (never silently allowed).

_WRITE_WORKSPACES_MANIFEST = "write_workspaces.yaml"
# {grove_realpath: (manifest_mtime_ns, frozenset(declared_realpath_roots))}. The
# evaluator runs per write-target, so the parsed manifest is cached by mtime — a
# stat is cheap, the YAML parse only re-runs when the manifest actually changes.
_write_workspaces_cache: dict = {}


def _tmp_roots() -> tuple:
    """Resolved scratch roots that are always write-allowed (source c): ``/tmp``
    and the platform temp dir. Realpath-resolved so a write to ``/tmp/x`` matches
    even where ``/tmp`` is itself a symlink (e.g. ``/private/tmp`` on macOS)."""
    roots = set()
    for cand in ("/tmp", tempfile.gettempdir()):
        try:
            roots.add(os.path.realpath(cand))
        except (OSError, ValueError):
            continue
    return tuple(roots)


def _load_write_workspaces(grove: str) -> frozenset:
    """Return the declared absolute write-workspace roots (realpath-resolved),
    cached by manifest mtime.

    FAIL-LOUD: a missing or unparseable manifest logs a WARNING and yields the
    empty set — declared workspaces become unavailable, never silently allow-all
    and never silently deny-all-without-notice."""
    ws_path = os.path.join(grove, _WRITE_WORKSPACES_MANIFEST)
    try:
        mtime = os.stat(ws_path).st_mtime_ns
    except OSError:
        logger.warning(
            "write_workspaces.yaml not found or unparseable — declared "
            "workspaces unavailable (%s)",
            ws_path,
        )
        return frozenset()
    cached = _write_workspaces_cache.get(grove)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        import yaml

        with open(ws_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        roots = frozenset(
            os.path.realpath(os.path.expanduser(p))
            for w in data.get("write_workspaces", [])
            if isinstance(w, dict) and (p := str(w.get("path", "")).strip())
        )
    except Exception:
        logger.warning(
            "write_workspaces.yaml not found or unparseable — declared "
            "workspaces unavailable (%s)",
            ws_path,
        )
        return frozenset()
    _write_workspaces_cache[grove] = (mtime, roots)
    return roots


def _canonical_write_target(target_path: str) -> str | None:
    """Canonicalize *target_path* for matching. For an existing path, realpath the
    target itself; for a not-yet-existing leaf, realpath the PARENT directory and
    rejoin the basename — the real escape is a symlinked parent
    (``/tmp/link-to-etc/newfile``), not the leaf. Return ``None`` if the path
    cannot be resolved (caller fails closed)."""
    try:
        expanded = os.path.expanduser(str(target_path))
        if os.path.lexists(expanded):
            return os.path.realpath(expanded)
        parent = os.path.realpath(os.path.dirname(expanded) or os.curdir)
        return os.path.join(parent, os.path.basename(expanded))
    except (OSError, ValueError):
        return None


def is_write_allowed(target_path: str, session_cwd: str = None) -> bool:
    """Single source of truth for write confinement (write-confinement-v1).

    Return ``True`` iff the canonicalized *target_path* falls in the union of:
      (a) ``~/.grove`` and NOT a secret,
      (b) a declared ``write_workspaces.yaml`` root (recursive),
      (c) ``/tmp`` / the platform scratch dir,
      (d) *session_cwd* (when provided — the live ACP working dir).
    Anything else hard-rejects. Unresolvable target → refuse (never allow-open).
    """
    from hermes_constants import get_hermes_home

    resolved = _canonical_write_target(target_path)
    if resolved is None:
        return False  # unresolvable → loud refuse, never a silent allow

    try:
        grove = os.path.realpath(get_hermes_home())
    except (OSError, ValueError):
        grove = None

    # (a) ~/.grove EXCEPT secrets. A secret under ~/.grove is never writable and
    # no other source can rescue it, so decide grove paths here and return.
    if grove is not None and (resolved == grove or resolved.startswith(grove + os.sep)):
        return not is_secret_path(resolved)

    # Secrets are refused WHEREVER they live (an .env dropped into a declared
    # workspace, a *.pem under /tmp): apply the deny-list before the positive
    # sources so no allow-list source can launder a secret.
    if is_secret_path(resolved):
        return False

    # (b) declared write_workspaces.yaml — absolute directory roots, recursive.
    if grove is not None:
        for root in _load_write_workspaces(grove):
            if resolved == root or resolved.startswith(root + os.sep):
                return True

    # (c) /tmp + the platform scratch dir.
    for root in _tmp_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return True

    # (d) live ACP session cwd (dynamic).
    if session_cwd is not None:
        try:
            cwd = os.path.realpath(os.path.expanduser(str(session_cwd)))
        except (OSError, ValueError):
            cwd = None
        if cwd is not None and (resolved == cwd or resolved.startswith(cwd + os.sep)):
            return True

    return False  # no source matched → hard reject


def write_refused_message(path: object) -> str:
    """The agent-facing refusal for an out-of-workspace write — never a dead end.
    Names the in-conversation recovery path (the add_write_workspace tool, gated
    by the sovereignty prompt) plus the always-allowed fallbacks, so the agent
    can offer the next move conversationally and retry after approval."""
    parent = os.path.dirname(os.path.realpath(os.path.expanduser(str(path)))) or str(path)
    return (
        f"Write refused — {path} is outside your declared write workspaces. "
        f"You can call add_write_workspace to propose adding {parent} — the "
        "operator will be asked to approve. Alternatively, you can write to "
        "~/.grove/research/ or /tmp/ instead."
    )


def append_write_workspace(new_path: object, grove_home: object = None) -> str:
    """Append an absolute directory root to ``write_workspaces.yaml``
    (comment-preserving via ruamel) and invalidate the cached manifest so the
    next :func:`is_write_allowed` sees it immediately.

    This is the apply-step of the workspace-grant flow: it runs only AFTER the
    operator approves the grant through the existing yellow-zone pipeline. Return
    the realpath that was granted. Idempotent: re-granting an existing root is a
    no-op write-wise but still hot-reloads the cache."""
    from hermes_constants import get_hermes_home
    from ruamel.yaml import YAML

    grove = os.path.realpath(
        str(grove_home) if grove_home is not None else get_hermes_home()
    )
    ws_path = os.path.join(grove, _WRITE_WORKSPACES_MANIFEST)
    abs_path = os.path.realpath(os.path.expanduser(str(new_path)))

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    if os.path.exists(ws_path):
        with open(ws_path, encoding="utf-8") as fh:
            data = yaml_rt.load(fh) or {}
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{ws_path} is not a mapping — refusing to rewrite it")
    if data.get("write_workspaces") is None:
        data["write_workspaces"] = []
    existing = {
        str(e.get("path", ""))
        for e in data["write_workspaces"]
        if isinstance(e, dict)
    }
    if abs_path not in existing:
        data["write_workspaces"].append({"path": abs_path})
        with open(ws_path, "w", encoding="utf-8") as fh:
            yaml_rt.dump(data, fh)
    _write_workspaces_cache.pop(grove, None)  # hot-reload on next check
    return abs_path
