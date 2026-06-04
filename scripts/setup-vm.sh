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

# ── 7. Python venv + editable install ──────────────────────────────────
say "Creating venv and installing the package (editable)"
as_hermes "cd '${REPO_DIR}' && [[ -d .venv ]] || python3.13 -m venv .venv"
as_hermes "cd '${REPO_DIR}' && .venv/bin/pip install --upgrade pip >/dev/null && .venv/bin/pip install -e ."

# NOTE (GATE-A decision B): no local `npm install` — MCP servers are
# npx-fetched (see step 4). Node/npx presence is all the gateway needs.

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
