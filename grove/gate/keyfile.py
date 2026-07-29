"""Operator DEVICE key store for the Confirmation Gate (P2).

The signing key lives on the OPERATOR's machine, never the gateway. It is a 0600
JSON bundle under ``~/.grove/gate/secrets/<kid>.json`` — covered by the
``is_secret_path`` ``secrets`` dir-anchor (grove/utils/fs_utils.py:521), so even
on a machine that also runs a Grove instance the autonomous loop cannot read it.

Layering: imports :mod:`grove.gate.constants` only (+ stdlib + cryptography).
Nothing from ``tools/`` or ``grove/api``. The private key NEVER enters a return
value except as the on-disk path — callers print the path, never the material.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from grove.gate import constants


class GateKeyError(RuntimeError):
    """A device key operation failed — missing/ambiguous key, unsafe perms, or a
    duplicate mint. Governed loud fault; the private key is never in the message."""


def keys_dir() -> Path:
    """``~/.grove/gate/secrets`` — is_secret_path-covered (the ``secrets`` dir
    anchor), resolved live so a redirected GROVE_HOME (tests) is honored."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "gate" / "secrets"


def derive_kid(public_key) -> str:
    """kid = a stable prefix of the canonical SPKI fingerprint (the shared
    :func:`grove.gate.constants.spki_fingerprint`). Same key → same kid."""
    return "gk-" + constants.spki_fingerprint(public_key)[:16]


def mint(operator_identity: str, *, directory: Optional[Path] = None) -> dict:
    """Generate a P-256 keypair on THIS machine and write the private bundle 0600
    with ``O_EXCL`` (never overwrite — a second mint is a NEW kid, new file).

    Returns ``{kid, public_key_pem, operator_identity, path}``. The private key
    is written to disk only; it is never placed in the returned dict."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if not operator_identity or not str(operator_identity).strip():
        raise GateKeyError("operator identity is required to mint a key.")
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    kid = derive_kid(pub)

    d = Path(directory) if directory is not None else keys_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kid}.json"
    bundle = {
        "kid": kid,
        "operator_identity": str(operator_identity).strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "private_key_pem": priv_pem,
        "public_key_pem": pub_pem,
    }
    payload = json.dumps(bundle, indent=2).encode("utf-8")
    # O_EXCL — refuse to overwrite an existing key; 0600 from creation (never a
    # world/group-readable window).
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise GateKeyError(
            f"a key already exists at {path} — minting will not overwrite it; a "
            "new key is a new kid. Remove it deliberately if you must re-mint."
        ) from exc
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return {
        "kid": kid,
        "public_key_pem": pub_pem,
        "operator_identity": bundle["operator_identity"],
        "path": str(path),
    }


def _assert_key_perms(path: Path) -> None:
    """StrictModes for the private key — refuse if ANY group/world bit is set
    (must be 0600). The signer will not touch a key others can read or write."""
    mode = os.stat(path).st_mode
    if mode & 0o077:
        raise GateKeyError(
            f"key file {path} has unsafe perms {oct(mode & 0o777)} — it must be "
            "0600 (no group/world access). Refusing to sign. `chmod 600` it."
        )


def load_signing_key(kid: Optional[str] = None, *, directory: Optional[Path] = None):
    """Load a device bundle → ``(private_key_obj, kid, operator_identity)``.

    ``kid=None`` uses the sole key when exactly one exists (ambiguous → refuse).
    StrictModes-for-key is enforced BEFORE the private key bytes are read."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    d = Path(directory) if directory is not None else keys_dir()
    if kid is not None:
        path = d / f"{kid}.json"
        if not path.exists():
            raise GateKeyError(f"no device key {kid!r} at {path}.")
    else:
        candidates = sorted(d.glob("*.json")) if d.exists() else []
        if not candidates:
            raise GateKeyError(
                f"no device keys under {d} — run `autonomaton gate keys mint` first."
            )
        if len(candidates) > 1:
            raise GateKeyError(
                f"multiple device keys under {d}; pass --kid to choose one of: "
                + ", ".join(p.stem for p in candidates)
            )
        path = candidates[0]

    _assert_key_perms(path)
    bundle = json.loads(path.read_text(encoding="utf-8"))
    priv = load_pem_private_key(
        bundle["private_key_pem"].encode("utf-8"), password=None
    )
    return priv, bundle["kid"], bundle["operator_identity"]
