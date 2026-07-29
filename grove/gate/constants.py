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
# signer emits exactly these. P4 dual-claim model: proposal_id is a LOCATOR,
# content_digest is the COMMITMENT (what the operator actually approved).
REQUIRED_CLAIMS = ("proposal_id", "disposition", "jti", "iat", "exp", "content_digest")

# The EXACT field set the content commitment covers — one authoritative
# definition (no duplicate literal elsewhere). Deliberately the hashed-content
# fields only; annotation fields (semantic_justification, proposer, detail,
# source_patterns, lease) are NOT part of the commitment and never signed.
DIGEST_FIELDS = ("type", "payload", "evidence")

# Domain separation for the content digest (GATE-B G3): the hash input is
# prefixed so a content_digest can never collide with, or be replayed as, any
# other SHA-256 the system computes (a proposal_id, an effect signature, …).
_DIGEST_DOMAIN = b"grove-gate-v1:content_digest:"


def canonical_digest(record) -> str:
    """SHA-256 hex over the domain-separated canonical JSON of exactly
    :data:`DIGEST_FIELDS` ({type, payload, evidence}) drawn from *record* (any
    mapping-like object exposing those keys).

    Canonical = ``sort_keys=True``, compact separators, ``ensure_ascii=True`` —
    so the device (pre-wire, from the in-memory proposal) and the server
    (post-JSON-wire, from the reloaded record) compute the IDENTICAL digest
    regardless of dict key ordering, whitespace, or non-ASCII transport. Evidence
    is normalized to a list (JSON round-trips tuples to lists), so a tuple on the
    server and a list on the client hash the same.

    This is the COMMITMENT the operator signs — independent of how any producer
    minted the locator proposal_id (identity-subset vs full-content), which is
    the id-semantics divergence P3's Andon exposed."""
    import json

    fields = {
        "type": record.get("type"),
        "payload": record.get("payload"),
        "evidence": list(record.get("evidence") or ()),
    }
    canonical = json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(_DIGEST_DOMAIN + canonical.encode("utf-8")).hexdigest()


def spki_fingerprint(public_key) -> str:
    """SHA-256 over the DER-encoded SubjectPublicKeyInfo of a public key object —
    the canonical key fingerprint (A3). Keying on the SPKI (not PEM text) makes
    it invariant to PEM whitespace/encoding. The ``kid`` derives from a prefix of
    this value; the R-6 provenance stamp records the full value. Both signer
    (kid derivation) and verifier (stamp) call THIS one function."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()
