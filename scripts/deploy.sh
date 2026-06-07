#!/usr/bin/env bash
#
# deploy.sh — push the latest origin/main to the running VM and restart the
# services.
#
# Runs from the OPERATOR'S Mac. SSHes in to force the VM's checkout to mirror
# origin/main (never diverges), reinstall the package, and restart the gateway.
# Prints the deployed commit. Exits non-zero on any failure.
#
# Usage: scripts/deploy.sh [--zone ZONE] [--instance NAME]
#
# Sprint 59 — gcp-hosting-v1. Dashboard UI build/ship added in Sprint 61 and
# removed again post-Sprint 64: the upstream dashboard is disabled and Open
# WebUI replaces it, so there is no dist to build or ship.

set -euo pipefail
cd "$(dirname "$0")/.."   # run from repo root: web/ + hermes_cli/ live there

ZONE="us-central1-a"
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

# Dashboard UI build/ship removed post-Sprint 64: the upstream dashboard is
# disabled and Open WebUI replaces it, so there is no Vite dist to build on the
# Mac or ship over the IAP tunnel. This deploy is now a pure code sync +
# gateway restart.

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
sudo -u hermes -H bash -c 'set -euo pipefail; cd "${REPO_DIR}"; git fetch origin main; git reset --hard origin/main; .venv/bin/pip install -e ".[web,mcp]" --quiet'
sudo systemctl restart hermes-gateway
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
echo "✓ Deployed ${DEPLOYED}, restarted gateway."
