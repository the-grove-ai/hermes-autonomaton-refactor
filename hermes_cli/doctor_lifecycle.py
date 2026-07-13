"""Sprint 47.5 — process-lifecycle crash-recovery for ``hermes doctor``.

Discovery + reaping that returns the box to a known-good state after a
crashed gateway/smoke-test loop strands MCP children, leaks sockets, or
leaves advisory locks behind.

GATE-A locked decisions (do not relax):

* Discover by PPID tree, NEVER by name. The only process markers used are
  the unambiguous Grove ones (``hermes_cli.main`` / ``bin/hermes``) — never
  ``node`` / ``npm`` / ``npx`` (this operator runs Notion.app and Claude.app
  node helpers; a name match would kill them).
* MCP orphans are reaped ONLY through ``reap_dead_owner_children()`` (the
  registry primitive, owner-PID + pgid-drift guarded). The PPID-1 scan is
  limited to Grove CLI processes.
* Locks: flock held-test with a re-check guard immediately before unlink.
* SQLite: NEVER blind-delete WAL/SHM. ``PRAGMA wal_checkpoint(TRUNCATE)``
  only when the DB is strictly unowned (no live process holds it).
* ``--dry-run`` is the default surface; destructive work needs ``--reap`` /
  ``--restart``. Stopping a gateway with a live socket needs ``--force``.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

_LOG = logging.getLogger(__name__)

# Unambiguous Grove CLI cmdline markers. The `-m hermes_cli.main ...` form
# (how the gateway service launches) and the `.venv/bin/hermes ...` console
# script form. NEVER `node`/`npm`/`npx`.
_GROVE_CLI_MARKERS = ("hermes_cli.main", "bin/hermes")


def _grove_home() -> Path:
    return Path(get_hermes_home())


def _is_grove_cli_cmd(cmd: str) -> bool:
    """True if *cmd* is a Grove gateway/chat CLI invocation (never node/npm)."""
    return any(m in cmd for m in _GROVE_CLI_MARKERS)


# ── process discovery ─────────────────────────────────────────────────


def find_live_gateway_pid() -> Optional[int]:
    """The authoritative live-gateway PID from the runtime lock, or None."""
    try:
        from gateway.status import get_running_pid
        return get_running_pid()
    except Exception:
        return None


def gateway_tree_pids(gw_pid: Optional[int]) -> set:
    """The live gateway PID + all descendants (recursive). PROTECTED — never
    reaped. Empty set when no gateway is running."""
    if not gw_pid:
        return set()
    pids = {gw_pid}
    try:
        import psutil
        for child in psutil.Process(gw_pid).children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    return pids


def _read_proc_cgroup(pid: int) -> Optional[str]:
    """Raw ``/proc/<pid>/cgroup`` text, or None on macOS (no /proc), a dead
    pid, or any read error. Isolated as a seam so the reaper's service-cgroup
    guard is testable without a live cgroup hierarchy."""
    try:
        return Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None


def _service_cgroup_of(pid: int) -> Optional[str]:
    """The systemd ``*.service`` cgroup path *pid* belongs to, or None.

    A crashed/stranded process reparented to PID 1 detaches into a user
    session scope (``…/session-N.scope``) or ``init.scope`` — no ``.service``
    segment. A live systemd-launched unit (e.g. the ledger-retention oneshot)
    is ALSO reparented to PID 1 but still sits inside its service cgroup — that
    is the distinction the bare PPID-1 orphan heuristic misses. Linux-only;
    returns None where /proc is absent (macOS)."""
    text = _read_proc_cgroup(pid)
    if not text:
        return None
    for line in text.splitlines():
        path = line.rsplit(":", 1)[-1].strip()
        if any(seg.endswith(".service") for seg in path.split("/")):
            return path
    return None


def find_orphaned_grove_processes(protected: set) -> List[Dict[str, Any]]:
    """Grove CLI processes reparented to PID 1 (crashed predecessors) that are
    NOT part of the live gateway tree. Identified by the Grove CLI markers
    only — never by node/npm. A PID-1-parented Grove process that still sits
    inside an active ``*.service`` cgroup is a systemd-launched unit (e.g. the
    ledger-retention oneshot), NOT a crash orphan — it is spared."""
    orphans: List[Dict[str, Any]] = []
    me = os.getpid()
    try:
        import psutil
    except Exception:
        return orphans
    for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            info = proc.info
            pid = info.get("pid")
            if pid in protected or pid == me:
                continue
            if info.get("ppid") != 1:
                continue
            cmd = " ".join(info.get("cmdline") or [])
            if not _is_grove_cli_cmd(cmd):
                continue
            svc = _service_cgroup_of(pid)
            if svc is not None:
                # Live systemd-launched Grove oneshot — PID-1-parented like a
                # crash orphan but inside its service cgroup. Sparing it stops
                # the watchdog from SIGTERMing a running unit mid-pass
                # (the retention-timer collision, test-baseline-hygiene R-T6).
                _LOG.info(
                    "doctor: sparing PID %d — inside active service cgroup %s",
                    pid, svc,
                )
                continue
            orphans.append({"pid": pid, "cmd": cmd})
        except Exception:
            continue
    return orphans


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False
    except PermissionError:
        return True


def _kill_pid_group(pid: int, sig: int) -> None:
    """Signal the process GROUP (wrapper + MCP children together), defensively
    falling back to a single-PID signal."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _signal_orphans(orphans: List[Dict[str, Any]]) -> None:
    """SIGTERM → 3s grace → SIGKILL survivors, by process group."""
    pids = [o["pid"] for o in orphans]
    for pid in pids:
        _kill_pid_group(pid, signal.SIGTERM)
    if not pids:
        return
    time.sleep(3)
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in pids:
        if _pid_alive(pid):
            _kill_pid_group(pid, sigkill)


# ── MCP orphan registry (read-only count for dry-run) ─────────────────


def count_dead_owner_mcp() -> int:
    """How many registered MCP children have a dead owner (would be reaped).
    Read-only — used for the dry-run report."""
    try:
        import json
        from tools.mcp_tool import _registry_path
        from gateway.status import _pid_exists
    except Exception:
        return 0
    path = Path(_registry_path())
    if not path.exists():
        return 0
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    dead = 0
    for e in entries if isinstance(entries, list) else []:
        owner = int(e.get("owner_pid") or 0)
        if owner and not _pid_exists(owner):
            dead += 1
    return dead


# ── lock cleanup (re-check guarded) ───────────────────────────────────


def _lock_is_held(path: Path) -> bool:
    """True if a live process holds an exclusive flock on *path*.

    Opens the file and tries a non-blocking exclusive flock: success means
    nobody holds it (we release immediately); failure means it's held.
    """
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        # Can't open (perms / vanished) — be conservative, treat as held.
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    finally:
        os.close(fd)


def _iter_lock_files(grove_home: Path):
    """Yield every ``*.lock`` / ``*.lck`` under *grove_home*, INCLUDING
    dot-prefixed names. ``glob('*.lock')`` skips leading-dot files, which is
    exactly the set of stale-prone advisory locks (``.mcp-children.lock``,
    ``.tick.lock``, ``.usage.json.lock``) — so we walk instead."""
    for root, _dirs, files in os.walk(grove_home):
        for fn in files:
            if fn.endswith((".lock", ".lck")):
                yield Path(root) / fn


def clean_stale_locks(
    grove_home: Path, dry_run: bool, *, gateway_live: bool = False,
) -> List[Dict[str, str]]:
    """Remove advisory lock files that are NOT currently held, with a re-check
    guard immediately before unlink (the guard that prevented a bad delete in
    the live audit when a lock was re-acquired mid-check).

    ``gateway_live`` is the load-bearing safety gate: a running gateway cycles
    its advisory locks (``.mcp-children.lock`` etc.) — they read as free at any
    given instant but are NOT stale. Removing them would be race-safe yet
    semantically wrong (reaping a live resource). So when a gateway is live we
    skip removal; genuinely stale locks only exist after a crash, when no
    gateway holds the environment."""
    results: List[Dict[str, str]] = []
    if not grove_home.exists():
        return results
    seen = set()
    for path in _iter_lock_files(grove_home):
        lp = str(path)
        if lp not in seen:
            seen.add(lp)
            if _lock_is_held(path):
                results.append({"path": lp, "action": "skip (held by live process)"})
                continue
            if gateway_live:
                results.append({"path": lp, "action": "skip (live gateway owns environment)"})
                continue
            if dry_run:
                results.append({"path": lp, "action": "would remove (stale)"})
                continue
            # Re-check guard: someone may have grabbed it since the first test.
            if _lock_is_held(path):
                results.append({"path": lp, "action": "skip (re-acquired)"})
                continue
            try:
                path.unlink()
                results.append({"path": lp, "action": "removed"})
            except OSError as exc:
                results.append({"path": lp, "action": f"error: {exc}"})
    return results


# ── SQLite WAL checkpoint (only when strictly unowned) ────────────────


def _db_is_open(db_path: str) -> bool:
    """True if any live process has *db_path* open. Fail-safe: on any
    uncertainty, returns True so we never touch a possibly-live DB."""
    try:
        out = subprocess.run(
            ["lsof", "-t", "--", db_path],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return True


def checkpoint_unowned_dbs(grove_home: Path, dry_run: bool) -> List[Dict[str, str]]:
    """``PRAGMA wal_checkpoint(TRUNCATE)`` for DBs with a non-empty WAL that
    NO live process holds. NEVER deletes WAL/SHM files (GATE-A decision 2)."""
    results: List[Dict[str, str]] = []
    if not grove_home.exists():
        return results
    for db in glob.glob(str(grove_home / "*.db")):
        wal = Path(db + "-wal")
        if not wal.exists() or wal.stat().st_size == 0:
            continue
        if _db_is_open(db):
            results.append({"db": db, "action": "skip (open by live process)"})
            continue
        if dry_run:
            results.append({"db": db, "action": "would checkpoint(TRUNCATE)"})
            continue
        try:
            con = sqlite3.connect(db, timeout=2)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
            results.append({"db": db, "action": "checkpointed (TRUNCATE)"})
        except Exception as exc:
            results.append({"db": db, "action": f"skip (error: {exc})"})
    return results


# ── live-socket guard (used by --restart in Phase 2) ──────────────────


def gateway_has_live_socket(gw_pid: Optional[int]) -> bool:
    """True if the gateway holds an ESTABLISHED inet connection (Telegram /
    API long-poll). Fail-safe: returns True on uncertainty so --restart
    refuses to stop a possibly-live gateway without --force."""
    if not gw_pid:
        return False
    try:
        import psutil
        for conn in psutil.Process(gw_pid).net_connections(kind="inet"):
            if conn.status == "ESTABLISHED":
                return True
        return False
    except Exception:
        return True


# ── orchestration ─────────────────────────────────────────────────────


def reap(*, dry_run: bool = True) -> Dict[str, Any]:
    """Reap orphaned Grove/MCP processes + clean stale locks + checkpoint
    unowned DBs. Leaves the live gateway tree untouched. ``dry_run`` reports
    without acting."""
    gw = find_live_gateway_pid()
    protected = gateway_tree_pids(gw)
    report: Dict[str, Any] = {
        "dry_run": dry_run,
        "live_gateway": gw,
        "protected_tree": sorted(protected),
    }

    # 1. Registered MCP orphans → registry primitive only.
    if dry_run:
        report["mcp_dead_owner"] = count_dead_owner_mcp()
        report["mcp_reaped"] = 0
    else:
        try:
            from tools.mcp_tool import reap_dead_owner_children
            report["mcp_reaped"] = reap_dead_owner_children()
        except Exception as exc:
            report["mcp_reaped"] = 0
            report["mcp_error"] = str(exc)

    # 2. Orphaned Grove CLI processes (PPID 1, not in the live tree).
    orphans = find_orphaned_grove_processes(protected)
    report["orphans"] = orphans
    if not dry_run and orphans:
        _signal_orphans(orphans)

    # 3. Stale locks (re-check guarded; a live gateway owns its own locks) +
    #    4. unowned-DB WAL checkpoint.
    report["locks"] = clean_stale_locks(
        _grove_home(), dry_run=dry_run, gateway_live=bool(gw),
    )
    report["dbs"] = checkpoint_unowned_dbs(_grove_home(), dry_run=dry_run)
    return report


def render_report(report: Dict[str, Any]) -> None:
    """Print a lifecycle report. Lazy-imports the doctor color helpers so this
    module stays importable in tests without the CLI color stack."""
    from hermes_cli.colors import Colors, color

    dry = report.get("dry_run", True)
    print()
    print(color("◆ Process lifecycle", Colors.CYAN, Colors.BOLD)
          + ("  (dry-run — nothing changed)" if dry else "  (reaped)"))

    gw = report.get("live_gateway")
    tree = report.get("protected_tree", [])
    if gw:
        print(f"  {color('✓', Colors.GREEN)} Live gateway PID {gw} "
              f"{color(f'(protected; {len(tree)} process(es) in its tree)', Colors.DIM)}")
    else:
        print(f"  {color('→', Colors.CYAN)} No live gateway running")

    orphans = report.get("orphans", [])
    verb = "would reap" if dry else "reaped"
    if orphans:
        print(f"  {color('⚠', Colors.YELLOW)} {len(orphans)} orphaned Grove "
              f"process(es) ({verb}):")
        for o in orphans:
            print(f"      pid {o['pid']}: {o['cmd'][:90]}")
    else:
        print(f"  {color('✓', Colors.GREEN)} No orphaned Grove processes")

    mcp = report.get("mcp_dead_owner", report.get("mcp_reaped", 0))
    mverb = "dead-owner MCP child entr" if dry else "MCP child entr"
    print(f"  {color('→', Colors.CYAN)} {mcp} {mverb}"
          f"{'y' if mcp == 1 else 'ies'} {verb}")

    locks = report.get("locks", [])
    removed = [l for l in locks if l["action"] in ("removed", "would remove (stale)")]
    held = [l for l in locks if l["action"].startswith("skip")]
    print(f"  {color('→', Colors.CYAN)} Locks: {len(removed)} {verb}, "
          f"{len(held)} held/skipped")
    for l in locks:
        mark = '✓' if l["action"].startswith(("removed", "would")) else '·'
        print(f"      {mark} {Path(l['path']).name}: {l['action']}")

    dbs = report.get("dbs", [])
    if dbs:
        print(f"  {color('→', Colors.CYAN)} SQLite WAL:")
        for d in dbs:
            print(f"      {Path(d['db']).name}: {d['action']}")
    print()


def restart_gateway(*, dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """Cycle the gateway via the managed service (Sprint 47.5 Phase 2).

    Stops then starts the gateway through ``hermes gateway stop`` /
    ``hermes gateway start`` (the launchd/systemd service model is left
    intact). REFUSES to stop a gateway holding a live ESTABLISHED socket
    (Telegram / API long-poll) unless ``force`` is set — cycling the live
    bot is disruptive and must be an explicit opt-in (Andon A3). ``dry_run``
    reports without acting.
    """
    from hermes_cli.colors import Colors, color

    gw = find_live_gateway_pid()
    result: Dict[str, Any] = {"gateway_pid": gw, "action": None}

    print(color("◆ Gateway restart", Colors.CYAN, Colors.BOLD))
    if not gw:
        result["action"] = "no live gateway — starting fresh" if not dry_run else "would start (none running)"
        print(f"  {color('→', Colors.CYAN)} No live gateway running")
        if dry_run:
            print(f"  {color('→', Colors.CYAN)} would start the gateway service")
            print()
            return result

    if gw and gateway_has_live_socket(gw) and not force:
        result["action"] = "refused (live socket; pass --force)"
        print(f"  {color('✗', Colors.RED)} Gateway PID {gw} holds a live "
              f"connection (Telegram/API). Refusing to cycle it.")
        print(f"  {color('→', Colors.CYAN)} Re-run with {color('--force', Colors.BOLD)} "
              f"to cycle the live gateway.")
        print()
        return result

    if dry_run:
        result["action"] = f"would restart gateway PID {gw} (stop + start)"
        print(f"  {color('→', Colors.CYAN)} would stop gateway PID {gw}, then start it")
        print()
        return result

    import sys
    base = [sys.executable, "-m", "hermes_cli.main", "gateway"]
    try:
        stop = subprocess.run(base + ["stop"], capture_output=True, text=True, timeout=60)
        result["stop_rc"] = stop.returncode
        print(f"  {color('✓', Colors.GREEN)} gateway stop "
              f"{color(f'(rc={stop.returncode})', Colors.DIM)}")
        time.sleep(2)
        start = subprocess.run(base + ["start"], capture_output=True, text=True, timeout=60)
        result["start_rc"] = start.returncode
        print(f"  {color('✓', Colors.GREEN)} gateway start "
              f"{color(f'(rc={start.returncode})', Colors.DIM)}")
        result["action"] = "restarted (stop+start)"
    except Exception as exc:
        result["action"] = f"error: {exc}"
        print(f"  {color('✗', Colors.RED)} restart failed: {exc}")
    print()
    return result
