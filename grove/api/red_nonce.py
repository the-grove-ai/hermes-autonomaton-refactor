"""Per-proposal CSRF nonces for the RED ``.env`` two-step approve flow.

propose-approve-deadlock-v1 Phase 1b-ii (Step 3). A same-origin script that
passes the portal's mesh-auth + same-origin gates could otherwise POST the
mint-capable approve endpoint with zero token. These nonces bind an approve/
confirm action to a specific ``proposal_id`` + step + time bucket, keyed on the
gateway ``api_key``, so:

  * a forged/tampered value is rejected (HMAC),
  * a stale value expires (time bucket, ~2×TTL grace),
  * the second (mint) step requires a ``confirm``-step nonce that ONLY a
    successful ``approve`` step issues — a skipped ``approve`` (step-jump) is
    rejected.

Lives in its own module so both the renderer (``fragments``) and the action
handlers (``actions``) import it without the fragments↔actions cycle. The
helpers are PURE — the caller extracts the key from ``request.app`` and passes
it, so this module imports nothing from the api layer.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

# Approve→confirm window. verify() accepts the current and previous bucket, so
# the effective grace is (TTL, 2×TTL).
RED_NONCE_TTL = 600  # seconds


def nonce_key_from_app(app: Any) -> bytes:
    """The HMAC key = the gateway api_key (``API_SERVER_KEY``), from the adapter.

    Empty when no key is configured (local dev): the nonce then still provides
    step-jump + expiry binding, but not secrecy — the mesh-auth + same-origin
    gates remain the primary defense. Prod (VM) always sets API_SERVER_KEY.
    """
    adapter = app.get("api_server_adapter") if app is not None else None
    key = (getattr(adapter, "_api_key", "") or "") if adapter is not None else ""
    return key.encode("utf-8")


def red_nonce(proposal_id: str, step: str, key: bytes, bucket: int | None = None) -> str:
    """HMAC(key, ``proposal_id:step:bucket``). ``bucket`` defaults to now // TTL."""
    if bucket is None:
        bucket = int(time.time() // RED_NONCE_TTL)
    msg = f"{proposal_id}:{step}:{bucket}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_red_nonce(proposal_id: str, step: str, nonce: str, key: bytes) -> bool:
    """Constant-time verify against the current and previous time bucket."""
    if not nonce:
        return False
    now_bucket = int(time.time() // RED_NONCE_TTL)
    for bucket in (now_bucket, now_bucket - 1):
        if hmac.compare_digest(nonce, red_nonce(proposal_id, step, key, bucket)):
            return True
    return False
