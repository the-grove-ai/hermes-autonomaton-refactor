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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from grove.fleet.config import WorkerConfig
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.paths import event_path, inbox_path, worker_dir


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class WorkerHandle:
    """What the ticker holds after a dispatch: the process + where to reap it."""

    worker_id: str
    run_id: str
    proc: subprocess.Popen
    event_path: Path


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

        proc = self._spawn(wid, rid)
        return WorkerHandle(
            worker_id=wid, run_id=rid, proc=proc, event_path=event_path(wid, rid)
        )

    def _spawn(self, worker_id: str, run_id: str) -> subprocess.Popen:
        """Launch the worker process. Phase 2 hardens THIS method (setsid,
        setrlimit, PID/PGID file); the dispatch contract above is unchanged."""
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
        return subprocess.Popen(cmd, cwd=str(_repo_root()))


# Module-level default runner + thin functional seam. Swapping isolation = swap
# this binding; call sites use dispatch(...) and never name the impl.
_runner: KanbanRunner = KanbanRunner()


def dispatch(
    worker_cfg: WorkerConfig, payload: Any, run_id: Optional[str] = None
) -> WorkerHandle:
    """Dispatch a worker run through the active runner impl."""
    return _runner.dispatch(worker_cfg, payload, run_id=run_id)
