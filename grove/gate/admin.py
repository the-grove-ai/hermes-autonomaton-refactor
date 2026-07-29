"""Operator-shell registry ADMIN for the Confirmation Gate (P2).

Runs on the VM in the OPERATOR's IAM shell (never the gateway process). Writes
``/etc/grove/gate/authorized_keys.yaml`` via tmp + rename. TWO walls guard it:

  1. refuse-if-service-user — a process invoked AS the gateway service account is
     refused here, so a compromised loop (even sudo-capable) cannot self-add a
     key. This is a belt.
  2. the filesystem — ``/etc`` is root-owned; the write needs the operator's
     root. This is the hard control.

Not reachable from the gateway process: nothing registers these on any tool or
route registry. The registry SCHEMA is the P1 loader's — imported, not
re-declared (one schema source). Reads use raw YAML, NOT ``load_registry``: the
verifier's StrictModes + sudo-capable refuse would trip a root operator shell,
which is exactly who is allowed to write here.

Layering: imports :mod:`grove.gate.registry` (schema + path) only.
"""
from __future__ import annotations

import getpass
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from grove.gate.registry import _REGISTRY_PATH, _REQUIRED_FIELDS

# not_after horizon — GCP service-account key rotation convention (90 days).
# Renewal is a re-add (mint stays; a fresh entry extends the horizon).
_NOT_AFTER_DAYS = 90


class GateAdminError(RuntimeError):
    """A registry admin op was refused — service-user invocation, duplicate kid,
    non-P-256 / unparseable PEM, schema mismatch, or a filesystem wall."""


def _service_username() -> str:
    """Best-effort service-account name: the gateway unit's ``User=`` if
    resolvable, else the known Grove service account ``hermes``. Used ONLY to
    refuse self-add; the filesystem wall is the hard control (so a stale/blank
    answer here never widens who can actually write /etc)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "hermes-gateway", "-p", "User", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        user = (out.stdout or "").strip()
        if user:
            return user
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return "hermes"


def _assert_not_service_user() -> None:
    invoker = os.environ.get("SUDO_USER") or getpass.getuser()
    service = _service_username()
    if invoker == service:
        raise GateAdminError(
            f"refused: `gate keys` was invoked as the service user ({service!r}). "
            "Registry administration is operator-only — run it in your own IAM "
            "shell (sudo as yourself), never as the gateway account."
        )


def _load_raw_entries(path: Path) -> list:
    """Raw YAML read of ``authorized_keys`` (admin path — NOT load_registry).

    ABSENT vs PRESENT is load-bearing (a tmp+rename over a registry we could not
    parse would silently destroy every prior registration):

      * absent file            -> ``[]`` (legitimate first registration),
      * present but unparseable -> REFUSE LOUD, zero writes,
      * present but wrong shape (not a mapping, no ``authorized_keys``, or
        ``authorized_keys`` not a list) -> REFUSE LOUD, zero writes,
      * present but EMPTY (0 bytes / ``null``) -> REFUSE LOUD — a truncated
        registry is indistinguishable from an intentionally-empty one, so we
        never read it as empty and overwrite; remove it by hand for a fresh
        first registration.

    A present, well-formed registry with ``authorized_keys: []`` reads as the
    empty list (a deliberately-empty registry is fine)."""
    import yaml

    if not path.exists():
        return []  # absent — legitimate first registration
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateAdminError(
            f"refuse: {path} is present but not parseable YAML ({exc}). Refusing "
            "to write — a tmp+rename over an unparseable registry would destroy "
            "every prior registration. Fix or remove the file by hand."
        ) from exc
    if doc is None:
        raise GateAdminError(
            f"refuse: {path} is present but empty. Refusing to write over a "
            "possibly-truncated registry — remove it deliberately for a fresh "
            "first registration."
        )
    if not isinstance(doc, dict) or "authorized_keys" not in doc:
        raise GateAdminError(
            f"refuse: {path} is present but not a valid registry (expected a "
            "mapping with an 'authorized_keys' list). Refusing to write — a "
            "malformed registry is never read as empty."
        )
    entries = doc["authorized_keys"]
    if not isinstance(entries, list):
        raise GateAdminError(f"refuse: {path} 'authorized_keys' is not a list.")
    return entries


def _atomic_write(path: Path, entries: list) -> None:
    """tmp + rename write. Creates parent dirs (0755, root-owned when the
    operator runs this as root) if absent."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump({"authorized_keys": entries}, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def add_key(
    kid: str,
    public_key_pem: str,
    operator_identity: str,
    *,
    registry_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Register a public key (operator shell). Validates: PEM parses as P-256,
    kid unique, exactly the P1 loader's required fields (strict, no defaults);
    ``not_after = registered_at + 90d``. tmp + rename write."""
    _assert_not_service_user()
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        pub = load_pem_public_key(public_key_pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise GateAdminError(f"public_key_pem does not parse: {exc}") from exc
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(
        pub.curve, ec.SECP256R1
    ):
        raise GateAdminError(
            "public key is not P-256 (SECP256R1) — ES256 requires P-256."
        )

    registered_at = now or datetime.now(timezone.utc)
    not_after = registered_at + timedelta(days=_NOT_AFTER_DAYS)
    entry = {
        "kid": str(kid),
        "public_key_pem": public_key_pem,
        "operator_identity": str(operator_identity),
        "registered_at": registered_at.isoformat(),
        "not_after": not_after.isoformat(),
    }
    # Strict schema mirror of the P1 loader (one schema source).
    missing = _REQUIRED_FIELDS - set(entry)
    unknown = set(entry) - _REQUIRED_FIELDS
    if missing or unknown:
        raise GateAdminError(
            f"schema mismatch (missing={sorted(missing)}, unknown={sorted(unknown)})."
        )

    entries = _load_raw_entries(path)
    if any(isinstance(e, dict) and e.get("kid") == entry["kid"] for e in entries):
        raise GateAdminError(
            f"kid {entry['kid']!r} is already registered — kids are unique; "
            "mint a new key for a new device."
        )
    entries.append(entry)
    _atomic_write(path, entries)
    return entry


def revoke_key(kid: str, *, registry_path: Optional[Path] = None) -> int:
    """Remove the entry for ``kid`` (operator shell). The removal IS the record —
    no ledger hook: the gateway ledger lives under the service account's
    ``~/.grove``, which a root operator write would pollute. Returns the count
    removed (raises if none matched)."""
    _assert_not_service_user()
    path = Path(registry_path) if registry_path is not None else _REGISTRY_PATH
    entries = _load_raw_entries(path)
    kept = [
        e for e in entries
        if not (isinstance(e, dict) and e.get("kid") == kid)
    ]
    if len(kept) == len(entries):
        raise GateAdminError(f"no registered key with kid {kid!r}.")
    _atomic_write(path, kept)
    return len(entries) - len(kept)
