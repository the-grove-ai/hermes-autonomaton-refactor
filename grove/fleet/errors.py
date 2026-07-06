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
        broadcast: bool = True,
    ) -> None:
        self.worker_id = worker_id
        self.surface = surface
        self.check = check
        # fleet-mcp-warm-unification-v1 P3 — the surfacer reads this to decide
        # whether the operator broadcast fires. Default True preserves every prior
        # raise's behavior; ensure_mcp_warm sets it False for EXPECTED, self-healing
        # conditions (breaker-open) so a persistently-cold server does not storm the
        # operator each cadence.
        self.broadcast = broadcast
        super().__init__(message)


class OperatorActionRequired(GroveError):
    """A condition ``ensure_mcp_warm`` (or any core hydration) cannot self-heal —
    the operator must act (e.g. re-authenticate a dead MCP OAuth secret). Distinct
    from :class:`FleetWorkerAndon` (a structural worker fault): this names a
    human-in-the-loop remediation, not a code fix.

    ``broadcast`` gates the operator alert at the surfacing seam: the FIRST time an
    auth-dead server is seen it is loud (``broadcast=True``, latched via
    ``auth_alert_surfaced``); subsequent cadences are local-only (``broadcast=False``)
    until a confirming reconnect clears the latch — loud-once, not a storm, and never
    permanently silent (the latch reset is the guard).
    """

    def __init__(
        self,
        message: str,
        *,
        server_id: Optional[str] = None,
        check: Optional[str] = None,
        broadcast: bool = True,
    ) -> None:
        self.server_id = server_id
        self.check = check
        self.broadcast = broadcast
        super().__init__(message)
