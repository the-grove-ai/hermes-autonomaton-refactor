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
| Zone | `us-central1-a` |
| Instance | `hermes-gateway` |
| Machine type | `e2-standard-4` (4 vCPU, 16 GB) |
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
gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap
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
gcloud compute scp --tunnel-through-iap --zone=us-central1-a \
  /tmp/grove-state.tgz hermes-gateway:/tmp/grove-state.tgz
gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap --command='
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
gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap \
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
scripts/deploy.sh                      # defaults: us-central1-a / hermes-gateway
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
gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap
```

**Tail logs (journald — the gateway and the watchdog both log here):**

```bash
journalctl -u hermes-gateway -f                 # gateway
journalctl -t hermes-watchdog -f                # watchdog
journalctl -u hermes-gateway --since "1 hour ago"
```

**Back up state (from your Mac):**

```bash
rsync -avz -e "gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap --" \
  :/mnt/grove-data/.grove/ ~/grove-vm-backup/
```

---

## Chat Access via Tailscale (Open WebUI)

The operator's chat surface is **Open WebUI**, reached over your private
Tailscale mesh — never the public internet. It talks to the Autonomaton through
the gateway's OpenAI-compatible API server (`127.0.0.1:8642`), so **all
governance runs server-side**: tier routing, zone classification, and
Kaizen-ledger logging happen in the gateway exactly as they do for Telegram.
Open WebUI is protocol-blind. It replaces the upstream dashboard (Sprint 64).

**Governance note (known limitation).** Interactive Yellow-zone approval ("reply
*go ahead*") is available on **Telegram and the CLI only**. On the web surface,
Yellow-zone actions **auto-allow once and are logged to the Kaizen ledger** —
there is no inline approval prompt. This is a deliberate MVP boundary, not a
defect: the OpenAI chat-completions protocol has no channel for an out-of-band
approval round-trip. Use Telegram or the CLI when you want to gate an action
before it runs.

**Prerequisites**
- A Tailscale account (free tier is sufficient): https://tailscale.com
- Tailscale on your Mac: `brew install tailscale` (or the macOS app), then `tailscale up`.

**One-time VM setup**
1. Generate an auth key at https://login.tailscale.com/admin/settings/keys
2. Join the tailnet from the VM:
   ```
   gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap \
     --command='sudo tailscale up --authkey=tskey-auth-XXXX'
   ```
   Note the tailnet hostname it reports (e.g. `hermes-gateway`).

**Install and start** (after secrets are on the VM and the gateway is running)
1. Configure the API server + install Open WebUI as the `hermes` user. This
   generates `API_SERVER_*` in `~/.grove/.env`, installs Open WebUI into a
   dedicated venv, and writes the launcher:
   ```
   gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap \
     --command="sudo -u hermes -H bash -lc 'cd ~/hermes-autonomaton-refactor && bash scripts/setup_open_webui.sh'"
   ```
2. The script prints two root steps (it never escalates itself). Run them to
   bind the API server and start Open WebUI:
   ```
   gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap \
     --command='sudo systemctl restart hermes-gateway && sudo systemctl enable --now open-webui'
   ```

**Access and verify**
- Open `http://<tailnet-hostname>:8080` from any device on your tailnet; create
  the admin account on first visit (then set `ENABLE_SIGNUP=false` if you want
  to lock signups).
- API server health (on the VM): `curl -fsS http://127.0.0.1:8642/health`.
- Logs:
  ```
  gcloud compute ssh hermes-gateway --zone=us-central1-a --tunnel-through-iap \
    --command='journalctl -u open-webui -f'
  ```

**Memory on the e2-small.** Open WebUI is heavier than the dashboard it replaces.
Setup adds a 2 GB swapfile on the persistent disk and disables the dashboard to
free RAM. If the box still thrashes swap under load, upgrade to `e2-medium`
(4 GB).

Open WebUI binds `0.0.0.0:8080`. That is safe here because the GCP firewall
exposes no inbound ports to the internet (SSH is IAP-only, 8080 is unreachable
publicly); the only path in is the Tailscale mesh. The API server it talks to
stays bound to `127.0.0.1:8642`. Anyone on your tailnet can use the chat — keep
the tailnet small.

## Tailscale Mesh

Tailscale is a private WireGuard VPN that connects your own devices directly,
with no public exposure.

- **Why not a public URL.** Sovereignty: no ports open to the internet, no TLS
  certificates to manage, no auth layer to build or rotate. The mesh *is* the
  boundary.
- **Adding a device.** Install Tailscale on it and run `tailscale up` under the
  same account; it joins the tailnet and reaches the VM by its tailnet name.
- **If Tailscale is down.** The IAP tunnel remains the fallback for
  administration: `gcloud compute ssh hermes-gateway --zone=us-central1-a
  --tunnel-through-iap`. Open WebUI itself is mesh-only by design.

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
| Open WebUI loads but "model not found" / connection error | The API server isn't bound. Confirm `API_SERVER_ENABLED=true` in `~/.grove/.env`, `sudo systemctl restart hermes-gateway`, then `curl -fsS http://127.0.0.1:8642/health`. Open WebUI persists its connection settings — if you saved a wrong key/URL in the Admin UI, fix it there. |
| `open-webui` unit won't start | The launcher only exists after `scripts/setup_open_webui.sh` runs as the `hermes` user. Re-run it, then `sudo systemctl enable --now open-webui`; check `journalctl -u open-webui -n 50`. |
| Open WebUI crash-loops on `ValueError: No embedding model is loaded` | open-webui initializes an embedding model at startup and can't download one under `OFFLINE_MODE`. The launcher sets `RAG_EMBEDDING_ENGINE=openai` to take the lazy path (no model load; retrieval is bypassed anyway). If you see this, the launcher predates that fix — re-run `setup_open_webui.sh` to regenerate it, then `sudo systemctl restart open-webui`. |
| Open WebUI runs but isn't reachable over the tailnet | The launcher must bind `0.0.0.0`, not `127.0.0.1`. Check `grep '^export HOST' ~hermes/.local/bin/start-open-webui-hermes.sh` (run as hermes). `setup_open_webui.sh` defaults to `0.0.0.0` on the VM; re-run it if the launcher still shows loopback. |
| Open WebUI install OOM-killed / VM thrashing | The 2 GB swapfile from `setup-vm.sh` is missing (`swapon --show`) — re-run `setup-vm.sh` (idempotent). If it persists under load, upgrade the instance to `e2-medium`. |
| Yellow-zone action ran on the web without asking | Expected. The web surface auto-allows Yellow-zone actions (logged to the Kaizen ledger); interactive approval is Telegram/CLI only. See "Chat Access via Tailscale." |

---

## Teardown

Stop and disable the service, then delete the VM (the persistent disk can be
kept to preserve state, or deleted):

```bash
# On the VM (or via deploy-style SSH):
sudo systemctl stop hermes-gateway
sudo systemctl disable hermes-gateway

# From your Mac — delete the VM (keeps the data disk):
gcloud compute instances delete hermes-gateway --zone=us-central1-a --keep-disks=data

# To also delete the persistent state disk (destructive):
gcloud compute disks delete grove-data-disk --zone=us-central1-a

# Remove the firewall rule:
gcloud compute firewall-rules delete allow-iap-ssh
```

Back up `/mnt/grove-data/.grove/` (see Operations) before deleting the disk.
