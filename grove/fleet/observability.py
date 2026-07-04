"""Fleet observed-event routing — surface an Andon to the operator (Phase 3).

A worker Andon (a failed run, a catastrophic no-event exit, a cold-MCP resolver
read, a wall-clock kill) routes here, and this is the ONLY quiet-path exception:
success and no_work never call it. Two legs, per the error-surfacing spine:

  * operator surface — ``broadcast_to_operator`` scheduled onto the gateway loop
    (the ticker runs in a background thread), with a guaranteed log floor.
  * governed Kaizen filing — an ``andon_halt`` entry in the run's Kaizen ledger.

Kaizen is the RECOMMENDER, not a reporter: every surfaced Andon carries operator
GO-FORWARD OPTIONS (keyed by the Andon's ``check`` token), so the message says
not just what broke but what the operator can do about it. Both legs are
defensive — a surfacing failure logs, it never crashes the 60s ticker.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Kaizen recommendations: check-token -> ordered go-forward options. The operator
# reads these WITH the failure, so an Andon is actionable, not just a report.
_GO_FORWARD_OPTIONS: Dict[str, List[str]] = {
    "index_surface_unwired": [
        "Wire a read-only + busy-timeout connection for the index surface, then re-enable the worker.",
        "Remove the index surface from the record's read_surfaces if the skill does not need it.",
        "Disable the worker (enabled: false) until the wiring lands.",
    ],
    "undeclared_surface": [
        "Add the surface to the record's read_surfaces if the skill legitimately needs it.",
        "Fix the skill so it only touches its declared surfaces.",
    ],
    "governed_denial": [
        "Grant the scope if the action is meant to be operator-authorized.",
        "Review the skill — a grant-less background worker must only draft to pending_review, never write externally.",
        "Disable the worker if the denial is expected behavior.",
    ],
    "wall_clock_exceeded": [
        "Raise limits.wall_clock_secs if the skill legitimately needs longer.",
        "Inspect the skill for a hang or infinite loop.",
        "Disable the worker if it repeatedly overruns.",
    ],
    "catastrophic_no_event": [
        "Inspect ~/.grove/fleet/<id>/ (session db, inbox) for the crash point.",
        "Check for an OOM/segfault — RLIMIT_AS (limits.mem_mb) is a VA ceiling; raise it with headroom.",
        "Disable the worker and re-run the skill interactively to reproduce.",
    ],
    "nonzero_exit": [
        "Read the terminal-state event for the failure detail.",
        "Inspect ~/.grove/fleet/<id>/ for diagnostics.",
        "Disable the worker (enabled: false) if failures persist.",
    ],
    "resolver_cold_mcp": [
        "The MCP server was cold/unreachable this tick; check connector health on the portal.",
        "No data was lost — the worker is skipped this tick and retries next cadence.",
        "Warm the server manually if it stays cold.",
    ],
    "resolver_failed": [
        "Inspect the input_state predicate in fleet_workers.yaml for this worker.",
        "Check the source (data_source/filter) the resolver reads.",
        "Disable the worker if the source is unavailable.",
    ],
    "orphan_pidfile_malformed": [
        "A worker pidfile was unreadable — a prior worker's process group MAY be unreaped.",
        "Check for stray processes under the worker id and kill the group manually if present.",
        "The stale pidfile was left in place for inspection; remove it once resolved.",
    ],
    "worker_not_registered": [
        "Add the worker to config/fleet_workers.yaml, or remove its stale inbox/pidfile.",
    ],
}

_DEFAULT_OPTIONS: List[str] = [
    "Inspect ~/.grove/fleet/<id>/ and the terminal-state event for diagnostics.",
    "Re-run the skill interactively to reproduce.",
    "Disable the worker (enabled: false) if this recurs.",
]


def go_forward_options(check: Optional[str]) -> List[str]:
    """The Kaizen-recommended go-forward options for an Andon ``check`` token."""
    return list(_GO_FORWARD_OPTIONS.get(check or "", _DEFAULT_OPTIONS))


def _compose_report(worker_id: str, run_id: str, message: str, options: List[str]) -> str:
    lines = [f"Fleet worker '{worker_id}' (run {run_id}) halted: {message}", "", "Go-forward options:"]
    lines += [f"  {i}. {opt}" for i, opt in enumerate(options, 1)]
    return "\n".join(lines)


def surface_fleet_andon(
    worker_id: str,
    run_id: str,
    message: str,
    *,
    check: Optional[str] = None,
    loop: Optional[Any] = None,
    severity: str = "error",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Route a fleet Andon to the operator + the governed Kaizen ledger.

    ``loop`` is the gateway event loop (the ticker holds it); when present the
    operator broadcast is scheduled onto it. Both legs are defensive — this must
    never raise into the ticker.
    """
    options = go_forward_options(check)
    report = _compose_report(worker_id, run_id, message, options)
    metadata = {"worker_id": worker_id, "run_id": run_id, "check": check, "options": options}
    if extra:
        metadata.update(extra)

    # ── Leg 1: operator surface (broadcast on the loop, or log floor). ──
    scheduled = False
    if loop is not None:
        try:
            from agent.async_utils import safe_schedule_threadsafe
            from grove.notify import broadcast_to_operator

            safe_schedule_threadsafe(
                broadcast_to_operator(report, severity=severity, metadata=metadata),
                loop,
                logger=logger,
                log_message="fleet Andon broadcast scheduling failed",
            )
            scheduled = True
        except Exception as exc:  # noqa: BLE001 — never crash the ticker
            logger.error("[fleet.observe] broadcast leg failed: %r; report:\n%s", exc, report)
    if not scheduled:
        # Guaranteed floor when there is no loop (out-of-band run / tests).
        getattr(logger, severity, logger.error)("[fleet.observe] %s", report)

    # ── Leg 2: governed Kaizen filing (andon_halt in the run's ledger). ──
    try:
        from grove.kaizen_ledger import KaizenLedger

        KaizenLedger(session_id=f"fleet:{worker_id}:{run_id}").record(
            "andon_halt",
            source="fleet_worker",
            worker=worker_id,
            run=run_id,
            check=check,
            detail=message,
            go_forward_options=options,
        )
    except Exception as exc:  # noqa: BLE001 — filing is best-effort, log floor stands
        logger.error("[fleet.observe] kaizen filing leg failed: %r", exc)

    return {"surfaced": True, "options": options, "broadcast_scheduled": scheduled}
