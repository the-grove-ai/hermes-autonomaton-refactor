#!/usr/bin/env bash
#
# sync-operator.sh — push operator-owned declarative config from the Mac to the
# VM's hermes instance over Tailscale. WHITELIST ONLY.
#
# Syncs:  dock/ soul.md constitution.md affordances.md operator.md zones.schema.yaml
# NEVER:  config.yaml .env memories/ sessions/ logs/ mcp-tokens/
#
# Transport: rsync over Tailscale SSH as the operator (jimcalhoun); writes as
# hermes via --rsync-path="sudo -u hermes rsync" (passwordless sudo to hermes is
# configured on the VM). Files land owned by hermes under /home/hermes/.grove/.
#
# Sync is ADDITIVE — no --delete. Removing a file locally does NOT remove it on
# the VM; clean those up by hand. (Conservative by design: a wrong --dest with
# --delete would be destructive.)
#
# Usage: scripts/sync-operator.sh [--dry-run] [--host HOST] [--dest DIR]
#
# Sprint 69.2 — dock-expansion-and-sync-v1.

set -euo pipefail

LOCAL_GROVE="${GROVE_HOME:-$HOME/.grove}"
HOST="hermes-gateway"
DEST="/home/hermes/.grove"
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="1"; shift ;;
    --host)    HOST="$2"; shift 2 ;;
    --dest)    DEST="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Whitelist — exactly the operator-owned declarative artifacts.
WHITELIST=( dock soul.md constitution.md affordances.md operator.md zones.schema.yaml )

# Blocklist — defense in depth. These must NEVER cross instances even if one
# somehow appears inside a whitelisted directory.
EXCLUDES=( --exclude=config.yaml --exclude=.env --exclude=memories/
           --exclude=sessions/ --exclude=logs/ --exclude=mcp-tokens/ )

command -v rsync     >/dev/null 2>&1 || { echo "ERROR: rsync not found."     >&2; exit 1; }
command -v tailscale >/dev/null 2>&1 || { echo "ERROR: tailscale not found." >&2; exit 1; }

RSYNC_OPTS=( -av "${EXCLUDES[@]}" --rsync-path="sudo -u hermes rsync" )
[[ -n "$DRY_RUN" ]] && RSYNC_OPTS+=( --dry-run )

echo "▸ Syncing operator config  ${LOCAL_GROVE}  →  ${HOST}:${DEST}"
[[ -n "$DRY_RUN" ]] && echo "  (dry run — no files written)"

synced=(); skipped=()
for item in "${WHITELIST[@]}"; do
  src="${LOCAL_GROVE}/${item}"
  if [[ ! -e "$src" ]]; then
    skipped+=( "$item (not present locally)" )
    continue
  fi
  if [[ -d "$src" ]]; then
    rsync "${RSYNC_OPTS[@]}" "${src}/" "${HOST}:${DEST}/${item}/"
  else
    rsync "${RSYNC_OPTS[@]}" "${src}"  "${HOST}:${DEST}/${item}"
  fi
  synced+=( "$item" )
done

echo
echo "── Summary ─────────────────────────────────"
echo "Synced (${#synced[@]}):"
for s in "${synced[@]}"; do echo "  ✓ $s"; done
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "Skipped (${#skipped[@]}):"
  for s in "${skipped[@]}"; do echo "  – $s"; done
fi
echo
if [[ -n "$DRY_RUN" ]]; then
  echo "Dry run complete. Re-run without --dry-run to apply."
else
  echo "⚠ Restart the gateway to load synced config:"
  echo "    ssh ${HOST} 'sudo systemctl restart hermes-gateway'"
fi
