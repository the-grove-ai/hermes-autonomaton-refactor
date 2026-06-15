"""Grove sovereignty — operator-facing skill quarantine review.

Implements the andon promote / reject / revoke workflow defined in
``docs/design/andon-design-v1.md``. The operator interacts via the CLI
(``hermes andon <verb>``); the verb names the *mechanism* (the Andon line),
while this module retains the broader ``sovereignty`` name because it
encapsulates the sovereignty discipline — zone-classifier results,
quarantine moves, and telemetry — not just CLI plumbing.

Every promote / reject / revoke is a deliberate sovereignty act and writes
a structured ``sovereignty_decision`` event. There is no ``--all`` flag.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
from typing import Any, Optional

from grove.skills import (
    active_path,
    andon_dir,
    append_promotion_history,
    archive_path,
    operator_email,
    parse_frontmatter,
    proposal_path,
    stamp_promotion_frontmatter,
    strip_promotion_frontmatter,
)
from grove.telemetry import log_sovereignty_decision

logger = logging.getLogger(__name__)


# ----- helpers ---------------------------------------------------------------

def _sha256_short(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _record_for_proposal(skill_name: str) -> Optional[str]:
    """The capability-record id governing *skill_name*, or None — a legacy pre-C2
    .andon proposal / external skill with no record (GRV-009 E6b C2)."""
    from grove.capability_registry import skill_record_id_for_name

    return skill_record_id_for_name(skill_name)


def _govern_transition(cap_id: Optional[str], to_state, *, actor: str, reason: str, verb: str) -> None:
    """GRV-009 E6b C2 (A6) — STATE-FIRST gate for a sovereignty file move.

    The record transition executes FIRST; the caller performs the physical
    ``.andon/`` move ONLY after this returns (i.e. on APPLIED). DEFERRED (lock
    contended by a concurrent write) and SKIPPED (illegal edge) both RAISE, so
    nothing moves and no partial state is left. A record-less legacy proposal
    (``cap_id is None``) is a no-op here — the caller falls back to a file move.
    """
    if cap_id is None:
        return
    from grove.capability_registry import (
        TRANSITION_APPLIED,
        TRANSITION_DEFERRED,
        transition_record,
    )

    result = transition_record(cap_id, to_state, actor=actor, reason=reason)
    if result.status == TRANSITION_DEFERRED:
        raise RuntimeError(
            f"andon {verb}: record {cap_id!r} is locked by a concurrent write "
            f"(DEFERRED) — retry. NOTHING moved."
        )
    if result.status != TRANSITION_APPLIED:
        raise RuntimeError(
            f"andon {verb}: record {cap_id!r} is not in a {verb}-able state "
            f"(transition {result.status}). NOTHING moved."
        )


def _read_skill_md(skill_dir) -> tuple[str, dict, str]:
    """Return (raw_content, frontmatter_dict, scan_verdict) for a skill directory.

    Empty strings / dicts if SKILL.md or frontmatter is missing.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return "", {}, "unknown"
    content = skill_md.read_text(encoding="utf-8")
    try:
        fm, _body = parse_frontmatter(content)
    except ValueError:
        return content, {}, "unknown"
    verdict = (fm.get("provenance") or {}).get("scan_verdict", "unknown")
    return content, fm, verdict


# ----- core API --------------------------------------------------------------

def list_proposals() -> list[dict[str, Any]]:
    """Return metadata for every proposal currently in ``~/.grove/skills/.andon/``.

    Sorted by skill name. Each entry is a dict with ``name``, ``description``,
    ``proposed_at``, ``scan_verdict``, ``scan_findings``, ``path``.
    """
    root = andon_dir()
    if not root.exists():
        return []

    proposals: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            fm, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except ValueError as exc:
            logger.warning("Skipping malformed proposal %s: %s", skill_md, exc)
            continue
        provenance = fm.get("provenance") or {}
        proposals.append(
            {
                "name": fm.get("name", child.name),
                "description": fm.get("description", ""),
                "proposed_at": fm.get("proposed_at", ""),
                "scan_verdict": provenance.get("scan_verdict", "unknown"),
                "scan_findings": provenance.get("scan_findings", []),
                "path": str(child),
            }
        )
    return proposals


def _render_identity_alignment(content: str) -> str:
    """Render a proposal's soul-alignment metadata as a review block.

    Returns "" when the SKILL.md has no frontmatter or no soul_alignment
    field — e.g. a proposal created before Sprint 14.
    """
    try:
        fm, _ = parse_frontmatter(content)
    except ValueError:
        return ""
    provenance = fm.get("provenance") or {}
    if not isinstance(provenance, dict) or "soul_alignment" not in provenance:
        return ""
    lines = [
        "----- identity alignment -----",
        f"  soul_alignment: {provenance.get('soul_alignment')}",
    ]
    note = provenance.get("tension_note")
    if note:
        lines.append(f"  tension_note:   {note}")
    goals = provenance.get("goals_served") or []
    if goals:
        lines.append("  goals_served:")
        lines.extend(f"    - {g}" for g in goals)
    else:
        lines.append("  goals_served:   (none)")
    return "\n".join(lines)


def show_diff(skill_name: str) -> Optional[str]:
    """Return a proposal's identity-alignment summary, full SKILL.md text,
    and a list of supporting files.

    Returns ``None`` if no proposal by that name exists.
    """
    dest = proposal_path(skill_name)
    skill_md = dest / "SKILL.md"
    if not skill_md.exists():
        return None
    content = skill_md.read_text(encoding="utf-8")
    alignment = _render_identity_alignment(content)
    head = f"{alignment}\n\n{content}" if alignment else content
    supporting = sorted(
        p.relative_to(dest)
        for p in dest.rglob("*")
        if p.is_file() and p.name != "SKILL.md"
    )
    if supporting:
        files_block = "\n".join(f"  {p}" for p in supporting)
        return f"{head}\n\n----- supporting files -----\n{files_block}\n"
    return head


def promote(skill_name: str, replace: bool = False) -> dict[str, Any]:
    """Move a proposal from ``.andon/`` to active. Emits sovereignty_decision event.

    Raises ``FileNotFoundError`` if no proposal exists.
    Raises ``FileExistsError`` if an active skill of the same name exists and
    ``replace`` is False.
    """
    source = proposal_path(skill_name)
    dest = active_path(skill_name)

    if not source.exists():
        raise FileNotFoundError(
            f"No proposal '{skill_name}' in {andon_dir()}. "
            f"Run `hermes andon list` to see pending proposals."
        )

    if dest.exists():
        if not replace:
            raise FileExistsError(
                f"Active skill '{skill_name}' already exists at {dest}. "
                f"Use `--replace` to archive the existing version before promotion."
            )
        # Archive the existing active skill before overwriting it.
        archive = archive_path(skill_name)
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), str(archive))
        log_sovereignty_decision(
            action="archive",
            skill_name=skill_name,
            source_path=str(dest),
            dest_path=str(archive),
            reason="superseded by promotion --replace",
        )

    operator = operator_email()
    skill_md = source / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")

    # GRV-009 E6b C2 (A6) — STATE-FIRST. Transition the record proposed→active
    # BEFORE any file move. A legacy pre-C2 proposal has no record: mint one
    # (proposed) from the .andon body first, so it joins the record world and
    # reaches executable ACTIVE rather than stranding.
    cap_id = _record_for_proposal(skill_name)
    if cap_id is None:
        from grove.capability_registry import _frontmatter_value, register_proposed_skill

        register_proposed_skill(skill_name, _frontmatter_value(content, "category") or "", content)
        cap_id = _record_for_proposal(skill_name)
    from grove.capability import LifecycleState

    _govern_transition(
        cap_id, LifecycleState.ACTIVE, actor=operator,
        reason="andon promote", verb="promote",
    )  # raises on DEFERRED/SKIPPED — nothing moves below

    # Record is now truth (ACTIVE). Stamp + move the body as a consequence.
    promoted = stamp_promotion_frontmatter(content, operator=operator)
    promoted = append_promotion_history(
        promoted, action="promote", operator=operator
    )
    skill_md.write_text(promoted, encoding="utf-8")

    fm, _ = parse_frontmatter(promoted)
    verdict = (fm.get("provenance") or {}).get("scan_verdict", "unknown")
    skill_hash = _sha256_short(promoted)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(dest))
    except OSError as move_exc:
        # RECOVERY: the record is ACTIVE (truth) and carries the body inline, so
        # the skill is live regardless. The .andon file is stray — flag LOUD with
        # skill_id + path; no crash, reconcilable.
        logger.error(
            "andon promote: record APPLIED (skill_id=%s -> active) but the file "
            "move FAILED — the record is truth and the skill is live. Stray "
            ".andon body at %s (reconcile: move to %s). Cause: %r",
            cap_id, source, dest, move_exc,
        )
        return log_sovereignty_decision(
            action="promote", skill_name=skill_name, skill_hash=skill_hash,
            scan_verdict=verdict, operator=operator, source_path=str(source),
            dest_path=str(dest),
            reason="record APPLIED; file move failed — record is truth, stray file flagged",
        )

    return log_sovereignty_decision(
        action="promote",
        skill_name=skill_name,
        skill_hash=skill_hash,
        scan_verdict=verdict,
        operator=operator,
        source_path=str(source),
        dest_path=str(dest),
    )


def reject(skill_name: str, reason: Optional[str] = None) -> dict[str, Any]:
    """Delete a proposal from ``.andon/`` and emit sovereignty_decision event.

    Raises ``FileNotFoundError`` if no proposal exists.
    """
    source = proposal_path(skill_name)
    if not source.exists():
        raise FileNotFoundError(
            f"No proposal '{skill_name}' in {andon_dir()}."
        )

    content, _fm, verdict = _read_skill_md(source)
    skill_hash = _sha256_short(content) if content else ""
    operator = operator_email()

    # GRV-009 E6b C2 (A6) — STATE-FIRST. Transition proposed→rejected BEFORE
    # deleting the file. DEFERRED/SKIPPED raise → nothing deleted. Legacy
    # record-less proposals fall through to a plain file delete.
    from grove.capability import LifecycleState

    cap_id = _record_for_proposal(skill_name)
    _govern_transition(
        cap_id, LifecycleState.REJECTED, actor=operator,
        reason=reason or "andon reject", verb="reject",
    )

    try:
        shutil.rmtree(source)
    except OSError as exc:
        # RECOVERY: the record is REJECTED (truth, terminal). Stray .andon file
        # flagged LOUD; no crash.
        logger.error(
            "andon reject: record APPLIED (skill_id=%s -> rejected) but the file "
            "delete FAILED — the record is truth. Stray .andon body at %s. "
            "Cause: %r", cap_id, source, exc,
        )

    return log_sovereignty_decision(
        action="reject",
        skill_name=skill_name,
        skill_hash=skill_hash,
        scan_verdict=verdict,
        operator=operator,
        source_path=str(source),
        dest_path=None,
        reason=reason,
    )


def revoke(skill_name: str) -> dict[str, Any]:
    """Move an active skill back to ``.andon/`` for re-review.

    Raises ``FileNotFoundError`` if no active skill exists.
    Raises ``FileExistsError`` if a proposal of the same name is already
    pending (resolve it first).
    """
    source = active_path(skill_name)
    dest = proposal_path(skill_name)

    if not source.exists():
        raise FileNotFoundError(f"No active skill '{skill_name}' at {source}.")
    if dest.exists():
        raise FileExistsError(
            f"A proposal '{skill_name}' is already pending in .andon/. "
            f"Resolve it (`hermes andon promote` or `hermes andon reject`) "
            f"before revoking the active skill."
        )

    operator = operator_email()

    # GRV-009 E6b C2 (A6) — STATE-FIRST. Transition active→proposed BEFORE moving
    # the body back to .andon. After APPLIED the skill is non-executable (the 4.1
    # checkpoint refuses a proposed record), so the move is a consequence.
    # DEFERRED/SKIPPED raise → nothing moves.
    from grove.capability import LifecycleState

    cap_id = _record_for_proposal(skill_name)
    _govern_transition(
        cap_id, LifecycleState.PROPOSED, actor=operator,
        reason="andon revoke", verb="revoke",
    )

    skill_md = source / "SKILL.md"
    if skill_md.exists():
        content = skill_md.read_text(encoding="utf-8")
        try:
            reverted = strip_promotion_frontmatter(content)
            reverted = append_promotion_history(
                reverted, action="revoke", operator=operator
            )
            skill_md.write_text(reverted, encoding="utf-8")
        except ValueError:
            logger.warning(
                "SKILL.md at %s has no frontmatter — moving as-is", skill_md
            )

    content, _fm, verdict = _read_skill_md(source)
    skill_hash = _sha256_short(content) if content else ""

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(dest))
    except OSError as exc:
        # RECOVERY: the record is PROPOSED (truth, non-executable). The active
        # file is stray — flag LOUD; no crash.
        logger.error(
            "andon revoke: record APPLIED (skill_id=%s -> proposed, "
            "non-executable) but the file move FAILED — the record is truth. "
            "Stray active body at %s. Cause: %r", cap_id, source, exc,
        )
        return log_sovereignty_decision(
            action="revoke", skill_name=skill_name, skill_hash=skill_hash,
            scan_verdict=verdict, operator=operator, source_path=str(source),
            dest_path=str(dest),
            reason="record APPLIED; file move failed — record is truth, stray file flagged",
        )

    return log_sovereignty_decision(
        action="revoke",
        skill_name=skill_name,
        skill_hash=skill_hash,
        scan_verdict=verdict,
        operator=operator,
        source_path=str(source),
        dest_path=str(dest),
    )


# ----- CLI renderers ---------------------------------------------------------
# These are thin print/stderr wrappers used by hermes_cli/main.py::cmd_andon.
# They convert API exceptions into operator-friendly messages and exit codes.

def cli_list() -> None:
    proposals = list_proposals()
    if not proposals:
        print(f"No pending proposals in {andon_dir()}.")
        return

    print(f"Pending proposals in {andon_dir()}:\n")
    width = max((len(p["name"]) for p in proposals), default=4)
    for p in proposals:
        findings_n = len(p["scan_findings"])
        verdict = p["scan_verdict"]
        marker = (
            ""
            if verdict == "safe"
            else f"  [{verdict}, {findings_n} finding{'s' if findings_n != 1 else ''}]"
        )
        print(f"  {p['name']:<{width}}  proposed {p['proposed_at']}{marker}")
        if p["description"]:
            print(f"  {' ' * width}    {p['description']}")
    print(
        f"\n{len(proposals)} pending. "
        f"Review with `hermes andon diff <skill>` "
        f"or promote with `hermes andon promote <skill>`."
    )


def cli_diff(skill_name: str) -> None:
    result = show_diff(skill_name)
    if result is None:
        print(
            f"No proposal '{skill_name}' in {andon_dir()}. "
            f"Run `hermes andon list` to see pending proposals.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(result)


def cli_promote(skill_name: str, replace: bool = False) -> None:
    try:
        event = promote(skill_name, replace=replace)
    except (FileExistsError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(
        f"Promoted '{skill_name}'.\n"
        f"  From: {event['source_path']}\n"
        f"  To:   {event['dest_path']}"
    )


def cli_reject(skill_name: str, reason: Optional[str] = None) -> None:
    try:
        reject(skill_name, reason=reason)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Rejected '{skill_name}'. (reason: {reason or 'none'})")


def cli_revoke(skill_name: str) -> None:
    try:
        event = revoke(skill_name)
    except (FileExistsError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(
        f"Revoked '{skill_name}' back to .andon/ for re-review.\n"
        f"  From: {event['source_path']}\n"
        f"  To:   {event['dest_path']}"
    )
