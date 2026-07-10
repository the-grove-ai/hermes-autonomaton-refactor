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

import filecmp
import fnmatch
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

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
    "is_capability_write_allowed",
    "capability_emission_precondition",
    "storage_transfer",
    "canonicalize_files",
    "promote_artifact",
    "purge_artifacts",
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


# ── structural-review-gate-v1 — per-capability write governance ───────────────
#
# Two additive gates that sit ABOVE the is_write_allowed / is_secret_path stack.
# They constrain ONLY paths a fleet capability record claims (via its
# governance.write_zone); every other path falls through untouched — a verb-kind
# or non-fleet write is never affected. Governance is resolved by MATCHING the
# write target against the loaded fleet records (no active-capability turn state):
# the artifact's declared destination selects its governing record, so the
# guarantee lives in the path the model writes to, not in prompt adherence.
#
# The `fleet_governance` argument both gates take is an iterable of
# ``(capability_id, governance_dict)`` — the dispatcher builds it from every
# kind=skill record whose governance block is non-None. Keeping the loading in
# the caller keeps these functions pure and unit-testable.


def _grove_subdir_realpath(rel: str, grove: str) -> str:
    """Resolve a write_zone dir declared relative to ``$GROVE_HOME`` to an
    absolute realpath. ``realpath`` normalizes even a not-yet-created dir (it
    resolves the existing prefix and appends the rest), so a staging/canonical
    dir that does not exist on disk yet still matches correctly."""
    return os.path.realpath(os.path.join(grove, rel))


def _path_within(child: str, parent: str) -> bool:
    """Component-boundary-safe containment: ``child`` IS ``parent`` or lives
    under ``parent/`` — so ``drafter`` never matches ``drafter_backup``."""
    return child == parent or child.startswith(parent + os.sep)


def _grove_home_realpath() -> str | None:
    from hermes_constants import get_hermes_home

    try:
        return os.path.realpath(get_hermes_home())
    except (OSError, ValueError):
        return None


def _staging_owner(resolved: str, fleet_governance, grove: str):
    """The single fleet record whose ``write_zone.staging_dir`` contains
    *resolved*, as ``(capability_id, governance_dict)``, or ``None``.

    Staging dirs are disjoint across the fleet (scout / researcher /
    drafter/pending_review / cultivator/pending_review), so at most one owns a
    given target."""
    for cid, gov in fleet_governance:
        wz = (gov or {}).get("write_zone") or {}
        sd = wz.get("staging_dir")
        if not sd:
            continue
        if _path_within(resolved, _grove_subdir_realpath(sd, grove)):
            return (cid, gov)
    return None


def is_capability_write_allowed(target_path: str, fleet_governance) -> "tuple[bool, str]":
    """WHERE gate — per-capability write-zone confinement (structural-review-gate-v1).

    Returns ``(True, "")`` when the write is permitted and ``(False, reason)``
    when it must be refused. A target that lands in NO fleet capability's
    governed zone passes through ``(True, "")`` — this gate is purely additive.

    Refusal cases (the WHERE failure this sprint closes):
      * **canonical-sink write** — the target is inside a record's canonical
        umbrella but NOT inside its staging dir (e.g. a draft written straight to
        ``~/.grove/drafter/`` instead of ``~/.grove/drafter/pending_review/``).
      * **cross-capability write** — the target is inside one record's staging
        dir but ALSO inside a *different* record's governed zone.

    Fail-closed on an unresolvable target; inert (allow) only when GROVE_HOME
    itself cannot be resolved (a grove-relative gate cannot apply, and the base
    is_write_allowed stack has already run)."""
    resolved = _canonical_write_target(target_path)
    if resolved is None:
        return (
            False,
            f"Capability write gate: refusing {target_path} — the target path "
            f"could not be canonicalized (fail-closed).",
        )
    grove = _grove_home_realpath()
    if grove is None:
        return (True, "")  # cannot anchor a grove-relative gate → does not apply

    zone_hits: list[str] = []  # capability_ids whose zone (canonical umbrella OR staging) contains the target
    for cid, gov in fleet_governance:
        wz = (gov or {}).get("write_zone") or {}
        sd, cd = wz.get("staging_dir"), wz.get("canonical_dir")
        if not sd or not cd:
            continue
        staging_abs = _grove_subdir_realpath(sd, grove)
        canonical_abs = _grove_subdir_realpath(cd, grove)
        if _path_within(resolved, canonical_abs) or _path_within(resolved, staging_abs):
            zone_hits.append(cid)

    if not zone_hits:
        return (True, "")  # non-fleet path — additive gate does not constrain it

    owner = _staging_owner(resolved, fleet_governance, grove)
    if owner is None:
        # In a governed umbrella but in no staging dir → written to the canonical
        # sink instead of the staging dir. This is the WHERE failure, structural.
        return (
            False,
            f"Write refused — {target_path} lands in the canonical sink governed "
            f"by capability {zone_hits[0]!r}. Fleet artifacts must be written to "
            f"that capability's staging dir, never its canonical sink; the "
            f"promotion into the canonical dir is a separate operator-gated step.",
        )

    owner_cid = owner[0]
    foreign = [cid for cid in zone_hits if cid != owner_cid]
    if foreign:
        return (
            False,
            f"Write refused — {target_path} is inside {owner_cid!r}'s staging dir "
            f"but also inside {foreign[0]!r}'s governed zone. A capability may "
            f"only write into its own staging dir.",
        )
    return (True, "")


def capability_emission_precondition(
    target_path: str, fleet_governance, turn_tool_counts: dict
) -> "tuple[bool, str]":
    """WHETHER gate — emission precondition on a capability's terminal artifact
    (structural-review-gate-v1).

    Returns ``(True, "")`` unless *target_path* IS the terminal artifact of the
    fleet record that governs its staging dir AND the turn's tool-class counts
    fall below that record's declared minimums — in which case ``(False, reason)``.

    Skipped (``True``) for: a non-fleet target, a governing record with no
    ``emission_preconditions``/``terminal_artifact.path_pattern``, and any
    non-terminal write inside the staging dir (basename does not match the
    pattern). Fail-closed on an unresolvable target.

    *turn_tool_counts* is a ``{class: count}`` dict the caller derives from the
    turn's invocation ledger via :func:`grove.tool_classes.count_tool_classes`."""
    resolved = _canonical_write_target(target_path)
    if resolved is None:
        return (
            False,
            f"Capability emission gate: refusing {target_path} — the target path "
            f"could not be canonicalized (fail-closed).",
        )
    grove = _grove_home_realpath()
    if grove is None:
        return (True, "")

    owner = _staging_owner(resolved, fleet_governance, grove)
    if owner is None:
        return (True, "")  # non-fleet write — no precondition to enforce

    cid, gov = owner
    pre = (gov or {}).get("emission_preconditions") or {}
    ta = pre.get("terminal_artifact") or {}
    pattern = ta.get("path_pattern")
    if not pattern:
        return (True, "")  # this capability declares no terminal artifact
    if not fnmatch.fnmatch(os.path.basename(resolved), str(pattern)):
        return (True, "")  # a non-terminal write within staging — not gated

    # This IS the governing capability's terminal artifact: enforce that the
    # required tool work actually happened this turn (the WHETHER failure — a
    # hollow artifact emitted without the tool calls it presupposes).
    shortfalls = []
    for req in pre.get("required_tool_classes", []) or []:
        cls = req.get("class")
        need = int(req.get("min_calls", 0) or 0)
        have = int(turn_tool_counts.get(cls, 0))
        if have < need:
            shortfalls.append((cls, have, need))
    if shortfalls:
        detail = "; ".join(f"{cls} {have}/{need}" for cls, have, need in shortfalls)
        return (
            False,
            f"Write refused — {os.path.basename(resolved)} is capability "
            f"{cid!r}'s terminal artifact, but the required tool activity for "
            f"this turn is incomplete ({detail}; have/need by tool class). Do the "
            f"outstanding work (searches / skill loads) before emitting the "
            f"artifact — an artifact written without it would be hollow.",
        )
    return (True, "")


def storage_transfer(files: Iterable[Path], dest_dir: Path) -> "list[str]":
    """THE lifecycle storage chokepoint (promoted-artifact-persistence-v1 P5,
    storage-seam constraint) — every lifecycle destination op routes here.

    CONTRACT: each file transfer completes atomically or fails loud. POSIX
    ``rename`` within the one ``~/.grove`` mount is TODAY'S IMPLEMENTATION,
    not the contract — a future remote backend (S3 etc.) becomes a
    config/declaration swap implementing this one function, never a rework of
    its callers. Destinations are always parameters; this function contains
    zero producer names and zero content knowledge (test-pinned).

    Moves *files* into *dest_dir* under their own basenames, creating the dir
    if absent. Returns the destination paths (moved or skip-verified), in
    input order.

    Per-file idempotency (P1 ruling 3, exactly — the same semantics serve a
    re-tapped promote AND a re-tapped purge):
      * source gone + same-name destination present → SATISFIED (the transfer
        already happened — a re-tap after a downstream failure);
      * source present + same-name destination byte-IDENTICAL → SKIP (source
        is left untouched; the destination state is already achieved);
      * source present + same-name destination DIVERGENT → last-write-wins
        overwrite via the rename (the pre-P1 ``os.rename`` semantic — no new
        collision Andon);
      * source gone + no destination → ``FileNotFoundError``, loud (a
        transfer that cannot be satisfied aborts the operation — never a
        silent partial).

    Callers own selection (e.g. the ``meta.json`` exclusion) and validation
    (staging/canonical membership, WHERE-gate discipline). The promote entry
    points — :func:`promote_artifact`, the portal's ``_fleet_promote_core``
    and ``_canonicalize_staged_package`` — and :func:`purge_artifacts` all
    delegate the destination act here; there is no second copy."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out: "list[str]" = []
    for f in files:
        src = Path(f)
        target = dest / src.name
        if not src.is_file():
            if target.is_file():
                out.append(str(target))  # satisfied — already transferred
                continue
            raise FileNotFoundError(
                f"storage_transfer: source {src} is gone and no destination "
                f"copy exists at {target} — the transfer cannot be satisfied"
            )
        if target.is_file() and (
            os.path.samefile(src, target)
            or filecmp.cmp(src, target, shallow=False)
        ):
            out.append(str(target))  # skip — byte-identical (or self-rename)
            continue
        src.rename(target)  # atomic within the one ~/.grove mount
        out.append(str(target))
    return out


# The P1 name, kept as a true alias of the chokepoint (promote-side callers
# and every shipped pin — producer-blind, rename-only — read the SAME body).
# ``inspect.getsource(canonicalize_files)`` resolves to storage_transfer's
# definition, so the P1-P4 pins hold unmodified.
canonicalize_files = storage_transfer


def promote_artifact(source_path: str, governance: dict) -> str:
    """Deterministic promotion of an approved artifact from a capability's
    staging dir to its canonical sink (structural-review-gate-v1).

    ORCHESTRATOR-ONLY — this is NOT a tool and is unreachable by the model. The
    model's terminal act is writing into the staging dir (gated by
    :func:`is_capability_write_allowed`); moving the approved artifact into the
    canonical sink, where the cellar poller ingests it, is the ENVIRONMENT's job,
    performed only AFTER operator approval. That the model has no path to this
    function is the structural guarantee — approval cannot be self-served.

    CORE, not surface: the signature is surface-agnostic (``source_path`` +
    ``governance`` only — no session, platform, or surface identifier). Every
    approval surface (Telegram button, CLI prompt, portal card) supplies its own
    UX shim but calls THIS one function — the single door from staging to
    canonical.

    Behavior:
      * Validates *source_path* resolves strictly INSIDE
        ``governance.write_zone.staging_dir`` (relative to ``$GROVE_HOME``,
        realpath-resolved, component-boundary safe — same discipline as the
        WHERE gate).
      * Creates ``canonical_dir`` if absent (the poller expects it), then
        atomically ``os.rename``s the file under its own basename into the
        canonical sink. ``~/.grove`` is one mount, so the rename is atomic.
      * If ``write_zone.promotion`` is ``auto_ingest``, immediately funnels the
        canonical path through :func:`grove.wiki.watcher.ingest_file` (the
        universal idempotency gatekeeper). For ``operator_approval`` the move
        ITSELF is the approval effect — the 60s poller picks it up next cycle,
        so no inline ingest is triggered.

    Returns the canonical path. The atomic move is the primary guarantee; the
    inline ingest is a best-effort immediate trigger with the poller as backstop,
    so an ingest failure is logged LOUDLY but does not unwind a completed move
    (re-raising would misreport a successful promotion and invite a retry that
    then fails on the already-moved source).

    Raises ``ValueError`` when: governance declares no ``write_zone`` /
    ``staging_dir`` / ``canonical_dir``; ``$GROVE_HOME`` or the source cannot be
    resolved; or the source does not resolve strictly inside the staging dir
    (never promotes an out-of-staging path)."""
    wz = (governance or {}).get("write_zone") or {}
    staging = wz.get("staging_dir")
    canonical = wz.get("canonical_dir")
    if not staging or not canonical:
        raise ValueError(
            "promote_artifact: governance.write_zone must declare both "
            "staging_dir and canonical_dir"
        )

    grove = _grove_home_realpath()
    if grove is None:
        raise ValueError("promote_artifact: could not resolve GROVE_HOME")

    try:
        source_real = os.path.realpath(os.path.expanduser(str(source_path)))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"promote_artifact: source path {source_path!r} could not be "
            f"resolved ({exc})"
        ) from exc

    staging_abs = _grove_subdir_realpath(staging, grove)
    # STRICTLY inside — the staging dir itself is not a promotable artifact, and
    # the trailing os.sep keeps the boundary component-safe (staging never
    # matches staging_evil).
    if source_real == staging_abs or not source_real.startswith(staging_abs + os.sep):
        raise ValueError(
            f"promote_artifact: source {source_path!r} does not resolve inside "
            f"the declared staging dir {staging!r} ({staging_abs}) — refusing to "
            f"promote"
        )

    # The canonical act delegates to the ONE shared implementation
    # (promoted-artifact-persistence-v1 P1, GATE-B ruling 1) — mkdir + atomic
    # rename under the source's own basename, skip-if-identical.
    canonical_abs = _grove_subdir_realpath(canonical, grove)
    target = canonicalize_files([Path(source_real)], Path(canonical_abs))[0]

    # auto_ingest — trigger the universal ingest gate immediately. operator_
    # approval capabilities skip this: the move is the approval effect and the
    # poller ingests on its next cycle. The move already succeeded, so an ingest
    # fault is logged loud (never swallowed) but does not unwind the promotion.
    if wz.get("promotion") == "auto_ingest":
        try:
            from grove.wiki.watcher import ingest_file

            ingest_file(target)
        except Exception as exc:  # noqa: BLE001 — surfaced loud; poller is the backstop
            logger.warning(
                "promote_artifact: auto_ingest of %s FAILED (%r) — the move "
                "succeeded; the cellar poller will re-ingest on its next cycle.",
                target, exc,
            )
    return target


def purge_artifacts(
    source_paths: "Iterable[str]",
    governance: dict,
    *,
    unit: str,
    reason: str,
    initiated_by: str,
    effect_signature: "str | None" = None,
    now: "object | None" = None,
) -> dict:
    """Operator-initiated purge of canonical artifacts — archive-first
    (promoted-artifact-persistence-v1 P5, the reverse door of
    :func:`promote_artifact`).

    ORCHESTRATOR-ONLY like promote: never a silent expiry, never model-
    self-served — the RED verb's approval ceremony (grant token / sovereign
    prompt / pending-store confirm) is the only path here. Semantic
    revocation of operator approval: the bytes SURVIVE in the archive; they
    leave the canonical (ambient) plane.

    Validation — the INVERSE of promote's staging discipline, same
    component-boundary realpath rules (never purges an out-of-canonical
    path):
      * every source must resolve STRICTLY inside
        ``governance.write_zone.canonical_dir``;
      * a source inside ``pending_review`` is REFUSED (staged work is
        rejected/revised through the disposition flow, never purged);
      * a source inside the declared archive dir is REFUSED (already
        archived; purge is not archive management).

    Mechanics:
      * a DIR source contributes its regular files (the P1 package layout is
        flat); a FILE source contributes itself;
      * moves route through :func:`storage_transfer` (THE chokepoint) into
        ``<canonical_dir>/<archive_dir>/<unit>-<utc-ts>/`` — the shipped
        P1 archive naming convention (actions.py's reject-archive helper),
        canonized;
      * ``archive_dir`` comes from the S2 ``write_zone.retention``
        declaration, defaulting to ``".archive"`` when absent
        (persist-by-default: absence of the block never blocks a purge, it
        only means the default destination);
      * MOVES-THEN-MANIFEST: ``purge-manifest.json`` (what / when / by whom /
        reason / effect signature) is written atomically (tmp + replace)
        AFTER the moves — a crash in between leaves the files findable but
        unmanifested, and a RE-TAP resumes the newest manifest-less archive
        dir for this unit (storage_transfer's source-gone semantics complete
        the moves) and completes the manifest. Idempotent end to end.

    PRODUCER-BLIND (generality pin extended): zero producer names; the sink,
    archive destination, and unit identity are all parameters or declared
    data. Returns ``{"archive_dir", "files", "manifest", "resumed"}``."""
    from datetime import datetime, timezone

    wz = (governance or {}).get("write_zone") or {}
    canonical = wz.get("canonical_dir")
    if not canonical:
        raise ValueError(
            "purge_artifacts: governance.write_zone must declare canonical_dir"
        )
    archive_rel = ((wz.get("retention") or {}).get("archive_dir")
                   or ".archive")
    if not unit or not str(unit).strip():
        raise ValueError("purge_artifacts: unit must be non-empty")
    # basename-guard (the feedback_store discipline): a crafted unit can never
    # escape the archive root or glob-inject the resume scan.
    unit_safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(unit)).strip(".") or "unit"

    grove = _grove_home_realpath()
    if grove is None:
        raise ValueError("purge_artifacts: could not resolve GROVE_HOME")
    canonical_abs = _grove_subdir_realpath(canonical, grove)
    archive_root = Path(canonical_abs) / archive_rel

    # ── validation: inverse-promote containment, every source ────────────────
    resolved: "list[Path]" = []
    for sp in source_paths:
        try:
            real = os.path.realpath(os.path.expanduser(str(sp)))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"purge_artifacts: source {sp!r} could not be resolved ({exc})"
            ) from exc
        if real == canonical_abs or not real.startswith(canonical_abs + os.sep):
            raise ValueError(
                f"purge_artifacts: source {sp!r} does not resolve inside the "
                f"declared canonical dir {canonical!r} ({canonical_abs}) — "
                f"refusing to purge"
            )
        rel_parts = Path(real).relative_to(canonical_abs).parts
        if "pending_review" in rel_parts[:-1] or rel_parts[0] == "pending_review":
            raise ValueError(
                f"purge_artifacts: source {sp!r} is STAGED (pending_review) — "
                f"staged work is rejected or revised through the disposition "
                f"flow, never purged"
            )
        if rel_parts[0] == archive_rel:
            raise ValueError(
                f"purge_artifacts: source {sp!r} is already inside the archive "
                f"dir {archive_rel!r} — purge is not archive management"
            )
        resolved.append(Path(real))

    # ── expand: dirs contribute their regular files (flat P1 package layout) ─
    files: "list[Path]" = []
    for p in resolved:
        if p.is_dir():
            files.extend(f for f in sorted(p.iterdir()) if f.is_file())
        else:
            files.append(p)  # existing file, or gone (re-tap) — transfer decides

    # ── destination: resume the newest manifest-less archive dir (re-tap),
    #    else a fresh <unit>-<utc-ts>/. Resume detection precedes the
    #    nothing-to-purge guard: an interrupted purge leaves the canonical
    #    sources GONE — the file identities live in the incomplete archive. ──
    ts_src = now or datetime.now(timezone.utc)
    resumed = False
    dest = None
    if archive_root.is_dir():
        # RESUME DISCRIMINATOR (P5-S4.3): an interrupted PURGE dir never
        # contains meta.json (the identity envelope never reaches canonical),
        # while promote/reject archive residue always does — a manifest-less
        # dir WITH meta.json is residue, never resumed (the merchants bake
        # coalesced into the promote-era meta dir before this pin).
        incomplete = [
            d for d in sorted(archive_root.glob(f"{unit_safe}-*"))
            if d.is_dir() and not (d / "purge-manifest.json").is_file()
            and not (d / "meta.json").is_file()
        ]
        if incomplete:
            dest = incomplete[-1]
            resumed = True
    if not files and not resumed:
        raise ValueError(
            f"purge_artifacts: nothing to purge for unit {unit!r} — no files "
            f"under the given sources and no interrupted purge to complete"
        )
    if dest is None:
        dest = archive_root / f"{unit_safe}-{ts_src.strftime('%Y%m%dT%H%M%SZ')}"

    archived = storage_transfer(files, dest) if files else []
    if resumed:
        # Fold in the files the interrupted attempt already archived, so the
        # manifest records the COMPLETE set (idempotent union, sorted).
        already = {
            str(f) for f in dest.iterdir()
            if f.is_file() and f.name != "purge-manifest.json"
            and not f.name.endswith(".tmp")
        }
        archived = sorted(set(archived) | already)

    # ── manifest, atomically, AFTER the moves ────────────────────────────────
    manifest = {
        "unit": str(unit),
        "purged_at": ts_src.isoformat(),
        "initiated_by": initiated_by,
        "reason": reason,
        "sources": [str(p) for p in resolved],
        "archived_files": archived,
        "effect_signature": effect_signature,
        "resumed": resumed,
    }
    manifest_path = dest / "purge-manifest.json"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, manifest_path)

    return {"archive_dir": str(dest), "files": archived,
            "manifest": str(manifest_path), "resumed": resumed}
