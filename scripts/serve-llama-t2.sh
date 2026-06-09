#!/usr/bin/env bash
#
# serve-llama-t2.sh — bring up the local T2 substrate (gpt-oss on llama.cpp),
# bound to the Tailscale interface, as a launchd-managed service on the
# operator's Mac. The VM agent reaches this over the tailnet (cross-machine
# bind: VM hermes-gateway -> Mac llama-server).
#
# PARAMETERIZED — no operator-specific paths baked in. The launchd plist
# (local, uncommitted) supplies the model path via the environment; this
# script is the committed, machine-agnostic launch contract.
#
# Boot-ordering vs Tailscale: the Tailscale IP does not exist until tailscaled
# has brought the interface up, so RunAtLoad at cold boot would fail to bind.
# We poll `tailscale ip -4` until the address is assigned before binding;
# launchd KeepAlive covers any residual race (a failed bind exits non-zero and
# is restarted).
#
# Failure mode: gpt-oss on llama.cpp is mmap/evictable — an over-pressure event
# evicts clean pages rather than OOM-ing the host, so the dRSS/dt watchdog armed
# here is a clean-kill backstop, not the primary guard.
#
# Environment (all optional except the model):
#   GROVE_LLAMA_T2_MODEL     REQUIRED — path to the gpt-oss GGUF.
#   GROVE_LLAMA_T2_PORT      listen port (default 8080).
#   GROVE_LLAMA_T2_CTX       context size; the prefill governor's ceiling sits
#                            below this (default 24576 — holds the ~15K live T2
#                            prefill + a 4096-tok generation; memory-validated
#                            at ~2.39 GB min-avail, Sprint 77.4).
#   GROVE_LLAMA_T2_NGL       GPU layers to offload (default 99 = all).
#   GROVE_LLAMA_BIN          llama-server binary (default: first on PATH).
#   GROVE_TAILSCALE_BIN      tailscale binary (default: first on PATH).
#   GROVE_LLAMA_T2_WATCHDOG  watchdog script (default: repo mlx-harness copy).
#   GROVE_LLAMA_T2_FLOOR_GB  watchdog floor in GB (default 1.5).
#
# Sprint 77.4 — gpt-oss-llama-bind.

set -uo pipefail

MODEL="${GROVE_LLAMA_T2_MODEL:?set GROVE_LLAMA_T2_MODEL to the gpt-oss GGUF path}"
PORT="${GROVE_LLAMA_T2_PORT:-8080}"
CTX="${GROVE_LLAMA_T2_CTX:-24576}"
NGL="${GROVE_LLAMA_T2_NGL:-99}"
LLAMA_BIN="${GROVE_LLAMA_BIN:-$(command -v llama-server || true)}"
TAILSCALE_BIN="${GROVE_TAILSCALE_BIN:-$(command -v tailscale || true)}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="${GROVE_LLAMA_T2_WATCHDOG:-${SELF_DIR}/mlx-harness/mlx_watchdog.py}"
FLOOR_GB="${GROVE_LLAMA_T2_FLOOR_GB:-1.5}"

[ -n "$LLAMA_BIN" ]     || { echo "[serve-llama-t2] llama-server not found (set GROVE_LLAMA_BIN)"; exit 1; }
[ -n "$TAILSCALE_BIN" ] || { echo "[serve-llama-t2] tailscale not found (set GROVE_TAILSCALE_BIN)"; exit 1; }
[ -f "$MODEL" ]         || { echo "[serve-llama-t2] model not found: $MODEL"; exit 1; }

# 1. Wait for the Tailscale IP (boot-ordering vs tailscaled).
TS_IP=""
for _ in $(seq 1 60); do
  TS_IP="$("$TAILSCALE_BIN" ip -4 2>/dev/null | head -1)"
  [ -n "$TS_IP" ] && break
  sleep 2
done
[ -n "$TS_IP" ] || { echo "[serve-llama-t2] no Tailscale IP after 120s — exiting (launchd KeepAlive retries)"; exit 1; }
echo "[serve-llama-t2] binding llama-server to ${TS_IP}:${PORT} (ctx=${CTX}, ngl=${NGL})"

# 2. llama-server, bound to the Tailscale IP only (not loopback, not 0.0.0.0).
#    --jinja applies the GGUF's embedded harmony template — the path by which
#    llama.cpp parses tool calls into structured message.tool_calls (parser-free).
"$LLAMA_BIN" -m "$MODEL" -c "$CTX" -ngl "$NGL" --jinja \
  --host "$TS_IP" --port "$PORT" &
LLAMA_PID=$!

# 3. Arm the watchdog backstop (best-effort; never blocks serving).
WD_PID=""
if [ -f "$WATCHDOG" ] && command -v python3 >/dev/null 2>&1; then
  python3 "$WATCHDOG" --target-pid "$LLAMA_PID" --floor-gb "$FLOOR_GB" --interval-ms 150 &
  WD_PID=$!
  echo "[serve-llama-t2] watchdog armed (pid ${WD_PID}, floor ${FLOOR_GB}GB, target ${LLAMA_PID})"
fi

# 4. Stay foreground for launchd. If llama-server dies, exit so KeepAlive
#    restarts the whole unit (and re-waits for Tailscale, re-arms the watchdog).
trap 'kill "$LLAMA_PID" "$WD_PID" 2>/dev/null' TERM INT
wait "$LLAMA_PID"
RC=$?
[ -n "$WD_PID" ] && kill "$WD_PID" 2>/dev/null
echo "[serve-llama-t2] llama-server exited (${RC}) — exiting for launchd restart"
exit "$RC"
