#!/usr/bin/env bash
#
# setup-vm.sh — provision the Hermes gateway runtime ON the VM.
#
# Runs on the VM after the first SSH (see scripts/provision-vm.sh output).
# Idempotent: safe to re-run. Every apt/curl call is non-interactive — there
# are NO prompts anywhere, so this works unattended over SSH.
#
# It does NOT start the gateway: secrets are not present yet. The operator
# rsyncs ~/.grove/ and then `systemctl start hermes-gateway` (docs/hosting.md).
#
# Sprint 59 — gcp-hosting-v1.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# ── Configuration ─────────────────────────────────────────────────────
HERMES_USER="hermes"
HOME_DIR="/home/${HERMES_USER}"
REPO_URL="https://github.com/the-grove-ai/hermes-autonomaton-refactor.git"
REPO_DIR="${HOME_DIR}/hermes-autonomaton-refactor"
DATA_MOUNT="/mnt/grove-data"
DATA_DEV="/dev/disk/by-id/google-grove-data"   # set by --device-name=grove-data
GROVE_LINK="${HOME_DIR}/.grove"
GROVE_TARGET="${DATA_MOUNT}/.grove"

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }

# Run a command as the hermes user (this script runs under sudo/root).
as_hermes() { sudo -u "${HERMES_USER}" -H bash -lc "$*"; }

[[ "${EUID}" -eq 0 ]] || { echo "ERROR: run with sudo (root needed for apt/disk/systemd)." >&2; exit 1; }

# ── 1. Persistent disk: format (first time only) + mount + fstab ───────
say "Configuring persistent data disk at ${DATA_MOUNT}"
mkdir -p "${DATA_MOUNT}"
if ! blkid "${DATA_DEV}" >/dev/null 2>&1; then
  say "Disk is unformatted — creating ext4 filesystem (first run only)"
  mkfs.ext4 -m 0 -F -E lazy_itable_init=0,lazy_journal_init=0,discard "${DATA_DEV}"
fi
if ! mountpoint -q "${DATA_MOUNT}"; then
  mount -o discard,defaults "${DATA_DEV}" "${DATA_MOUNT}"
fi
# Persist across reboots via a UUID entry (nofail so a missing disk never
# blocks boot). Idempotent: only append if the UUID line is absent.
DISK_UUID="$(blkid -s UUID -o value "${DATA_DEV}")"
if ! grep -q "${DISK_UUID}" /etc/fstab; then
  echo "UUID=${DISK_UUID} ${DATA_MOUNT} ext4 discard,defaults,nofail 0 2" >> /etc/fstab
  ok "Added ${DATA_MOUNT} to /etc/fstab"
fi

# ── 2. hermes service user ─────────────────────────────────────────────
if ! id "${HERMES_USER}" >/dev/null 2>&1; then
  say "Creating service user ${HERMES_USER}"
  useradd -m -s /bin/bash "${HERMES_USER}"
fi
chown "${HERMES_USER}:${HERMES_USER}" "${DATA_MOUNT}"

# ── 3. Python 3.13 (deadsnakes) ────────────────────────────────────────
say "Installing Python 3.13"
apt-get update -y
apt-get install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -y
apt-get install -y python3.13 python3.13-venv python3.13-dev

# ── 4. Node.js 20 LTS (NodeSource) ─────────────────────────────────────
# Node is required ONLY so `npx` can fetch the notion MCP server on demand —
# config.yaml runs it via `command: npx @notionhq/notion-mcp-server`. The MCP
# servers are npx-fetched, NOT local packages in this repo, so there is NO
# local `npm install` step (Sprint 59 GATE-A decision B: skip local npm
# installs; Node present for npx is sufficient).
say "Installing Node.js 20 LTS (for npx-fetched MCP servers)"
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# ── 4b. Tailscale (private mesh for dashboard access) ──────────────────
# Installs the Tailscale client non-interactively. Does NOT join the tailnet —
# that needs the operator's auth key (run `sudo tailscale up --authkey=...`
# manually; see docs/hosting.md). No public ports are opened by this.
say "Installing Tailscale (mesh client; joining the tailnet is a manual step)"
curl -fsSL https://tailscale.com/install.sh | sh
ok "Tailscale installed — run 'sudo tailscale up --authkey=<YOUR_KEY>' to join your tailnet"

# ── 5. System dependencies ─────────────────────────────────────────────
say "Installing system dependencies (git, jq)"
apt-get install -y git jq

# ── 6. Repo: clone or fast-forward to origin/main ──────────────────────
if [[ -d "${REPO_DIR}/.git" ]]; then
  say "Repo present — syncing to origin/main"
  as_hermes "cd '${REPO_DIR}' && git fetch origin main && git reset --hard origin/main"
else
  say "Cloning repo into ${REPO_DIR}"
  as_hermes "git clone '${REPO_URL}' '${REPO_DIR}'"
fi

# ── 7. Python venv + editable install (with the `web` extra) ────────────
# The `web` extra (fastapi + uvicorn[standard]) is required by the `hermes
# dashboard` HTTP server — a declared package extra in pyproject, NOT a separate
# skill dep, so it installs here with the package. (Sprint 61 deployment
# finding: the dashboard crash-loops on a missing fastapi import without it.)
say "Creating venv and installing the package (editable, with the web extra)"
as_hermes "cd '${REPO_DIR}' && [[ -d .venv ]] || python3.13 -m venv .venv"
as_hermes "cd '${REPO_DIR}' && .venv/bin/pip install --upgrade pip >/dev/null && .venv/bin/pip install -e '.[web]'"

# NOTE (GATE-A decision B): no local `npm install` — MCP servers are
# npx-fetched (see step 4). Node/npx presence is all the gateway needs.

# ── 7b. Skill runtime deps (NOT in the hermes package) ─────────────────
# The google-workspace skill shells out to scripts/google_api.py, which
# imports the Google client libraries. `pip install -e .` does not pull them
# (they're a skill dep, not a package dep), so install them into the venv
# here (Sprint 59 deployment finding). Run from the repo dir so pip's cwd is
# readable by the hermes user.
say "Installing google-workspace skill deps into the venv"
as_hermes "cd '${REPO_DIR}' && .venv/bin/pip install \
  google-api-python-client google-auth-httplib2 google-auth-oauthlib"

# ── 8. State directory on the persistent disk + symlink ────────────────
say "Wiring ~/.grove -> ${GROVE_TARGET} (state lives on the persistent disk)"
mkdir -p "${GROVE_TARGET}"
chown "${HERMES_USER}:${HERMES_USER}" "${GROVE_TARGET}"
if [[ -L "${GROVE_LINK}" ]]; then
  : # symlink already present
elif [[ -e "${GROVE_LINK}" ]]; then
  echo "ERROR: ${GROVE_LINK} exists and is not a symlink — move it aside first." >&2
  exit 1
else
  as_hermes "ln -s '${GROVE_TARGET}' '${GROVE_LINK}'"
fi
as_hermes "mkdir -p '${GROVE_LINK}/logs'"

# ── 9. systemd service (enabled, NOT started — secrets absent) ─────────
say "Installing the systemd unit"
install -m 0644 "${REPO_DIR}/scripts/hermes-gateway.service" /etc/systemd/system/hermes-gateway.service
systemctl daemon-reload
systemctl enable hermes-gateway
ok "Service enabled (start it after copying secrets)"

# ── 9b. Upstream dashboard: disabled — Open WebUI replaces it (Sprint 64) ──
# Keep the unit file on disk (so it can be re-enabled if ever needed) but stop
# and disable it: Open WebUI is now the operator's web surface, and the e2-small
# needs the RAM the dashboard would otherwise hold.
say "Disabling the upstream dashboard (Open WebUI replaces it)"
install -m 0644 "${REPO_DIR}/scripts/hermes-dashboard.service" /etc/systemd/system/hermes-dashboard.service
systemctl daemon-reload
systemctl disable --now hermes-dashboard 2>/dev/null || true
ok "Dashboard stopped and disabled (unit file retained, not deleted)"

# ── 9c. Swapfile (e2-small has no swap; Open WebUI install/runtime needs it) ──
# 2G swapfile on the persistent disk. The e2-small has 2G RAM and no swap;
# `pip install open-webui` and Open WebUI's runtime both spike memory and would
# risk an OOM kill without headroom. nofail so a missing disk never blocks boot.
if ! swapon --show 2>/dev/null | grep -q "${DATA_MOUNT}/swapfile"; then
  say "Creating a 2G swapfile on the persistent disk"
  fallocate -l 2G "${DATA_MOUNT}/swapfile" || dd if=/dev/zero of="${DATA_MOUNT}/swapfile" bs=1M count=2048
  chmod 600 "${DATA_MOUNT}/swapfile"
  mkswap "${DATA_MOUNT}/swapfile"
  swapon "${DATA_MOUNT}/swapfile"
  if ! grep -q "${DATA_MOUNT}/swapfile" /etc/fstab; then
    echo "${DATA_MOUNT}/swapfile none swap sw,nofail 0 0" >> /etc/fstab
  fi
  ok "2G swap active and persisted in fstab"
else
  ok "Swapfile already active"
fi

# ── 9d. Open WebUI systemd unit (enabled, NOT started) ─────────────────
# Same enable-but-don't-start pattern as the gateway: it survives reboot, but
# its launcher (and the API key it reads) only exist after the operator runs
# scripts/setup_open_webui.sh post-secrets. That script prints the `enable
# --now` command once the launcher is in place.
say "Installing the Open WebUI systemd unit"
install -m 0644 "${REPO_DIR}/scripts/open-webui.service" /etc/systemd/system/open-webui.service
systemctl daemon-reload
systemctl enable open-webui
ok "Open WebUI unit enabled (configure + start with setup_open_webui.sh after secrets land)"

# ── 10. Watchdog cron for the hermes user ──────────────────────────────
say "Installing the 5-minute watchdog cron"
CRON_LINE="*/5 * * * * ${REPO_DIR}/scripts/watchdog.sh"
# Idempotent: rebuild the crontab without any prior watchdog line, then add one.
as_hermes "( crontab -l 2>/dev/null | grep -v 'scripts/watchdog.sh' ; echo '${CRON_LINE}' ) | crontab -"
ok "Watchdog cron installed"

# ── Done ───────────────────────────────────────────────────────────────
echo
ok "Setup complete."
echo
echo "Copy secrets + state from your Mac, then start the service:"
echo
echo "    rsync -avz ~/.grove/ hermes-vm:~/.grove/"
echo "    sudo systemctl start hermes-gateway"
echo
echo "See docs/hosting.md for the full state-migration list and the"
echo "verification checklist."
echo
