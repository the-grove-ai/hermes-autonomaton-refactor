"""grants_tool — surface-agnostic operator interface for standing grants (GRV-001).

Registered tools that run through the shared agent/dispatcher loop — so Telegram,
CLI, and API all inherit the same behaviour with zero per-surface code.  This is
the correct pattern (following flywheel_review_tool) rather than a CLI-only graft.

  review_grants   — read-only list of active standing grants.  Green zone.
  revoke_grant    — revoke a standing grant by ID.  Yellow zone (governance
                    mutation — the operator is retracting authority the system
                    previously earned via a sovereignty prompt or flywheel tap).

Both tools route through grove.grants.GrantStore; they never bypass the B1
registry gate.  revoke_grant is Yellow-zoned in zones.schema.yaml so the
Sovereign Prompt governs the act: the agent cannot revoke a grant without
the operator's mechanical confirmation.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


REVIEW_GRANTS_SCHEMA = {
    "name": "review_grants",
    "description": (
        "List the active standing grants — persisted operator authorizations that "
        "allow the agent to execute governance-mutation verbs (skill promotion, "
        "flywheel approval, etc.) without showing a sovereignty prompt each time. "
        "Read-only. Use this when the operator asks what grants the system holds, "
        "what authorities are standing, or before deciding to revoke one."
    ),
    "parameters": {"type": "object", "properties": {}},
}

REVOKE_GRANT_SCHEMA = {
    "name": "revoke_grant",
    "description": (
        "Revoke a standing grant by its ID, removing the persisted authorization. "
        "After revocation the sovereignty prompt will fire again for the affected "
        "(scope, write_class) pair. Use when the operator decides a previously "
        "granted standing authority should no longer apply. Always call "
        "review_grants first to confirm the grant ID before revoking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "grant_id": {
                "type": "string",
                "description": (
                    "The grant ID to revoke (e.g. 'grant-abc12345'). "
                    "Obtain from review_grants."
                ),
            },
        },
        "required": ["grant_id"],
    },
}


def review_grants() -> str:
    """Return a formatted list of active standing grants for operator review."""
    try:
        from grove.grants import get_grant_store
        store = get_grant_store()
        grants = store.list_grants()
        if not grants:
            return "No active standing grants."
        lines = ["Active standing grants:\n"]
        for g in grants:
            lines.append(
                f"  ID:          {g.id}\n"
                f"  Scope:       {g.scope}\n"
                f"  Write class: {g.write_class}\n"
                f"  Issued at:   {g.issued_at or 'unknown'}\n"
                f"  Granted by:  {g.authorized_by}\n"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("[grants_tool] review_grants failed: %r", exc)
        return f"Error reading grants: {exc}"


def revoke_grant(grant_id: str) -> str:
    """Revoke a standing grant by ID. Returns confirmation or error message."""
    if not grant_id or not grant_id.strip():
        return "Error: grant_id is required. Call review_grants to see available IDs."
    grant_id = grant_id.strip()
    try:
        from grove.grants import get_grant_store
        store = get_grant_store()
        found = store.revoke_grant(grant_id)
        if found:
            return f"Grant '{grant_id}' revoked. The sovereignty prompt will fire again for future requests with this scope."
        return f"Grant '{grant_id}' not found or already revoked."
    except Exception as exc:
        logger.error("[grants_tool] revoke_grant(%r) failed: %r", grant_id, exc)
        return f"Error revoking grant: {exc}"


def register(reg) -> None:
    """Auto-discovered by tools.registry.register_builtin_tools — one registration,
    inherited by every surface through the shared agent/dispatcher loop."""
    reg.register(
        name="review_grants",
        toolset="governance",
        schema=REVIEW_GRANTS_SCHEMA,
        handler=lambda args, **kw: review_grants(),
        emoji="🔑",
    )
    reg.register(
        name="revoke_grant",
        toolset="governance",
        schema=REVOKE_GRANT_SCHEMA,
        handler=lambda args, **kw: revoke_grant(args.get("grant_id", "")),
        emoji="🚫",
    )
