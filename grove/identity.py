"""Grove identity composition — Atlas-pattern layered identity.

Sprint 07 (persona-soul-retrofit-v1). Composes the system prompt's
identity layer from up to six files in ``~/.grove/``:

    constitution.md  — sovereignty guardrails in prose      [Jidoka]
    soul.md          — voice, personality, thinking style   [Jidoka]
    operator.md      — operator context and preferences     [graceful]
    goals.md         — current objectives                   [graceful]
    memory.md        — corrections, patterns learned        [graceful]
    agents.md        — multi-agent config                   [silent skip]

Tiered failure (the Atlas pattern): the two Jidoka-tier files hard-fail
if missing — the Autonomaton refuses to start without sovereignty
guardrails (constitution) or an identity (soul). The graceful-tier
files log a warning and composition continues without them. agents.md
is a silent skip.

First-run: missing constitution / soul / operator / goals files are
seeded from ``config/identity/`` BEFORE the tiered-failure check, so a
fresh install always has its Jidoka-tier files. The hard-fail only
fires if the operator copy AND the reference template are both absent.

Backward compatibility: soul.md falls back to ``SOUL.md``, operator.md
to ``USER.md``, memory.md to ``MEMORY.md``, agents.md to ``AGENTS.md``
— existing Hermes installs keep working without a forced migration.

Composition order (Sprint 07 design D4): constitution → soul →
operator → goals → memory → agents. Governance constrains identity;
identity constrains context; context constrains learned material.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from grove.skills import parse_frontmatter

logger = logging.getLogger(__name__)


# (canonical name, legacy fallback, reference template or None, tier)
# tier ∈ {"jidoka", "graceful", "silent"}
_IDENTITY_FILES: list[tuple[str, Optional[str], Optional[str], str]] = [
    ("constitution.md", None,        "constitution.md", "jidoka"),
    ("soul.md",         "SOUL.md",   "soul.md",         "jidoka"),
    ("operator.md",     "USER.md",   "operator.md",     "graceful"),
    ("goals.md",        None,        "goals.md",        "graceful"),
    ("memory.md",       "MEMORY.md", None,              "graceful"),
    ("agents.md",       "AGENTS.md", None,              "silent"),
]


class IdentityError(RuntimeError):
    """A Jidoka-tier identity file (constitution / soul) is missing and could
    not be seeded — the Autonomaton must not start. Fail loud."""


@dataclass
class IdentityComposition:
    """The composed identity layer.

    Each field holds the file's stripped content (str), or None if the file
    was absent. ``frontmatter`` is the parsed YAML frontmatter from soul.md
    (empty dict when soul.md has none — frontmatter is optional per D5).
    """

    constitution: Optional[str] = None
    soul: Optional[str] = None
    operator: Optional[str] = None
    goals: Optional[str] = None
    memory: Optional[str] = None
    agents: Optional[str] = None
    frontmatter: dict = field(default_factory=dict)

    def compose(self) -> str:
        """Assemble all six layers in D4 precedence order.

        constitution → soul → operator → goals → memory → agents.
        Absent layers are skipped. Returns the joined prompt text.
        """
        layers = [
            self.constitution, self.soul, self.operator,
            self.goals, self.memory, self.agents,
        ]
        return "\n\n".join(p.strip() for p in layers if p and p.strip())

    def compose_stable(self) -> str:
        """Assemble only the four stable-tier layers (constitution → soul →
        operator → goals).

        Sprint 07 injects this subset into the system prompt's stable tier;
        memory and agents keep their existing delivery mechanisms (the
        MemoryStore volatile tier and the context-files prompt). Returns the
        joined prompt text.
        """
        layers = [self.constitution, self.soul, self.operator, self.goals]
        return "\n\n".join(p.strip() for p in layers if p and p.strip())


def load_identity(persona: Optional[str] = None) -> IdentityComposition:
    """Load and compose the operator's identity from ``~/.grove/``.

    Args:
        persona: v0.1 accepts only ``None`` (the flat-file layout). A string
            would select ``~/.grove/identity/<persona>/`` — the v0.1.5
            multi-persona path. The signature is forward-compatible; the
            multi-persona resolution is not implemented.

    Returns:
        An IdentityComposition with the six content fields populated where
        files exist, plus the parsed soul.md frontmatter.

    Raises:
        NotImplementedError: if ``persona`` is not None.
        IdentityError: if constitution.md or soul.md is missing/empty and the
            reference template needed to seed it is also absent.
    """
    if persona is not None:
        raise NotImplementedError(
            "Multi-persona identity (persona=<name>) is a v0.1.5 feature. "
            "v0.1 composes a single identity from the flat ~/.grove/ files. "
            "See https://the-grove.ai/standards/001"
        )

    home = get_hermes_home()
    ref_dir = _reference_dir()
    composition = IdentityComposition()

    for canonical, legacy, template, tier in _IDENTITY_FILES:
        content = _resolve_file(home, canonical, legacy, template, ref_dir, tier)
        setattr(composition, canonical.removesuffix(".md"), content)

    # Parse soul.md's optional YAML frontmatter (D5). Reuses
    # grove.skills.parse_frontmatter — same format as SKILL.md, no conflict
    # (Andon A5 does not fire). A soul.md without frontmatter is valid and
    # SPEC-expected: the prose body is the primary content.
    if composition.soul:
        try:
            fm, _body = parse_frontmatter(composition.soul)
            composition.frontmatter = fm
        except ValueError:
            logger.debug(
                "[identity] soul.md has no YAML frontmatter; "
                "using prose body only (frontmatter is optional per D5)"
            )
            composition.frontmatter = {}

    return composition


# ----- internals -------------------------------------------------------------

def _reference_dir() -> Path:
    """Return ``config/identity/`` in the repo — the first-run template source."""
    return Path(__file__).resolve().parent.parent / "config" / "identity"


def _resolve_file(
    home: Path,
    canonical: str,
    legacy: Optional[str],
    template: Optional[str],
    ref_dir: Path,
    tier: str,
) -> Optional[str]:
    """Resolve one identity file, then apply the tier's missing-file policy.

    Resolution order: canonical name → legacy name → seed from reference
    template. If all three miss, the tier decides: jidoka raises, graceful
    warns and returns None, silent returns None.
    """
    content = _resolve_raw(home, canonical, legacy, template, ref_dir)
    if content:
        return content

    if tier == "jidoka":
        raise IdentityError(
            f"Jidoka-tier identity file '{canonical}' is missing or empty "
            f"and could not be seeded — no operator copy at {home / canonical} "
            f"and no reference template at {ref_dir / (template or canonical)}. "
            f"The Autonomaton will not start without it. "
            f"See https://the-grove.ai/standards/001"
        )
    if tier == "graceful":
        logger.warning(
            "[identity] %s is missing; composing without it (graceful-tier).",
            canonical,
        )
    # silent tier: return None with no log
    return None


def _resolve_raw(
    home: Path,
    canonical: str,
    legacy: Optional[str],
    template: Optional[str],
    ref_dir: Path,
) -> Optional[str]:
    """Find content for one identity file: canonical → legacy → seed.

    Returns the file content (str) or None if no source resolves.
    """
    canonical_path = home / canonical
    if canonical_path.exists():
        return _read(canonical_path)

    if legacy is not None:
        legacy_path = home / legacy
        if legacy_path.exists():
            logger.info(
                "[identity] %s not found; using legacy %s (backward compat)",
                canonical, legacy,
            )
            return _read(legacy_path)

    if template is not None:
        ref_path = ref_dir / template
        if ref_path.exists():
            home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ref_path, canonical_path)
            logger.info("[identity] seeded %s from %s", canonical, ref_path)
            return _read(canonical_path)

    return None


def _read(path: Path) -> Optional[str]:
    """Read a file; return stripped content, or None if empty or unreadable."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("[identity] could not read %s: %r", path, exc)
        return None
    return content or None
