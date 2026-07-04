"""Process-group isolation, resource limits, and group-safe kills (Phase 2).

A worker runs in its OWN process group (``os.setsid`` in a ``preexec_fn``), so a
wall-clock kill reaps the worker AND anything it spawned via one ``os.killpg`` —
a single leaked child cannot outlive the group. The ``preexec_fn`` also applies
the memory ceiling (``resource.setrlimit``) and niceness from the worker's
``fleet_workers.yaml`` limits.

Kills mirror the MCP orphan-reaping precedent (``_safe_killpg_or_kill``): signal
the group, but NEVER the caller's own group/pid — a bookkeeping accident must not
kill the gateway. POSIX-only by construction; the fleet runtime targets Linux/macOS.
"""

from __future__ import annotations

import os
import signal
from typing import Callable, Optional

# resource is POSIX-only. The fleet runtime is POSIX; a missing module is a hard,
# loud failure at the point a limit is requested, not a silent skip.
try:
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX
    _resource = None


# Belt-and-suspenders storage/fd caps (Phase-2 amendment). RLIMIT_AS bounds
# process-tree memory but NOT disk-fill or fd-exhaustion; a worker self-writes
# until Phase-4 Option 2 drops its write tool, so cap those cheaply and always.
# Generous documented defaults (backstops, not tuning targets) — overridable per
# worker via limits.fsize_mb / limits.nofile.
DEFAULT_FSIZE_MB = 256   # per-file write cap — bounds one runaway file.
DEFAULT_NOFILE = 512     # open-fd cap — bounds fd exhaustion.


def build_preexec(
    mem_mb: Optional[int] = None,
    nice_increment: Optional[int] = None,
    fsize_mb: Optional[int] = None,
    nofile: Optional[int] = None,
) -> Callable[[], None]:
    """Return a ``preexec_fn`` that runs in the child after fork, before exec:

    1. ``os.setsid()`` — the child becomes a session/group leader; its pgid
       equals its pid and is distinct from the gateway's group.
    2. ``RLIMIT_AS`` = ``mem_mb`` when given. NOTE: this caps VIRTUAL
       address space, not RSS — allocator arenas / mmap reservations count
       against it, so ``mem_mb`` is a VA ceiling that must carry headroom over
       the worker's real RSS, NOT an RSS target.
    3. ``RLIMIT_FSIZE`` = ``fsize_mb`` (default DEFAULT_FSIZE_MB) — per-file cap.
    4. ``RLIMIT_NOFILE`` = ``nofile`` (default DEFAULT_NOFILE) — open-fd cap.
    5. ``os.nice(nice_increment)`` when given.

    Storage/fd caps are always applied (belt-and-suspenders); the memory ceiling
    only when ``mem_mb`` is declared.
    """
    eff_fsize = DEFAULT_FSIZE_MB if fsize_mb is None else int(fsize_mb)
    eff_nofile = DEFAULT_NOFILE if nofile is None else int(nofile)

    def _preexec() -> None:
        os.setsid()
        if _resource is None:
            # A limit was requested but the platform cannot enforce it — fail
            # loud rather than silently run an unbounded worker.
            raise RuntimeError(
                "resource.setrlimit unavailable on this platform — the fleet "
                "runtime requires POSIX to enforce worker resource limits"
            )
        if mem_mb is not None:
            nbytes = int(mem_mb) * 1024 * 1024
            _resource.setrlimit(_resource.RLIMIT_AS, (nbytes, nbytes))
        fbytes = eff_fsize * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_FSIZE, (fbytes, fbytes))
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (eff_nofile, eff_nofile))
        if nice_increment:
            os.nice(int(nice_increment))

    return _preexec


def _killpg_is_safe(pgid: int) -> bool:
    """True only when signalling *pgid* as a group cannot hit the caller itself."""
    if not (hasattr(os, "killpg") and pgid > 0 and pgid != os.getpid()):
        return False
    try:
        if pgid == os.getpgrp():
            return False
    except OSError:
        return False
    return True


def safe_kill_group(pid: int, pgid: int, sig: int = signal.SIGKILL) -> None:
    """Signal the worker's process group, defensively falling back to a single-PID
    kill when a group signal would be unsafe. Idempotent: a group that is already
    gone (ProcessLookupError) is success, not an error."""
    try:
        if _killpg_is_safe(pgid):
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def group_alive(pid: int, pgid: int) -> bool:
    """Liveness probe (signal 0) for the worker's group/pid. False when gone."""
    try:
        if _killpg_is_safe(pgid):
            os.killpg(pgid, 0)
        else:
            os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another uid — still alive for our purposes.
        return True
    except OSError:
        return False
