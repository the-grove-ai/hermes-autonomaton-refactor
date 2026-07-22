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
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from grove.fleet import paths
from grove.fleet.limits import group_alive, safe_kill_group
from grove.fleet.staging import _atomic_write_bytes

logger = logging.getLogger(__name__)


def _process_cmdline(pid: int) -> Optional[List[str]]:
    """The live process's argv as a list, or None if it cannot be read.

    A dead process, an access-denied read, a zombie, or an empty argv all
    collapse to None — an unreadable command line is UNVERIFIABLE, never a
    match. Identity is proven only by a positive run_id membership on a
    readable argv (fleet-receipt-custody-v1 C2a). psutil is cross-platform, so
    tests exercise this exact path, not a Linux-only ``/proc`` variant.
    """
    try:
        cl = psutil.Process(pid).cmdline()
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        psutil.Error,
        OSError,
    ):
        return None
    return cl or None


def _group_effectively_dead(pid: int, pgid: int) -> bool:
    """True when the group holds no running process.

    ``group_alive`` (signal 0) reports a SIGKILLed-but-unreaped ZOMBIE leader as
    alive — a zombie still occupies the process table until its parent reaps it
    (in production a re-parented orphan is reaped by init). A zombie runs no code
    and holds nothing, so it is a successful kill, not a survivor.
    """
    if not group_alive(pid, pgid):
        return True
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True  # gone between the two probes
    except (psutil.AccessDenied, psutil.Error, OSError):
        return False  # cannot tell -> not confirmed dead


def _confirm_group_dead(
    pid: int, pgid: int, attempts: int = 5, pause: float = 0.05
) -> bool:
    """Bounded confirmation that the group is dead after a SIGKILL.

    A few short attempts — SIGKILL does not take effect immediately on a process
    in uninterruptible sleep. Deliberately NOT a deadline loop: nothing derives
    state from the timing, so this is not lease logic.
    """
    for _ in range(attempts):
        if _group_effectively_dead(pid, pgid):
            return True
        time.sleep(pause)
    return _group_effectively_dead(pid, pgid)


def _unlink_quietly(pidfile: Path) -> None:
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass


def _surface_reap_andon(
    worker_id: str, run_id: str, message: str, *, check: str, loop: Optional[Any]
) -> None:
    """Route a reap-side Andon, best-effort — visibility must never crash the
    sweep or block startup. Lazily imported at call time so a monkeypatched
    ``surface_fleet_andon`` is honored."""
    try:
        from grove.fleet.observability import surface_fleet_andon

        surface_fleet_andon(worker_id, run_id, message, check=check, loop=loop)
    except Exception as exc:  # noqa: BLE001 — visibility is best-effort
        logger.error("[fleet.reap] could not surface %s andon: %r", check, exc)


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


def sweep_orphans(loop: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Startup reap. Must run BEFORE the cron ticker thread starts.

    For every ``$GROVE_HOME/fleet/<id>/worker.pid`` the sweep gates in order —
    liveness, then identity, then a confirmed kill (C2a):

      - dead group (stale pidfile)            -> remove the pidfile, nothing to kill
      - alive but NOT verifiably ours         -> do NOT signal, KEEP the pidfile,
                                                 surface an actionable Andon
      - alive and ours, confirmed dead by kill -> SIGKILL the group, remove the
                                                  pidfile (counts as reaped)
      - alive and ours, kill not confirmed     -> KEEP the pidfile, surface an
                                                  Andon so the next boot retries

    Identity: the worker's argv carries ``--run-id <hex>`` (runner._spawn), so a
    live process is proven ours only when that run_id is an element of its
    cmdline. A recycled pid holding our stored integers fails the check and is
    left untouched — a wrong kill is unrecoverable, a missed one is retried.

    Per-pidfile defensive: a malformed/unreadable pidfile is a possible UNREAPED
    orphan, so it is routed to the observed-event bus (operator visibility, with
    go-forward options) AND left in place for inspection — never silently
    skipped, and one bad file never aborts the sweep or blocks startup.
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
            worker_id = pidfile.parent.name
            logger.error("[fleet.reap] unreadable pidfile %s: %s", pidfile, exc)
            try:
                from grove.fleet.observability import surface_fleet_andon

                surface_fleet_andon(
                    worker_id,
                    "unknown",
                    f"unreadable worker pidfile {pidfile} ({exc}) — a prior "
                    f"worker's process group may be unreaped",
                    check="orphan_pidfile_malformed",
                    loop=loop,
                )
            except Exception as surf_exc:  # noqa: BLE001 — visibility is best-effort
                logger.error("[fleet.reap] could not surface malformed pidfile: %r", surf_exc)
            continue  # leave the malformed file in place for inspection
        run_id = rec.get("run_id")
        worker_id = rec.get("worker_id") or pidfile.parent.name

        # ── Liveness FIRST: is anything still holding these integers? ──
        if not group_alive(pid, pgid):
            logger.debug(
                "[fleet.reap] stale pidfile for %s (pid=%s already dead) — removing",
                worker_id,
                pid,
            )
            _unlink_quietly(pidfile)
            continue

        # ── IDENTITY gate, before any signal: never kill a process we cannot
        # prove is ours. The worker's argv carries --run-id <hex> (runner._spawn),
        # so the stored run_id must be an ELEMENT of the live process's cmdline.
        # A recycled pid would have to be running a process carrying the same
        # uuid4 — far stronger than a start-time comparison. group_alive answered
        # "is anything running"; this answers "is it ours".
        observed = _process_cmdline(pid)
        if run_id is None or observed is None or run_id not in observed:
            # Identity CANNOT be verified -> do NOT kill, do NOT unlink. A wrong
            # kill destroys an unrelated process and cannot be undone; a missed
            # kill leaves an orphan the next boot retries. The asymmetry decides.
            # The Andon must be ACTIONABLE (pid, expected run_id, observed
            # cmdline) — this path repeats every boot until a human clears it.
            _surface_reap_andon(
                worker_id,
                run_id or "unknown",
                f"pidfile {pidfile} names pid {pid} for run_id {run_id!r}, but the "
                f"live process is not verifiably ours (cmdline={observed!r}). "
                f"Leaving the pidfile in place and NOT signalling — clear it by "
                f"hand once you confirm the process is unrelated.",
                check="orphan_identity_unverified",
                loop=loop,
            )
            continue

        # ── Confirmed ours + alive -> SIGKILL the group. ──
        logger.warning(
            "[fleet.reap] orphaned worker %s (pid=%s pgid=%s run=%s) survived a "
            "gateway restart — SIGKILL group",
            worker_id,
            pid,
            pgid,
            run_id,
        )
        safe_kill_group(pid, pgid, signal.SIGKILL)

        # ── CONFIRM the kill before unlinking (SIGKILL is not instant on a
        # process in uninterruptible sleep). Confirmed-dead is the ONLY path
        # that unlinks — and that counts as reaped. ──
        if _confirm_group_dead(pid, pgid):
            _unlink_quietly(pidfile)
            reaped.append(rec)
        else:
            # Still alive after the bound: keep the pidfile so the next boot
            # retries — losing the handle would be worse than a repeat attempt.
            _surface_reap_andon(
                worker_id,
                run_id,
                f"SIGKILLed the group for pid {pid} run_id {run_id!r} but it did "
                f"not confirm dead within the recheck bound. Keeping the pidfile "
                f"so the next boot retries the reap.",
                check="orphan_kill_unconfirmed",
                loop=loop,
            )
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
