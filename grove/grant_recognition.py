"""T0 grant recognition — deterministic pattern match for operator governance commands.

When the operator sends an explicit governance verb (e.g. "promote grove-site-fetch"),
try_mint_implicit_grant() returns a GrantToken before the LLM processes the message.
The token is injected into the kaizen handler closure and bypasses the sovereignty
prompt when the LLM subsequently executes the matching terminal command.

This module is STATIC — no imports from grove.zones, grove.capability_registry,
or any zone/capability system. The verb list is hardcoded here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from time import time
from uuid import uuid4


@dataclass
class GrantToken:
    id: str = field(default_factory=lambda: f"grant-{uuid4().hex[:8]}")
    source: str = ""          # "operator_telegram", "flywheel_approval", "standing"
    scope: str = ""           # target skill/resource name
    write_class: str = ""     # "andon_promote", "andon_reject", etc.
    timestamp: float = field(default_factory=time)
    disposition: str = "once" # "once", "session", "standing"
    issued_at: str = ""       # ISO timestamp
    authorized_by: str = ""   # operator identifier
    revoked: bool = False


# Static list — no imports from zone/capability system.
GOVERNANCE_VERBS: dict[str, str] = {
    "promote": "andon_promote",
    "reject": "andon_reject",
    "revoke": "andon_revoke",
    "approve": "flywheel_approve",
    "demote": "flywheel_demote",
    "downgrade": "flywheel_downgrade",
}

GOVERNANCE_PATTERN = re.compile(
    r"^/?(?:hermes\s+(?:andon|flywheel)\s+(?:patterns\s+)?)?"
    r"(" + "|".join(GOVERNANCE_VERBS.keys()) + r")"
    r"\s+([a-zA-Z0-9_.-]+)"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)


def try_mint_implicit_grant(
    raw_message: str,
    source: str = "operator_telegram",
) -> "GrantToken | None":
    """If the operator's message is an explicit governance verb, mint a grant.

    Returns None if the message is not a governance command.
    The grant is implicit — it derives its authority from the operator's
    choice of channel (already authenticated) and the explicit verb in their
    message. No secondary prompt is shown.
    """
    m = GOVERNANCE_PATTERN.match(raw_message.strip())
    if not m:
        return None
    verb, target = m.group(1).lower(), m.group(2)
    return GrantToken(
        source=source,
        scope=target,
        write_class=GOVERNANCE_VERBS[verb],
        disposition="once",
        authorized_by=source,
    )


def grant_covers_halt(grant: "GrantToken", halt: object) -> bool:
    """Return True if the grant authorizes the halted action.

    Checks that:
    - The grant scope appears in the terminal command string.
    - The grant write_class verb appears in the command.
    """
    try:
        triggering = halt.intents[halt.triggering_index]  # type: ignore[attr-defined]
        args = getattr(triggering, "arguments", None) or {}
        cmd = str(args.get("command", "")).lower()
        if not cmd:
            return False
        if grant.scope and grant.scope.lower() not in cmd:
            return False
        if grant.write_class:
            expected_verb = next(
                (k for k, v in GOVERNANCE_VERBS.items() if v == grant.write_class),
                None,
            )
            if expected_verb and expected_verb not in cmd:
                return False
        return True
    except Exception:
        return False
