"""Confirmation Gate — server-side verification core (confirmation-gate-grant-token-v1).

The Gate authorizes an INLINE apply of a scope-defining RED proposal ONLY on an
ES256 grant token signed by an operator key registered in the root-owned trust
anchor (:mod:`grove.gate.registry`) and verified here (:mod:`grove.gate.verify`).

Layering: this package imports nothing from ``tools/``. ``grove/api`` may import
it. P1 ships the LOADER/VERIFIER side only — there is no writer for the registry
(registration verbs are P2, on the IAM channel).
"""
from grove.gate.registry import (
    AuthorizedKey,
    RegistryError,
    load_registry,
)
from grove.gate.verify import (
    GrantVerificationError,
    VerifiedGrant,
    verify_grant_token,
)

__all__ = [
    "AuthorizedKey",
    "RegistryError",
    "load_registry",
    "GrantVerificationError",
    "VerifiedGrant",
    "verify_grant_token",
]
