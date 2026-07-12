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


def _resolve_record(skill_name: str):
    """The P1 canonical slug-tail resolution for *skill_name* — the guard's
    single record-lookup seam (skill-invocation-path-integrity-v1 P2).

    Replaces the retired ``_skill_record_state`` exact-frontmatter-name match:
    flat (``forge-jobsearch``) and category-qualified (``fleet/forge-jobsearch``)
    shapes now key on the SAME record. Returns a
    :class:`grove.capability_registry.SkillResolution`; module-level so tests
    monkeypatch this seam.
    """
    from grove.capability_registry import resolve_skill_record

    return resolve_skill_record(skill_name)


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

    # skill-invocation-path-integrity-v1 P2 — the guard keys on the RESOLVED
    # capability record (P1 canonical slug-tail resolver), not exact
    # frontmatter-name equality, so flat and category-qualified
    # ("fleet/<name>") invocations key identically. Dispositions:
    #   * no record   -> allow (legacy semantics: external / pre-C2 skills)
    #   * ambiguous   -> refuse, naming every colliding record id
    #   * resolved    -> the record's lifecycle state must AGREE with the
    #     disk-inferred state (active tree <=> EXECUTABLE_STATES; .andon <=>
    #     proposed); any record/disk divergence refuses, naming both states.
    res = _resolve_record(skill_name)
    if res.status == "ambiguous":
        logger.warning(
            "[invoke_skill] ambiguous skill slug for %r -> %s; refusing",
            skill_name,
            list(res.matches),
        )
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Skill name '{skill_name}' is ambiguous — its slug "
                    f"matches multiple capability records: "
                    f"{', '.join(res.matches)}. Give the records unique "
                    f"trailing id segments before this skill can run."
                ),
            },
            ensure_ascii=False,
        )
    if res.status == "resolved":
        from grove.capability import EXECUTABLE_STATES, LifecycleState

        _rec_state = res.record.lifecycle.state
        _state_label = getattr(_rec_state, "value", str(_rec_state))
        if zone == "green" and _rec_state not in EXECUTABLE_STATES:
            logger.warning(
                "[invoke_skill] record/disk divergence for %r: record %s "
                "state %r vs disk location 'active tree'; refusing",
                skill_name,
                res.record_id,
                _state_label,
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{skill_name}' (record {res.record_id}) has a "
                        f"non-executable lifecycle state ({_state_label}) but "
                        f"its directory sits in the active tree — record/disk "
                        f"state divergence; refusing to run. Only active / "
                        f"managed / refined records execute "
                        f"(EXECUTABLE_STATES @ grove/capability.py)."
                    ),
                },
                ensure_ascii=False,
            )
        if zone == "yellow" and _rec_state is not LifecycleState.PROPOSED:
            logger.warning(
                "[invoke_skill] record/disk divergence for %r: record %s "
                "state %r vs disk location '.andon quarantine'; refusing",
                skill_name,
                res.record_id,
                _state_label,
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{skill_name}' (record {res.record_id}) has "
                        f"lifecycle state ({_state_label}) but its directory "
                        f"sits in the .andon quarantine (which implies state "
                        f"'proposed') — record/disk state divergence; "
                        f"refusing to run."
                    ),
                },
                ensure_ascii=False,
            )
    # res.status == "none" -> no governing record: legacy-allow (unchanged).

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

    # GRV-009 E6a C3 — the read-side .usage.json view/use bump is RETIRED here
    # (records are sole-source; the curator's stale timer moves onto the record
    # lifecycle in E6b). Invocation/execution is unaffected (A7).

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
