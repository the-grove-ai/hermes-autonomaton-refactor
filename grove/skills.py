"""Grove skills helpers — quarantine writes and Grove frontmatter generation.

Per ``docs/design/andon-design-v1.md`` (Sprint 05): agent-authored skills
land in ``~/.grove/skills/.andon/<skill-name>/`` (the quarantine), not in
the active skills directory. The operator promotes via the ``sovereignty``
CLI after reviewing the scan verdict captured in frontmatter.

This module provides the file-write primitives and the Grove frontmatter
schema. Sprint 06a's modified ``_create_skill`` calls into it; the
``grove/sovereignty.py`` CLI uses it on the promotion side.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


ANDON_DIRNAME = ".andon"
ARCHIVE_DIRNAME = ".archive"

# Soul-alignment tags for proposal frontmatter (Sprint 14 D2).
SOUL_ALIGNMENT_TAGS = ("aligned", "neutral", "tension")


# ----- path helpers ----------------------------------------------------------

def skills_dir() -> Path:
    """Return the active skills directory (``~/.grove/skills/`` by default)."""
    return get_hermes_home() / "skills"


def andon_dir() -> Path:
    """Return the quarantine directory (``~/.grove/skills/.andon/``)."""
    return skills_dir() / ANDON_DIRNAME


def archive_dir() -> Path:
    """Return the archive directory (``~/.grove/skills/.archive/``)."""
    return skills_dir() / ARCHIVE_DIRNAME


def proposal_path(skill_name: str) -> Path:
    """Path to ``~/.grove/skills/.andon/<skill_name>/`` (may not yet exist)."""
    return andon_dir() / skill_name


def active_path(skill_name: str) -> Path:
    """Path to ``~/.grove/skills/<skill_name>/`` (may not yet exist)."""
    return skills_dir() / skill_name


def archive_path(skill_name: str, when: Optional[datetime] = None) -> Path:
    """Path to ``~/.grove/skills/.archive/<skill_name>-<timestamp>/``."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return archive_dir() / f"{skill_name}-{stamp}"


# ----- operator identity -----------------------------------------------------

def operator_email() -> str:
    """Return the operator's email from ``GROVE_OPERATOR_EMAIL`` or ``"unknown"``.

    Per Sprint 05 design D5: if unset, warn and record ``"unknown"`` —
    do NOT block. The operator can choose anonymity; the decision is still
    logged.
    """
    val = os.environ.get("GROVE_OPERATOR_EMAIL", "").strip()
    if not val:
        logger.warning(
            "GROVE_OPERATOR_EMAIL not set; recording operator as 'unknown'"
        )
        return "unknown"
    return val


# ----- time helpers ----------------------------------------------------------

def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- frontmatter manipulation ----------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse a SKILL.md string into (frontmatter_dict, body).

    Raises ValueError if the content has no leading YAML frontmatter block.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError("SKILL.md content has no YAML frontmatter block")
    fm_text = match.group(1)
    body = match.group(2)
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError("SKILL.md frontmatter did not parse to a mapping")
    return fm, body


def serialize_frontmatter(frontmatter: dict, body: str) -> str:
    """Render (frontmatter, body) back into a SKILL.md string."""
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{fm_text}\n---\n{body}"


def _normalize_soul_alignment(value: Optional[str]) -> str:
    """Return a valid soul-alignment tag (Sprint 14 D2).

    ``None`` defaults to ``neutral`` — an un-assessed proposal genuinely
    is neutral. An unrecognized non-None value is a real problem: log it
    loudly and normalize to ``neutral`` rather than write garbage into
    the operator's review queue.
    """
    if value is None:
        return "neutral"
    if isinstance(value, str) and value.strip().lower() in SOUL_ALIGNMENT_TAGS:
        return value.strip().lower()
    logger.warning(
        "[skills] invalid soul_alignment %r; normalizing to 'neutral'", value
    )
    return "neutral"


def stamp_proposal_frontmatter(
    content: str,
    *,
    scan_verdict: str = "safe",
    scan_findings: Optional[list] = None,
    soul_alignment: Optional[str] = None,
    tension_note: Optional[str] = None,
    goals_served: Optional[list] = None,
    tier: Optional[str] = None,
    register: Optional[str] = None,
    lineage: Optional[list] = None,
) -> str:
    """Add Grove proposal fields to SKILL.md's YAML frontmatter.

    Idempotent on the additive fields — re-stamping rewrites ``proposed_at``
    and the provenance scan / soul-alignment results but preserves the
    operator's existing name/description.

    ``soul_alignment`` / ``tension_note`` / ``goals_served`` are the
    Sprint 14 identity fields (D6): every proposal carries all three so
    ``hermes andon diff`` always shows identity context. An un-assessed
    proposal defaults to ``soul_alignment: neutral``, ``tension_note:
    null``, ``goals_served: []``.

    ``tier`` / ``register`` / ``lineage`` are the Grove provenance
    fields: which cognitive tier authored the proposal, the operator's
    communication register from soul.md, and the skills this one
    composes with. All optional — ``tier`` / ``register`` default to
    ``null`` when unavailable, ``lineage`` to ``[]``.
    """
    fm, body = parse_frontmatter(content)
    fm["created_by"] = "autonomaton"
    fm["proposed_at"] = utc_now_iso()
    fm["zone"] = "yellow"
    fm["tier"] = tier
    fm["register"] = register
    fm["lineage"] = list(lineage or [])

    provenance = fm.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["created_by"] = "autonomaton"
    provenance["scan_verdict"] = scan_verdict
    provenance["scan_findings"] = list(scan_findings or [])
    provenance["soul_alignment"] = _normalize_soul_alignment(soul_alignment)
    provenance["tension_note"] = (
        tension_note.strip()
        if isinstance(tension_note, str) and tension_note.strip()
        else None
    )
    provenance["goals_served"] = list(goals_served or [])
    fm["provenance"] = provenance

    return serialize_frontmatter(fm, body)


def stamp_promotion_frontmatter(
    content: str,
    *,
    operator: str,
    promoted_at: Optional[str] = None,
) -> str:
    """Add Grove promotion fields to SKILL.md's YAML frontmatter.

    Sets ``promoted_at``, flips ``zone`` to ``green``, and records
    ``provenance.approved_by``. Leaves the proposal-time fields intact.
    """
    fm, body = parse_frontmatter(content)
    fm["promoted_at"] = promoted_at or utc_now_iso()
    fm["zone"] = "green"

    provenance = fm.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["approved_by"] = operator
    fm["provenance"] = provenance

    return serialize_frontmatter(fm, body)


def strip_promotion_frontmatter(content: str) -> str:
    """Revert promotion: clear ``promoted_at``, set ``zone: yellow``, drop ``approved_by``.

    Used by ``sovereignty revoke`` to move an active skill back to ``.andon/``.
    """
    fm, body = parse_frontmatter(content)
    fm.pop("promoted_at", None)
    fm["zone"] = "yellow"
    provenance = fm.get("provenance") or {}
    if isinstance(provenance, dict):
        provenance.pop("approved_by", None)
        fm["provenance"] = provenance
    return serialize_frontmatter(fm, body)


def append_promotion_history(
    content: str,
    *,
    action: str,
    operator: str,
    timestamp: Optional[str] = None,
) -> str:
    """Append a sovereignty action to SKILL.md's ``promotion_history``.

    Each entry — ``action`` / ``timestamp`` / ``operator`` — mirrors the
    ``sovereignty_decision`` telemetry event, so a skill that has been
    promoted, revoked, and re-promoted carries the full audit trail.
    ``sovereignty promote`` and ``revoke`` call this; ``reject`` does not
    — it deletes the skill, leaving the telemetry event as the only
    record. The list is created on first append.
    """
    fm, body = parse_frontmatter(content)
    history = fm.get("promotion_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "action": action,
            "timestamp": timestamp or utc_now_iso(),
            "operator": operator,
        }
    )
    fm["promotion_history"] = history
    return serialize_frontmatter(fm, body)


# ----- proposal write --------------------------------------------------------

def write_proposal(skill_name: str, content: str) -> Path:
    """Write a SKILL.md proposal to ``~/.grove/skills/.andon/<skill_name>/``.

    Creates the quarantine directory tree on demand. Overwrites an existing
    proposal of the same name (re-proposal is allowed; promotion is the
    irreversible step). Returns the proposal directory.

    Does NOT scan, stamp frontmatter, or copy supporting files — those are
    the caller's responsibility. This is a pure write primitive.
    """
    dest = proposal_path(skill_name)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(content, encoding="utf-8")
    return dest


# ----- soul-alignment heuristic (Sprint 14 Phase 1.5) ------------------------

# Words a goal or refusal shares with almost any skill — too generic to
# signal real alignment. Pruned before keyword matching.
_ALIGNMENT_STOPWORDS = frozenset({
    "this", "that", "with", "from", "into", "your", "what", "when",
    "will", "have", "find", "keep", "make", "more", "than", "then",
    "they", "them", "over", "such", "also", "just", "like", "some",
    "each", "both", "skill", "skills", "system", "work", "using",
    "used", "able", "need", "want", "help",
})


def _alignment_keywords(phrase: str) -> set:
    """Significant lowercased words of a phrase — len > 3, minus stopwords."""
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return {w for w in words if len(w) > 3 and w not in _ALIGNMENT_STOPWORDS}


def _split_goal_lines(goals_text: Optional[str]) -> list:
    """Extract goal statements from goals.md — markdown bullet items, with
    wrapped continuation lines joined. Bracketed template placeholders
    (e.g. '[fill in ...]') are skipped."""
    if not goals_text:
        return []
    goals: list = []
    current: Optional[list] = None
    for line in goals_text.splitlines():
        stripped = line.strip()
        bullet = re.match(r"[-*]\s+(.+)", stripped)
        if bullet:
            if current:
                goals.append(" ".join(current))
            current = [bullet.group(1).strip()]
        elif current is not None and stripped and not stripped.startswith("#"):
            current.append(stripped)  # continuation of the current bullet
        else:
            if current:
                goals.append(" ".join(current))
            current = None
    if current:
        goals.append(" ".join(current))
    return [g for g in goals if g and not (g.startswith("[") and g.endswith("]"))]


def assess_soul_alignment(skill_name: str, description: str) -> tuple:
    """Heuristically assess a proposed skill against the operator's identity.

    For agent-created skills outside the Curator review cycle (Sprint 14
    Phase 1.5): the agent does not assess soul-alignment, so the code
    does — the operator sees identity metadata on ``hermes andon diff``
    immediately, not after a 7-day Curator cycle.

    Keyword match of the skill's name and description against the
    operator's declared goals (goals.md) and refusals (soul.md
    frontmatter):

      - overlaps a declared refusal   -> ("tension", <note>, goals_served)
      - else overlaps a declared goal -> ("aligned", None, goals_served)
      - else                          -> ("neutral", None, [])

    Returns ``(soul_alignment, tension_note, goals_served)``. On any
    identity-load failure: a loud log, then ``("neutral", None, [])`` —
    the commanded graceful degradation (Sprint 14 PC6).
    """
    from grove.identity import load_identity  # local: breaks an import cycle

    try:
        identity = load_identity()
    except Exception as exc:
        logger.warning(
            "[skills] identity unavailable; proposal '%s' tagged "
            "soul_alignment=neutral. Cause: %r",
            skill_name, exc,
        )
        return "neutral", None, []

    skill_words = _alignment_keywords(f"{skill_name} {description}")

    goals_served = [
        goal for goal in _split_goal_lines(identity.goals)
        if _alignment_keywords(goal) & skill_words
    ]

    refusals = identity.frontmatter.get("refusals")
    if isinstance(refusals, list):
        for refusal in refusals:
            if isinstance(refusal, str) and _alignment_keywords(refusal) & skill_words:
                note = (
                    "The skill overlaps the operator's declared refusal: "
                    f'"{refusal.strip()}". Surfaced for the operator to '
                    "weigh — not suppressed."
                )
                return "tension", note, goals_served

    if goals_served:
        return "aligned", None, goals_served
    return "neutral", None, goals_served
