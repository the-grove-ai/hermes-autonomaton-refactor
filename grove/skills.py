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


def stamp_proposal_frontmatter(
    content: str,
    *,
    scan_verdict: str = "safe",
    scan_findings: Optional[list] = None,
) -> str:
    """Add Grove proposal fields to SKILL.md's YAML frontmatter.

    Idempotent on the additive fields — re-stamping rewrites ``proposed_at``
    and the provenance scan results but preserves the operator's existing
    name/description.
    """
    fm, body = parse_frontmatter(content)
    fm["created_by"] = "autonomaton"
    fm["proposed_at"] = utc_now_iso()
    fm["zone"] = "yellow"

    provenance = fm.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["created_by"] = "autonomaton"
    provenance["scan_verdict"] = scan_verdict
    provenance["scan_findings"] = list(scan_findings or [])
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
