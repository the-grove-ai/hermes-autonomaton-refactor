"""invoke_skill — the governed execution entrypoint for skills (Sprint 63 §1).

Skills are normally *viewed* (``skill_view``) and then carried out by the
model following their procedure. The risk that creates: a model can read a
quarantined (``.andon/``) skill's body and then run its code via
``execute_code`` or a terminal heredoc — bypassing the Yellow-zone Andon
halt and the post-execution promotion prompt entirely. Governance becomes an
instruction the model can ignore rather than a mechanical guarantee.

``invoke_skill`` closes that gap. It is a dedicated *execution intent*: the
Dispatcher classifies an ``invoke_skill`` targeting a ``.andon/`` skill as
Yellow (exactly as it does ``skill_view`` since Sprint 62), so the Sovereign
Prompt fires BEFORE the handler runs and ``PostExecutionKaizenYield`` fires
AFTER. Promoted skills classify Green and run without governance. The tool's
description steers the model here instead of to ``execute_code``; the
Dispatcher hook is what actually enforces it.

This handler does no privileged work itself — the Dispatcher has already
applied governance to the intent by the time the handler is reached. It loads
the SKILL.md (active set or quarantine) via the existing ``grove.skills``
path helpers and returns the procedure for the model to carry out.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


INVOKE_SKILL_SCHEMA = {
    "name": "invoke_skill",
    "description": (
        "Run a skill by name. This is the ONLY correct way to execute a "
        "skill's procedure. NEVER use execute_code or a terminal heredoc to "
        "run skill code — this tool routes the skill through governance "
        "automatically: a quarantined skill halts for the operator's approval "
        "before it runs and offers promotion after; a promoted skill runs "
        "freely. Loads the skill's SKILL.md and returns its procedure for you "
        "to carry out.\n\n"
        "Use this when the operator asks to run a skill, or when the operator "
        "accepts a drafted-skill proposal you surfaced."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "The skill name (use skills_list to see available skills)."
                ),
            },
            "args": {
                "type": "object",
                "description": (
                    "OPTIONAL: parameters to pass to the skill, matching the "
                    "parameters its SKILL.md declares. Omit for skills that "
                    "take none."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "OPTIONAL: path to a linked file within the skill "
                    "(e.g., 'references/api.md'). Omit to get SKILL.md."
                ),
            },
        },
        "required": ["name"],
    },
}


def invoke_skill(
    name: str,
    args: Optional[Dict[str, Any]] = None,
    file_path: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """Load a skill's procedure for execution.

    Governance (Yellow-zone halt for ``.andon/`` skills, post-execution
    promotion prompt) is applied by the Dispatcher to the ``invoke_skill``
    *intent* before this handler runs — see
    ``grove.dispatcher.Dispatcher._classify_one_intent`` and
    ``_maybe_flag_quarantine_execution``. By the time control reaches here
    the operator has already approved (or the skill is promoted/Green), so
    this handler only resolves and returns the SKILL.md content.

    Resolution order: active set (``~/.grove/skills/<name>/``) first, then
    the quarantine (``~/.grove/skills/.andon/<name>/``). Returns a JSON
    string mirroring ``skill_view`` so the model handles both tools the same
    way.
    """
    if not isinstance(name, str) or not name.strip():
        return json.dumps(
            {"success": False, "error": "invoke_skill requires a non-empty 'name'."},
            ensure_ascii=False,
        )
    skill_name = name.strip()

    from grove.skills import active_path, proposal_path

    active = active_path(skill_name)
    quarantined = proposal_path(skill_name)
    if active.exists():
        base, zone = active, "green"
    elif quarantined.exists():
        base, zone = quarantined, "yellow"
    else:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Skill '{skill_name}' not found in the active set or the "
                    f"quarantine. Use skills_list to see available skills."
                ),
            },
            ensure_ascii=False,
        )

    target = base / (file_path.strip() if isinstance(file_path, str) and file_path.strip() else "SKILL.md")
    # Confine reads to the skill directory — a file_path must not escape it.
    try:
        target = target.resolve()
        if base.resolve() not in target.parents and target != (base / "SKILL.md").resolve():
            return json.dumps(
                {"success": False, "error": "file_path escapes the skill directory."},
                ensure_ascii=False,
            )
        content = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return json.dumps(
            {"success": False, "error": f"File not found in skill '{skill_name}': {file_path}"},
            ensure_ascii=False,
        )
    except OSError as exc:
        # Fail loud: surface the real I/O fault rather than a silent empty load.
        return json.dumps(
            {"success": False, "error": f"Could not read skill '{skill_name}': {exc!r}"},
            ensure_ascii=False,
        )

    # Record use so the Curator's stale timer keys off real invocations,
    # mirroring skill_view's bump. Best-effort: a usage-store hiccup must not
    # fail the invocation.
    try:
        from tools.skill_usage import bump_use, bump_view
        bump_view(skill_name)
        bump_use(skill_name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[invoke_skill] usage bump failed (non-fatal): %r", exc)

    return json.dumps(
        {
            "success": True,
            "name": skill_name,
            "zone": zone,
            "path": str(base),
            "args": args or {},
            "content": content,
        },
        ensure_ascii=False,
    )


def register(reg):
    """Sprint 63 — Dispatcher-driven registration entrypoint."""
    from tools.skills_tool import check_skills_requirements

    reg.register(
        name="invoke_skill",
        toolset="skills",
        schema=INVOKE_SKILL_SCHEMA,
        handler=lambda args, **kw: invoke_skill(
            name=args.get("name", ""),
            args=args.get("args"),
            file_path=args.get("file_path"),
            task_id=kw.get("task_id"),
        ),
        check_fn=check_skills_requirements,
        emoji="▶️",
    )
