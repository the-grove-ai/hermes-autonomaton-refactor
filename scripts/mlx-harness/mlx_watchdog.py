#!/usr/bin/env python3
"""Andon watchdog for mlx_lm.server — kill the server BEFORE a prefill spike
OOMs the host. Fail loud. (Sprint 77.0a redesign.)

The committed Sprint-71 watchdog sampled free RAM every 2 s and needed 3
consecutive breaches — a ~4-6 s reaction floor, far too slow for a sub-second
MLX prefill activation spike (the spike that crashed the 24 GB M5 in Sprint
71). This redesign is the BACKSTOP to the primary pre-flight token governor
(grove/tier_budget.py + run_agent.py); together they are the two-layer guard.

Primary trigger here is a dRSS/dt PREDICTOR: sample the server's RSS at a fast
cadence (~150 ms), and if it is growing, project the growth a couple of windows
ahead (RSS growth consumes free RAM ~1:1) and fire the moment projected free
RAM would cross the floor — i.e. kill mid-climb, before free RAM is actually
spent. The absolute free-RAM floor is a LOW last resort (~0.8-1.0 GB), set
below the live ~5 K T2 prefill's working minimum so it never false-fires on a
normal turn — NOT the 2-2.5 GB a naive reading would pick.

``evaluate()`` is a pure function so the predictor is provable against a
simulated spike trace with zero real allocation (see test_watchdog_predictor.py).
"""
import argparse
import os
import re
import signal
import subprocess
import time

# Defaults (overridable via CLI). The floor is LOW on purpose — see module doc.
DEFAULT_FLOOR_GB = 1.0
DEFAULT_INTERVAL_MS = 150
DEFAULT_LOOKAHEAD_WINDOWS = 2
DEFAULT_FLOOR_STRIKES = 2


def avail_gb():
    """Available RAM in GB, parsed from ``vm_stat`` (free + inactive +
    purgeable + speculative pages)."""
    out = subprocess.check_output(["vm_stat"]).decode()
    ps = int(re.search(r"page size of (\d+)", out).group(1))

    def pages(label):
        m = re.search(label + r":\s+(\d+)", out)
        return int(m.group(1)) if m else 0

    free = (pages("Pages free") + pages("Pages inactive")
            + pages("Pages purgeable") + pages("Pages speculative"))
    return free * ps / 1073741824


def rss_gb(pid):
    """Resident set size of ``pid`` in GB (via ``ps -o rss=``), or None if the
    process is gone."""
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
    except subprocess.CalledProcessError:
        return None
    return int(out) / 1024 / 1024 if out else None  # ps reports KB


def server_pid(match="mlx_lm.server"):
    """PID of the inference server process matching ``match`` (pgrep -f), or
    None."""
    try:
        out = subprocess.check_output(["pgrep", "-f", match]).decode().split()
        return int(out[0]) if out else None
    except subprocess.CalledProcessError:
        return None


def evaluate(avail_gb_now, rss_gb_now, prev_rss_gb, dt_s,
             floor_gb=DEFAULT_FLOOR_GB, lookahead_windows=DEFAULT_LOOKAHEAD_WINDOWS):
    """Pure decision: should the watchdog fire NOW?

    Returns ``(fire: bool, kind: str, detail: str)`` with ``kind`` in
    ``{"", "predictor", "floor"}``.

    * ``predictor`` (PRIMARY): the server's RSS is growing and, projected
      ``lookahead_windows`` sample-windows ahead, would drive free RAM below
      ``floor_gb``. Fires mid-climb — this is what catches a sub-second spike
      before free RAM is gone. RSS growth is treated as consuming free RAM 1:1.
    * ``floor`` (LAST RESORT): free RAM is already below ``floor_gb``. The
      caller applies a strike count so transient dips don't false-fire.

    No allocation, no I/O — fully determined by its arguments, so a spike trace
    can be replayed sample-by-sample in a unit test.
    """
    if prev_rss_gb is not None and rss_gb_now is not None and dt_s > 0:
        drss_dt = (rss_gb_now - prev_rss_gb) / dt_s  # GB/s
        if drss_dt > 0:
            projected_avail = avail_gb_now - drss_dt * dt_s * lookahead_windows
            if projected_avail < floor_gb:
                return True, "predictor", (
                    f"dRSS/dt={drss_dt:.2f}GB/s projects avail "
                    f"{projected_avail:.2f}GB < floor {floor_gb}GB in "
                    f"{lookahead_windows} window(s)"
                )
    if avail_gb_now < floor_gb:
        return True, "floor", f"avail {avail_gb_now:.2f}GB < floor {floor_gb}GB"
    return False, "", ""


def watch(*, target_pid=None, match="mlx_lm.server", floor_gb=DEFAULT_FLOOR_GB,
          interval_ms=DEFAULT_INTERVAL_MS, lookahead_windows=DEFAULT_LOOKAHEAD_WINDOWS,
          floor_strikes=DEFAULT_FLOOR_STRIKES, max_seconds=None):
    """Sample loop. SIGKILLs the target on a predictor fire (immediate) or on
    ``floor_strikes`` consecutive floor breaches. Returns the killed PID, or
    None if the process exited on its own / the time budget elapsed.

    ``target_pid`` pins a specific process (used by the sandboxed-balloon
    validation); otherwise the server is found by ``match``. ``max_seconds``
    bounds the loop for tests/validation."""
    interval = interval_ms / 1000.0
    prev_rss = None
    prev_t = None
    floor_breaches = 0
    t_start = time.monotonic()
    print(f"[watchdog] floor={floor_gb}GB cadence={interval_ms}ms "
          f"lookahead={lookahead_windows}w floor_strikes={floor_strikes} "
          f"target={'pid:%d' % target_pid if target_pid else match}", flush=True)
    while True:
        if max_seconds is not None and (time.monotonic() - t_start) > max_seconds:
            print("[watchdog] time budget elapsed, exiting", flush=True)
            return None
        pid = target_pid if target_pid is not None else server_pid(match)
        if pid is None:
            print("[watchdog] target gone, exiting", flush=True)
            return None
        avail = avail_gb()
        rss = rss_gb(pid)
        if rss is None:
            print("[watchdog] target gone, exiting", flush=True)
            return None
        now = time.monotonic()
        dt = (now - prev_t) if prev_t is not None else interval
        fire, kind, detail = evaluate(avail, rss, prev_rss, dt, floor_gb, lookahead_windows)
        if fire and kind == "predictor":
            print(f"[watchdog] ANDON (predictor): {detail} — SIGKILL pid {pid} "
                  f"mid-climb to prevent OOM", flush=True)
            os.kill(pid, signal.SIGKILL)
            return pid
        elif fire and kind == "floor":
            floor_breaches += 1
            print(f"[watchdog] WARN (floor): {detail} ({floor_breaches}/{floor_strikes})",
                  flush=True)
            if floor_breaches >= floor_strikes:
                print(f"[watchdog] ANDON (floor): SIGKILL pid {pid} to prevent "
                      f"OOM crash", flush=True)
                os.kill(pid, signal.SIGKILL)
                return pid
        else:
            floor_breaches = 0
        prev_rss = rss
        prev_t = now
        time.sleep(interval)


def _parse_args():
    p = argparse.ArgumentParser(description="Andon watchdog for mlx_lm.server (dRSS/dt predictor + low floor).")
    p.add_argument("--floor-gb", type=float, default=DEFAULT_FLOOR_GB,
                   help=f"absolute free-RAM floor, last resort (default {DEFAULT_FLOOR_GB})")
    p.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS,
                   help=f"sample cadence (default {DEFAULT_INTERVAL_MS})")
    p.add_argument("--lookahead-windows", type=int, default=DEFAULT_LOOKAHEAD_WINDOWS,
                   help=f"predictor projection horizon in sample windows (default {DEFAULT_LOOKAHEAD_WINDOWS})")
    p.add_argument("--floor-strikes", type=int, default=DEFAULT_FLOOR_STRIKES,
                   help=f"consecutive floor breaches before firing (default {DEFAULT_FLOOR_STRIKES})")
    p.add_argument("--match", default="mlx_lm.server",
                   help="pgrep -f pattern for the server process (default mlx_lm.server)")
    p.add_argument("--target-pid", type=int, default=None,
                   help="pin a specific PID instead of pgrep (used by validation)")
    p.add_argument("--max-seconds", type=float, default=None,
                   help="bound the loop (tests/validation); default unbounded")
    return p.parse_args()


if __name__ == "__main__":
    a = _parse_args()
    watch(target_pid=a.target_pid, match=a.match, floor_gb=a.floor_gb,
          interval_ms=a.interval_ms, lookahead_windows=a.lookahead_windows,
          floor_strikes=a.floor_strikes, max_seconds=a.max_seconds)
