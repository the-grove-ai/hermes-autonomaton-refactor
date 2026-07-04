"""Fleet manager — the ticker's per-tick fleet check (Phase 3).

Holds cross-tick state the 60s ticker cannot: the running worker handles and each
worker's last-dispatch time. One ``tick()`` does two defensive passes:

  1. REAP — poll every running handle, enforce its wall-clock, and on exit apply
     death observability: exit-0 + valid terminal event = done (success/no_work
     distinguished, the quiet paths); exit-0 + NO event = catastrophic -> Andon;
     nonzero exit -> Andon. Andons route to the observed-event bus.
  2. DISPATCH — for each enabled worker not already running, if cadence is due
     and it is outside quiet hours, resolve its input_state; work -> Popen the
     worker off-thread via the runner; no work -> quiet; a cold/failed resolve ->
     Andon (never blocks the tick, never silent-skips).

``tick()` is fully defensive — no single worker's failure stops the others or
the tick — so the ticker can call it OUTSIDE its ``except Exception: debug``
swallow with only a thin last-resort guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from grove.fleet import runner
from grove.fleet.cadence import cadence_due, in_quiet_hours
from grove.fleet.config import WorkerConfig, load_fleet_workers
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.observability import surface_fleet_andon
from grove.fleet.reap import enforce_wall_clock, remove_pidfile
from grove.fleet.resolvers import resolve_input_state
from grove.fleet.runner import WorkerHandle

logger = logging.getLogger(__name__)


class FleetManager:
    def __init__(self, loop: Optional[Any] = None, workers_path: Optional[Any] = None):
        self._loop = loop
        self._workers_path = workers_path
        self._running: Dict[str, WorkerHandle] = {}
        self._last_dispatch: Dict[str, datetime] = {}

    # ── public ───────────────────────────────────────────────────────────────

    def tick(self, now: Optional[datetime] = None) -> None:
        """One fleet tick. Never raises — surfaces failures via the bus."""
        now = now or datetime.now(timezone.utc)
        self._reap_running()
        self._maybe_dispatch(now)

    # ── reap / death observability ─────────────────────────────────────────────

    def _reap_running(self) -> None:
        for wid, handle in list(self._running.items()):
            try:
                self._reap_one(wid, handle)
            except Exception as exc:  # noqa: BLE001 — one reap must not stop the rest
                logger.error("[fleet.manager] reap of worker %s crashed: %r", wid, exc)
                self._running.pop(wid, None)  # drop the stuck handle

    def _reap_one(self, wid: str, handle: WorkerHandle) -> None:
        killed = enforce_wall_clock(handle)
        rc = handle.proc.poll()
        if rc is None:
            return  # still running within its window
        # Exited (naturally or via the wall-clock kill) — reap and classify.
        self._running.pop(wid, None)
        remove_pidfile(wid)
        event = self._read_event(handle.event_path)
        self._classify_terminal(wid, handle, rc, event, killed)

    @staticmethod
    def _read_event(event_path) -> Optional[Dict[str, Any]]:
        try:
            if event_path.exists():
                import json

                return json.loads(event_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a torn/missing event is handled as absent
            return None
        return None

    def _classify_terminal(self, wid, handle, rc, event, killed) -> None:
        run_id = handle.run_id
        if killed:
            surface_fleet_andon(
                wid,
                run_id,
                f"worker exceeded its wall-clock ({handle.wall_clock_secs}s) and "
                f"was killed",
                check="wall_clock_exceeded",
                loop=self._loop,
            )
            return
        if rc == 0:
            if event is None:
                # exit-0 with NO terminal event — the worker died without writing.
                surface_fleet_andon(
                    wid,
                    run_id,
                    "worker exited 0 but wrote NO terminal-state event — "
                    "catastrophic (died before its terminal write)",
                    check="catastrophic_no_event",
                    loop=self._loop,
                )
                return
            status = event.get("status")
            if status in ("success", "no_work"):
                logger.info("[fleet.manager] worker %s run %s -> %s", wid, run_id, status)
                return  # the quiet paths
            # exit-0 but a non-terminal status — the worker exits nonzero on
            # failure, so this is anomalous; surface it.
            surface_fleet_andon(
                wid,
                run_id,
                f"worker reported status={status!r}: {event.get('detail')}",
                check=event.get("check") or "nonzero_exit",
                loop=self._loop,
            )
            return
        # Nonzero exit -> Andon; read the terminal event for the WHY.
        detail = (event or {}).get("detail") or f"exit code {rc}"
        check = (event or {}).get("check") or "nonzero_exit"
        surface_fleet_andon(
            wid, run_id, f"worker exited {rc}: {detail}", check=check, loop=self._loop
        )

    # ── dispatch ───────────────────────────────────────────────────────────────

    def _maybe_dispatch(self, now: datetime) -> None:
        try:
            workers = load_fleet_workers(self._workers_path)
        except FleetWorkerAndon as exc:
            surface_fleet_andon(
                "<registry>", "load", str(exc), check=exc.check, loop=self._loop
            )
            return
        except Exception as exc:  # noqa: BLE001 — a broken registry must not kill the tick
            surface_fleet_andon(
                "<registry>", "load", f"{type(exc).__name__}: {exc}",
                check="registry_error", loop=self._loop,
            )
            return

        for wid, cfg in workers.items():
            if not cfg.enabled or wid in self._running:
                continue
            try:
                self._maybe_dispatch_one(wid, cfg, now)
            except FleetWorkerAndon as exc:
                surface_fleet_andon(
                    wid, "dispatch", str(exc), check=exc.check, loop=self._loop
                )
            except Exception as exc:  # noqa: BLE001 — one worker's failure is isolated
                surface_fleet_andon(
                    wid, "dispatch", f"{type(exc).__name__}: {exc}",
                    check="resolver_failed", loop=self._loop,
                )

    def _maybe_dispatch_one(self, wid: str, cfg: WorkerConfig, now: datetime) -> None:
        if in_quiet_hours(cfg.quiet_hours):
            return
        if not cadence_due(cfg.cadence, self._last_dispatch.get(wid), now):
            return
        payload = resolve_input_state(cfg.input_state, wid)  # None -> no work; raises -> Andon
        if payload is None:
            return  # legitimate no_work — the quiet path
        handle = runner.dispatch(cfg, payload)
        self._running[wid] = handle
        self._last_dispatch[wid] = now
        logger.info("[fleet.manager] dispatched worker %s run %s", wid, handle.run_id)
