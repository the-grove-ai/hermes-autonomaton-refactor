"""Confirmation Gate — ES256 grant-token verification (F3/F6).

Verifies an operator grant token against the root-owned key registry
(:mod:`grove.gate.registry`). A token authorizes ONE inline apply of ONE
scope-defining proposal. Everything here is fail-closed and loud: any structural,
cryptographic, binding, or window failure raises :class:`GrantVerificationError`
with a DISTINCT message — the wire-in surfaces it on the refusal card.

Layering: imports the registry (``grove.gate``) and PyJWT/cryptography only —
nothing from ``tools/`` or ``grove/api``.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import jwt

from grove.gate.registry import AuthorizedKey, RegistryError, load_registry

# ── Gate-fixed token contract (all literals — not config) ────────────────────
# Explicit audience so a token minted for any OTHER Grove surface (or a GCP OIDC
# token, aud=<client-id>) fails structurally, never by accident of reuse.
_AUDIENCE = "grove-confirmation-gate"
# Custom media type in the JWT ``typ`` header — a GCP OIDC token (typ="JWT")
# fails this structural check before any signature math.
_TYP = "grove-grant+jwt"
# The ONLY accepted algorithm, a hardcoded literal (F3): the alg-confusion
# defense is the allow-list itself — an HS256 token forged with the PEM public
# key as the HMAC secret, or an alg=none token, is rejected here, not verified.
_ALGORITHMS = ["ES256"]
# Clock-skew grace for exp/iat (seconds). Small: mint-after-decision means the
# operator's clock and the gateway's are both live and NTP-disciplined; 10s
# absorbs ordinary drift without widening the replay window meaningfully.
_LEEWAY = 10
# SERVER-ENFORCED maximum token lifetime (seconds). This is a SERVER control,
# not a client promise: a token is a transport wrapper around a decision the
# operator JUST made, so it needs only enough life to cross the wire and be
# consumed. exp - iat > 60 is refused even if the signature is valid — a
# long-lived token is a standing bearer credential, which this Gate must not
# accept.
_MAX_WINDOW_SECONDS = 60
# The closed disposition vocabulary. P1 gates the APPROVE verb only (reject /
# dismiss for the six types are unchanged this sprint), so "approve" is the sole
# authorized disposition; any other value is an unknown-disposition refuse.
_ALLOWED_DISPOSITIONS = frozenset({"approve"})
# The required claim set — strict, no defaults. PyJWT ``require`` enforces
# presence; a missing claim is a refuse, never a defaulted value.
_REQUIRED_CLAIMS = ["proposal_id", "disposition", "jti", "iat", "exp"]


class GrantVerificationError(RuntimeError):
    """A grant token failed verification — structural, cryptographic, binding,
    expiry, or window. Governed loud fault; carries a distinct reason."""


@dataclass(frozen=True)
class VerifiedGrant:
    """The verified authority to apply ONE proposal inline. Every field is drawn
    from a fully verified token + its registry key — safe to stamp as provenance."""

    jti: str
    kid: str
    proposal_id: str
    disposition: str
    iat: int
    exp: int
    operator_identity: str
    operator_key_fingerprint: str  # sha256 of the registered public_key_pem


def _key_fingerprint(public_key) -> str:
    """SHA-256 over the DER-encoded SubjectPublicKeyInfo of the registered key —
    the canonical SPKI fingerprint (A3). Keying on the SPKI, not the PEM text,
    makes the fingerprint invariant to PEM whitespace/line-wrap/encoding
    differences: the same key always stamps the same provenance id (R-6)."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _parse_not_after(key: AuthorizedKey) -> datetime:
    try:
        dt = datetime.fromisoformat(key.not_after)
    except (ValueError, TypeError) as exc:
        raise GrantVerificationError(
            f"Registry key {key.kid!r} has a malformed not_after "
            f"({key.not_after!r}): {exc}."
        ) from exc
    # A naive timestamp is treated as UTC (the registry is authored in UTC).
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def verify_grant_token(
    token: str,
    *,
    expected_proposal_id: str,
    registry: Optional[Dict[str, AuthorizedKey]] = None,
) -> VerifiedGrant:
    """Verify *token* authorizes applying *expected_proposal_id*.

    Returns a :class:`VerifiedGrant` on success; raises
    :class:`GrantVerificationError` (distinct message) on ANY failure. Does NOT
    consume the ``jti`` — replay protection is the caller's CONSUME step, which
    runs AFTER this returns and BEFORE the apply (F2 ordering).
    """
    if not token or not isinstance(token, str):
        raise GrantVerificationError("No grant token supplied.")

    # 1) Structural header — typ + kid BEFORE any signature work. A GCP OIDC
    #    token (typ="JWT", no operator kid) fails here.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise GrantVerificationError(f"Malformed token header: {exc}.") from exc
    if header.get("typ") != _TYP:
        raise GrantVerificationError(
            f"Wrong token typ header {header.get('typ')!r} — expected {_TYP!r} "
            "(a general-purpose JWT / OIDC token is refused structurally)."
        )
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise GrantVerificationError("Token header carries no operator kid.")

    # 2) Key lookup by kid against the trust anchor. A registry fault (absent /
    #    StrictModes-unsafe / malformed) propagates as a loud refuse.
    try:
        keys = registry if registry is not None else load_registry()
    except RegistryError as exc:
        raise GrantVerificationError(
            f"Cannot verify — trust anchor unavailable: {exc}"
        ) from exc
    key = keys.get(kid)
    if key is None:
        raise GrantVerificationError(
            f"Unknown kid {kid!r} — no operator key with that id is registered."
        )

    # 3) Key expiry (registry not_after) — distinct from token exp; names the
    #    IAM-channel renewal path.
    now = int(time.time())
    not_after = _parse_not_after(key)
    if datetime.now(timezone.utc) > not_after:
        raise GrantVerificationError(
            f"Operator key {kid!r} expired ({key.not_after}). Register a fresh "
            "key via the IAM channel (operator-only) — expired keys are refused."
        )

    # 4) Load the public key — malformed PEM is a distinct refuse.
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        public_key = load_pem_public_key(key.public_key_pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise GrantVerificationError(
            f"Registry key {kid!r} has a malformed public_key_pem: {exc}."
        ) from exc

    # 5) Signature + registered claims. algorithms is the HARDCODED ES256 literal
    #    (alg-confusion defense). PyJWT verifies signature, aud, iss (== the
    #    registered operator identity for this kid), exp (with leeway), and the
    #    presence of every required claim.
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=_ALGORITHMS,
            audience=_AUDIENCE,
            issuer=key.operator_identity,
            leeway=_LEEWAY,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        # PyJWTError (not just InvalidTokenError) so the alg-confusion refusal —
        # InvalidAlgorithmError, which is NOT under InvalidTokenError — is caught
        # here as a loud governed refuse rather than escaping raw.
        raise GrantVerificationError(
            f"Token rejected ({exc.__class__.__name__}): {exc}."
        ) from exc

    # 6) Binding + disposition + window — server-side, post-signature.
    disposition = claims["disposition"]
    if disposition not in _ALLOWED_DISPOSITIONS:
        raise GrantVerificationError(
            f"Unknown disposition {disposition!r} — the Confirmation Gate "
            f"authorizes only {sorted(_ALLOWED_DISPOSITIONS)}."
        )
    if claims["proposal_id"] != expected_proposal_id:
        raise GrantVerificationError(
            "Token is bound to a different proposal "
            f"({claims['proposal_id']!r}), not {expected_proposal_id!r} — a "
            "grant for one proposal cannot apply another."
        )

    iat = int(claims["iat"])
    exp = int(claims["exp"])
    # Future-dated iat beyond skew grace: a token minted in the future is not a
    # decision just made — refuse (PyJWT does not reject a future iat itself).
    if iat > now + _LEEWAY:
        raise GrantVerificationError(
            f"Token iat is in the future ({iat} > now {now} + leeway {_LEEWAY})."
        )
    # Server-enforced lifetime ceiling — the TTL is OUR control, not the token's.
    if exp - iat > _MAX_WINDOW_SECONDS:
        raise GrantVerificationError(
            f"Token lifetime {exp - iat}s exceeds the server maximum "
            f"{_MAX_WINDOW_SECONDS}s — a grant token is a transport wrapper for "
            "a just-made decision, not a standing bearer credential."
        )

    return VerifiedGrant(
        jti=str(claims["jti"]),
        kid=kid,
        proposal_id=str(claims["proposal_id"]),
        disposition=str(disposition),
        iat=iat,
        exp=exp,
        operator_identity=key.operator_identity,
        operator_key_fingerprint=_key_fingerprint(public_key),
    )
