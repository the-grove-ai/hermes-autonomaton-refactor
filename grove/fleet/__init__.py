"""Grove Autonomaton — Fleet Background-Worker runtime (background-worker-runtime-v1).

The reference pattern every fleet skill capability runs on. A worker is defined
by (a) an operational entry in ``config/fleet_workers.yaml`` (WHEN / HOW-MUCH it
runs) and (b) its capability record (WHAT it is / WHAT it may touch). The runtime
reads those and runs the pinned skill in a short-lived, grant-less subprocess —
it never hard-codes a skill.

This package is SKILL-AGNOSTIC. No skill name, skill-specific read surface, or
skill I/O shape appears here. Forge is the first worker to exercise the runtime;
all forge specifics live in its config entry + capability record, never in code.

Modules:
  errors        — FleetWorkerAndon (loud, diagnostic-carrying fail-closed).
  paths         — per-worker isolated dir + subpath conventions under GROVE_HOME.
  staging       — atomic (tmp -> os.rename) draft staging + terminal-event write.
  read_surfaces — generic, record-driven read_surface enforcement.
  config        — fleet_workers.yaml loader (WorkerConfig + loud dup-id guard).
  runner        — the runner seam (single kanban Popen impl; swap = impl change).
  worker_entry  — the worker process: grant-less principal, isolated session db,
                  non-interactive deny handler, run the pinned skill, stage the
                  draft, write a terminal-state event, exit.
"""

from __future__ import annotations

from grove.fleet.errors import FleetWorkerAndon

__all__ = ["FleetWorkerAndon"]
