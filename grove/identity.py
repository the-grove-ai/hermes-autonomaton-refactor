"""Grove identity composition — Atlas-pattern layered identity.

Sprint 07 (persona-soul-retrofit-v1) seeded this module with the
Atlas pattern; Sprint 23 (soul-affordances-register-v1) extended it
with the register / affordances / capabilities layers.

Composes the system prompt's identity layer from operator files in
``~/.grove/`` plus runtime introspection:

    constitution.md         — sovereignty guardrails        [Jidoka]
    soul.md                 — voice, personality            [Jidoka]
    registers/<name>.md     — voice modulation overlay      [Jidoka if named]
    affordances.md          — capability landscape          [graceful]
    (live capabilities)     — introspected at session start [auto]
    operator.md             — operator context              [graceful]
    goals.md                — current objectives            [graceful]
    memory.md               — corrections, patterns         [graceful]
    agents.md               — multi-agent config            [silent skip]

Tiered failure (the Atlas pattern): the Jidoka-tier files hard-fail
if missing — the Autonomaton refuses to start without sovereignty
guardrails (constitution), an identity (soul), or a declared-but-
unresolvable register overlay. Standards Register's reference
template is a structural install requirement regardless of soul
reference (Sprint 23 D4). Graceful-tier files log a warning and
composition continues without them. agents.md is a silent skip.

First-run: missing constitution / soul / operator / goals /
affordances files are seeded from ``config/identity/`` BEFORE the
tiered-failure check, so a fresh install always has its Jidoka-tier
files. The hard-fail only fires if the operator copy AND the
reference template are both absent.

Backward compatibility: soul.md falls back to ``SOUL.md``, operator.md
to ``USER.md``, memory.md to ``MEMORY.md``, agents.md to ``AGENTS.md``
— existing Hermes installs keep working without a forced migration.
Sprint 23 D8 adds a one-entry synonym map for the legacy
``register: strategic-concise`` Sprint 07 value → ``operator``; see
``grove.register._SOUL_REGISTER_SYNONYMS``.

Composition order (Sprint 23 D5):
    constitution → soul → register → affordances → capabilities →
    operator → goals → memory → agents

Governance constrains identity; identity sets voice and capabilities;
context constrains learned material. The register overlay sits
between soul and affordances because it modulates voice (soul's
territory) but session-scoped; affordances sits between register and
operator because it modifies the action surface — what the soul, in
this register, can do.
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

    Sprint 23 additions:
        active_register: canonical register name post-synonym mapping
            (``standards`` | ``operator`` | ``editorial``), or None if
            soul.md omitted the field.
        register_overlay: register prose content, loaded from
            ``~/.grove/registers/<name>.md`` (operator copy preferred)
            or ``config/identity/registers/<name>.md``.
        affordances: static affordances.md content (operator-curated
            capability landscape). None if both operator copy and
            reference template are absent and the file is graceful.
        capabilities: live introspection block — connected MCPs, router
            tier bindings, slash command index, cellar status. Produced
            fresh at every ``load_identity()`` call.
    """

    constitution: Optional[str] = None
    soul: Optional[str] = None
    operator: Optional[str] = None
    goals: Optional[str] = None
    memory: Optional[str] = None
    agents: Optional[str] = None
    frontmatter: dict = field(default_factory=dict)
    # ── Sprint 23 (soul-affordances-register-v1) ───────────────────────
    active_register: Optional[str] = None
    register_overlay: Optional[str] = None
    affordances: Optional[str] = None
    capabilities: Optional[str] = None

    def compose(self) -> str:
        """Assemble all layers in the D5 (Sprint 23) precedence order.

        constitution → soul → register → affordances → capabilities →
        operator → goals → memory → agents. Absent layers are skipped.
        The soul layer's YAML frontmatter is stripped — it is parsed
        into ``frontmatter`` and must not also appear as prose (PL-2).
        Returns the joined prompt text.
        """
        layers = [
            self.constitution,
            _strip_frontmatter(self.soul),
            self.register_overlay,
            self.affordances,
            self.capabilities,
            self.operator,
            self.goals,
            self.memory,
            self.agents,
        ]
        return "\n\n".join(p.strip() for p in layers if p and p.strip())

    def compose_stable(self) -> str:
        """Assemble the stable-tier layers in D5 (Sprint 23) order.

        Sprint 07 injects this subset into the system prompt's stable
        tier; memory and agents keep their existing delivery mechanisms
        (the MemoryStore volatile tier and the context-files prompt).
        Sprint 23 inserts register / affordances / capabilities between
        soul and operator. The soul layer's YAML frontmatter is
        stripped — parsed into ``frontmatter`` (PL-2). Returns the
        joined prompt text.
        """
        layers = [
            self.constitution,
            _strip_frontmatter(self.soul),
            self.register_overlay,
            self.affordances,
            self.capabilities,
            self.operator,
            self.goals,
        ]
        return "\n\n".join(p.strip() for p in layers if p and p.strip())


def load_identity(
    persona: Optional[str] = None,
    *,
    session_register: Optional[str] = None,
) -> IdentityComposition:
    """Load and compose the operator's identity from ``~/.grove/``.

    Args:
        persona: v0.1 accepts only ``None`` (the flat-file layout). A string
            would select ``~/.grove/identity/<persona>/`` — the v0.1.5
            multi-persona path. The signature is forward-compatible; the
            multi-persona resolution is not implemented.
        session_register: Sprint 23 D6 — explicit session-overlay register
            name. When provided, it overrides the soul.md ``register:``
            frontmatter value for THIS composition only (the
            ``/register <name>`` slash command threads this through). The
            override goes through the same ``validate_soul_register``
            check as the soul value — unknown names raise. Passing
            ``None`` (the default) defers to the soul frontmatter.

    Returns:
        An IdentityComposition with all content fields populated where
        files / introspection succeed, plus parsed soul.md frontmatter
        and the canonical active register name.

    Raises:
        NotImplementedError: if ``persona`` is not None.
        IdentityError: if constitution.md or soul.md is missing/empty and
            the reference template needed to seed it is also absent; or
            if the Standards Register reference template is missing
            (Sprint 23 D4 — install-time canon check); or if
            ``session_register`` / soul ``register:`` names an
            unresolvable overlay; or if affordances reference template
            is missing and the operator copy is absent.
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

    # ── Sprint 23 (soul-affordances-register-v1) ─────────────────────────
    # Register overlay + static affordances + live capability introspection.
    # Imports are local to avoid module-load-time cycles — grove.register
    # imports IdentityError from this module, and grove.affordances pulls
    # in routing/config readers that themselves may import grove.identity
    # transitively in some session contexts.
    from grove.register import (
        load_register,
        validate_canon_present,
        validate_soul_register,
    )
    from grove.affordances import introspect_capabilities, load_affordances

    # D4 Jidoka — install-time check that runs unconditionally. Standards
    # Register is canon; broadcasts and bicameral nodes depend on it. If
    # the reference template is missing, the install is structurally
    # incomplete and we refuse to start regardless of which register the
    # soul references (or whether it references one at all).
    validate_canon_present()

    # D6 precedence: explicit session override > soul.frontmatter.register
    # > None (graceful — no register layer composed). BOTH paths go through
    # validate_soul_register so the D8 synonym mapping and the Jidoka
    # unknown-name check fire on either source. Empty/whitespace values
    # collapse to None inside the validator (graceful).
    if session_register:
        composition.active_register = validate_soul_register(
            session_register, home,
        )
    else:
        soul_register_raw = composition.frontmatter.get("register")
        composition.active_register = validate_soul_register(
            soul_register_raw, home,
        )

    if composition.active_register:
        composition.register_overlay = load_register(
            composition.active_register, home,
        )

    # D1 affordances: graceful for the operator copy (warn + None if
    # empty), Jidoka inside load_affordances if the reference template
    # is missing entirely (install incomplete).
    composition.affordances = load_affordances(home)

    # D2 introspection: composer-orchestrated per GATE-A. Read-only; the
    # helpers degrade to "(unavailable)" prose on read failures rather
    # than raising — introspection is reporting, not governance. See
    # grove/affordances.py module docstring for the asymmetry rationale.
    composition.capabilities = introspect_capabilities()

    return composition


# ----- internals -------------------------------------------------------------

def _strip_frontmatter(content: Optional[str]) -> Optional[str]:
    """Return *content* with any leading YAML frontmatter block removed.

    soul.md's frontmatter is parsed separately into
    IdentityComposition.frontmatter; emitting it again as prose in the
    composed prompt is PL-2 — the frontmatter would be injected twice.
    Reuses parse_frontmatter so the strip matches the parse exactly;
    content with no (or unparseable) frontmatter is returned unchanged.
    """
    if not content or not content.startswith("---"):
        return content
    try:
        _fm, body = parse_frontmatter(content)
    except ValueError:
        return content
    return body


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
