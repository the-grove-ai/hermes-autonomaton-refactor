"""Grove Capability Registry — load declarative Capability records (GRV-009 E2).

Loads every ``config/capabilities/*.yaml`` into a :class:`grove.capability.Capability`
under the E2 migration discipline (GRV-009 Amendment A3): **dry-run validation** —
full ``Capability`` construction at load time, so ``validate()`` fires on every
record before the Router can ever consume it. The Router must never discover a
validation error at runtime.

Fail-loud (Architectural Prime Directive): ANY unreadable / malformed / invalid
record raises :class:`CapabilityLoadError` naming the **filename + offending
field**. The load is all-or-nothing — a partial registry is never returned; one
bad file aborts the whole load.

This module is the loader only. It is consumed by its own tests in E2; the
per-turn disclosure hook that reads the registry lands in E2 commit 3.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional

import yaml

from grove.capability import (
    LEGAL_TRANSITIONS,
    Capability,
    LifecycleState,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

__all__ = [
    "CapabilityLoadError",
    "default_capabilities_dir",
    "load_capabilities",
    "transition_record",
    "TransitionResult",
    "TRANSITION_APPLIED",
    "TRANSITION_DEFERRED",
    "TRANSITION_SKIPPED",
    "register_installed_skill",
    "register_proposed_skill",
    "register_skills_in_tree",
    "skill_record_id_for_name",
    "skill_record_for_name",
    "SkillResolution",
    "resolve_skill_record",
    "scan_skill_slug_collisions",
    "set_skill_pinned",
    "update_lifecycle_fields",
]

logger = logging.getLogger(__name__)

# Process-level guard so the migration-coverage report (uncovered CONFIGURABLE_
# TOOLSETS keys) is logged once per distinct gap, not on every load_capabilities
# call (run_agent loads the registry several times per turn).
_reported_uncovered: set[FrozenSet[str]] = set()


class CapabilityLoadError(RuntimeError):
    """A capability record failed to load or validate.

    The message names the offending file and (via the wrapped validation error)
    the offending field.
    """


def default_capabilities_dir() -> Path:
    """The repo-default record directory: ``<repo>/config/capabilities``.

    Holds the version-controlled bundled records (the migrated 92, verbs, MCP).
    """
    return Path(__file__).resolve().parent.parent / "config" / "capabilities"


def grove_home_capabilities_dir() -> Path:
    """The machine-local record directory: ``<GROVE_HOME>/capabilities``.

    GRV-009 E6b C1 — installed/managed records (provenance:installed) mint HERE,
    not into the repo tree. GROVE_HOME is the real per-machine / per-profile
    boundary, so installed state is naturally machine-local and test-isolated
    (a tmp GROVE_HOME yields tmp mints). ``load_capabilities`` overlays this dir
    on the repo dir; the bundled records stay tracked in the repo.
    """
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "capabilities"


def _validate_binding_uniqueness(records: Dict[str, Capability]) -> None:
    """Strict 1:1 tool-to-record ownership (GRV-009 E5 Amendment A4).

    A collection-level post-load pass: scans every record's ``bindings.tools``
    and fails loud — naming both owning records and the colliding tool — if any
    tool name is claimed by two records. Inert until the C-BACKFILL / C-VERBS
    records populate bindings; the invariant exists from the schema commit so the
    resolution swap (C-RESOLVE) can trust single-owner attribution.
    """
    owner: Dict[str, str] = {}
    for rid in sorted(records):
        for tool in records[rid].bindings.tools:
            if tool in owner:
                raise CapabilityLoadError(
                    f"binding collision: tool {tool!r} is claimed by both "
                    f"{owner[tool]!r} and {rid!r} — A4 requires strict 1:1 "
                    f"tool-to-record ownership"
                )
            owner[tool] = rid


def _configurable_toolset_keys() -> FrozenSet[str]:
    """The known CONFIGURABLE_TOOLSETS keys, imported lazily.

    GRV-009 E5 C-SEAM4 — the import is deferred to call time (not module top) so
    the capability layer carries no import-time dependency on the CLI layer; no
    circular coupling. ``tools_config`` does not import the capability layer, so
    by the time the post-load pass runs both modules are fully resolved.
    """
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    return frozenset(key for key, *_ in CONFIGURABLE_TOOLSETS)


def _validate_toolset_keys(records: Dict[str, Capability]) -> FrozenSet[str]:
    """The D2<->D3 mutual check (GRV-009 E5 C-SEAM4) — ONE post-load pass.

    Two directions, two dispositions (per the locked design):

    * **record -> key (fail loud):** a record whose ``bindings.toolset_key`` is
      non-null but not a known CONFIGURABLE_TOOLSETS key is a binding to a
      phantom toolset — raise :class:`CapabilityLoadError` naming the record, the
      bad key, and the known set. (Hosted-MCP records carry ``toolset_key: null``
      and are skipped — they have no CONFIGURABLE_TOOLSETS key by design.)

    * **key -> record (reported):** a CONFIGURABLE_TOOLSETS key that no record
      yet governs is a migration-coverage gap (D4 verb backfill closes it), not a
      corruption — returned for the caller to report, never raised. Returning it
      (rather than logging here) keeps the pass pure and deterministically
      testable.
    """
    valid = _configurable_toolset_keys()
    governed: set[str] = set()
    for rid in sorted(records):
        tk = records[rid].bindings.toolset_key
        if tk is None:
            continue
        if tk not in valid:
            raise CapabilityLoadError(
                f"{rid}: bindings.toolset_key {tk!r} is not a known "
                f"CONFIGURABLE_TOOLSETS key — known: {sorted(valid)} "
                f"(defined in hermes_cli/tools_config.py::CONFIGURABLE_TOOLSETS)"
            )
        governed.add(tk)
    return valid - frozenset(governed)


def _report_uncovered_toolsets(uncovered: FrozenSet[str]) -> None:
    """Report (log once per distinct gap) the CONFIGURABLE_TOOLSETS keys that no
    capability record governs yet — the migration-coverage signal D4 drives to
    zero. Non-fatal by design (see :func:`_validate_toolset_keys`)."""
    if not uncovered or uncovered in _reported_uncovered:
        return
    _reported_uncovered.add(uncovered)
    logger.warning(
        "[grove.capability_registry] %d CONFIGURABLE_TOOLSETS key(s) have no "
        "governing capability record yet (D4 verb backfill pending): %s",
        len(uncovered),
        sorted(uncovered),
    )


def _load_records_from_dir(target: Path) -> Dict[str, Capability]:
    """Load + dry-run-validate every ``*.yaml`` in one dir (no collection-level
    validation, no empty-check). Raises :class:`CapabilityLoadError` on an
    unreadable / malformed / invalid / duplicate-id record."""
    if not target.is_dir():
        raise CapabilityLoadError(f"capabilities directory not found: {target}")

    records: Dict[str, Capability] = {}
    for path in sorted(target.glob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CapabilityLoadError(f"{path.name}: unreadable ({exc})") from exc

        # Dry-run validation (Amendment A3): full construction triggers
        # Capability.validate(); a malformed YAML or invalid field raises here,
        # naming the field, and we wrap it with the filename.
        try:
            cap = Capability.from_yaml(text)
        except Exception as exc:
            raise CapabilityLoadError(f"{path.name}: {exc}") from exc

        if cap.id in records:
            raise CapabilityLoadError(
                f"{path.name}: duplicate capability id {cap.id!r} — already "
                f"loaded from another record file"
            )
        records[cap.id] = cap
    return records


def load_capabilities(directory: Optional[Path] = None) -> Dict[str, Capability]:
    """Load and dry-run-validate every ``*.yaml`` record.

    With no *directory*, loads the repo bundled records and overlays the
    machine-local ``<GROVE_HOME>/capabilities`` dir (GRV-009 E6b C1 installed
    records) on top. An explicit *directory* loads exactly that dir (no overlay)
    — the path tests and ``transition_record`` use for isolation.

    Returns an id -> :class:`Capability` mapping. Raises
    :class:`CapabilityLoadError` (fail loud) on any unreadable, malformed,
    invalid, or duplicate-id record. Never returns a partial registry.

    COLLISION RULE: an id present in BOTH the repo dir and the GROVE_HOME overlay
    is a write-once violation (the dedup guard should have prevented the mint) —
    it raises LOUDLY rather than silently shadowing. No last-glob-wins.
    """
    if directory is not None:
        records = _load_records_from_dir(Path(directory))
    else:
        records = _load_records_from_dir(default_capabilities_dir())
        overlay = grove_home_capabilities_dir()
        if overlay.is_dir():
            for cap_id, cap in _load_records_from_dir(overlay).items():
                if cap_id in records:
                    raise CapabilityLoadError(
                        f"capability id {cap_id!r} exists in BOTH the repo "
                        f"registry and the machine-local overlay ({overlay}) — "
                        f"write-once violation; the install dedup guard should "
                        f"have prevented this mint. Remove the overlay duplicate "
                        f"— no silent shadowing."
                    )
                records[cap_id] = cap

    if not records:
        raise CapabilityLoadError(
            f"no capability records found in {default_capabilities_dir()}"
            if directory is None else f"no capability records found in {directory}"
        )

    # A4 collection-level invariant — strict 1:1 tool ownership across records.
    _validate_binding_uniqueness(records)

    # D2<->D3 mutual check (C-SEAM4): record toolset_keys must be real (fail
    # loud); uncovered CONFIGURABLE_TOOLSETS keys are reported (non-fatal).
    _report_uncovered_toolsets(_validate_toolset_keys(records))

    return records


# ─────────────────────────────────────────────────────────────────────────────
# GRV-009 E6b C1 — the SOLE capability-record write path (write-once invariant).
#
# Until E6b the registry was load-only. ``transition_record`` is the only
# function that mutates a record on disk, and it does so under a per-record
# advisory lock with an atomic replace. It never blocks and never throws on
# contention: a non-blocking lock that is already held returns DEFERRED so the
# caller (the curator) retries on its next interval. Legality is pre-checked
# before ``Capability.transition()``, so a terminal/managed record returns
# SKIPPED rather than raising.
#
# Lock + atomic-write primitives mirror tools/skill_usage.py:66 (_usage_file_
# lock, fcntl.flock LOCK_EX) and :340 (save_usage, tempfile+fsync+os.replace).
# ─────────────────────────────────────────────────────────────────────────────

TRANSITION_APPLIED = "applied"      # transition legal + written
TRANSITION_DEFERRED = "deferred"    # lock contended — caller retries next interval
TRANSITION_SKIPPED = "skipped"      # illegal/terminal edge — pre-checked, no write


class TransitionResult(NamedTuple):
    status: str                # one of TRANSITION_APPLIED / _DEFERRED / _SKIPPED
    record: Optional[Any]      # the grove.capability.TransitionRecord when APPLIED


def _record_path_for_id(cap_id: str, target: Path) -> Optional[Path]:
    """Locate the ``*.yaml`` whose top-level ``id:`` equals *cap_id*.

    Parses only the id key (yaml.safe_load), not the full Capability, so the
    scan stays cheap for the common no-match files.
    """
    for path in sorted(target.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and doc.get("id") == cap_id:
            return path
    return None


def _atomic_write_yaml(path: Path, text: str) -> None:
    """tempfile + fsync + os.replace — mirrors tools/skill_usage.save_usage:340."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".cap_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _transition_locked(
    path: Path,
    to_state: LifecycleState,
    actor: str,
    reason: str,
    evidence: Optional[List[str]],
    lifecycle_fields: Dict[str, Any],
) -> TransitionResult:
    """Re-read the record under the held lock, transition, atomically write."""
    cap = Capability.from_yaml(path.read_text(encoding="utf-8"))
    current = cap.lifecycle.state
    # Legality pre-check: a terminal/managed record (no legal exits) is SKIPPED,
    # never raised — the curator no-throw contract.
    if to_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
        return TransitionResult(TRANSITION_SKIPPED, None)

    record = cap.transition(to_state, actor=actor, reason=reason, evidence=evidence)
    for key, value in lifecycle_fields.items():
        if not hasattr(cap.lifecycle, key):
            raise CapabilityLoadError(
                f"transition_record: unknown lifecycle field {key!r}"
            )
        setattr(cap.lifecycle, key, value)

    _atomic_write_yaml(path, cap.to_yaml())
    return TransitionResult(TRANSITION_APPLIED, record)


def transition_record(
    cap_id: str,
    to_state: LifecycleState | str,
    *,
    actor: str,
    reason: str,
    evidence: Optional[List[str]] = None,
    directory: Optional[Path] = None,
    **lifecycle_fields: Any,
) -> TransitionResult:
    """Mutate a capability record's lifecycle state on disk (the SOLE write path).

    Acquires a non-blocking per-record advisory lock, re-reads the record,
    validates + applies the transition, and writes atomically. ``lifecycle_fields``
    (e.g. ``use_count=…``, ``last_used=…``, ``pinned=…``) are applied alongside the
    state change.

    Returns a :class:`TransitionResult`:
      * APPLIED  — transition legal and written (``.record`` is the TransitionRecord)
      * DEFERRED — the lock was contended; caller retries next interval (no write)
      * SKIPPED  — the edge is illegal/terminal; pre-checked, no write

    Raises :class:`CapabilityLoadError` only when no record carries *cap_id*.
    """
    if not isinstance(to_state, LifecycleState):
        to_state = LifecycleState(to_state)

    # Resolve the record's file. With no explicit directory, search BOTH the repo
    # bundled dir AND the machine-local GROVE_HOME overlay — agent records
    # (proposed/active, installed/managed) live in the overlay, bundled records
    # in the repo. The write lands in whichever dir holds the record.
    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(cap_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"transition_record: no capability record with id {cap_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _transition_locked(
            path, to_state, actor, reason, evidence, lifecycle_fields
        )

    lock_path = path.with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # Contended by an active turn writing the same record — defer, never
            # block, never throw.
            return TransitionResult(TRANSITION_DEFERRED, None)
        try:
            return _transition_locked(
                path, to_state, actor, reason, evidence, lifecycle_fields
            )
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def update_lifecycle_fields(
    cap_id: str,
    *,
    directory: Optional[Path] = None,
    **fields: Any,
) -> bool:
    """Write NON-state lifecycle fields (e.g. ``pinned``) to a record without a
    state transition — the registry write path for the CLI pin toggle and any
    telemetry-on-record (GRV-009 E6b C2-bridge).

    Same per-record advisory lock + atomic replace as ``transition_record``.
    Returns True when written, False on lock contention (caller may retry).
    Raises :class:`CapabilityLoadError` when no record carries *cap_id* or an
    unknown lifecycle field is given.
    """
    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(cap_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"update_lifecycle_fields: no capability record with id {cap_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    def _apply() -> bool:
        cap = Capability.from_yaml(path.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if not hasattr(cap.lifecycle, key):
                raise CapabilityLoadError(
                    f"update_lifecycle_fields: unknown lifecycle field {key!r}"
                )
            setattr(cap.lifecycle, key, value)
        _atomic_write_yaml(path, cap.to_yaml())
        return True

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    lock_path = path.with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False  # contended — caller may retry
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# ─────────────────────────────────────────────────────────────────────────────
# GRV-009 E6b C1 — static-registration hook: mint read-only installed/managed
# records at the install perimeter. INFRASTRUCTURE, not faucet — these records
# inline the installed SKILL.md and govern no tools (write-once, no mutation
# surface). They are curator-exempt (MANAGED is terminal). Dedup-guarded: a mint
# is a no-op when the registry already holds the skill's id (so the bundled 92
# provenance:migrated records and re-installs are never overwritten).
#
# Zone (operator lock): inherit RED/YELLOW from the SKILL.md frontmatter; a
# self-declared GREEN, a silent record, or an unparseable zone all fall back to
# YELLOW. Never GREEN, never default-RED.
# ─────────────────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    """Lowercase, non-alphanumeric runs -> single hyphen, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _skill_name_slug(name: str) -> str:
    """The canonical slug-tail of a skill name (skill-invocation-path-integrity-v1).

    THE one slug computation for every skill-name resolver: take the last
    ``/``-segment (category-qualified ``fleet/forge-jobsearch``), then the last
    ``:``-segment, then slugify. Flat, category-qualified, and colon-qualified
    shapes all resolve to the same slug.
    """
    return _slug(name.rsplit("/", 1)[-1].rsplit(":", 1)[-1])


def _skill_matches(name: str) -> List[tuple]:
    """Every kind=skill record whose id trailing segment matches *name*'s slug.

    The shared scan behind :func:`resolve_skill_record` and the legacy
    single-result helpers — one slug computation, one match rule, no forks.
    Returns ``(record_id, Capability)`` pairs sorted by id (deterministic).
    An unloadable registry returns ``[]`` (parity with the legacy helpers'
    ``None``; the invoke-path guard treats no-record as legacy-allow).
    """
    from grove.capability import CapabilityKind

    slug = _skill_name_slug(name)
    if not slug:
        return []
    try:
        caps = load_capabilities()
    except CapabilityLoadError:
        return []
    return sorted(
        (
            (cid, cap)
            for cid, cap in caps.items()
            if cap.kind is CapabilityKind.SKILL and cid.rsplit(".", 1)[-1] == slug
        ),
        key=lambda pair: pair[0],
    )


class SkillResolution(NamedTuple):
    """Result of :func:`resolve_skill_record` (skill-invocation-path-integrity-v1).

    ``status`` is one of ``"resolved"`` (exactly one record: ``record`` +
    ``record_id`` set), ``"none"`` (no record governs the name — the legacy-
    allow shape), or ``"ambiguous"`` (the slug matches >1 record id trailing
    segment; ``matches`` carries every colliding id, sorted).
    """

    status: str
    record: Optional[Capability]
    record_id: Optional[str]
    matches: tuple


def resolve_skill_record(name: str) -> SkillResolution:
    """Canonically resolve a skill name to its governing capability record.

    Slug-tail resolution (the one canonical resolver): flat, category-
    qualified (``fleet/<name>``), and colon-qualified shapes all key on the
    same slug. Exactly one match → ``resolved``; zero → ``none``; more than
    one → ``ambiguous`` with the colliding ids (the invoke path refuses;
    the boot scan logs).
    """
    matches = _skill_matches(name)
    if not matches:
        return SkillResolution("none", None, None, ())
    if len(matches) > 1:
        return SkillResolution(
            "ambiguous", None, None, tuple(cid for cid, _ in matches)
        )
    cid, cap = matches[0]
    return SkillResolution("resolved", cap, cid, (cid,))


def scan_skill_slug_collisions() -> Dict[str, List[str]]:
    """Boot-time slug-collision scan (skill-invocation-path-integrity-v1 P1).

    Groups every kind=skill record id by its trailing segment and logs each
    group with more than one member at WARNING, naming every colliding id.
    LOG-ONLY: never raises, never halts startup — a collision degrades to
    per-invoke ambiguity refusal, not a boot failure. Returns the collision
    map (slug -> sorted ids) for tests and callers that want the census.
    """
    from grove.capability import CapabilityKind

    try:
        caps = load_capabilities()
    except Exception as exc:  # noqa: BLE001 — log-only by contract
        logger.warning(
            "[capability_registry] skill slug collision scan skipped — "
            "registry unloadable: %r",
            exc,
        )
        return {}
    groups: Dict[str, List[str]] = {}
    for cid, cap in caps.items():
        if cap.kind is not CapabilityKind.SKILL:
            continue
        groups.setdefault(cid.rsplit(".", 1)[-1], []).append(cid)
    collisions = {
        slug: sorted(ids) for slug, ids in groups.items() if len(ids) > 1
    }
    for slug, ids in sorted(collisions.items()):
        logger.warning(
            "[capability_registry] skill slug collision: %r -> %s "
            "(invoke_skill refuses ambiguous resolutions for this slug)",
            slug,
            ids,
        )
    return collisions


def skill_record_id_for_name(name: str) -> Optional[str]:
    """The kind=skill record id whose name-slug matches *name*, or None.

    GRV-009 E6b C2 — the faucet (edit/patch/delete) and sovereignty
    (promote/reject/revoke) use this to find the record governing an on-disk
    skill, matching the trailing id segment (the slug). Returns None when no
    record governs the skill (external skills, pre-C2 legacy .andon proposals).
    Delegates to the shared scan; on a slug collision returns the first id in
    sorted order (deterministic; the legacy scan's first-match was load-order).
    """
    matches = _skill_matches(name)
    return matches[0][0] if matches else None


def skill_record_for_name(name: str):
    """The kind=skill :class:`Capability` whose name-slug matches *name*, or None
    (GRV-009 E6b C2-bridge — the CLI reads state/pinned from the record).
    Delegates to the shared scan; see :func:`skill_record_id_for_name` for the
    collision tiebreak."""
    matches = _skill_matches(name)
    return matches[0][1] if matches else None


def set_skill_pinned(name: str, pinned: bool) -> bool:
    """Set ``lifecycle.pinned`` on the record governing *name* (GRV-009 E6b
    C2-bridge — the CLI pin toggle, record-backed). Returns True when written,
    False when no record governs the skill (caller informs the operator)."""
    cap_id = skill_record_id_for_name(name)
    if cap_id is None:
        return False
    return update_lifecycle_fields(cap_id, pinned=bool(pinned))


def _frontmatter_value(payload: str, key: str) -> Optional[str]:
    """A single string value from a SKILL.md frontmatter block, or None."""
    if not payload.startswith("---"):
        return None
    parts = payload.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    val = front.get(key)
    return str(val).strip() if val else None


def _frontmatter_zone(payload: str) -> Optional[str]:
    """The lowercased ``zone:`` from a SKILL.md frontmatter block, or None."""
    if not payload.startswith("---"):
        return None
    parts = payload.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    zone = front.get("zone")
    return str(zone).strip().lower() if zone else None


def _resolve_minted_zone(payload: str):
    """Inherit RED/YELLOW from frontmatter; green/silent/invalid -> YELLOW."""
    from grove.capability import Zone

    declared = _frontmatter_zone(payload)
    if declared == "red":
        return Zone.RED
    if declared == "yellow":
        return Zone.YELLOW
    # Self-declared green, silent, or unparseable -> default-deny-but-usable.
    return Zone.YELLOW


def _body_hash(content: str) -> str:
    """sha256 body hash for ``lifecycle.body_hash``. Mirrors
    ``grove.sovereignty._sha256_short`` exactly so a future wake-match compares
    like-for-like (GRV-009 E6b C2 — populate only; reactivation DEFERRED)."""
    import hashlib

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _mint_skill_record(
    name: str,
    category: str,
    payload: str,
    *,
    provenance,
    state,
    filename_tag: str,
    use_count: int = 0,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Shared minter: build + dedup-guard + atomically write a kind=skill record.

    Returns the written path, or ``None`` (dedup hit or unusable input). The
    record mints to the machine-local ``<GROVE_HOME>/capabilities`` overlay
    (ruling A); an explicit *directory* (tests) wins. ``body_hash`` is always
    populated. ``filename_tag`` marks provenance in the filename
    (``skill__<tag>__<cat>__<name>.yaml``); the record *id* is unchanged.
    """
    from grove.capability import (
        Capability,
        CapabilityKind,
        CircuitBreaker,
        Context,
        Disclosure,
        DockComposition,
        Failure,
        Lifecycle,
        SkillPresentation,
        Telemetry,
        TierRule,
        TierValidation,
        Trigger,
        TriggerDisclosure,
    )

    name_slug = _slug(name)
    if not name_slug or not (payload or "").strip():
        return None
    # E6a id convention: a categorized skill is skill.<category>.<name>; a
    # top-level skill (no category) is skill.<name>.<name>. Matching this keeps
    # the dedup guard aligned with the migrated 92 (e.g. skill.dogfood.dogfood).
    cat_slug = _slug(category) or name_slug
    cap_id = f"skill.{cat_slug}.{name_slug}"

    target = Path(directory) if directory is not None else grove_home_capabilities_dir()
    target.mkdir(parents=True, exist_ok=True)

    # Dedup against the registry the loader reads. A pre-loaded id set (batch
    # callers) avoids re-loading per skill; otherwise load once here. Fail loud
    # on an invalid registry (a real defect).
    if existing_ids is not None:
        if cap_id in existing_ids:
            return None
    elif directory is None:
        # Full registry = repo bundled + machine-local overlay.
        if cap_id in load_capabilities():
            return None
    elif any(target.glob("*.yaml")) and cap_id in load_capabilities(target):
        return None

    cap = Capability(
        id=cap_id,
        kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(
            eligible=[1, 2, 3],
            preferred=1,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=_resolve_minted_zone(payload),
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(
            disclosure=Disclosure.PULL,
            payload=payload,
            dock_composition=DockComposition.NONE,
        ),
        lifecycle=Lifecycle(
            state=state,
            provenance=provenance,
            body_hash=_body_hash(payload),
            use_count=use_count,
        ),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category=cat_slug),
    )

    path = target / f"skill__{filename_tag}__{cat_slug}__{name_slug}.yaml"
    _atomic_write_yaml(path, cap.to_yaml())
    return path


def register_installed_skill(
    name: str,
    category: str,
    payload: str,
    *,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Mint a read-only ``provenance:installed`` / ``lifecycle:managed`` skill
    record for a freshly installed skill — IF none exists (dedup guard).

    Returns the written record path, or ``None`` when the skill already has a
    record (idempotent) or the inputs are unusable (empty name/payload).

    ``existing_ids`` lets a batch caller (the profile-clone tree walk, the sync
    loop) pre-load the registry's ids ONCE and pass them in, avoiding an
    O(skills x registry) reload per skill.
    """
    from grove.capability import LifecycleState, Provenance

    written = _mint_skill_record(
        name, category, payload,
        provenance=Provenance.INSTALLED,
        state=LifecycleState.MANAGED,
        filename_tag="installed",
        directory=directory,
        existing_ids=existing_ids,
    )
    # GRV-010 C2b — minter provenance. This minter is operator-only (CLI /
    # boot-sync / profile-clone; no agent-loop path — GATE-A thread 3 ruling).
    # Record a stateless sovereignty_decision so every minted record has an
    # audit trail. ``log_sovereignty_decision`` writes to the telemetry logger
    # with no session/turn context, so it is safe from a bare CLI execution.
    if written is not None:
        _log_minter_provenance(
            action="skill_record_minted", skill_name=name,
            dest_path=str(written),
        )
    return written


def _log_minter_provenance(
    *, action: str, skill_name: str, dest_path: Optional[str] = None,
    scan_verdict: str = "n/a", reason: Optional[str] = None,
) -> None:
    """Emit an operator/CLI-attributed provenance record for a minter.

    Bare-CLI-context safe: ``log_sovereignty_decision`` is a stateless telemetry
    write (no SessionDB, no active turn). Failures never block the mint.
    """
    try:
        from grove.telemetry import log_sovereignty_decision
        log_sovereignty_decision(
            action=action,
            skill_name=skill_name,
            operator="operator/CLI",
            scan_verdict=scan_verdict,
            dest_path=dest_path,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[capability_registry] minter provenance log failed "
            "(non-fatal): %r", exc,
        )


# GRV-010 C2b — ``ingest_pre_faucet_skill`` deleted: it was a dead, un-audited
# minter (zero production callers; it minted an ACTIVE/executable record outside
# the proposed-quarantine gate). The pre-faucet ingest it served is long past.


def register_proposed_skill(
    name: str,
    category: str,
    payload: str,
    *,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Mint a ``provenance:agent_proposed`` / ``lifecycle:proposed`` record for an
    agent-generated skill — the C2 faucet (dedup-guarded).

    ``proposed`` is the SOLE authoritative quarantine state (GRV-009 E6b C2
    .andon fork ruling a): the record is **non-executable** until promoted — the
    dispatch checkpoint refuses ``state:proposed`` even though the record loads
    and the body is readable in ``.andon/`` for operator review. ``body_hash`` is
    populated for future wake-match (reactivation DEFERRED).
    """
    from grove.capability import LifecycleState, Provenance

    return _mint_skill_record(
        name, category, payload,
        provenance=Provenance.AGENT_PROPOSED,
        state=LifecycleState.PROPOSED,
        filename_tag="proposed",
        directory=directory,
        existing_ids=existing_ids,
    )


def register_skills_in_tree(
    skills_root: Path,
    *,
    directory: Optional[Path] = None,
) -> List[Path]:
    """Mint installed/managed records for every ``<cat>/<name>/SKILL.md`` under
    *skills_root* (dedup-guarded). Used by the profile-clone perimeter, which
    copies whole skill trees. Returns the list of newly written record paths.
    """
    minted: List[Path] = []
    if not skills_root.is_dir():
        return minted

    # Load the registry's ids ONCE for the whole tree (not per skill) — a
    # profile clone can carry the full skill set; a per-skill reload would make
    # profile creation time out. directory=None -> the full merged registry
    # (repo + GROVE_HOME overlay); an explicit directory -> that dir only.
    if directory is None:
        existing_ids: set = set(load_capabilities().keys())
    else:
        target = Path(directory)
        existing_ids = (
            set(load_capabilities(target).keys()) if any(target.glob("*.yaml")) else set()
        )

    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        rel = skill_md.parent.relative_to(skills_root)
        # Skip hidden infrastructure dirs (.archive/.andon/.hub/.curator_backups
        # etc.) — they are not installable skills (E6a excluded them too).
        if any(part.startswith(".") for part in rel.parts):
            continue
        # <category>/<name>/SKILL.md -> category, name; bare <name>/ -> "".
        category = rel.parent.as_posix() if rel.parent.as_posix() != "." else ""
        name = rel.name
        try:
            payload = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            written = register_installed_skill(
                name, category, payload, directory=directory,
                existing_ids=frozenset(existing_ids),
            )
        except Exception:
            # FLAG 2 (operator lock): a mint failure names the skill_id and the
            # absolute skill-body path so the operator has exact reconcile
            # coordinates — never a bare traceback.
            cat_slug = _slug(category) or _slug(name)
            logger.warning(
                "capability-record mint FAILED skill_id=skill.%s.%s body=%s "
                "— record NOT minted; reconcile manually",
                cat_slug, _slug(name), skill_md.resolve(), exc_info=True,
            )
            continue
        if written is not None:
            minted.append(written)
            # Keep the in-memory id set current so a duplicate name later in the
            # same tree dedups without a reload.
            existing_ids.add(f"skill.{_slug(category) or _slug(name)}.{_slug(name)}")
    return minted
