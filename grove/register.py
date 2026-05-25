"""Register overlay loader for the Grove Autonomaton identity layer.

Sprint 23 (soul-affordances-register-v1) extends Sprint 07's tiered-
failure identity composition with the **register** overlay — a
session-scoped voice modulation. Three canonical registers ship in
v0.1: ``standards`` (broadcasts, bicameral nodes), ``operator``
(direct exchanges with the operator), ``editorial`` (ledger entries,
divergence registers).

The overlay sits between ``soul`` and ``affordances`` in the D5
composition order. It modulates voice within the soul's authority;
it does not replace the soul, and the operator's `/register` slash
command cannot hot-swap the entire identity (Sprint 23 D7 — Hermes
``/personality`` is anti-canon and explicitly rejected).

Failure tiers (Sprint 23 D4):
    * Soul declares ``register: <name>`` and the name is unknown
      → ``IdentityError`` (Jidoka).
    * Soul declares ``register:`` and the named template is missing
      from BOTH ``~/.grove/registers/<name>.md`` and
      ``config/identity/registers/<name>.md`` → ``IdentityError``
      (Jidoka).
    * ``config/identity/registers/standards.md`` is missing from the
      install regardless of any soul referencing it
      → ``IdentityError`` (Jidoka — Standards is canon).
    * Soul has no ``register:`` frontmatter field at all
      → ``None``; composition continues without the register layer
      (graceful — Sprint 07 installs without the field keep working).

Lookup precedence per D4: operator copy at
``~/.grove/registers/<name>.md`` overrides the reference template at
``config/identity/registers/<name>.md``. Same pattern as Sprint 07's
first-run seeding.

Synonym table (Sprint 23 D8 — bounded backward-compat):
    ``strategic-concise`` → ``operator``
    Strict bare-equality match, single entry. Unknown values still
    raise. One-time debug log per process. Scoped for one release;
    removed at v0.2. See ``_SOUL_REGISTER_SYNONYMS`` below.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from grove.identity import IdentityError

logger = logging.getLogger(__name__)


# ── Canon + synonyms ────────────────────────────────────────────────────────

#: The three registers that ship as reference templates in v0.1.
#: Operators may add registers via ``~/.grove/registers/<name>.md``;
#: ``list_registers()`` returns the union. This constant exists for
#: tests and documentation — the live availability check always goes
#: through ``list_registers()``.
CANON_REGISTERS = frozenset({"standards", "operator", "editorial"})

#: Bounded backward-compat synonym table (Sprint 23 D8).
#:
#: Strict bare-equality lookup. ONE entry. Sprint 07's reference
#: ``soul.md`` shipped with ``register: strategic-concise`` — that
#: phrase is canon-Grove under a different name. Map it to the
#: canonical ``operator`` register so operators who installed before
#: Sprint 23 do not hit Jidoka on their next session.
#:
#: This table is **NOT** a fallback for unknown values. Any value not
#: in this dict that also is not a real register raises
#: ``IdentityError`` per D4. The synonym is for the one named legacy
#: value only.
#:
#: Removal target: v0.2. By then operators have had at least one
#: release cycle to update their ``~/.grove/soul.md`` frontmatter to
#: ``register: operator``. When the table empties, this comment block
#: documents the migration that closed the surface.
_SOUL_REGISTER_SYNONYMS: dict[str, str] = {
    "strategic-concise": "operator",
}

# Track which synonym translations have already been logged so a long
# session does not emit the debug line every turn. Process-scoped, not
# session-scoped — a fresh process logs once and then quiets.
_synonym_logged: set[str] = set()


class RegisterError(IdentityError):
    """A register-layer failure. Subclasses ``IdentityError`` so existing
    Sprint 07 catch sites still handle it; distinct type when callers
    want to branch on register vs. constitution/soul failures."""


# ── Path resolution ────────────────────────────────────────────────────────


def _reference_registers_dir() -> Path:
    """Return ``config/identity/registers/`` in the repo — the first-run
    template source."""
    return (
        Path(__file__).resolve().parent.parent
        / "config" / "identity" / "registers"
    )


def _operator_registers_dir(home: Path) -> Path:
    """Return ``<home>/registers/`` — the operator's customisation dir.

    May not exist (graceful) — the operator never needs to create this
    directory unless they're customising a register beyond what ships
    in the reference templates.
    """
    return Path(home) / "registers"


# ── Install-time canon validation ──────────────────────────────────────────


def validate_canon_present() -> None:
    """Standards Register reference template must exist in the install.

    D4 Jidoka: Standards is canon — broadcasts and bicameral nodes
    depend on it. The install is structurally incomplete if the
    reference template is missing, regardless of whether the
    operator's soul references it.

    Called from ``load_identity()`` at session start so install errors
    surface immediately rather than lazily on first broadcast.
    """
    ref = _reference_registers_dir() / "standards.md"
    if not ref.exists():
        raise IdentityError(
            f"Standards Register reference template is missing at "
            f"{ref}. Standards is canon — the install is structurally "
            f"incomplete and the Autonomaton will not start. "
            f"See https://the-grove.ai/standards/001"
        )


# ── Listing + loading ──────────────────────────────────────────────────────


def list_registers(home: Path) -> list[str]:
    """Return the union of register names available from operator and
    reference paths.

    Operator-only registers (in ``~/.grove/registers/`` but not in
    the reference dir) appear here too — operators may add registers
    via that path. Returned names are sorted; duplicates collapse.
    """
    names: set[str] = set()
    op_dir = _operator_registers_dir(home)
    if op_dir.exists():
        for path in op_dir.glob("*.md"):
            names.add(path.stem)
    ref_dir = _reference_registers_dir()
    if ref_dir.exists():
        for path in ref_dir.glob("*.md"):
            names.add(path.stem)
    return sorted(names)


def load_register(name: str, home: Path) -> str:
    """Load the content of one register, operator copy preferred.

    Args:
        name: canonical register name (post-synonym mapping).
        home: operator's ``~/.grove/`` root.

    Returns:
        The register's prose content with leading/trailing whitespace
        stripped.

    Raises:
        IdentityError: if the named register resolves to neither the
            operator path nor the reference template (D4 Jidoka).
    """
    op_path = _operator_registers_dir(home) / f"{name}.md"
    if op_path.exists():
        content = _read(op_path)
        if content:
            return content

    ref_path = _reference_registers_dir() / f"{name}.md"
    if ref_path.exists():
        content = _read(ref_path)
        if content:
            return content

    raise IdentityError(
        f"Register '{name}' resolves to no file. Looked at {op_path} "
        f"and {ref_path}. Available registers: {list_registers(home)}. "
        f"See https://the-grove.ai/standards/001"
    )


# ── Soul frontmatter validation ────────────────────────────────────────────


def validate_soul_register(
    register_value: Optional[str],
    home: Path,
) -> Optional[str]:
    """Validate and canonicalise the soul.md ``register:`` field.

    Args:
        register_value: the raw value from soul.md frontmatter. May be
            ``None`` or empty — soul.md is allowed to omit the field.
        home: operator's ``~/.grove/`` root.

    Returns:
        The canonical register name (post-synonym mapping), or ``None``
        if the soul omitted the field (graceful — no register layer
        composes for that session).

    Raises:
        IdentityError: if a non-empty value is present but does not
            resolve to any available register, and is not in the
            synonym table (D4 Jidoka). The synonym table is checked
            FIRST so legacy Sprint 07 values are not seen as unknown.
    """
    if not register_value:
        return None
    name = str(register_value).strip()
    if not name:
        return None

    # D8 backward-compat: strict bare-equality synonym mapping. The
    # one entry in this table maps Sprint 07's reference-template
    # value (`strategic-concise`) to the canonical `operator`. Any
    # other value falls through to the availability check, which
    # raises if unknown — silent degradation is NOT what this is.
    if name in _SOUL_REGISTER_SYNONYMS:
        mapped = _SOUL_REGISTER_SYNONYMS[name]
        _log_synonym_once(name, mapped)
        name = mapped

    available = list_registers(home)
    if name not in available:
        raise IdentityError(
            f"soul.md declares register {name!r} but it resolves to no "
            f"template. Looked at "
            f"{_operator_registers_dir(home) / (name + '.md')} and "
            f"{_reference_registers_dir() / (name + '.md')}. "
            f"Available registers: {available}. "
            f"See https://the-grove.ai/standards/001"
        )
    return name


def _log_synonym_once(legacy_name: str, canonical_name: str) -> None:
    """Emit a process-scoped debug log the first time a synonym fires.

    A long-running session would otherwise see this on every turn.
    The flag lives at module scope and is never cleared — that's
    correct: a process picks up the legacy value once and quiets, and
    a fresh process gets one nudge to update soul.md.
    """
    if legacy_name in _synonym_logged:
        return
    _synonym_logged.add(legacy_name)
    logger.debug(
        "[register] Sprint 23 D8 backward-compat: mapping legacy "
        "register=%r to canonical %r. Update ~/.grove/soul.md to "
        "register: %s to silence this log. Synonym table is scoped "
        "for one release and will be removed at v0.2.",
        legacy_name, canonical_name, canonical_name,
    )


# ── Internals ──────────────────────────────────────────────────────────────


def _read(path: Path) -> Optional[str]:
    """Read a file; return stripped content, or None if empty/unreadable."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("[register] could not read %s: %r", path, exc)
        return None
    return content or None
