#!/usr/bin/env bash
#
# deploy.sh — push the latest origin/main to the running VM and restart.
#
# Runs from the OPERATOR'S Mac. SSHes to the VM over the IAP tunnel, forces
# the VM's checkout to mirror origin/main (never diverges), reinstalls the
# package, and restarts the gateway. Prints the deployed commit. Exits
# non-zero on any failure.
#
# No local npm install (Sprint 59 GATE-A decision B — MCP servers are
# npx-fetched).
#
# Usage: scripts/deploy.sh [--zone ZONE] [--instance NAME]
#
# Sprint 59 — gcp-hosting-v1.

set -euo pipefail

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

# The remote block is forced-sync + reinstall + restart, then echo the hash.
# `set -e` inside the remote shell makes any step's failure fail the whole
# command, and gcloud propagates that non-zero exit back to us.
REMOTE_CMD="$(cat <<REMOTE
set -euo pipefail
cd '${REPO_DIR}'
git fetch origin main
git reset --hard origin/main
.venv/bin/pip install -e . --quiet
sudo systemctl restart hermes-gateway
echo "DEPLOYED_COMMIT=\$(git rev-parse --short HEAD)"
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
echo "✓ Deployed ${DEPLOYED} and restarted hermes-gateway."
