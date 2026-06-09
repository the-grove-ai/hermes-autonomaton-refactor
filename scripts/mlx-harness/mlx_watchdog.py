#!/usr/bin/env python3
"""Andon watchdog: kill mlx_lm.server if available RAM falls below floor. Fail loud."""
import subprocess, time, sys, os, signal, re
FLOOR_GB = 1.0
def avail_gb():
    out = subprocess.check_output(["vm_stat"]).decode()
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    def pages(label):
        m = re.search(label + r":\s+(\d+)", out)
        return int(m.group(1)) if m else 0
    free = pages("Pages free")+pages("Pages inactive")+pages("Pages purgeable")+pages("Pages speculative")
    return free*ps/1073741824
def server_pid():
    try:
        out = subprocess.check_output(["pgrep","-f","mlx_lm.server"]).decode().split()
        return int(out[0]) if out else None
    except subprocess.CalledProcessError:
        return None
if __name__ == "__main__":
    breaches = 0
    print(f"[watchdog] floor={FLOOR_GB}GB, sampling 2s", flush=True)
    while True:
        a = avail_gb(); pid = server_pid()
        if pid is None:
            print("[watchdog] server gone, exiting", flush=True); break
        if a < FLOOR_GB:
            breaches += 1
            print(f"[watchdog] WARN avail={a:.2f}GB < {FLOOR_GB} ({breaches}/3)", flush=True)
            if breaches >= 3:
                print(f"[watchdog] ANDON: killing server pid {pid} to prevent OOM crash", flush=True)
                os.kill(pid, signal.SIGKILL); break
        else:
            breaches = 0
        time.sleep(2)
