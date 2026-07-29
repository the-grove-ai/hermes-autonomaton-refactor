"""Shared Confirmation Gate token constants — the SINGLE source both the signer
(operator device, :mod:`grove.gate.client`) and the verifier (server,
:mod:`grove.gate.verify`) import. Divergence between signer and verifier
constants is the drift class this module kills: there is exactly one definition
of each value here, and both sides bind to it.

Imports nothing from ``tools/``, ``grove/api``, or ``grove/eval`` — a leaf.
"""
from __future__ import annotations

import hashlib

# Explicit audience so a token minted for any other Grove surface (or a GCP OIDC
# token, aud=<client-id>) fails structurally, never by accident of reuse.
AUDIENCE = "grove-confirmation-gate"

# Custom media type in the JWT ``typ`` header — a general-purpose JWT / OIDC
# token (typ="JWT") fails this structural check before any signature math.
TOKEN_TYP = "grove-grant+jwt"

# The ONLY accepted signature algorithm — a hardcoded literal on the verify side
# (algorithms=[ALGORITHM]); the alg-confusion defense is this allow-list.
ALGORITHM = "ES256"

# Clock-skew grace for exp/iat (seconds). Small: mint-after-decision means the
# operator's clock and the gateway's are both live and NTP-disciplined; 10s
# absorbs ordinary drift without meaningfully widening the replay window.
LEEWAY_SECONDS = 10

# SERVER-enforced maximum token lifetime (seconds). A token is a transport
# wrapper around a decision the operator JUST made, so it needs only enough life
# to cross the wire and be consumed. The signer mints exp = iat + this; the
# verifier refuses exp - iat > this even on a valid signature. Both sides read
# the SAME number here — that is the point of this module.
MAX_WINDOW_SECONDS = 60

# The gated verb. P1/P2 gate APPROVE only (reject/dismiss for the six scope-
# defining types are unchanged); any other disposition is an unknown-disposition
# refuse.
APPROVE_DISPOSITION = "approve"
ALLOWED_DISPOSITIONS = frozenset({APPROVE_DISPOSITION})

# Required claim set — strict, no defaults. The verifier enforces presence; the
# signer emits exactly these.
REQUIRED_CLAIMS = ("proposal_id", "disposition", "jti", "iat", "exp")


def spki_fingerprint(public_key) -> str:
    """SHA-256 over the DER-encoded SubjectPublicKeyInfo of a public key object —
    the canonical key fingerprint (A3). Keying on the SPKI (not PEM text) makes
    it invariant to PEM whitespace/encoding. The ``kid`` derives from a prefix of
    this value; the R-6 provenance stamp records the full value. Both signer
    (kid derivation) and verifier (stamp) call THIS one function."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()
