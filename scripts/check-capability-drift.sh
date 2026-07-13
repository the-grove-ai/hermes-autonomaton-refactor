#!/usr/bin/env bash
#
# check-capability-drift.sh — deploy pre-reset drift guard.
# fleet-hygiene-sweep P3 (R-B5).
#
# Runtime writers target the STATE overlay (~/.grove/capabilities/state/), so
# config/capabilities/ should NEVER carry uncommitted changes at deploy time.
# If it does — a stray pin from before the P2 retarget, a manual edit, a bug —
# `git reset --hard` would silently destroy it. This guard runs BEFORE the
# reset: any tracked change to config/capabilities/ halts the deploy loud,
# names the offending paths, files a ledger event, and exits nonzero.
#
# Untracked writer litter (.bak / .lock / .tmp) is IGNORED — it is not operator
# state and is expected residue. Only tracked modifications / additions /
# deletions (the destroy-on-reset class) trip the guard.
#
# Defined as a FUNCTION so deploy.sh embeds this file's source verbatim into
# its remote heredoc (no dependency on the VM's pre-reset checkout carrying the
# guard), and the local rehearsal test sources it directly.

check_capability_drift() {
  local repo_dir="${1:?check_capability_drift: repo_dir required}"
  local grove_home="${GROVE_HOME:-$HOME/.grove}"

  # Porcelain over config/capabilities/, dropping untracked writer litter.
  local drift
  drift="$(git -C "$repo_dir" status --porcelain -- config/capabilities/ \
    | grep -vE '\.(bak|lock|tmp)$' || true)"

  if [ -z "$drift" ]; then
    return 0
  fi

  echo "==============================================================" >&2
  echo "DEPLOY HALT (fleet-hygiene-sweep P3): config/capabilities/ has" >&2
  echo "uncommitted changes that 'git reset --hard' would DESTROY." >&2
  echo "Runtime state belongs in ~/.grove/capabilities/state/, not here." >&2
  echo "Offending paths:" >&2
  echo "$drift" >&2
  echo "Commit or discard these before deploying." >&2
  echo "==============================================================" >&2

  # Ledger event (best-effort — never let the audit write mask the halt).
  local ledger_dir="$grove_home/.kaizen_ledger"
  if mkdir -p "$ledger_dir" 2>/dev/null && command -v python3 >/dev/null 2>&1; then
    DRIFT="$drift" python3 - "$ledger_dir/deploy.jsonl" <<'PY' 2>/dev/null || true
import json, os, sys, datetime
ledger = sys.argv[1]
paths = [ln[3:] for ln in os.environ.get("DRIFT", "").splitlines() if ln.strip()]
event = {
    "event_type": "deploy_drift_halt",
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "guard": "check_capability_drift",
    "paths": paths,
}
with open(ledger, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event) + "\n")
PY
  fi

  return 1
}

# Direct invocation: `check-capability-drift.sh <repo_dir>` runs the guard.
# (When SOURCED — the deploy embed + the test — this block is skipped.)
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  check_capability_drift "${1:-.}"
fi
