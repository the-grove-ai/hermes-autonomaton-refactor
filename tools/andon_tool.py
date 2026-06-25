"""andon_tool — surface-agnostic operator interface for skill quarantine (GRV-001).

Registered tools following the flywheel_review_tool / grants_tool pattern so
Telegram, CLI, and API all inherit the same behaviour with zero per-surface code.
The agent uses these tools inline — it NEVER tells the operator to run a terminal
command (hermes andon promote, etc.) for skill governance.

  andon_list    — read-only list of quarantined skill proposals.  Green zone.
  andon_promote — promote a skill from quarantine to active.      Red zone.
  andon_reject  — reject (delete) a quarantined skill proposal.   Red zone.
  andon_revoke  — revoke an active skill back to quarantine.       Red zone.

Red-zoned operations are grantable under the GRV-001 Grant Token model:
  Red + valid grant (T0 implicit or standing) → execute immediately.
  Red + no grant (agent-synthesized)          → sovereignty prompt (4 choices).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tool schemas ──────────────────────────────────────────────────────────────

ANDON_LIST_SCHEMA = {
    "name": "andon_list",
    "description": (
        "List the skills currently in quarantine (.andon/) awaiting operator review. "
        "Read-only. Use this before deciding whether to promote or reject a skill."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ANDON_PROMOTE_SCHEMA = {
    "name": "andon_promote",
    "description": (
        "Promote a quarantined skill to the active skill set. This is a governed "
        "sovereignty act — the skill moves from .andon/ to the live skills directory "
        "and becomes available immediately. Requires operator authority (implicit from "
        "the operator's message or via sovereignty prompt). Always call andon_list "
        "first to confirm the skill name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "The name of the skill to promote (directory name under .andon/).",
            },
            "replace": {
                "type": "boolean",
                "description": (
                    "If True, archive an existing active skill of the same name before "
                    "promoting. Default False."
                ),
            },
        },
        "required": ["skill_name"],
    },
}

ANDON_REJECT_SCHEMA = {
    "name": "andon_reject",
    "description": (
        "Reject a quarantined skill proposal, permanently deleting it from .andon/. "
        "This is irreversible. Requires operator authority. Use when the operator "
        "decides a proposed skill should not be promoted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "The name of the skill to reject.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for rejection (recorded in telemetry).",
            },
        },
        "required": ["skill_name"],
    },
}

ANDON_REVOKE_SCHEMA = {
    "name": "andon_revoke",
    "description": (
        "Revoke an active skill, returning it to .andon/ quarantine for re-review. "
        "The skill is no longer available after revocation. Requires operator authority. "
        "Use when the operator decides a previously promoted skill needs review."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "The name of the active skill to revoke.",
            },
        },
        "required": ["skill_name"],
    },
}


# ── Handler functions ─────────────────────────────────────────────────────────

def andon_list() -> str:
    """Return a formatted list of quarantined skill proposals."""
    try:
        from grove.sovereignty import list_proposals, andon_dir
        proposals = list_proposals()
        if not proposals:
            return f"No pending skill proposals in quarantine ({andon_dir()})."
        lines = [f"Quarantined skill proposals ({len(proposals)}):\n"]
        for p in proposals:
            verdict = p.get("scan_verdict", "unknown")
            findings_n = len(p.get("scan_findings", []))
            marker = (
                ""
                if verdict == "safe"
                else f"  [{verdict}, {findings_n} finding{'s' if findings_n != 1 else ''}]"
            )
            desc = p.get("description") or ""
            lines.append(f"  {p['name']}  (proposed {p['proposed_at']}){marker}")
            if desc:
                lines.append(f"    {desc}")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("[andon_tool] andon_list failed: %r", exc)
        return f"Error listing quarantined skills: {exc}"


def andon_promote(skill_name: str, replace: bool = False) -> str:
    """Promote a skill from quarantine to active."""
    if not skill_name or not skill_name.strip():
        return "Error: skill_name is required."
    skill_name = skill_name.strip()
    try:
        from grove.sovereignty import promote
        event = promote(skill_name, replace=replace)
        return (
            f"Skill '{skill_name}' promoted to active.\n"
            f"  From: {event['source_path']}\n"
            f"  To:   {event['dest_path']}"
        )
    except FileNotFoundError as exc:
        return f"Skill '{skill_name}' not found in quarantine: {exc}"
    except FileExistsError as exc:
        return (
            f"Active skill '{skill_name}' already exists. "
            f"Pass replace=true to archive and replace it. ({exc})"
        )
    except Exception as exc:
        logger.error("[andon_tool] andon_promote(%r) failed: %r", skill_name, exc)
        return f"Error promoting skill '{skill_name}': {exc}"


def andon_reject(skill_name: str, reason: Optional[str] = None) -> str:
    """Reject (permanently delete) a quarantined skill proposal."""
    if not skill_name or not skill_name.strip():
        return "Error: skill_name is required."
    skill_name = skill_name.strip()
    try:
        from grove.sovereignty import reject
        reject(skill_name, reason=reason)
        return f"Skill '{skill_name}' rejected and removed from quarantine. (reason: {reason or 'none'})"
    except FileNotFoundError as exc:
        return f"Skill '{skill_name}' not found in quarantine: {exc}"
    except Exception as exc:
        logger.error("[andon_tool] andon_reject(%r) failed: %r", skill_name, exc)
        return f"Error rejecting skill '{skill_name}': {exc}"


def andon_revoke(skill_name: str) -> str:
    """Revoke an active skill, returning it to quarantine."""
    if not skill_name or not skill_name.strip():
        return "Error: skill_name is required."
    skill_name = skill_name.strip()
    try:
        from grove.sovereignty import revoke
        event = revoke(skill_name)
        return (
            f"Skill '{skill_name}' revoked and returned to quarantine.\n"
            f"  From: {event['source_path']}\n"
            f"  To:   {event['dest_path']}"
        )
    except FileNotFoundError as exc:
        return f"Skill '{skill_name}' not found in active skills: {exc}"
    except FileExistsError as exc:
        return f"Conflict returning '{skill_name}' to quarantine: {exc}"
    except Exception as exc:
        logger.error("[andon_tool] andon_revoke(%r) failed: %r", skill_name, exc)
        return f"Error revoking skill '{skill_name}': {exc}"


# ── Registration ──────────────────────────────────────────────────────────────

def register(reg) -> None:
    """Auto-discovered by tools.registry.register_builtin_tools — one registration,
    inherited by every surface through the shared agent/dispatcher loop."""
    reg.register(
        name="andon_list",
        toolset="andon",
        schema=ANDON_LIST_SCHEMA,
        handler=lambda args, **kw: andon_list(),
        emoji="📋",
    )
    reg.register(
        name="andon_promote",
        toolset="andon",
        schema=ANDON_PROMOTE_SCHEMA,
        handler=lambda args, **kw: andon_promote(
            args.get("skill_name", ""),
            replace=bool(args.get("replace", False)),
        ),
        emoji="🟢",
    )
    reg.register(
        name="andon_reject",
        toolset="andon",
        schema=ANDON_REJECT_SCHEMA,
        handler=lambda args, **kw: andon_reject(
            args.get("skill_name", ""),
            reason=args.get("reason"),
        ),
        emoji="🔴",
    )
    reg.register(
        name="andon_revoke",
        toolset="andon",
        schema=ANDON_REVOKE_SCHEMA,
        handler=lambda args, **kw: andon_revoke(args.get("skill_name", "")),
        emoji="↩️",
    )
