"""PID/PGID tracking, startup orphan-reap, and wall-clock enforcement (Phase 2).

The runner writes a PID/PGID file for each running worker. On gateway startup —
BEFORE the cron ticker thread starts — ``sweep_orphans`` reads every worker
pidfile: a group still alive is an orphan stranded by a prior gateway life (no
ticker owns it now), so it is SIGKILLed as a group and its pidfile removed; a
dead pidfile is stale and just removed. This mirrors mcp-orphan-reaping-v1:
persisted state survives a hard gateway kill so a later process can reap what the
dead one stranded.

``enforce_wall_clock`` is the absolute-timeout kill the ticker calls each poll:
past its monotonic deadline and still alive -> SIGKILL the group.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from typing import Any, Dict, List, Optional

from grove.fleet import paths
from grove.fleet.limits import group_alive, safe_kill_group
from grove.fleet.staging import _atomic_write_bytes

logger = logging.getLogger(__name__)


def write_pidfile(
    worker_id: str,
    run_id: str,
    pid: int,
    pgid: int,
    wall_clock_secs: Optional[int],
    started_at: str,
) -> None:
    """Persist the running worker's PID/PGID so a later gateway can reap it."""
    rec = {
        "worker_id": worker_id,
        "run_id": run_id,
        "pid": pid,
        "pgid": pgid,
        "wall_clock_secs": wall_clock_secs,
        "started_at": started_at,
    }
    _atomic_write_bytes(
        paths.pid_path(worker_id), json.dumps(rec).encode("utf-8")
    )


def read_pidfile(worker_id: str) -> Optional[Dict[str, Any]]:
    p = paths.pid_path(worker_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def remove_pidfile(worker_id: str) -> None:
    p = paths.pid_path(worker_id)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def sweep_orphans() -> List[Dict[str, Any]]:
    """Startup reap. Must run BEFORE the cron ticker thread starts.

    For every ``$GROVE_HOME/fleet/<id>/worker.pid``: a live group is an orphan
    (no ticker owns it after a gateway restart) -> SIGKILL the group and remove
    the pidfile; a dead one is stale -> just remove. Returns the reaped records.

    Per-pidfile defensive: a malformed/unreadable pidfile is logged loudly and
    skipped so one bad file cannot abort the whole sweep or block startup.
    """
    reaped: List[Dict[str, Any]] = []
    root = paths.fleet_root()
    if not root.is_dir():
        return reaped
    for pidfile in sorted(root.glob("*/worker.pid")):
        try:
            rec = json.loads(pidfile.read_text(encoding="utf-8"))
            pid, pgid = int(rec["pid"]), int(rec["pgid"])
        except Exception as exc:  # malformed pidfile — surface, do not crash
            logger.error("[fleet.reap] unreadable pidfile %s: %s", pidfile, exc)
            continue
        if group_alive(pid, pgid):
            logger.warning(
                "[fleet.reap] orphaned worker %s (pid=%s pgid=%s) survived a "
                "gateway restart — SIGKILL group",
                rec.get("worker_id"),
                pid,
                pgid,
            )
            safe_kill_group(pid, pgid, signal.SIGKILL)
            reaped.append(rec)
        else:
            logger.debug(
                "[fleet.reap] stale pidfile for %s (pid=%s already dead) — removing",
                rec.get("worker_id"),
                pid,
            )
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass
    return reaped


def enforce_wall_clock(handle) -> bool:
    """Kill the worker's GROUP if it exceeded its absolute wall-clock deadline.

    Returns True iff it killed. No-op when the worker declared no deadline, has
    already exited, or is still within its window. Called by the ticker (Phase 3)
    on each poll; safe to call repeatedly.
    """
    deadline = getattr(handle, "deadline_monotonic", None)
    if deadline is None:
        return False
    if time.monotonic() < deadline:
        return False
    if handle.proc.poll() is not None:
        return False  # already exited on its own
    logger.warning(
        "[fleet.reap] worker %s (run %s) exceeded wall-clock %ss — SIGKILL group "
        "(pid=%s pgid=%s)",
        handle.worker_id,
        handle.run_id,
        getattr(handle, "wall_clock_secs", None),
        handle.proc.pid,
        handle.pgid,
    )
    safe_kill_group(handle.proc.pid, handle.pgid, signal.SIGKILL)
    return True
