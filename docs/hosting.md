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
- An **OS Login role** for your account — Project Owner alone does **not**
  grant SSH to a VM with `enable-oslogin=TRUE`, so you'll get
  `Permission denied (publickey)` without it. Run once:
  ```bash
  gcloud projects add-iam-policy-binding grove-hermes-autonomaton \
    --member="user:$(gcloud config get-value account)" \
    --role=roles/compute.osAdminLogin
  ```
  (`osAdminLogin` also gives passwordless `sudo` on the VM, which `setup-vm.sh`
  needs. Propagation takes up to ~2 min.)
- Your local Mac gateway working, with `~/.grove/` populated (config, secrets,
  skills, memory). You will copy the **essential** subset to the VM (Step 3).

---

## Step 1 — Provision the infrastructure (from your Mac)

```bash
scripts/provision-vm.sh
```

This enables the Compute + IAP APIs, creates the `grove-data-disk` persistent
disk, the IAP-SSH firewall rule, **disables the default network's broad
`default-allow-ssh`/`-rdp` rules**, and creates the `hermes-gateway` VM (no
service account). It is idempotent — re-run it freely. On success it prints
the SSH command + the OS Login role reminder.

> **Networking:** the VM gets an **ephemeral external IP for egress only**
> (git/pip/npx at setup; the npx-fetched MCP servers at runtime — there is no
> Cloud NAT in this minimal setup). Inbound SSH stays **IAP-only** because the
> broad `0.0.0.0/0` SSH/RDP rules are disabled, so always connect with
> `--tunnel-through-iap` (a direct connection to the public IP is blocked).

---

## Step 2 — Set up the runtime (on the VM)

SSH in over the IAP tunnel:

```bash
gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap
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
repo, builds the venv, `pip install -e .`, **installs the google-workspace
skill's Google client libraries into the venv** (they're a skill dep, not a
package dep, so the editable install doesn't pull them), wires
`~/.grove → /mnt/grove-data/.grove`, installs and **enables** the systemd unit
(without starting it — secrets are not present yet), and installs the watchdog
cron. It is idempotent.

> **No `npm install`.** The MCP servers (e.g. notion) are fetched on demand by
> `npx`; Node.js is installed only so `npx` works. There are no local Node
> packages to build for the headless gateway.

---

## Step 3 — Migrate state (from your Mac)

**Do NOT rsync all of `~/.grove`.** It's ~200 MB, most of it non-portable: a
macOS binary (`bin/tirith` is Mach-O — broken on Linux), session transcripts,
LSP caches, and a large `telemetry.db` that regenerates. Copy only the
**essential, portable** state (~2 MB). Stop the Mac gateway first
(`hermes gateway stop`) so the SQLite DBs are quiesced.

**Build a curated tarball on your Mac:**

```bash
cd ~/.grove
tar czf /tmp/grove-state.tgz \
  .env config.yaml $(ls routing.config*.yaml 2>/dev/null) \
  zones.schema.yaml tool_groups.yaml context_length_cache.yaml \
  goals.md soul.md constitution.md operator.md affordances.md \
  google_token.json google_client_secret.json \
  pattern_cache.db intent_records.jsonl \
  skills memories
```

What travels (the essentials): `.env` (Telegram/Notion tokens), `config.yaml`,
the `routing.config*.yaml` set, `zones.schema.yaml`, the identity files
(`goals/soul/constitution/operator/affordances.md`), the Google OAuth files
(`google_token.json` / `google_client_secret.json` — file-based, no env var),
`pattern_cache.db` (T0 patterns), `intent_records.jsonl` (Flywheel evidence),
`skills/`, and `memories/`. Excluded: `bin/`, `sessions/`, `lsp/`, `logs/`,
`telemetry.db*`, caches, `kanban.db`, and the volatile `*.lock`/`*-wal`/`*-shm`.

**Ship + extract it** (pipes over IAP; extracts as `hermes`):

```bash
gcloud compute scp --tunnel-through-iap --zone=europe-west1-b \
  /tmp/grove-state.tgz hermes-gateway:/tmp/grove-state.tgz
gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap --command='
  sudo tar xzf /tmp/grove-state.tgz -C /mnt/grove-data/.grove/
  sudo chown -R hermes:hermes /mnt/grove-data/.grove
  rm -f /tmp/grove-state.tgz'
rm -f /tmp/grove-state.tgz   # the tarball holds secrets — delete the local copy
```

### The Anthropic key is NOT in `~/.grove`

On the Mac, `ANTHROPIC_API_KEY` is injected from the **macOS Keychain** via
`.zshrc` — it lives in neither `.env` nor the launchd plist, so it does **not**
travel with the tarball. The VM (blind to your Keychain) needs it written into
its `.env`. This reads the key from Keychain and pipes it over SSH **stdin**
(the value never prints to your terminal or the VM's process args):

```bash
KEY=$(security find-generic-password -s grove-anthropic-api-key -w) && \
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" | \
gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap \
  --command='sudo -u hermes tee -a /home/hermes/.grove/.env >/dev/null'
```

> Any other credential injected from the Keychain/shell on the Mac (rather than
> stored in `.grove`) follows the same pattern: pull on the Mac, append to the
> VM's `.env`. The Google OAuth token is file-based, so it rides the tarball.

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

> `deploy.sh` syncs **code only**. It does **not** re-copy the systemd unit or
> reinstall skill runtime deps. If a deploy changes `hermes-gateway.service`,
> re-install it manually:
> `sudo install -m644 <repo>/scripts/hermes-gateway.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart hermes-gateway`.
> If a deploy adds a skill dependency, install it into the venv as in Step 2.

**SSH in:**

```bash
gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap
```

**Tail logs (journald — the gateway and the watchdog both log here):**

```bash
journalctl -u hermes-gateway -f                 # gateway
journalctl -t hermes-watchdog -f                # watchdog
journalctl -u hermes-gateway --since "1 hour ago"
```

**Back up state (from your Mac):**

```bash
rsync -avz -e "gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap --" \
  :/mnt/grove-data/.grove/ ~/grove-vm-backup/
```

---

## Dashboard Access via Tailscale

The web dashboard (config, API keys, session management) runs as its own
service, reached over your private Tailscale mesh — never the public internet.

**Prerequisites**
- A Tailscale account (free tier is sufficient): https://tailscale.com
- Tailscale on your Mac: `brew install tailscale` (or the macOS app), then `tailscale up`.

**One-time VM setup**
1. Generate an auth key at https://login.tailscale.com/admin/settings/keys
2. Join the tailnet from the VM:
   ```
   gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap \
     --command='sudo tailscale up --authkey=tskey-auth-XXXX'
   ```
   Note the tailnet hostname it reports (e.g. `hermes-gateway`).

**Build, ship, and start the dashboard**
1. From your Mac, run `scripts/deploy.sh` — it builds the UI locally and ships
   the compiled dist to the VM (the e2-small can't build it itself).
2. Start the service:
   ```
   gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap \
     --command='sudo systemctl start hermes-dashboard'
   ```

**Access and verify**
- Open `http://<tailnet-hostname>:9119` from any device on your tailnet.
- From the Mac: `curl http://<tailnet-hostname>:9119` (expect HTML).
- Logs:
  ```
  gcloud compute ssh hermes-gateway --zone=europe-west1-b --tunnel-through-iap \
    --command='journalctl -u hermes-dashboard -f'
  ```

The dashboard binds `0.0.0.0:9119` with `--insecure`. That is safe here because
the GCP firewall exposes no inbound ports to the internet (SSH is IAP-only,
9119 is unreachable publicly); the only path in is the Tailscale mesh. Anyone
on your tailnet can read and manage the keys it surfaces — keep the tailnet small.

## Tailscale Mesh

Tailscale is a private WireGuard VPN that connects your own devices directly,
with no public exposure.

- **Why not a public URL.** Sovereignty: no ports open to the internet, no TLS
  certificates to manage, no auth layer to build or rotate. The mesh *is* the
  boundary.
- **Adding a device.** Install Tailscale on it and run `tailscale up` under the
  same account; it joins the tailnet and reaches the VM by its tailnet name.
- **If Tailscale is down.** The IAP tunnel remains the fallback for
  administration: `gcloud compute ssh hermes-gateway --zone=europe-west1-b
  --tunnel-through-iap`. The dashboard itself is mesh-only by design.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `409 Conflict` from Telegram, bot flaps | Two pollers on one token. Stop the Mac gateway (`hermes gateway stop`); only the VM should poll. |
| Service won't start, `status` shows `(code=exited)` | Secrets missing — confirm `~/.grove/.env` migrated and has a provider key + bot token. `journalctl -u hermes-gateway -n 50`. |
| `/mnt/grove-data` empty after reboot | fstab line missing or disk detached. `mount \| grep grove-data`; re-run `setup-vm.sh` (idempotent) to re-add the fstab entry. |
| `~/.grove` is a real dir, not a symlink | Something wrote to it before the symlink existed. Move it aside (`mv ~/.grove ~/.grove.bak`) and re-run `setup-vm.sh`. |
| IAP tunnel refused | IAP API not enabled, or org policy blocks IAP. Re-run `provision-vm.sh`; if still blocked, the VM has an external IP — temporarily add your own IP to a `tcp:22` firewall rule and SSH directly. |
| `Permission denied (publickey)` on SSH | OS Login role missing — grant `roles/compute.osAdminLogin` (see Prerequisites; Owner alone is not enough), wait ~2 min for propagation. If the role is present and it still fails, your `~/.ssh/google_compute_engine` key is **passphrase-protected** — that only works from an interactive terminal (a non-interactive script can't supply the passphrase). |
| Service stuck `failed`, won't restart | systemd start-limit hit. `sudo systemctl reset-failed hermes-gateway && sudo systemctl start hermes-gateway`. The watchdog also recovers this within 5 minutes via `hermes doctor --restart --force`. |
| `status` shows `Failed (code=exited, status=1)` after a restart | **Cosmetic.** The gateway returns non-zero on `SIGTERM` rather than 0, so systemd logs the stop as "failed." `Restart=always` brings it straight back up — confirm `Active: active (running)` and recent logs. |
| Calendar / Gmail skill errors | Either the Google client libs aren't in the venv (`setup-vm.sh` installs them; if you rebuilt the venv, re-run `.venv/bin/pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib` from the repo dir) or `google_token.json` didn't migrate / expired (re-copy it into `~/.grove/`). |

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
