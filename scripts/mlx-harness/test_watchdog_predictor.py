#!/usr/bin/env python3
"""Safe validation for the Sprint 77.0a watchdog redesign.

HARD CONSTRAINT: never a live prefill OOM. Two layers, neither of which loads a
model or runs mlx_real.py:

  1. PRIMARY PROOF (pytest, zero allocation): replay a simulated RSS/free-RAM
     trace with a sub-second multi-GB spike through the pure ``evaluate()`` and
     assert the predictor fires WITHIN ONE WINDOW of onset — mid-climb, while
     free RAM is still abundant (a 2 s/3-strike sampler would have missed it).

  2. INTEGRATION (run explicitly, not in CI): a sandboxed, self-capped memory
     balloon that allocates a bounded amount and HARD-STOPS far above any real
     OOM; confirm ``watch()`` SIGKILLs it at a high, safe test floor. Run with
     ``python test_watchdog_predictor.py --balloon``. Guarded so it refuses to
     run unless free RAM is comfortably high.

Run the unit proof:   .venv/bin/python -m pytest scripts/mlx-harness/test_watchdog_predictor.py -q
Run the balloon:      /Users/jimcalhoun/mlx-env/bin/python scripts/mlx-harness/test_watchdog_predictor.py --balloon
"""
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "mlx_watchdog", os.path.join(_HERE, "mlx_watchdog.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


W = _load_watchdog()

# A simulated sub-second spike at a 150 ms cadence, floor 1.0 GB, lookahead 2.
# (avail_gb, rss_gb) per sample. Model-loaded steady state, then RSS runs away.
DT = 0.15
FLOOR = 1.0
SPIKE_TRACE = [
    (9.5, 10.0),   # 0   steady (no prev → no fire)
    (9.5, 10.0),   # 1   steady: dRSS/dt=0 → no fire
    (8.0, 11.5),   # 2   onset: +1.5 GB/window → projected 8.0-3.0=5.0 > 1.0, safe
    (5.0, 14.5),   # 3   climbing: +3.0 GB/window → projected 5.0-6.0=-1.0 < 1.0 → FIRE
    (1.0, 18.0),   # 4   (would be near-OOM — the predictor already fired at #3)
]


def _replay(trace, floor=FLOOR, dt=DT, lookahead=2):
    """Replay a sample trace through evaluate(); return the first fire as
    (index, kind, detail, avail_at_fire) or None."""
    prev_rss = None
    for i, (avail, rss) in enumerate(trace):
        fire, kind, detail = W.evaluate(avail, rss, prev_rss, dt, floor, lookahead)
        if fire:
            return i, kind, detail, avail
        prev_rss = rss
    return None


def test_predictor_fires_mid_climb_within_one_window():
    res = _replay(SPIKE_TRACE)
    assert res is not None, "predictor never fired on the spike trace"
    idx, kind, detail, avail_at_fire = res
    # Fires at the climbing sample (#3), driven by the predictor — not the floor.
    assert kind == "predictor", f"expected predictor fire, got {kind}: {detail}"
    assert idx == 3, f"expected fire at sample 3 (onset+1 window), got {idx}"
    # The whole point: it fires with free RAM STILL ABUNDANT (5 GB), well above
    # the 1.0 GB floor — i.e. before free RAM is spent. A reactive floor-only
    # watchdog would not fire until sample #4 (1.0 GB), one window too late.
    assert avail_at_fire >= 4.0, f"fired too late: only {avail_at_fire}GB free"


def test_onset_window_is_still_safe_no_premature_fire():
    # Sample #2 (onset) must NOT fire — growth has begun but projected RAM is
    # still safe. Firing here would be a false positive on a normal ramp.
    fire, kind, _ = W.evaluate(8.0, 11.5, 10.0, DT, FLOOR, 2)
    assert not fire, "fired prematurely at onset while still safe"


def test_steady_state_never_fires():
    # Flat RSS at the model-loaded baseline (the normal local turn): no fire.
    prev = None
    for avail, rss in [(9.5, 10.0)] * 6:
        fire, _, _ = W.evaluate(avail, rss, prev, DT, FLOOR, 2)
        assert not fire, "false positive on steady state"
        prev = rss


def test_slow_safe_growth_does_not_false_fire():
    # A gentle climb that never threatens the floor must not trip the predictor.
    prev = None
    for avail, rss in [(9.0, 10.0), (8.9, 10.1), (8.8, 10.2), (8.7, 10.3)]:
        fire, kind, _ = W.evaluate(avail, rss, prev, DT, FLOOR, 2)
        assert not fire, f"false positive on slow safe growth ({kind})"
        prev = rss


def test_absolute_floor_is_last_resort_low():
    # Below the low floor with no growth signal → floor fire (last resort).
    fire, kind, _ = W.evaluate(0.7, 9.0, 9.0, DT, FLOOR, 2)
    assert fire and kind == "floor"
    # And the live ~5 K prefill's working minimum (~1.3 GB) must NOT trip the
    # floor — the reason it is set low, not at 2-2.5 GB.
    fire2, _, _ = W.evaluate(1.3, 10.0, 10.0, DT, FLOOR, 2)
    assert not fire2, "floor too high — would false-fire on a normal local turn"


# ── Integration: sandboxed self-capped balloon (run explicitly, never in CI) ──

def _balloon_child(target_grow_gb, stop_if_avail_below_gb):
    """Allocate up to target_grow_gb in touched 128 MB chunks, then hold.
    HARD-STOPS if system free RAM drops below stop_if_avail_below_gb — the
    balloon can never approach a real OOM."""
    blocks = []
    chunk = 128 * 1024 * 1024
    grown = 0.0
    while grown < target_grow_gb:
        if W.avail_gb() < stop_if_avail_below_gb:
            print(f"[balloon] hard-stop: avail < {stop_if_avail_below_gb}GB", flush=True)
            break
        b = bytearray(chunk)
        b[::4096] = b"\x01" * len(b[::4096])  # touch pages so RSS actually grows
        blocks.append(b)
        grown += chunk / 1e9
        time.sleep(0.05)
    time.sleep(20)  # hold so the watchdog has a live target


def run_balloon_validation():
    """Spawn the sandboxed balloon and confirm the watchdog SIGKILLs it at a
    high, safe test floor. Returns True on success."""
    import multiprocessing as mp

    avail0 = W.avail_gb()
    print(f"[validation] free RAM now: {avail0:.2f} GB")
    if avail0 < 6.0:
        print("[validation] REFUSING: need >6 GB free to run the balloon safely.")
        return False

    # Fire after the balloon consumes ~1.5 GB — abundant headroom. The balloon
    # hard-stops if avail ever falls within 3 GB of now: never near OOM.
    test_floor = round(avail0 - 1.5, 2)
    safety_stop = round(avail0 - 3.0, 2)
    grow_cap = 3.0
    print(f"[validation] test_floor={test_floor}GB  balloon hard-stop<{safety_stop}GB  "
          f"grow_cap={grow_cap}GB (all far above real OOM)")

    proc = mp.Process(target=_balloon_child, args=(grow_cap, safety_stop))
    proc.start()
    print(f"[validation] balloon pid={proc.pid}; arming watchdog (150ms, predictor + floor)")
    killed = W.watch(target_pid=proc.pid, floor_gb=test_floor, interval_ms=100,
                     lookahead_windows=2, floor_strikes=2, max_seconds=25)
    proc.join(3)
    alive = proc.is_alive()
    if alive:
        proc.terminate(); proc.join(2)
    ok = (killed == proc.pid) and not alive
    print(f"[validation] watchdog killed pid={killed}; balloon alive after kill={alive}")
    print(f"[validation] {'PASS' if ok else 'FAIL'}: "
          f"watchdog SIGKILLed the balloon at a safe floor, box never near OOM.")
    return ok


if __name__ == "__main__":
    if "--balloon" in sys.argv:
        sys.exit(0 if run_balloon_validation() else 1)
    print("Run unit proof via pytest; run the balloon with --balloon.")
