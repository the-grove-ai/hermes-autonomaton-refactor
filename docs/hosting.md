# Hosting the Hermes gateway on GCP

This runbook stands up the Hermes Autonomaton gateway as an always-on node on
a Google Compute Engine VM, so the Telegram bot (and HTTP API) runs in the
cloud instead of on your Mac. State lives on a separate persistent disk, the
service is managed by `systemd`, and a 5-minute watchdog keeps it healthy.

The VM is a **sovereign node**: it is deliberately blind to GCP APIs
(`--no-service-account --no-scopes`). It holds its own secrets, which you copy
in by hand. Nothing about the cloud provider can read or change the
Autonomaton's governance state.

> Scripts referenced below live in `scripts/`. Run the `*-vm` provisioning and
> deploy scripts from your **Mac**; run `setup-vm.sh` and `watchdog.sh` **on
> the VM**.

---

## Locked configuration

| Setting | Value |
|---|---|
| Project | `grove-hermes-autonomaton` |
| Zone | `europe-west1-b` |
| Instance | `hermes-gateway` |
| Machine type | `e2-small` (2 vCPU, 2 GB) |
| OS | Ubuntu 24.04 LTS |
| Boot disk | 20 GB SSD |
| Persistent disk | `grove-data-disk`, 10 GB SSD, mounted at `/mnt/grove-data` |
| Service user | `hermes` |
| Repo path | `/home/hermes/hermes-autonomaton-refactor` |
| State path | `/home/hermes/.grove` → `/mnt/grove-data/.grove` (symlink) |
| SSH | IAP tunnel only (no public IP) |
| Service unit | `hermes-gateway.service` |

---

## Prerequisites

- The **gcloud CLI** installed and authenticated (`gcloud auth login`).
- A **billing-enabled** GCP project named `grove-hermes-autonomaton`
  (`gcloud projects create grove-hermes-autonomaton` if it does not exist,
  then link billing in the console).
- Your local Mac gateway working, with `~/.grove/` populated (config, secrets,
  skills, memory). You will copy this to the VM.

---

## Step 1 — Provision the infrastructure (from your Mac)

```bash
scripts/provision-vm.sh
```

This enables the Compute + IAP APIs, creates the `grove-data-disk` persistent
disk, the IAP-SSH firewall rule, and the `hermes-gateway` VM (no public IP,
no service account). It is idempotent — re-run it freely. On success it prints
the SSH command.

If your org policy blocks IAP, the script prints a **fallback** (ephemeral
public IP restricted to your current IP). It does not apply the fallback
automatically — run those commands yourself only if IAP is unavailable.

---

## Step 2 — Set up the runtime (on the VM)

SSH in over the IAP tunnel:

```bash
gcloud compute ssh hermes@hermes-gateway --zone=europe-west1-b --tunnel-through-iap
```

Then run the setup script with sudo:

```bash
sudo bash /home/hermes/hermes-autonomaton-refactor/scripts/setup-vm.sh
```

> First time, the repo will not exist yet. Either clone it first
> (`git clone https://github.com/the-grove-ai/hermes-autonomaton-refactor.git
> ~/hermes-autonomaton-refactor`) and run the script from there, or copy
> `setup-vm.sh` over with `gcloud compute scp` and run it — it clones the repo
> for you.

`setup-vm.sh` formats and mounts the data disk (adding it to `/etc/fstab`),
creates the `hermes` user, installs Python 3.13 and Node.js 20, clones the
repo, builds the venv, wires `~/.grove → /mnt/grove-data/.grove`, installs and
**enables** the systemd unit (without starting it — secrets are not present
yet), and installs the watchdog cron. It is idempotent.

> **No `npm install`.** The MCP servers (e.g. notion) are fetched on demand by
> `npx`; Node.js is installed only so `npx` works. There are no local Node
> packages to build for the headless gateway.

---

## Step 3 — Migrate state (from your Mac)

Copy your local `~/.grove/` to the VM. This is what makes the cloud node *your*
node — its config, secrets, skills, and memory.

```bash
# Optional: define an SSH alias so rsync can reach the VM through IAP.
# In ~/.ssh/config the gcloud tunnel can be wrapped, or rsync directly:
rsync -avz -e "gcloud compute ssh hermes@hermes-gateway --zone=europe-west1-b --tunnel-through-iap --" \
  ~/.grove/ :/mnt/grove-data/.grove/
```

What travels (everything under `~/.grove/`):

- `.env` — provider API keys, bot tokens (the secrets)
- `config.yaml` — gateway + platform configuration (Telegram token wiring, MCP
  server definitions)
- `routing.config.yaml` — tier bindings (operator copy)
- `routing.autonomaton.yaml` — Flywheel-approved routing changes
- `zones.schema.yaml` — sovereignty rules
- `goals.md` — operator goals for the classifier
- `skills/` — promoted skills (incl. `productivity/google-workspace/` and its
  `google_token.json` / `google_client_secret.json` credentials)
- `memories/` — persistent memory store
- `pattern_cache.db` — compiled T0 patterns
- `intent_records.jsonl` — the Flywheel evidence the T0 compiler mines
- `proposals.jsonl` — pending Flywheel proposals
- `telemetry.db` — telemetry (optional; large, can be skipped)

> **Do not copy** `*.lock`, `*.db-wal`, `*.db-shm`, or the `.kaizen_ledger`
> live files while the Mac gateway is running — stop it first (Step 4 note).

---

## Step 4 — Start the service (on the VM)

```bash
sudo systemctl start hermes-gateway
sudo systemctl status hermes-gateway --no-pager
```

> **Stop the Mac gateway first.** Telegram allows only ONE long-poller per bot
> token; two pollers produce `409 Conflict` and neither works reliably. On the
> Mac: `hermes gateway stop` (or unload the launchd job). Only one node should
> hold the token at a time.

---

## Step 5 — Verification checklist

Work through these from your phone (Telegram) and an SSH session:

- [ ] **Telegram responds from the VM** — send a message; confirm a reply, and
      confirm the Mac gateway is stopped (the VM is now the only poller).
- [ ] **`hermes doctor` runs clean** — on the VM:
      `~/hermes-autonomaton-refactor/.venv/bin/hermes doctor`
- [ ] **T0 patterns serve** — ask a query you know is cached (e.g. a promoted
      "what is 2+2"); confirm an instant answer with no tier footer.
- [ ] **Calendar query works** — ask "what's on my calendar today?"; confirms
      the `google-workspace` skill + its OAuth token migrated correctly.
- [ ] **Memory works** — ask it to remember a fact, then recall it.
- [ ] **`hermes doctor --restart --force` cycles cleanly** — on the VM; confirm
      the bot reconnects after.
- [ ] **`deploy.sh` works** — from your Mac: `scripts/deploy.sh`; confirm it
      prints a commit hash and the bot reconnects.
- [ ] **Mac gateway is stopped** — only one Telegram poller per token.

---

## Operations

**Deploy the latest `origin/main`:**

```bash
scripts/deploy.sh                      # defaults: europe-west1-b / hermes-gateway
scripts/deploy.sh --zone us-central1-a --instance hermes-gateway
```

Forces the VM's checkout to mirror `origin/main`, reinstalls the package,
restarts the service, and prints the deployed commit.

**SSH in:**

```bash
gcloud compute ssh hermes@hermes-gateway --zone=europe-west1-b --tunnel-through-iap
```

**Tail logs (journald — the gateway and the watchdog both log here):**

```bash
journalctl -u hermes-gateway -f                 # gateway
journalctl -t hermes-watchdog -f                # watchdog
journalctl -u hermes-gateway --since "1 hour ago"
```

**Back up state (from your Mac):**

```bash
rsync -avz -e "gcloud compute ssh hermes@hermes-gateway --zone=europe-west1-b --tunnel-through-iap --" \
  :/mnt/grove-data/.grove/ ~/grove-vm-backup/
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `409 Conflict` from Telegram, bot flaps | Two pollers on one token. Stop the Mac gateway (`hermes gateway stop`); only the VM should poll. |
| Service won't start, `status` shows `(code=exited)` | Secrets missing — confirm `~/.grove/.env` migrated and has a provider key + bot token. `journalctl -u hermes-gateway -n 50`. |
| `/mnt/grove-data` empty after reboot | fstab line missing or disk detached. `mount \| grep grove-data`; re-run `setup-vm.sh` (idempotent) to re-add the fstab entry. |
| `~/.grove` is a real dir, not a symlink | Something wrote to it before the symlink existed. Move it aside (`mv ~/.grove ~/.grove.bak`) and re-run `setup-vm.sh`. |
| IAP tunnel refused | IAP API not enabled, or org policy blocks IAP. Re-run `provision-vm.sh`; if still blocked, use the printed public-IP fallback. |
| Service stuck `failed`, won't restart | systemd start-limit hit. `sudo systemctl reset-failed hermes-gateway && sudo systemctl start hermes-gateway`. The watchdog also recovers this within 5 minutes via `hermes doctor --restart --force`. |
| Calendar / Gmail skill errors | The `google_token.json` did not migrate, or expired. Re-copy `~/.grove/skills/productivity/google-workspace/` and re-run the skill's setup if needed. |

---

## Teardown

Stop and disable the service, then delete the VM (the persistent disk can be
kept to preserve state, or deleted):

```bash
# On the VM (or via deploy-style SSH):
sudo systemctl stop hermes-gateway
sudo systemctl disable hermes-gateway

# From your Mac — delete the VM (keeps the data disk):
gcloud compute instances delete hermes-gateway --zone=europe-west1-b --keep-disks=data

# To also delete the persistent state disk (destructive):
gcloud compute disks delete grove-data-disk --zone=europe-west1-b

# Remove the firewall rule:
gcloud compute firewall-rules delete allow-iap-ssh
```

Back up `/mnt/grove-data/.grove/` (see Operations) before deleting the disk.
