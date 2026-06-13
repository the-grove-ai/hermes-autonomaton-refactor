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

from pathlib import Path
from typing import Dict, Optional

from grove.capability import Capability

__all__ = ["CapabilityLoadError", "default_capabilities_dir", "load_capabilities"]


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

    return records
