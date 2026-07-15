"""Fleet observed-event routing — surface an Andon to the operator (Phase 3).

A worker Andon (a failed run, a catastrophic no-event exit, a cold-MCP resolver
read, a wall-clock kill) routes here, and this is the ONLY quiet-path exception:
success and no_work never call it. Two legs, per the error-surfacing spine:

  * operator surface — ``broadcast_to_operator`` scheduled onto the gateway loop
    (the ticker runs in a background thread), with a guaranteed log floor.
  * governed Kaizen filing — an ``andon_halt`` entry in the run's Kaizen ledger.

Fleet Andon surfacing. Andon reports facts per instance — worker, run, check,
detail, source — uninterpreted. Kaizen interprets recurring fault patterns
downstream and presents go-forward direction to the operator.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

def _compose_report(worker_id: str, run_id: str, message: str) -> str:
    return f"Fleet worker '{worker_id}' (run {run_id}) halted: {message}"


def surface_fleet_andon(
    worker_id: str,
    run_id: str,
    message: str,
    *,
    check: Optional[str] = None,
    loop: Optional[Any] = None,
    severity: str = "error",
    extra: Optional[Dict[str, Any]] = None,
    broadcast: bool = True,
) -> Dict[str, Any]:
    """Route a fleet Andon to the operator + the governed Kaizen ledger.

    ``loop`` is the gateway event loop (the ticker holds it); when present the
    operator broadcast is scheduled onto it. Both legs are defensive — this must
    never raise into the ticker.

    ``broadcast`` (fleet-mcp-warm-unification-v1 P2 / LOCK-1) gates ONLY the
    operator-broadcast leg. ``broadcast=False`` suppresses the operator alert for
    an EXPECTED, self-healing condition (e.g. a persistently-cold MCP server the
    per-dispatch warm re-attempts every cadence — no operator storm), while the
    local log floor AND the governed Kaizen andon_halt filing still fire — the
    halt is always recorded, only the operator ping is muted. Default True is
    byte-identical for every existing caller.
    """
    report = _compose_report(worker_id, run_id, message)
    metadata = {"worker_id": worker_id, "run_id": run_id, "check": check}
    if extra:
        metadata.update(extra)

    # ── Leg 1: operator surface (broadcast on the loop, or log floor). ──
    # broadcast=False skips the operator ping; the log floor below still fires, so
    # a suppressed Andon is muted-to-operator but never silent-in-logs.
    scheduled = False
    if broadcast and loop is not None:
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
        )
    except Exception as exc:  # noqa: BLE001 — filing is best-effort, log floor stands
        logger.error("[fleet.observe] kaizen filing leg failed: %r", exc)

    return {"surfaced": True, "broadcast_scheduled": scheduled}


def surface_fleet_event(
    worker_id: str,
    run_id: str,
    message: str,
    *,
    event: str,
    loop: Optional[Any] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Route an operator-visible fleet NOTICE — a non-halt outcome — to the
    operator. The informational sibling of :func:`surface_fleet_andon`, for
    forge-unattended-publish-v1 P2's success published-event.

    ONE leg (deliberately, this phase): the operator broadcast at ``info``
    severity, scheduled onto the gateway ``loop`` (the ticker runs off-thread),
    with a guaranteed ``logger.info`` floor when there is no loop. ``extra``
    (e.g. the Drive ``folder_link`` / ``status``) rides in the broadcast metadata.

    NOT filed to the Kaizen ledger: ``KaizenLedger.record`` rejects any
    ``event_type`` outside its governance whitelist, which carries no
    informational fleet/publish type — adding one is a Kaizen-vocabulary change
    outside this phase's scope. The ``event`` string is retained in metadata for
    a future audit leg. (Known wart: ``broadcast_to_operator``'s log line is
    failure-flavored — the operator-facing adapter message is clean.)

    Defensive — never raises into the ticker.
    """
    metadata = {"worker_id": worker_id, "run_id": run_id, "event": event}
    if extra:
        metadata.update(extra)
    report = f"Fleet worker '{worker_id}' (run {run_id}): {message}"

    scheduled = False
    if loop is not None:
        try:
            from agent.async_utils import safe_schedule_threadsafe
            from grove.notify import broadcast_to_operator

            safe_schedule_threadsafe(
                broadcast_to_operator(report, severity="info", metadata=metadata),
                loop,
                logger=logger,
                log_message="fleet event broadcast scheduling failed",
            )
            scheduled = True
        except Exception as exc:  # noqa: BLE001 — never crash the ticker
            logger.error("[fleet.observe] event broadcast leg failed: %r; report:\n%s", exc, report)
    if not scheduled:
        logger.info("[fleet.observe] %s", report)

    return {"surfaced": True, "broadcast_scheduled": scheduled}
