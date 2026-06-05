#!/usr/bin/env bash
#
# deploy.sh — push the latest origin/main to the running VM, ship the freshly
# built dashboard UI, and restart the services.
#
# Runs from the OPERATOR'S Mac. Builds the dashboard web UI locally, ships the
# compiled dist to the VM over the IAP tunnel, then SSHes in to force the VM's
# checkout to mirror origin/main (never diverges), reinstall the package, and
# restart the gateway (and a running dashboard). Prints the deployed commit.
# Exits non-zero on any failure.
#
# The dashboard UI is built on the Mac because the e2-small VM lacks the RAM to
# run `vite build`; the dist (hermes_cli/web_dist, vite's outDir) is gitignored,
# so `git reset` never carries it and it must be shipped out of band.
#
# Usage: scripts/deploy.sh [--zone ZONE] [--instance NAME]
#
# Sprint 59 — gcp-hosting-v1. Dashboard UI build/ship added in Sprint 61.

set -euo pipefail
cd "$(dirname "$0")/.."   # run from repo root: web/ + hermes_cli/ live there

ZONE="europe-west1-b"
INSTANCE="hermes-gateway"
REPO_DIR="/home/hermes/hermes-autonomaton-refactor"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone)     ZONE="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v gcloud >/dev/null 2>&1 || {
  echo "ERROR: gcloud CLI not found." >&2; exit 1; }

echo "▸ Deploying origin/main to ${INSTANCE} (${ZONE})"

# ── Build the dashboard UI locally and ship it ─────────────────────────
# The dashboard serves a compiled Vite UI from hermes_cli/web_dist (vite's
# outDir — gitignored, so `git reset` never carries it). The e2-small can't
# build it (RAM), so build on the Mac and ship the dist over the IAP tunnel.
# NOTE: a plain `rsync hermes@<IP>:web/dist` can't work here — inbound SSH is
# IAP-only (no direct-IP SSH), OS Login logs us in as the operator account (not
# hermes), /home/hermes is 0750, and the build lands in hermes_cli/web_dist
# (not web/dist). So tar the real dist and pipe it through IAP, extracting AS
# hermes; `rm -rf` first gives the mirror (--delete) semantics.
echo "▸ Building dashboard UI locally"
( cd web && npm install && npm run build )

echo "▸ Shipping dashboard UI to ${INSTANCE} over IAP"
SHIP_CMD="sudo -u hermes bash -c 'set -euo pipefail; rm -rf ${REPO_DIR}/hermes_cli/web_dist; tar -C ${REPO_DIR}/hermes_cli -xf -'"
tar -C hermes_cli -cf - web_dist | gcloud compute ssh "hermes@${INSTANCE}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --command="${SHIP_CMD}"
echo "  dashboard UI shipped"

# The remote block is forced-sync + reinstall + restart, then echo the hash.
# `set -e` inside the remote shell makes any step's failure fail the whole
# command, and gcloud propagates that non-zero exit back to us.
#
# OS Login logs us in as the operator's own account, NOT 'hermes' (the
# `hermes@` in the ssh target is overridden — gcloud prints a notice). That
# account can't even enter /home/hermes (mode 0750 on Ubuntu 24.04), and the
# repo + venv are owned by hermes anyway, so the checkout + reinstall run AS
# hermes via `sudo -u hermes`. The service restart needs root. Operator
# accounts with roles/compute.osAdminLogin get passwordless sudo from the OS
# Login guest agent, so neither sudo call prompts. (Sprint 59 deploy.sh
# assumed the remote ran as hermes; corrected inline during the Sprint 60
# deploy — its first real end-to-end run.)
REMOTE_CMD="$(cat <<REMOTE
set -euo pipefail
sudo -u hermes -H bash -c 'set -euo pipefail; cd "${REPO_DIR}"; git fetch origin main; git reset --hard origin/main; .venv/bin/pip install -e . --quiet'
sudo systemctl restart hermes-gateway
# Restart the dashboard only if its unit exists AND is already running, so a
# deploy refreshes a live dashboard with the new dist but never auto-starts one
# the operator has deliberately left stopped (mirrors the enable-but-not-start
# pattern).
if systemctl cat hermes-dashboard >/dev/null 2>&1; then sudo systemctl try-restart hermes-dashboard; fi
echo "DEPLOYED_COMMIT=\$(sudo -u hermes git -C '${REPO_DIR}' rev-parse --short HEAD)"
REMOTE
)"

OUTPUT="$(gcloud compute ssh "hermes@${INSTANCE}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --command="${REMOTE_CMD}")"

echo "${OUTPUT}"

# Surface the deployed hash explicitly.
DEPLOYED="$(printf '%s\n' "${OUTPUT}" | sed -n 's/^DEPLOYED_COMMIT=//p' | tail -1)"
if [[ -z "${DEPLOYED}" ]]; then
  echo "ERROR: deploy did not report a commit hash — check the output above." >&2
  exit 1
fi
echo "✓ Deployed ${DEPLOYED}, shipped dashboard UI, restarted services."
