#!/usr/bin/env bash
#
# provision-vm.sh — create the GCP infrastructure for the Hermes gateway.
#
# Runs from the OPERATOR'S Mac (needs the gcloud CLI + a billing-enabled
# project). Idempotent: re-running it reconciles existing resources rather
# than failing. It creates the persistent disk, the VM, and an IAP SSH
# firewall rule, then prints the SSH command to connect.
#
# The VM is deliberately BLIND to GCP APIs (--no-service-account --no-scopes):
# it is a sovereign node that holds its own secrets, not a cloud-integrated
# service. Secrets arrive only via the operator's rsync (see docs/hosting.md).
#
# Sprint 59 — gcp-hosting-v1.

set -euo pipefail

# ── Locked configuration (Sprint 59 SPEC) ─────────────────────────────
PROJECT="grove-hermes-autonomaton"
ZONE="europe-west1-b"
INSTANCE="hermes-gateway"
MACHINE_TYPE="e2-small"
DISK_NAME="grove-data-disk"
DISK_SIZE="10GB"
BOOT_DISK_SIZE="20GB"
IMAGE_FAMILY="ubuntu-2404-lts-amd64"
IMAGE_PROJECT="ubuntu-os-cloud"
IAP_RANGE="35.235.240.0/20"   # Google's fixed IAP TCP-forwarding source range
FIREWALL_RULE="allow-iap-ssh"

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# ── Preflight ─────────────────────────────────────────────────────────
command -v gcloud >/dev/null 2>&1 || {
  echo "ERROR: gcloud CLI not found. Install the Google Cloud SDK first." >&2
  exit 1
}

current_project="$(gcloud config get-value project 2>/dev/null || true)"
if [[ "${current_project}" != "${PROJECT}" ]]; then
  say "Setting active project to ${PROJECT} (was: ${current_project:-unset})"
  gcloud config set project "${PROJECT}"
fi

# ── Enable required APIs ──────────────────────────────────────────────
say "Enabling Compute + IAP APIs (idempotent)"
gcloud services enable compute.googleapis.com iap.googleapis.com
ok "APIs enabled"

# ── Persistent data disk ──────────────────────────────────────────────
if gcloud compute disks describe "${DISK_NAME}" --zone="${ZONE}" >/dev/null 2>&1; then
  ok "Disk ${DISK_NAME} already exists"
else
  say "Creating ${DISK_SIZE} SSD persistent disk ${DISK_NAME}"
  gcloud compute disks create "${DISK_NAME}" \
    --zone="${ZONE}" \
    --size="${DISK_SIZE}" \
    --type=pd-ssd
  ok "Disk created"
fi

# ── Firewall: SSH only via IAP ────────────────────────────────────────
if gcloud compute firewall-rules describe "${FIREWALL_RULE}" >/dev/null 2>&1; then
  ok "Firewall rule ${FIREWALL_RULE} already exists"
else
  say "Creating IAP SSH firewall rule (${IAP_RANGE} → tcp:22)"
  gcloud compute firewall-rules create "${FIREWALL_RULE}" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges="${IAP_RANGE}" \
    --description="SSH via IAP tunnel only (Hermes gateway, Sprint 59)"
  ok "Firewall rule created"
fi

# ── VM instance ───────────────────────────────────────────────────────
if gcloud compute instances describe "${INSTANCE}" --zone="${ZONE}" >/dev/null 2>&1; then
  ok "Instance ${INSTANCE} already exists"
else
  say "Creating VM ${INSTANCE} (${MACHINE_TYPE}, Ubuntu 24.04, no public IP)"
  gcloud compute instances create "${INSTANCE}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --image-family="${IMAGE_FAMILY}" \
    --image-project="${IMAGE_PROJECT}" \
    --boot-disk-size="${BOOT_DISK_SIZE}" \
    --boot-disk-type=pd-ssd \
    --disk="name=${DISK_NAME},device-name=grove-data,mode=rw,boot=no" \
    --no-address \
    --no-service-account \
    --no-scopes \
    --metadata=enable-oslogin=TRUE
  ok "VM created"
fi

# ── Output: how to connect ────────────────────────────────────────────
echo
ok "Provisioning complete."
echo
echo "Connect over the IAP tunnel (no public IP):"
echo
echo "    gcloud compute ssh hermes@${INSTANCE} --zone=${ZONE} --tunnel-through-iap"
echo
echo "Then run scripts/setup-vm.sh on the VM. See docs/hosting.md."
echo
warn "If the IAP tunnel will not connect (org policy blocks IAP, or you need"
warn "faster iteration), the documented fallback is an ephemeral public IP"
warn "restricted to YOUR IP. This is NOT applied automatically — run it"
warn "yourself only if IAP is unavailable:"
echo
echo "    MY_IP=\$(curl -fsS ifconfig.me)"
echo "    gcloud compute instances add-access-config ${INSTANCE} --zone=${ZONE}"
echo "    gcloud compute firewall-rules create allow-operator-ssh \\"
echo "      --direction=INGRESS --action=ALLOW --rules=tcp:22 \\"
echo "      --source-ranges=\"\${MY_IP}/32\""
echo "    # then: gcloud compute ssh hermes@${INSTANCE} --zone=${ZONE}"
echo
