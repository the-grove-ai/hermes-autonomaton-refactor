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

# Deploy gate — the andon handler's invariants. Nothing deploys unless every
# andon event closes with exactly one Kaizen answer, nothing accepted in chat
# can write a scope-defining surface, no detector imports Kaizen, and Kaizen's
# own failure goes back through the same handler. One dedicated file, run on
# its own, so a refusal names the invariant and the test that broke it rather
# than drowning in the wider suite. Skip only on purpose: SKIP_INVARIANTS=1.
# The adaptation invariants ride the same gate: an alias cannot reach a verb
# the signed session rule does not name, a typed "yes" never approves one, and
# adaptation switched off changes nothing.
GATE="tests/grove/test_andon_invariants.py tests/grove/test_adaptation.py"
if [[ "${SKIP_INVARIANTS:-0}" != "1" ]]; then
  echo "▸ Checking andon-handler invariants (${GATE})"
  GATE_OUT="$(mktemp)"
  if ! .venv/bin/python -m pytest ${GATE} -p no:cacheprovider -n0 -q -rf \
        >"${GATE_OUT}" 2>&1; then
    echo "✗ DEPLOY REFUSED — an andon-handler invariant failed:" >&2
    grep -E '^(FAILED|ERROR) ' "${GATE_OUT}" | sed 's/^/    /' >&2 \
      || tail -n 20 "${GATE_OUT}" >&2
    echo "  What was asserted (the message carries the andon id where one applies):" >&2
    grep -E '^E  ' "${GATE_OUT}" | head -n 8 | sed 's/^/    /' >&2 || true
    echo "  Each test name states the invariant it guards." >&2
    echo "  Full output: ${GATE_OUT}" >&2
    exit 1
  fi
  tail -n 1 "${GATE_OUT}"
  rm -f "${GATE_OUT}"
fi

echo "▸ Deploying origin/main to ${INSTANCE} (${ZONE})"

# Dashboard UI build/ship removed post-Sprint 64: the upstream dashboard is
# disabled and Open WebUI replaces it, so there is no Vite dist to build on the
# Mac or ship over the IAP tunnel. This deploy is now a pure code sync +
# gateway restart.

# The remote block is forced-sync + reinstall + restart, then echo the hash.
# `set -e` inside the remote shell makes any step's failure fail the whole
# command, and gcloud propagates that non-zero exit back to us.
#
# OS Login logs us in as the operator's own account, NOT 'hermes'. That
# account can't even enter /home/hermes (mode 0750 on Ubuntu 24.04), and the
# repo + venv are owned by hermes anyway, so the checkout + reinstall run AS
# hermes via `sudo -u hermes`. The service restart needs root. Operator
# accounts with roles/compute.osAdminLogin get passwordless sudo from the OS
# Login guest agent, so neither sudo call prompts. (Sprint 59 deploy.sh
# assumed the remote ran as hermes; corrected inline during the Sprint 60
# deploy — its first real end-to-end run.)
# fleet-hygiene-sweep P3 — embed the pre-reset drift guard's SOURCE (this
# Mac's current copy) into the remote command, so the guard runs on the VM
# BEFORE the reset without depending on the VM's pre-reset checkout carrying
# it. Runtime state lives in ~/.grove/capabilities/state/; a dirty
# config/capabilities/ at deploy time halts loud (R-B5).
GUARD_SRC="$(cat "$(dirname "$0")/check-capability-drift.sh")"

REMOTE_CMD="$(cat <<REMOTE
set -euo pipefail
sudo -u hermes -H bash -s <<'HERMES'
set -euo pipefail
cd "${REPO_DIR}"
${GUARD_SRC}
check_capability_drift "${REPO_DIR}"
git fetch origin main
git reset --hard origin/main
.venv/bin/pip install -e ".[web,mcp,dev]" --quiet

# No routing sync: ~/.grove operational/authority are operator-owned
#  instance state and are NEVER written from the repo (GRV-001 §IV;
#  SPEC 3ab780a78eef81688e15c4b5f524f5c4 Andon 5).

# capability-mutation-surface-v1 P6 (M7) — post-deploy admission recon:
# base<->overlay diff per slug + orphan ALERTs, into this transcript.
# READ-ONLY by contract (F6): deploy NEVER deletes or flushes overlay state
# under ~/.grove/capabilities/state/ — upgrades cannot cross the sovereignty
# line. Non-fatal: a recon failure is loud but never blocks the deploy.
echo "=== capability admission recon (base <-> overlay) ==="
.venv/bin/python -m grove.capability_recon \
  || echo "ANDON: admission recon failed (deploy proceeds; investigate)" >&2
HERMES
sudo systemctl restart hermes-gateway
echo "DEPLOYED_COMMIT=\$(sudo -u hermes git -C '${REPO_DIR}' rev-parse --short HEAD)"
REMOTE
)"

# Log in as the operator's own OS Login account, which has sudo; the remote
# block drops to hermes itself. A `hermes@` target used to be overridden by OS
# Login. On 2026-10-09 it was honored instead, and hermes is not a sudoer: the
# deploy stopped at its first sudo, before changing anything.
OUTPUT="$(gcloud compute ssh "${INSTANCE}" \
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
