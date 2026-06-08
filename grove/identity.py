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
#
# ``goals`` is NOT a file here. Sprint 69 retired the stale ~/.grove/goals.md
# in favor of the Dock (~/.grove/dock/dock.yaml) as the single source of
# truth for operator goals. The goals layer is rendered from the Dock by
# ``_render_dock_goals`` and assigned to ``composition.goals`` in
# ``load_identity`` after this loop runs.
_IDENTITY_FILES: list[tuple[str, Optional[str], Optional[str], str]] = [
    ("constitution.md", None,        "constitution.md", "jidoka"),
    ("soul.md",         "SOUL.md",   "soul.md",         "jidoka"),
    ("operator.md",     "USER.md",   "operator.md",     "graceful"),
    ("memory.md",       "MEMORY.md", None,              "graceful"),
    ("agents.md",       "AGENTS.md", None,              "silent"),
]


class IdentityError(RuntimeError):
    """A Jidoka-tier identity file (constitution / soul) is missing and could
    not be seeded — the Autonomaton must not start. Fail loud."""


# ── Sprint 75 — tier-aware identity composition ──────────────────────────
# Which identity layers ride each cognition tier. The always-on set
# (constitution = values/safety, soul = character, register = voice, goals =
# the Dock strategic index) rides EVERY tier — it is the irreducible Mylo.
# Heavier self-model / operator-context layers escalate with the tier:
#   T1 (irreducible) = constitution + soul + register + goals
#   T2 (medium)      = T1 + operator + capabilities
#   T3 (full)        = T2 + affordances
# An unknown / falsy tier returns None ⇒ FULL composition (legacy, safe — a
# tier we don't recognize never silently drops character).
_ALWAYS_LAYERS = ("constitution", "soul", "register", "goals")
_TIER_IDENTITY_LAYERS: dict[str, frozenset] = {
    "T1": frozenset(_ALWAYS_LAYERS),
    "T2": frozenset(_ALWAYS_LAYERS + ("operator", "capabilities")),
    "T3": frozenset(_ALWAYS_LAYERS + ("operator", "capabilities", "affordances")),
}


def _identity_layers_for_tier(tier: Optional[str]) -> Optional[frozenset]:
    """The layer-name set a tier admits, or ``None`` for the full set.

    ``None`` means "no gate" — the full composition. A falsy tier (legacy /
    non-routed) and an unrecognized tier both return ``None`` so character is
    never silently dropped on an unexpected tier.
    """
    if not tier:
        return None
    return _TIER_IDENTITY_LAYERS.get(tier)  # unknown tier -> None -> full


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

    def compose_stable(self, tier: Optional[str] = None) -> str:
        """Assemble the stable-tier layers in D5 (Sprint 23) order, gated by
        ``tier`` (Sprint 75).

        Sprint 07 injects this subset into the system prompt's stable
        tier; memory and agents keep their existing delivery mechanisms
        (the MemoryStore volatile tier and the context-files prompt).
        Sprint 23 inserts register / affordances / capabilities between
        soul and operator. The soul layer's YAML frontmatter is
        stripped — parsed into ``frontmatter`` (PL-2).

        Sprint 75 — ``tier`` selects which layers compose: T1 the irreducible
        set (constitution + soul + register + goals), T2 adds operator +
        capabilities, T3 adds affordances. ``None`` / unknown tier composes the
        full set (legacy). The D5 ORDER is preserved; gated layers are skipped
        in place. Returns the joined prompt text.
        """
        admit = _identity_layers_for_tier(tier)

        def _keep(name: str) -> bool:
            return admit is None or name in admit

        ordered = [
            ("constitution", self.constitution),
            ("soul", _strip_frontmatter(self.soul)),
            ("register", self.register_overlay),
            ("affordances", self.affordances),
            ("capabilities", self.capabilities),
            ("operator", self.operator),
            ("goals", self.goals),
        ]
        layers = [text for name, text in ordered if _keep(name)]
        return "\n\n".join(p.strip() for p in layers if p and p.strip())


def load_identity(
    persona: Optional[str] = None,
    *,
    session_register: Optional[str] = None,
    tier: Optional[str] = None,
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

    # Sprint 75 — which identity layers this tier admits (None ⇒ full).
    admit = _identity_layers_for_tier(tier)

    def _admits(name: str) -> bool:
        return admit is None or name in admit

    # NB: the loop variable below is the FILE's failure tier (jidoka/graceful/
    # silent) from _IDENTITY_FILES — named ``file_tier`` so it cannot shadow the
    # cognition-``tier`` parameter (Sprint 75).
    for canonical, legacy, template, file_tier in _IDENTITY_FILES:
        content = _resolve_file(home, canonical, legacy, template, ref_dir, file_tier)
        setattr(composition, canonical.removesuffix(".md"), content)

    # Sprint 75 — operator-context is gated out below T2 (T1 is irreducible).
    if not _admits("operator"):
        composition.operator = None

    # Goals come from the Dock, not a file (Sprint 69). Absent Dock →
    # graceful (None, layer skipped); malformed dock.yaml → fail loud
    # (load_dock raises ValueError, which propagates here by design — a
    # broken goals manifest must surface, not silently drop goals).
    composition.goals = _render_dock_goals()

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
    # is missing entirely (install incomplete). Sprint 75 — ALWAYS call
    # load_affordances so the Jidoka template check runs on every tier; null
    # the content when the tier doesn't admit it (T1/T2). The install-integrity
    # invariant must not weaken just because the layer is gated off.
    _affordances = load_affordances(home)
    composition.affordances = _affordances if _admits("affordances") else None

    # D2 introspection: composer-orchestrated per GATE-A. Read-only; the
    # helpers degrade to "(unavailable)" prose on read failures rather
    # than raising — introspection is reporting, not governance. Sprint 75 —
    # SKIP it entirely when the tier doesn't admit capabilities (T1): the live
    # enumeration is the one genuinely expensive per-turn op, so gating it off
    # is a real cost saving, not just a token saving.
    composition.capabilities = (
        introspect_capabilities() if _admits("capabilities") else None
    )

    return composition


# ----- internals -------------------------------------------------------------

def _render_dock_goals() -> Optional[str]:
    """Render the operator's active Dock goals as the identity goals layer.

    Sprint 69: the Dock (``~/.grove/dock/dock.yaml``) is the single source
    of truth for goals; the stale ``goals.md`` is retired. Reuses
    ``grove.dock`` so identity and the classifier read the same manifest.

    Returns the rendered goals prose, or ``None`` when there is nothing to
    compose (Dock not installed, or no active goals). A MALFORMED
    ``dock.yaml`` is NOT swallowed here — ``load_dock`` raises ``ValueError``
    and it propagates, per the Architectural Prime Directive (a broken
    goals manifest must fail loud, not silently drop goals).
    """
    from grove.dock import active_goals, load_dock  # local: avoid import cycle

    dock = load_dock()
    if dock is None:
        logger.warning(
            "[identity] no Dock manifest; composing without goals (graceful)."
        )
        return None
    goals = active_goals(dock)
    if not goals:
        logger.info("[identity] Dock has no active goals; goals layer omitted.")
        return None

    lines = [
        "# Goals",
        "",
        "The operator's active goals, from the Dock "
        "(~/.grove/dock/dock.yaml). Use them to understand what matters "
        "right now; when asked about goals, answer from these.",
        "",
    ]
    for g in goals:
        lines.append(f"- **{g.name}** [{g.vector} · {g.status}]")
        dod = " ".join(g.definition_of_done.split())
        if dod:
            lines.append(f"  Done when: {dod}")
    return "\n".join(lines)


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
    # Memory is the one identity file whose store lives in a subdirectory:
    # the memory subsystem writes ~/.grove/memories/MEMORY.md via
    # get_memory_dir(), not the ~/.grove root the other identity files use.
    # Resolve it through the substrate's own path function so identity and
    # the memory tool can never diverge again.
    if canonical == "memory.md":
        from tools.memory_tool import get_memory_dir  # local: avoid import cycle
        store_path = get_memory_dir() / "MEMORY.md"
        return _read(store_path) if store_path.exists() else None

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
