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
import logging
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
from grove.fleet.paths import dispatch_path, event_path, inbox_path, worker_dir
from grove.fleet.reap import write_pidfile
from grove.fleet.staging import _atomic_write_bytes


logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    """The projected, in-memory view of a genesis dispatch record (A5 wall).

    Deliberately carries NO timestamp. The on-disk record stamps one for
    forensics, but a field absent from this dataclass cannot be read, so no
    reader can build a lease/timeout on dispatch time. ``slots=True`` denies an
    instance a ``__dict__``, so a ts cannot be smuggled on at runtime either.
    Readers get THIS, never the raw dict.
    """

    run_id: str
    unit_id: Optional[str]
    worker_id: str


def write_dispatch_record(
    worker_id: str, run_id: str, unit_id: Optional[str]
) -> Path:
    """Mint the genesis record BEFORE the worker exists (one per run_id).

    The on-disk dict carries a forensics-only ``ts``; the projection
    (:func:`read_dispatch_record`) drops it so no read path can consult it. A
    write failure raises (fail loud) — the caller must abort without spawning:
    a unit never legally dispatched must not reach any terminal state.
    """
    rec = {
        "run_id": run_id,
        "unit_id": unit_id,
        "worker_id": worker_id,
        # Forensics ONLY — no read path may consult this (Andon A5). It exists
        # nowhere in the DispatchRecord projection above.
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    dest = dispatch_path(worker_id, run_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(dest, json.dumps(rec).encode("utf-8"))
    except OSError as exc:
        raise FleetWorkerAndon(
            f"could not write dispatch record at {dest}: {exc}",
            worker_id=worker_id,
            check="dispatch_record_unwritable",
        ) from exc
    return dest


def read_dispatch_record(worker_id: str, run_id: str) -> Optional[DispatchRecord]:
    """Project the raw on-disk dict onto the typed :class:`DispatchRecord`.

    The A5 wall: the forensics ``ts`` is DROPPED here and never reaches a
    reader. Returns None when no record exists for this run.
    """
    p = dispatch_path(worker_id, run_id)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return DispatchRecord(
        run_id=raw["run_id"],
        unit_id=raw.get("unit_id"),
        worker_id=raw["worker_id"],
    )


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

        # Identity is minted by the host and echoed — one shared definition
        # (worker_entry._dispatched_unit_id), never duplicated here. worker_entry
        # is stdlib-only at module load, so this lazy import cannot cycle.
        from grove.fleet.worker_entry import _dispatched_unit_id

        # ── Step 2: validate the wall-clock config BEFORE minting a record. ──
        # A kanban worker MUST carry an absolute wall-clock bound — an unbounded
        # background process is a runaway risk. This is pure config validation
        # with no I/O, deliberately ordered above the record write so an invalid
        # config never mints a genesis record: a deleted abort path, not a
        # handled one.
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

        # ── Step 3: mint the genesis record BEFORE the worker exists. ──
        # With interactive mode retired, this is the genesis event: no unit
        # exists without one. A write failure aborts WITHOUT spawning — a unit
        # never legally dispatched must not reach any terminal state, so this
        # path writes no receipt (fail loud and stop). Identity is the
        # host-minted unit id from the payload, echoed, never named by a worker.
        write_dispatch_record(wid, rid, _dispatched_unit_id(payload))

        # ── Step 4: broker the payload at the process boundary. ──
        # The worker holds no MCP and never reads the source itself. The record
        # now exists, so a failure here must CLOSE it with a terminal receipt
        # keyed by the same rid (carrying identity), then raise as before.
        inbox = inbox_path(wid, rid)
        try:
            inbox.parent.mkdir(parents=True, exist_ok=True)
            inbox.write_text(
                json.dumps({"worker_id": wid, "run_id": rid, "payload": payload}),
                encoding="utf-8",
            )
        except OSError as exc:
            self._close_genesis(
                worker_cfg, rid, payload, "inbox_unwritable",
                f"could not write worker inbox at {inbox}: {exc}",
            )
            raise FleetWorkerAndon(
                f"could not write worker inbox at {inbox}: {exc}",
                worker_id=wid,
                check="inbox_unwritable",
            ) from exc

        # ── Step 5: spawn. A spawn failure is the last pre-live abort — close
        # the genesis record with a terminal receipt, then re-raise unchanged. ──
        try:
            proc = self._spawn(wid, rid, limits)
        except Exception as exc:
            self._close_genesis(
                worker_cfg, rid, payload, "spawn_failed",
                f"worker spawn failed: {type(exc).__name__}: {exc}",
            )
            raise
        write_pidfile(
            wid,
            rid,
            pid=proc.pid,
            pgid=proc.pid,  # setsid makes the child a group leader: pgid == pid
            wall_clock_secs=wall_clock_secs,
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

    def _close_genesis(
        self,
        worker_cfg: WorkerConfig,
        run_id: str,
        payload: Any,
        check: str,
        detail: str,
    ) -> None:
        """Close an open genesis record on an abort that happens AFTER the
        record exists but BEFORE a live process — write a terminal 'failed'
        receipt keyed by the same run_id, carrying the dispatched identity.

        Routed through ``worker_entry._event`` (one receipt builder, shared) so
        the P1.2C AST identity invariant auto-enrolls these branches: the scan
        now covers this module, and a receipt written without identity fails it.
        The write is best-effort — if the receipt sink is itself unwritable we
        log loud but never mask the original abort, which the caller re-raises.
        """
        from grove.fleet.staging import write_terminal_event
        from grove.fleet.worker_entry import _dispatched_unit_id, _event

        unit_id = _dispatched_unit_id(payload)
        try:
            event = _event(
                worker_cfg.id,
                run_id,
                worker_cfg.skill,
                "failed",
                detail=detail,
                check=check,
                unit_id=unit_id,
            )
            write_terminal_event(event_path(worker_cfg.id, run_id), event)
        except Exception:  # noqa: BLE001 — never mask the original abort
            logger.exception(
                "[fleet.runner] could not write %s abort receipt for %s/%s",
                check,
                worker_cfg.id,
                run_id,
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
                mem_mb=limits.get("mem_mb"),
                nice_increment=limits.get("nice"),
                fsize_mb=limits.get("fsize_mb"),
                nofile=limits.get("nofile"),
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
