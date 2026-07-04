"""Generic, record-driven read_surface enforcement (background-worker-runtime-v1).

A worker may read ONLY the data surfaces its capability record declares in
``read_surfaces``. The vocabulary and load-time validation live in
``grove.capability`` (READ_SURFACE_VOCABULARY); this module is the RUNTIME
enforcement the worker applies before and during a run. It is skill-agnostic:
it reads the record and enforces, knowing nothing about the skill.

Two enforcement points:

  * ``enforce_declared_surfaces`` — before the skill runs. Plain-file surfaces
    (corpus_file) need no special handling. Index surfaces (cellar, wiki) need a
    collision-safe injected SQLite connection; both index opens are bare today
    (discovery: cellar.py:168, wiki/index.py:253), so a worker DECLARING an index
    surface raises a loud Andon — declare-but-unwired. This is the guard that
    bites in Phase 1: no worker can currently reach an index surface.

  * ``assert_surface_allowed`` — the contract guard for the touch point. When
    index surfaces are wired (the sprint that first declares one), the injected
    index read calls this so a worker reaching a surface it did NOT declare fails
    loud. Inert today only because the declare-side Andon above forecloses index
    use entirely; wired in tandem with the first index surface.
"""

from __future__ import annotations

from typing import List

from grove.capability import READ_SURFACE_VOCABULARY, Capability
from grove.fleet.errors import FleetWorkerAndon

# Plain-file surfaces need no collision-safe connection — a worker reads the
# file directly. Index surfaces are FTS SQLite indexes shared with the live
# gateway; a concurrent worker read needs a read-only + busy-timeout connection.
PLAIN_FILE_SURFACES = frozenset({"corpus_file"})
INDEX_SURFACES = frozenset({"cellar", "wiki"})

# Invariant: the runtime's surface partition must cover the whole vocabulary, or
# a newly-added token would fall through enforcement unnoticed. Fail loud at
# import if capability.py grew a token this module has not classified.
_UNCLASSIFIED = set(READ_SURFACE_VOCABULARY) - (PLAIN_FILE_SURFACES | INDEX_SURFACES)
if _UNCLASSIFIED:
    raise FleetWorkerAndon(
        f"read_surface token(s) {sorted(_UNCLASSIFIED)} exist in "
        f"READ_SURFACE_VOCABULARY but are not classified as plain-file or index "
        f"in grove/fleet/read_surfaces.py — classify before use",
        check="unclassified_surface",
    )

# Which module opens the bare connection, named in the Andon so the operator
# knows where the wiring must land.
_INDEX_SOURCE = {"cellar": "grove/cellar.py:168", "wiki": "grove/wiki/index.py:253"}


def enforce_declared_surfaces(capability: Capability, worker_id: str) -> List[str]:
    """Enforce the record's declared read_surfaces before the skill runs.

    Returns the declared surface list. Raises a loud Andon for any declared
    INDEX surface (un-injectable today). Plain-file surfaces pass through.
    """
    declared = list(capability.read_surfaces)
    for surface in declared:
        if surface in INDEX_SURFACES:
            raise FleetWorkerAndon(
                f"read_surface {surface!r} declared but collision-safe connection "
                f"not yet wired ({_INDEX_SOURCE.get(surface, '?')} opens a bare "
                f"sqlite3 connection). Index-surface workers are declare-but-"
                f"unwired: inject a read-only + busy-timeout connection for "
                f"{surface!r} before enabling this worker.",
                worker_id=worker_id,
                surface=surface,
                check="index_surface_unwired",
            )
    return declared


def assert_surface_allowed(capability: Capability, surface: str, worker_id: str) -> None:
    """Contract guard: the worker touched *surface*; the record must declare it.

    Called at the (future) index read-injection point. A surface not in the
    record's ``read_surfaces`` is a contract breach — fail loud.
    """
    if surface not in set(capability.read_surfaces):
        raise FleetWorkerAndon(
            f"worker reached read_surface {surface!r}, which is NOT in its "
            f"declared read_surfaces {sorted(capability.read_surfaces)} — the "
            f"record contract forbids it.",
            worker_id=worker_id,
            surface=surface,
            check="undeclared_surface",
        )
