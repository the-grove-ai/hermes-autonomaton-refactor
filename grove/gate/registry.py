"""Confirmation Gate trust anchor — the authorized operator signing-key registry.

confirmation-gate-grant-token-v1 P1 (F7). The Gate authorizes an inline RED apply
ONLY on an ES256 grant token signed by an operator key registered HERE. This
module is the LOADER/VERIFIER side; the registry FILE is provisioned out of band
(the IAM channel; registration verbs are P2), NEVER by this process — P1 ships no
writer.

WHY ``/etc``, not ``~/.grove``:
  A trust anchor must live OUTSIDE the service user's writable universe — the
  sshd ``authorized_keys`` / ``sudoers`` precedent. ``~/.grove`` is disqualified:
  the service user OWNS that directory, so file ownership alone cannot stop an
  unlink+recreate — the loop could delete the anchor and write its own. ``/etc``
  is root-owned (verified live: ``/etc`` is ``root:root 0755``, and the service
  user ``hermes`` cannot write it), so the anchor sits where the loop provably
  cannot reach it with the filesystem.

WHY the path is a CODE CONSTANT, not config:
  A configurable trust-anchor path lets the loop point the verifier at a file it
  owns — the whole anchor would be forgeable. The path is therefore a
  module-level constant, config-blind BY DESIGN (the ``_FLEET_FLOOR`` precedent
  in ``grove.dispatcher``). Nothing reads it from config. Tests override it by
  monkeypatching :data:`_REGISTRY_PATH` directly — never through config.

StrictModes (sshd ``StrictModes yes`` precedent):
  At EVERY load the anchor is refused if the file OR ANY PARENT DIRECTORY is
  writable by the service user, the service group, or world. A writable parent
  means unlink+recreate; a writable file means direct tamper. Either → the
  verifier refuses to operate, LOUDLY (:class:`RegistryError`). This is a
  filesystem check on the running process's own identity — it is the CONTROL,
  not the shell-classifier RED route (that is defense-in-depth, F7c).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

# ── the trust-anchor path — a CODE CONSTANT, config-blind by design ──────────
# A configurable path would let the loop aim the verifier at a file it owns.
# Tests monkeypatch THIS attribute; nothing reads it from config (the
# _FLEET_FLOOR precedent). Do not add a config/env override.
_REGISTRY_PATH = Path("/etc/grove/gate/authorized_keys.yaml")


class RegistryError(RuntimeError):
    """The trust-anchor registry is UNSAFE (StrictModes), ABSENT, or MALFORMED —
    a governed loud fault (F7). The verifier refuses to operate; never silently
    defaulted, never degraded to an empty allow-list."""


@dataclass(frozen=True)
class AuthorizedKey:
    """One registered operator signing key. Every field is REQUIRED — there is
    no getattr/dict.get default anywhere in the parse (silent-default is the
    banned class). ``not_after`` is enforced at verification time, not load."""

    kid: str
    public_key_pem: str
    operator_identity: str
    registered_at: str
    not_after: str


# The exact, CLOSED field set for one registry entry. STRICT parse: an entry
# missing any field OR carrying an unknown field is a loud refuse.
_REQUIRED_FIELDS = frozenset(
    {"kid", "public_key_pem", "operator_identity", "registered_at", "not_after"}
)


def _service_identity() -> tuple[int, set[int]]:
    """The running process's (euid, {egid + supplementary gids}). The verifier
    runs AS the service user in prod, so this IS the untrusted identity the
    StrictModes check must exclude from write access to the anchor."""
    egids = set(os.getgroups())
    egids.add(os.getegid())
    return os.geteuid(), egids


def _component_unsafe(p: Path, euid: int, egids: set[int]) -> Optional[str]:
    """Return a reason string if *p* is writable by the service user/group/world,
    else None. Follows symlinks (``os.stat``): the REAL inode's perms are what a
    writer would exploit.

    ``euid == 0`` (running as root) skips the owner-uid clause — root is the
    trust root that OWNS the anchor, not the untrusted service user; group/world
    write still refuse."""
    try:
        st = os.stat(p)
    except OSError as exc:
        return f"{p} cannot be stat'd ({exc.__class__.__name__}: {exc})"
    mode = st.st_mode
    if mode & stat.S_IWOTH:
        return f"{p} is world-writable (mode {oct(mode & 0o777)})"
    if (mode & stat.S_IWGRP) and st.st_gid in egids:
        return (
            f"{p} is group-writable by the service group "
            f"(gid {st.st_gid}, mode {oct(mode & 0o777)})"
        )
    if euid != 0 and st.st_uid == euid:
        return (
            f"{p} is owned by the service user (uid {euid}) — the owner can "
            f"rewrite or unlink it regardless of mode"
        )
    return None


def _sudo_capable() -> bool:
    """True if the service user can obtain root NON-INTERACTIVELY (``sudo -n
    true`` exits 0). A sudo-capable service user can write ``/etc`` regardless of
    file modes, so the StrictModes wall is a claim without a control.

    A missing ``sudo`` binary, a nonzero exit, a timeout, or any exec failure
    means NOT capable (pass) — the check only refuses on an affirmative,
    non-interactive success."""
    import subprocess

    try:
        proc = subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _assert_registry_secure(path: Path) -> None:
    """StrictModes gate — raise :class:`RegistryError` if the file or ANY parent
    directory up to ``/`` is writable by the service user, service group, or
    world (sshd ``StrictModes yes`` precedent), OR if the service user is
    sudo-capable. This is the CONTROL for F7.

    Detection honesty (A1): the anchor's guarantee is file modes AND privilege
    posture. A verifier running happily under a sudo-capable service user claims
    a wall it does not have — that user can ``sudo`` past root-owned modes. So a
    sudo-capable service user is a loud refuse: approvals stay dead (fail-closed)
    until the sudo grant is removed."""
    resolved = Path(os.path.realpath(path))
    euid, egids = _service_identity()
    # The file itself, then every ancestor directory up to and including '/'.
    for component in (resolved, *resolved.parents):
        reason = _component_unsafe(component, euid, egids)
        if reason is not None:
            raise RegistryError(
                "Confirmation Gate registry refused (StrictModes): "
                f"{reason}. The trust anchor and every parent directory must be "
                "root-owned and unwritable by the service user/group/world "
                "(the /etc/grove/gate provisioning is operator-only, via the "
                "IAM channel). Refusing to verify any grant token."
            )
    # Privilege posture — modes are moot if the service user can sudo past them.
    if _sudo_capable():
        raise RegistryError(
            "Confirmation Gate registry refused (privilege posture): the service "
            "user is sudo-capable (`sudo -n true` succeeds), so it can write "
            "/etc regardless of file modes — the trust anchor is not actually "
            "walled. Remove the service user's sudo grant (deploy gate, "
            "operator-authorized) before the Gate will verify any token. "
            "Fail-closed by design."
        )


def load_registry(path: Optional[Path] = None) -> Dict[str, AuthorizedKey]:
    """Load and STRICT-parse the operator key registry, keyed by ``kid``.

    Order is load-bearing: StrictModes FIRST (never read bytes from an
    unsafe-perm file), then parse. Absence, unsafe perms, malformed YAML, a
    non-list ``authorized_keys``, a missing/unknown field, or a duplicate kid
    are each a loud :class:`RegistryError` — never a silent empty registry.

    ``not_after`` is NOT filtered here: an expired key still loads, and its
    expiry is refused at verification (with the IAM-renewal message), so the
    two failure modes stay distinct.
    """
    reg_path = Path(path) if path is not None else _REGISTRY_PATH

    if not os.path.lexists(reg_path):
        raise RegistryError(
            f"Confirmation Gate registry absent at {reg_path} — no operator "
            "signing keys are provisioned, so no grant token can be verified. "
            "Provision it out of band (operator-only, IAM channel)."
        )

    # StrictModes BEFORE any read — an unsafe-perm anchor is never trusted enough
    # to even parse.
    _assert_registry_secure(reg_path)

    import yaml

    try:
        raw = reg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(
            f"Confirmation Gate registry unreadable at {reg_path} "
            f"({exc.__class__.__name__}: {exc})."
        ) from exc

    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RegistryError(
            f"Confirmation Gate registry malformed YAML at {reg_path}: {exc}."
        ) from exc

    if not isinstance(doc, dict) or "authorized_keys" not in doc:
        raise RegistryError(
            f"Confirmation Gate registry at {reg_path} must be a mapping with an "
            "'authorized_keys' list."
        )
    entries = doc["authorized_keys"]
    if not isinstance(entries, list):
        raise RegistryError(
            f"Confirmation Gate registry 'authorized_keys' at {reg_path} must be "
            f"a list, got {type(entries).__name__}."
        )

    keys: Dict[str, AuthorizedKey] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistryError(
                f"Confirmation Gate registry entry #{i} at {reg_path} must be a "
                f"mapping, got {type(entry).__name__}."
            )
        present = set(entry.keys())
        missing = _REQUIRED_FIELDS - present
        unknown = present - _REQUIRED_FIELDS
        if missing:
            raise RegistryError(
                f"Confirmation Gate registry entry #{i} at {reg_path} is missing "
                f"required field(s): {sorted(missing)}. Strict parse — no field "
                "may be defaulted."
            )
        if unknown:
            raise RegistryError(
                f"Confirmation Gate registry entry #{i} at {reg_path} carries "
                f"unknown field(s): {sorted(unknown)}. Strict parse — an unknown "
                "field is a refuse, not an ignore."
            )
        key = AuthorizedKey(
            kid=str(entry["kid"]),
            public_key_pem=str(entry["public_key_pem"]),
            operator_identity=str(entry["operator_identity"]),
            registered_at=str(entry["registered_at"]),
            not_after=str(entry["not_after"]),
        )
        if key.kid in keys:
            raise RegistryError(
                f"Confirmation Gate registry at {reg_path} has a duplicate kid "
                f"{key.kid!r} — kid must be unique."
            )
        keys[key.kid] = key
    return keys
