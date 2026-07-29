"""Operator DEVICE-side Confirmation Gate client (P2).

Fetch the RAW proposal record over the mesh, recompute + verify its content id
against the id being approved, render the HASHED authoritative surface (F4),
sign an ES256 grant token bound to the shared constants, submit it, and surface
the outcome. The private key is loaded and used only inside :mod:`grove.gate.keyfile`
/ PyJWT — it never transits this module's output.

Layering: imports :mod:`grove.gate.constants` and, at call time,
``grove.eval.proposal_queue.compute_proposal_id`` (NEVER reimplemented — P1a
pin 5). Nothing from ``tools/`` or ``grove/api``.
"""
from __future__ import annotations

import json
import time
from typing import Optional
from uuid import uuid4

from grove.gate import constants

# Fields NOT covered by compute_proposal_id (P1a pin 5). They may only appear
# under the UNVERIFIED banner, never in the authoritative surface.
_UNHASHED_FIELDS = (
    "semantic_justification",
    "proposer",
    "detail",
    "source_patterns",
    "lease",
)

_UNVERIFIED_BANNER = (
    "--- UNVERIFIED ANNOTATION — not covered by the hash you are approving ---"
)


class GateClientError(RuntimeError):
    """Device-side approval failed — content-id mismatch, fetch/transport error,
    or a server refusal. Governed loud fault."""


def recompute_and_verify_id(record: dict, claimed_id: str) -> str:
    """Recompute the content id from the HASHED fields via the IMPORTED
    :func:`grove.eval.proposal_queue.compute_proposal_id` (never reimplemented)
    and compare the FULL string incl. the ``sha256:`` prefix (P1a pin 5).

    Returns the recomputed id; raises :class:`GateClientError` naming BOTH ids on
    mismatch — the caller must not prompt or sign after a raise."""
    from grove.eval.proposal_queue import compute_proposal_id

    recomputed = compute_proposal_id(
        type=record["type"],
        payload=record["payload"],
        evidence=tuple(record.get("evidence") or ()),
    )
    if recomputed != claimed_id:
        raise GateClientError(
            "REFUSE — content-id mismatch. The fetched record does not hash to "
            "the id you are approving; the stored record was altered or the id "
            "is wrong. Nothing signed, no prompt shown.\n"
            f"  claimed:    {claimed_id}\n"
            f"  recomputed: {recomputed}"
        )
    return recomputed


def render_for_approval(record: dict) -> str:
    """Render the AUTHORITATIVE surface: hashed fields ONLY (type, payload,
    evidence) — payload as raw JSON, no summarization. Unhashed annotations, if
    any, appear ONLY below, under the loud UNVERIFIED banner — never above or
    interleaved with the hashed surface (F4)."""
    lines = [
        "=== AUTHORITATIVE — this is exactly what the hash covers ===",
        f"proposal type: {record['type']}",
        "payload:",
        json.dumps(record["payload"], indent=2, sort_keys=True),
        f"evidence: {json.dumps(list(record.get('evidence') or ()))}",
    ]
    unhashed = {
        k: record[k]
        for k in _UNHASHED_FIELDS
        if k in record and record[k] not in (None, "", [], {})
    }
    if unhashed:
        lines.append("")
        lines.append(_UNVERIFIED_BANNER)
        for k, v in unhashed.items():
            lines.append(f"{k}: {json.dumps(v)}")
    return "\n".join(lines)


def build_token(
    proposal_id: str,
    private_key,
    kid: str,
    operator_identity: str,
    *,
    now: Optional[int] = None,
) -> str:
    """Mint the ES256 grant token. EVERY value binds to
    :mod:`grove.gate.constants` (the single source the verifier reads); ``exp =
    iat + MAX_WINDOW_SECONDS``, ``jti`` fresh per call."""
    import jwt

    iat = int(time.time()) if now is None else int(now)
    claims = {
        "proposal_id": proposal_id,
        "disposition": constants.APPROVE_DISPOSITION,
        "jti": uuid4().hex,
        "iat": iat,
        "exp": iat + constants.MAX_WINDOW_SECONDS,
        "aud": constants.AUDIENCE,
        "iss": operator_identity,
    }
    return jwt.encode(
        claims,
        private_key,
        algorithm=constants.ALGORITHM,
        headers={"kid": kid, "typ": constants.TOKEN_TYP},
    )


def _raw_url(base_url: str, proposal_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/substrate/proposals/{proposal_id}/raw"


def _approve_url(base_url: str, proposal_id: str) -> str:
    return f"{base_url.rstrip('/')}/portal/actions/proposals/{proposal_id}/approve"


def fetch_raw_record(base_url: str, proposal_id: str) -> dict:
    """GET the raw stored record over the mesh. 404 → loud refuse."""
    import urllib.error
    import urllib.request

    url = _raw_url(base_url, proposal_id)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GateClientError(
                f"no proposal {proposal_id!r} on the gateway (404)."
            ) from exc
        raise GateClientError(
            f"gateway GET failed ({exc.code} {exc.reason}) for {url}."
        ) from exc
    except urllib.error.URLError as exc:
        raise GateClientError(
            f"cannot reach the gateway at {url}: {exc.reason}. Is the mesh up "
            "and the base URL correct?"
        ) from exc


def submit_approval(base_url: str, proposal_id: str, token: str) -> tuple:
    """POST the token to the existing approve route. Returns ``(status, body)``.
    A refusal comes back as a 4xx with the server's distinct message in the
    body — returned as-is for verbatim surfacing (never raised over)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        _approve_url(base_url, proposal_id),
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise GateClientError(
            f"cannot reach the gateway to submit: {exc.reason}."
        ) from exc
