"""The runner seam (background-worker-runtime-v1).

``dispatch(worker_cfg, payload)`` brokers a worker run. The seam exists so a
future isolation swap (container, remote executor) is an IMPL change behind a
stable call — Phase 1 ships exactly one impl: the Kanban runner, which Popens a
short-lived worker PROCESS. No other impl.

The ticker (Phase 3) owns the returned handle: it polls ``proc.returncode`` and,
on reap, reads the worker's terminal-state event. Phase 2 hardens ``_spawn``
(preexec setsid, resource limits, PID/PGID file); the seam and handle are stable
across that change.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from grove.fleet.config import WorkerConfig
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.limits import build_preexec
from grove.fleet.paths import event_path, inbox_path, worker_dir
from grove.fleet.reap import write_pidfile


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class WorkerHandle:
    """What the ticker holds after a dispatch: the process + where to reap it.

    ``pgid`` and ``deadline_monotonic`` are the Phase-2 hardening contract — the
    ticker enforces the absolute wall-clock timeout by group-killing ``pgid``
    once ``time.monotonic()`` passes ``deadline_monotonic``.
    """

    worker_id: str
    run_id: str
    proc: subprocess.Popen
    event_path: Path
    pgid: int
    wall_clock_secs: int
    deadline_monotonic: float


class KanbanRunner:
    """Single Phase-1 impl: Popen ``python -m grove.fleet.worker_entry``.

    Writes the ticker-brokered resolved payload to the worker's inbox, then
    launches the worker with the isolated (worker_id, run_id) so it reads only
    its own inbox. Returns a handle for reap.
    """

    def dispatch(
        self, worker_cfg: WorkerConfig, payload: Any, run_id: Optional[str] = None
    ) -> WorkerHandle:
        rid = run_id or uuid.uuid4().hex
        wid = worker_cfg.id

        # Broker the payload at the process boundary — the worker holds no MCP
        # and never reads the source itself. Written atomically-ish (the worker
        # only starts after this returns); an unwritable inbox fails loud.
        inbox = inbox_path(wid, rid)
        try:
            inbox.parent.mkdir(parents=True, exist_ok=True)
            inbox.write_text(
                json.dumps({"worker_id": wid, "run_id": rid, "payload": payload}),
                encoding="utf-8",
            )
        except OSError as exc:
            raise FleetWorkerAndon(
                f"could not write worker inbox at {inbox}: {exc}",
                worker_id=wid,
                check="inbox_unwritable",
            ) from exc

        # A kanban worker MUST carry an absolute wall-clock bound — an unbounded
        # background process is a runaway risk. Fail loud rather than spawn one.
        limits = worker_cfg.limits or {}
        wall_clock_secs = limits.get("wall_clock_secs")
        if not isinstance(wall_clock_secs, int) or wall_clock_secs <= 0:
            raise FleetWorkerAndon(
                f"worker {wid!r}: limits.wall_clock_secs must be a positive int "
                f"(a background worker requires an absolute wall-clock bound); "
                f"got {wall_clock_secs!r}",
                worker_id=wid,
                check="missing_wall_clock",
            )

        proc = self._spawn(wid, rid, limits)
        write_pidfile(
            wid,
            rid,
            pid=proc.pid,
            pgid=proc.pid,  # setsid makes the child a group leader: pgid == pid
            wall_clock_secs=wall_clock_secs,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        return WorkerHandle(
            worker_id=wid,
            run_id=rid,
            proc=proc,
            event_path=event_path(wid, rid),
            pgid=proc.pid,
            wall_clock_secs=wall_clock_secs,
            deadline_monotonic=time.monotonic() + wall_clock_secs,
        )

    def _spawn(
        self, worker_id: str, run_id: str, limits: Optional[dict] = None
    ) -> subprocess.Popen:
        """Launch the worker in its OWN process group with resource limits.

        ``preexec_fn`` runs setsid (own group -> group-killable), the memory
        ceiling (RLIMIT_AS from limits.mem_mb), and niceness (limits.nice). The
        dispatch contract above (inbox + handle) is unchanged from Phase 1.
        """
        limits = limits or {}
        cmd = [
            sys.executable,
            "-m",
            "grove.fleet.worker_entry",
            "--worker-id",
            worker_id,
            "--run-id",
            run_id,
        ]
        # Ensure the private subtree exists before the child writes into it.
        worker_dir(worker_id).mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            cmd,
            cwd=str(_repo_root()),
            preexec_fn=build_preexec(
                mem_mb=limits.get("mem_mb"), nice_increment=limits.get("nice")
            ),
        )


# Module-level default runner + thin functional seam. Swapping isolation = swap
# this binding; call sites use dispatch(...) and never name the impl.
_runner: KanbanRunner = KanbanRunner()


def dispatch(
    worker_cfg: WorkerConfig, payload: Any, run_id: Optional[str] = None
) -> WorkerHandle:
    """Dispatch a worker run through the active runner impl."""
    return _runner.dispatch(worker_cfg, payload, run_id=run_id)
