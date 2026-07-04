"""Fleet-worker fail-loud exception (background-worker-runtime-v1).

Per the Architectural Prime Directive (no silent degradation), a fleet worker
that hits a structural violation — an undeclared/unwired read surface, a path
that escapes its sink, an unresolvable model, a missing capability record —
raises loudly instead of guessing. The exception carries the diagnostic context
the terminal-state event (Phase 1) and the observed-event bus (Phase 3) surface:
WHAT halted, WHERE, and WHY.
"""

from __future__ import annotations

from typing import Optional

from grove.errors import GroveError


class FleetWorkerAndon(GroveError):
    """A fleet background worker raised a structural Andon and must fail closed.

    ``worker_id`` / ``surface`` / ``check`` are the machine-readable diagnostic
    slots. ``check`` is a short stable token (e.g. ``index_surface_unwired``,
    ``undeclared_surface``, ``path_escape``, ``no_routing_config``,
    ``record_not_found``) so downstream surfacing can branch without parsing the
    message. The message itself names the fix in prose (operator-facing).
    """

    def __init__(
        self,
        message: str,
        *,
        worker_id: Optional[str] = None,
        surface: Optional[str] = None,
        check: Optional[str] = None,
    ) -> None:
        self.worker_id = worker_id
        self.surface = surface
        self.check = check
        super().__init__(message)
