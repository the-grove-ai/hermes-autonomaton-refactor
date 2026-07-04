"""Per-worker isolated filesystem layout (background-worker-runtime-v1).

Every worker gets a private subtree under ``$GROVE_HOME/fleet/<id>/`` — its
session DB, its (never-created, therefore grant-less) grants file, its inbox for
the ticker-brokered resolved payload, its terminal-state event bus sink, and its
PID/PGID file. The gateway session DB is NEVER touched; isolation is by path.

``worker_id`` is both the isolation key and a path component, so it is validated
as a strict slug — a traversal attempt (``..``, ``/``) fails loud rather than
escaping the fleet subtree.
"""

from __future__ import annotations

import re
from pathlib import Path

from hermes_constants import get_hermes_home

from grove.fleet.errors import FleetWorkerAndon

# A worker id is a filesystem-safe slug: lowercase alnum, hyphen, underscore;
# must start with an alphanumeric. This is the isolation key — anything that
# could climb out of the fleet subtree is rejected at the door.
_WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_worker_id(worker_id: str) -> str:
    """Return *worker_id* unchanged if it is a safe slug, else fail loud."""
    if not isinstance(worker_id, str) or not _WORKER_ID_RE.match(worker_id):
        raise FleetWorkerAndon(
            f"worker id {worker_id!r} is not a valid slug "
            f"(^[a-z0-9][a-z0-9_-]*$) — it is a filesystem isolation key and "
            f"must not be able to escape the fleet subtree",
            worker_id=str(worker_id),
            check="bad_worker_id",
        )
    return worker_id


def fleet_root() -> Path:
    """``$GROVE_HOME/fleet`` — the parent of every worker's isolated subtree."""
    return Path(get_hermes_home()) / "fleet"


def worker_dir(worker_id: str) -> Path:
    """``$GROVE_HOME/fleet/<id>/`` — the worker's private subtree."""
    return fleet_root() / validate_worker_id(worker_id)


def session_db_path(worker_id: str) -> Path:
    """Isolated SessionDB path — NEVER the gateway session DB."""
    return worker_dir(worker_id) / "session.db"


def grantless_grants_path(worker_id: str) -> Path:
    """Grants file path for the worker's principal.

    Deliberately never created: ``GrantStore`` is fail-closed on a missing file,
    so pointing the process-global store here yields a grant-less principal.
    """
    return worker_dir(worker_id) / "grants.yaml"


def inbox_path(worker_id: str, run_id: str) -> Path:
    """Where the ticker writes the brokered, resolved input payload for a run."""
    return worker_dir(worker_id) / "inbox" / f"{run_id}.json"


def events_dir(worker_id: str) -> Path:
    """The terminal-state event bus sink directory for this worker."""
    return worker_dir(worker_id) / "events"


def event_path(worker_id: str, run_id: str) -> Path:
    """The terminal-state event file for a single run (one event per run)."""
    return events_dir(worker_id) / f"{run_id}.json"


def pid_path(worker_id: str) -> Path:
    """PID/PGID file for the running worker (Phase 2 orphan-reap contract)."""
    return worker_dir(worker_id) / "worker.pid"
