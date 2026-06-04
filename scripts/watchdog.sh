#!/usr/bin/env bash
#
# watchdog.sh — keep the Hermes gateway alive and the process table clean.
#
# Runs on the VM via the hermes user's crontab every 5 minutes (installed by
# setup-vm.sh). It is a BACKSTOP to systemd's own Restart=always: if systemd
# has given up (start-limit hit) and the service is inactive, it forces a
# doctor restart. On every tick it reaps orphaned MCP processes.
#
# All output goes to journald via `logger -t hermes-watchdog` — journald
# handles retention/rotation. This script never writes raw log files.
#
# Note: no `set -e` — a watchdog must always run BOTH legs (restart check and
# reap) even if one leg returns non-zero.
#
# Sprint 59 — gcp-hosting-v1.

set -uo pipefail

REPO_DIR="/home/hermes/hermes-autonomaton-refactor"
HERMES="${REPO_DIR}/.venv/bin/hermes"
export GROVE_HOME="/home/hermes/.grove"

log() { logger -t hermes-watchdog "$*"; }

# ── Leg 1: is the gateway up? ─────────────────────────────────────────
if ! systemctl is-active --quiet hermes-gateway; then
  log "hermes-gateway is INACTIVE — forcing doctor restart"
  "${HERMES}" doctor --restart --force 2>&1 | logger -t hermes-watchdog
fi

# ── Leg 2: always reap orphaned MCP/Grove processes ───────────────────
"${HERMES}" doctor --reap 2>&1 | logger -t hermes-watchdog
