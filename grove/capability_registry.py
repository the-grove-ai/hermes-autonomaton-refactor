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
from pathlib import Path
from typing import Dict, FrozenSet, Optional

from grove.capability import Capability

__all__ = ["CapabilityLoadError", "default_capabilities_dir", "load_capabilities"]

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
    """The repo-default record directory: ``<repo>/config/capabilities``."""
    return Path(__file__).resolve().parent.parent / "config" / "capabilities"


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


def load_capabilities(directory: Optional[Path] = None) -> Dict[str, Capability]:
    """Load and dry-run-validate every ``*.yaml`` record in *directory*.

    Returns an id -> :class:`Capability` mapping. Raises
    :class:`CapabilityLoadError` (fail loud, naming file + field) on any
    unreadable, malformed, invalid, or duplicate-id record. Never returns a
    partial registry — a single failure aborts the whole load.
    """
    target = Path(directory) if directory is not None else default_capabilities_dir()
    if not target.is_dir():
        raise CapabilityLoadError(
            f"capabilities directory not found: {target}"
        )

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

    if not records:
        raise CapabilityLoadError(f"no capability records found in {target}")

    # A4 collection-level invariant — strict 1:1 tool ownership across records.
    _validate_binding_uniqueness(records)

    # D2<->D3 mutual check (C-SEAM4): record toolset_keys must be real (fail
    # loud); uncovered CONFIGURABLE_TOOLSETS keys are reported (non-fatal).
    _report_uncovered_toolsets(_validate_toolset_keys(records))

    return records
