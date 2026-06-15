#!/usr/bin/env python3
"""
Skill Manager Tool -- Agent-Managed Skill Creation & Editing

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge. New skills are created in
~/.grove/skills/. Existing skills (bundled, hub-installed, or user-created)
can be modified or deleted wherever they live.

Skills are the agent's procedural memory: they capture *how to do a specific
type of task* based on proven experience. General memory (MEMORY.md, USER.md) is
broad and declarative. Skills are narrow and actionable.

Actions:
  create     -- Create a new skill (SKILL.md + directory structure)
  edit       -- Replace the SKILL.md content of a user skill (full rewrite)
  patch      -- Targeted find-and-replace within SKILL.md or any supporting file
  delete     -- Remove a user skill entirely
  write_file -- Add/overwrite a supporting file (reference, template, script, asset)
  remove_file-- Remove a supporting file from a user skill

Directory layout for user skills:
    ~/.grove/skills/
    ├── my-skill/
    │   ├── SKILL.md
    │   ├── references/
    │   ├── templates/
    │   ├── scripts/
    │   └── assets/
    └── category-name/
        └── another-skill/
            └── SKILL.md
"""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from hermes_constants import get_hermes_home, display_hermes_home
from typing import Dict, Any, Optional, Tuple

from utils import atomic_replace, is_truthy_value
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Import security scanner — external hub installs always get scanned;
# agent-created skills only get scanned when skills.guard_agent_created is on.
try:
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
    _GUARD_AVAILABLE = True
except ImportError:
    _GUARD_AVAILABLE = False


def _guard_agent_created_enabled() -> bool:
    """Read skills.guard_agent_created from config (default False).

    Off by default because the agent can already execute the same code
    paths via terminal() with no gate, so the scan adds friction without
    meaningful security.  Users who want belt-and-suspenders can turn it
    on via `hermes config set skills.guard_agent_created true`.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return is_truthy_value(
            cfg_get(cfg, "skills", "guard_agent_created"),
            default=False,
        )
    except Exception:
        return False


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """Scan a skill directory after write. Returns error string if blocked, else None.

    No-op when skills.guard_agent_created is disabled (the default).
    """
    if not _GUARD_AVAILABLE:
        return None
    if not _guard_agent_created_enabled():
        return None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False:
            report = format_scan_report(result)
            return f"Security scan blocked this skill ({reason}):\n{report}"
        if allowed is None:
            # "ask" verdict — for agent-created skills this means dangerous
            # findings were detected.  Surface as an error so the agent can
            # retry with the flagged content removed.
            report = format_scan_report(result)
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
            return f"Security scan blocked this skill ({reason}):\n{report}"
    except Exception as e:
        logger.warning("Security scan failed for %s: %s", skill_dir, e, exc_info=True)
    return None

import yaml


# All skills live in ~/.grove/skills/ (single source of truth)
GROVE_HOME = get_hermes_home()
SKILLS_DIR = GROVE_HOME / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


def _containing_skills_root(skill_path: Path) -> Path:
    """Return the skills root directory (local or external_dirs entry) that
    contains ``skill_path``.  Falls back to the local ``SKILLS_DIR`` if no
    match is found (defensive — callers should have located the skill via
    ``_find_skill`` first).
    """
    from agent.skill_utils import get_all_skills_dirs

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    for root in get_all_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return SKILLS_DIR


def _pinned_guard(name: str) -> Optional[str]:
    """Return a refusal message if *name* is pinned, else None.

    Pin protects a skill from **deletion** — both the curator's auto-archive
    passes and the agent's ``skill_manage(action="delete")`` tool call. The
    agent can still patch/edit pinned skills; pin only guards against
    irrecoverable loss, not against content evolution.

    Best-effort: if the record is unreadable we let the delete through rather
    than block on a registry hiccup.

    GRV-009 E6b C2-bridge — pin is read from the record (lifecycle.pinned), not
    the .usage.json sidecar.
    """
    try:
        from grove.capability_registry import skill_record_for_name
        rec = skill_record_for_name(name)
        if rec is not None and rec.lifecycle.pinned:
            return (
                f"Skill '{name}' is pinned and cannot be deleted by "
                f"skill_manage. Ask the user to run "
                f"`hermes curator unpin {name}` if they want to delete it. "
                f"Patches and edits are allowed on pinned skills; only "
                f"deletion is blocked."
            )
    except Exception:
        logger.debug("pinned-guard lookup failed for %s", name, exc_info=True)
    return None


MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


# =============================================================================
# Validation helpers
# =============================================================================

def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """
    Validate that SKILL.md content has proper frontmatter with required fields.
    Returns error message or None if valid.
    """
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """Check that content doesn't exceed the character limit for agent writes.

    Returns an error message or None if within bounds.
    """
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _resolve_skill_dir(name: str, category: str = None) -> Path:
    """Build the directory path for a new skill, optionally under a category."""
    if category:
        return SKILLS_DIR / category / name
    return SKILLS_DIR / name


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """
    Find a skill by name across all skill directories.

    Searches the local skills dir (~/.grove/skills/) first, then any
    external dirs configured via skills.external_dirs.  Returns
    {"path": Path} or None.
    """
    from agent.skill_utils import EXCLUDED_SKILL_DIRS, get_all_skills_dirs
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if any(part in EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            if skill_md.parent.name == name:
                return {"path": skill_md.parent}
    return None


def _validate_file_path(file_path: str) -> Optional[str]:
    """
    Validate a file path for write_file/remove_file.
    Must be under an allowed subdirectory and not escape the skill dir.
    """
    from tools.path_security import has_traversal_component

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # Prevent path traversal
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # Must be under an allowed subdirectory
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # Must have a filename (not just a directory)
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Atomically write text content to a file.
    
    Uses a temporary file in the same directory and os.replace() to ensure
    the target file is never left in a partially-written state if the process
    crashes or is interrupted.
    
    Args:
        file_path: Target file path
        content: Content to write
        encoding: Text encoding (default: utf-8)
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        atomic_replace(temp_path, file_path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temporary file %s during atomic write", temp_path, exc_info=True)
        raise


# =============================================================================
# Core actions
# =============================================================================

def _create_skill(
    name: str,
    content: str,
    category: str = None,
    *,
    soul_alignment: Optional[str] = None,
    tension_note: Optional[str] = None,
    goals_served: Optional[list] = None,
    lineage: Optional[list] = None,
) -> Dict[str, Any]:
    """Propose a new skill into the quarantine for operator review.

    Per Sprint 06a (jidoka-andon-implementation-v1): every agent-authored
    skill lands in ~/.grove/skills/.andon/<name>/SKILL.md, not in the active
    skills directory. The security scan still runs; the verdict and findings
    are recorded in the proposal's frontmatter; the operator promotes (or
    rejects) via the ``sovereignty`` CLI. See docs/design/andon-design-v1.md.

    ``category`` is accepted for API compatibility but ignored — proposals
    use the flat structure documented in the design contract.
    """
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Collision check: ACTIVE skills only. A re-proposal in .andon/ overwrites
    # the prior proposal — promotion is the irreversible step. ``.andon`` is
    # listed in EXCLUDED_SKILL_DIRS so _find_skill naturally skips it.
    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": (
                f"An active skill named '{name}' already exists at "
                f"{existing['path']}. Choose a different name, or ask the "
                f"operator to revoke the active skill first "
                f"(`hermes andon revoke {name}`)."
            ),
        }

    from grove.skills import (
        assess_soul_alignment,
        parse_frontmatter,
        proposal_path,
        stamp_proposal_frontmatter,
        write_proposal,
    )

    # Write the proposal body to .andon/ first so the scanner has files on disk.
    proposal_dir = write_proposal(name, content)
    skill_md = proposal_dir / "SKILL.md"

    # Always scan agent-created proposals (no config gate — operator is the gate).
    scan_verdict = "unknown"
    scan_findings: list = []
    if _GUARD_AVAILABLE:
        try:
            scan_result = scan_skill(proposal_dir, source="agent-created")
            scan_verdict = scan_result.verdict
            scan_findings = [
                {
                    "pattern_id": f.pattern_id,
                    "severity": f.severity,
                    "category": f.category,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                }
                for f in scan_result.findings
            ]
        except Exception as exc:
            logger.warning(
                "Security scan failed for proposal %s: %s",
                proposal_dir, exc, exc_info=True,
            )

    # Soul-alignment (Sprint 14). The Curator review model assesses and
    # passes the soul-alignment tool args; a normal foreground create
    # passes none, so the code assesses heuristically — the operator
    # still sees identity metadata on `hermes andon diff`.
    if soul_alignment is None:
        try:
            _fm, _ = parse_frontmatter(content)
            _description = str(_fm.get("description", ""))
        except ValueError:
            _description = ""
        soul_alignment, tension_note, goals_served = assess_soul_alignment(
            name, _description
        )

    # Grove provenance: the cognitive tier that authored this proposal
    # (recorded at routing time — the system's truth, not the model's
    # self-report) and the operator's register from soul.md.
    from grove.providers import current_tier

    tier = current_tier()
    register = None
    try:
        from grove.identity import load_identity

        register = load_identity().frontmatter.get("register")
    except Exception as exc:
        logger.warning(
            "[skills] identity unavailable; proposal '%s' register=null. "
            "Cause: %r",
            name, exc,
        )

    # Stamp Grove proposal frontmatter (created_by, proposed_at, zone,
    # tier, register, lineage, provenance) and rewrite the SKILL.md so
    # the verdict is visible in `sovereignty diff`.
    stamped = stamp_proposal_frontmatter(
        content,
        scan_verdict=scan_verdict,
        scan_findings=scan_findings,
        soul_alignment=soul_alignment,
        tension_note=tension_note,
        goals_served=goals_served,
        tier=tier,
        register=register,
        lineage=lineage,
    )
    _atomic_write_text(skill_md, stamped)

    # GRV-009 E6b C2 — mint a state:proposed capability record for the new agent
    # skill. The record is the authoritative review lock (A6: proposed is the
    # sole quarantine state) and is NON-EXECUTABLE behind the 4.1 checkpoint; the
    # .andon/ file remains the reviewable body. Best-effort: a mint failure logs
    # loudly but never blocks the proposal (the body is already on disk).
    _category = ""
    try:
        from grove.capability_registry import (
            _frontmatter_value, register_proposed_skill,
        )
        _category = _frontmatter_value(stamped, "category") or ""
        register_proposed_skill(name, _category, stamped)
    except Exception:
        from grove.capability_registry import _slug as _s
        logger.warning(
            "proposed-record mint FAILED skill_id=skill.%s.%s body=%s — proposal "
            "written but record NOT minted; reconcile manually",
            _s(_category) or _s(name), _s(name), skill_md, exc_info=True,
        )

    message = (
        f"Proposed skill '{name}' to your review queue. "
        f"Run `hermes andon list` to see all pending proposals, "
        f"or `hermes andon diff {name}` to review this one. "
        f"Promotion to active skills requires an explicit "
        f"`hermes andon promote {name}`."
    )
    if scan_verdict in ("caution", "dangerous") and scan_findings:
        message += (
            f"\n\nScan verdict: {scan_verdict} "
            f"({len(scan_findings)} findings — recorded in frontmatter)."
        )

    return {
        "success": True,
        "message": message,
        "path": str(proposal_dir),
        "skill_md": str(skill_md),
        "zone": "yellow",
        "quarantined": True,
        "scan_verdict": scan_verdict,
        "scan_findings_count": len(scan_findings),
    }


def _managed_edit_refusal(name: str) -> Optional[Dict[str, Any]]:
    """GRV-009 E6b C2 — refuse to edit a MANAGED (installed) skill cleanly.

    A managed record is terminal (no REFINED exit) and the skill is upstream-
    managed, so editing it via skill_manage would silently drift from upstream
    and strand the record on an illegal transition. Returns an error dict to
    return to the caller, or None to proceed. Best-effort: any lookup failure
    proceeds (the edit's own validation still applies)."""
    try:
        from grove.capability import CapabilityKind, LifecycleState
        from grove.capability_registry import _slug, load_capabilities

        slug = _slug(name)
        for cid, cap in load_capabilities().items():
            if cap.kind is CapabilityKind.SKILL and cid.rsplit(".", 1)[-1] == slug:
                if cap.lifecycle.state is LifecycleState.MANAGED:
                    return {
                        "success": False,
                        "error": (
                            f"Skill '{name}' is an installed/managed skill "
                            f"(record state:managed) — it is upstream-managed and "
                            f"cannot be edited via skill_manage. Re-install or "
                            f"fork it under a new name instead."
                        ),
                    }
                return None
    except Exception:
        return None
    return None


def _refine_record_after_edit(name: str, new_body: str) -> None:
    """GRV-009 E6b C2 — after a successful edit/patch, transition the skill's
    record ACTIVE->REFINED and re-populate body_hash (the body changed).

    Best-effort: fires only when a record exists and is ACTIVE (transition_record
    SKIPS other states without writing). External/legacy record-less skills are a
    no-op. A failure logs loudly but never undoes the body edit."""
    try:
        from grove.capability import LifecycleState
        from grove.capability_registry import (
            _body_hash, skill_record_id_for_name, transition_record,
        )

        cap_id = skill_record_id_for_name(name)
        if cap_id is None:
            return
        transition_record(
            cap_id, LifecycleState.REFINED, actor="agent",
            reason="skill body edited", body_hash=_body_hash(new_body),
        )
    except Exception:
        logger.warning(
            "refine-transition failed for skill %r (body edit kept)", name,
            exc_info=True,
        )


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    # GRV-009 E6b C2 — managed (installed) skills are not editable here.
    managed_refusal = _managed_edit_refusal(name)
    if managed_refusal is not None:
        return managed_refusal

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found. Use skills_list() to see available skills."}

    skill_md = existing["path"] / "SKILL.md"
    # Back up original content for rollback
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    _atomic_write_text(skill_md, content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(skill_md, original_content)
        return {"success": False, "error": scan_error}

    _refine_record_after_edit(name, content)
    return {
        "success": True,
        "message": f"Skill '{name}' updated.",
        "path": str(existing["path"]),
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    # GRV-009 E6b C2 — managed (installed) skills are not editable here.
    managed_refusal = _managed_edit_refusal(name)
    if managed_refusal is not None:
        return managed_refusal

    skill_dir = existing["path"]

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    # Use the same fuzzy matching engine as the file patch tool.
    # This handles whitespace normalization, indentation differences,
    # escape sequences, and block-anchor matching — saving the agent
    # from exact-match failures on minor formatting mismatches.
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # Show a short preview of the file so the model can self-correct
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content  # for rollback
    _atomic_write_text(target, new_content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        _atomic_write_text(target, original_content)
        return {"success": False, "error": scan_error}

    # GRV-009 E6b C2 — a SKILL.md body change refines the record + re-hashes.
    # Supporting-file patches (file_path set) don't change the skill body.
    if not file_path:
        _refine_record_after_edit(name, new_content)
    return {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
    }


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # Validate absorbed_into target when declared non-empty
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)
    shutil.rmtree(skill_dir)

    # Clean up empty category directories (don't remove the skills root itself)
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    # GRV-009 E6b C2 — deprecate the record (terminal-graceful; the record
    # persists with its inline body, hidden from the index). Do NOT hard-remove
    # it. Best-effort; only fires for record-backed ACTIVE skills.
    try:
        from grove.capability import LifecycleState
        from grove.capability_registry import (
            skill_record_id_for_name, transition_record,
        )

        cap_id = skill_record_id_for_name(name)
        if cap_id is not None:
            transition_record(
                cap_id, LifecycleState.DEPRECATED, actor="agent",
                reason="skill deleted",
            )
    except Exception:
        logger.warning(
            "deprecate-transition failed for skill %r (body removed)", name,
            exc_info=True,
        )

    message = f"Skill '{name}' deleted."
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        message += f" Content absorbed into '{absorbed_into.strip()}'."

    return {
        "success": True,
        "message": message,
    }


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found. Create it first with action='create'."}

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    # Back up for rollback
    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write_text(target, file_content)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(target, original_content)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        # List what's actually there for the model to see
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


# =============================================================================
# Main entry point
# =============================================================================

def skill_manage(
    action: str,
    name: str,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
    soul_alignment: str = None,
    tension_note: str = None,
    goals_served: list = None,
    lineage: list = None,
) -> str:
    """
    Manage user-created skills. Dispatches to the appropriate action handler.

    Returns JSON string with results.
    """
    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(
            name, content, category,
            soul_alignment=soul_alignment,
            tension_note=tension_note,
            goals_served=goals_served,
            lineage=lineage,
        )

    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(name, content)

    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(name, old_string, new_string, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, absorbed_into=absorbed_into)

    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file(name, file_path)

    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

    if result.get("success"):
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass
        # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
        # that mutate an existing skill's guidance), drop the record on delete.
        # Only mark a skill as agent-created when the background self-improvement
        # review fork creates it — foreground `skill_manage(create)` calls are
        # user-directed, and those skills belong to the user (the curator must
        # not touch them). Best-effort; telemetry failures never break the tool.
        try:
            from tools.skill_usage import bump_patch, forget, mark_agent_created
            from tools.skill_provenance import is_background_review
            if action == "create":
                if is_background_review():
                    mark_agent_created(name)
            elif action in {"patch", "edit", "write_file", "remove_file"}:
                bump_patch(name)
            elif action == "delete":
                forget(name)
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Manage skills (create, update, delete). Skills are reusable tools and "
        "procedures you build for the operator — on request, or when you spot a "
        "repeatable workflow worth keeping. "
        f"New skills go to {display_hermes_home()}/skills/; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when the operator asks you to build a skill — ANY complexity, "
        "including simple ones. Scaffold it immediately with action='create'; "
        "do NOT gate on task size and do NOT ask 'want to save this?' first. "
        "You may also propose a skill on your own when you notice a repeatable "
        "workflow that would help. Every new skill lands in quarantine "
        f"({display_hermes_home()}/skills/.andon/) where the operator approves it "
        "by trying it — so the tool itself never gatekeeps creation.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples.\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                )
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                )
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                )
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'). Can be empty string "
                    "to delete the matched text."
                )
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                )
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                )
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'."
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so the curator can "
                    "tell consolidation from pruning without guessing. "
                    "Pass the umbrella skill name when this skill's content "
                    "was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and "
                    "being pruned with no forwarding target. Omitting the arg "
                    "on delete is supported for backward compatibility but "
                    "downstream tooling (e.g. cron-job skill reference "
                    "rewriting) will have to guess at intent."
                )
            },
            "soul_alignment": {
                "type": "string",
                "enum": ["aligned", "neutral", "tension"],
                "description": (
                    "For 'create' by the skill curator only — your "
                    "assessment of how the new skill fits the operator's "
                    "declared identity (see the OPERATOR IDENTITY preamble). "
                    "'aligned': fits the identity and serves a current goal. "
                    "'neutral': useful but not identity-specific. "
                    "'tension': in tension with a declared boundary or "
                    "refusal. Omit on a foreground create — the code "
                    "assesses alignment heuristically when this is absent."
                )
            },
            "tension_note": {
                "type": "string",
                "description": (
                    "For 'create' with soul_alignment='tension' — one or "
                    "two sentences naming the conflict so the operator can "
                    "weigh it. Omit otherwise."
                )
            },
            "goals_served": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For 'create' by the skill curator only — the "
                    "operator's current goals (quoted from <current_goals>) "
                    "that this skill advances. Empty or omitted if none."
                )
            },
            "lineage": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For 'create' — names of other skills this one "
                    "composes with, depends on, or extends. Omit or leave "
                    "empty if the skill stands alone."
                )
            },
        },
        "required": ["action", "name"],
    },
}


# --- Registry ---
from tools.registry import tool_error
def register(reg):
    """Sprint 53 — Dispatcher-driven registration entrypoint."""
    reg.register(
        name="skill_manage",
        toolset="skills",
        schema=SKILL_MANAGE_SCHEMA,
        handler=lambda args, **kw: skill_manage(
            action=args.get("action", ""),
            name=args.get("name", ""),
            content=args.get("content"),
            category=args.get("category"),
            file_path=args.get("file_path"),
            file_content=args.get("file_content"),
            old_string=args.get("old_string"),
            new_string=args.get("new_string"),
            replace_all=args.get("replace_all", False),
            absorbed_into=args.get("absorbed_into"),
            soul_alignment=args.get("soul_alignment"),
            tension_note=args.get("tension_note"),
            goals_served=args.get("goals_served"),
            lineage=args.get("lineage")),
        emoji="📝",
    )
