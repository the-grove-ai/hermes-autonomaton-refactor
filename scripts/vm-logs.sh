#!/usr/bin/env bash
# scripts/vm-logs.sh — View hermes-gateway VM logs via IAP SSH.
# Usage:
#   bash scripts/vm-logs.sh                    # last 50 lines of gateway
#   bash scripts/vm-logs.sh -f                 # follow (tail -f) gateway
#   bash scripts/vm-logs.sh -n 100             # last 100 lines
#   bash scripts/vm-logs.sh openwebui          # Open WebUI logs
#   bash scripts/vm-logs.sh openwebui -f       # follow Open WebUI logs
#   bash scripts/vm-logs.sh grep "notion"      # grep gateway logs
#   bash scripts/vm-logs.sh status             # systemctl status of all services
#   bash scripts/vm-logs.sh tools              # dump the agent's runtime tool count
#   bash scripts/vm-logs.sh mem                # memory usage snapshot

set -euo pipefail

ZONE="europe-west1-b"
VM="hermes-gateway"
SSH="gcloud compute ssh ${VM} --zone=${ZONE} --tunnel-through-iap --command"

SERVICE="hermes-gateway"
LINES=50
MODE="tail"
GREP_PATTERN=""
FOLLOW=""

# Parse arguments
case "${1:-}" in
  openwebui|open-webui|webui)
    SERVICE="open-webui"
    shift
    ;;
  dashboard)
    SERVICE="hermes-dashboard"
    shift
    ;;
  status)
    exec $SSH "systemctl is-active hermes-gateway open-webui 2>/dev/null; echo '---'; sudo ss -tlnp | grep -E '8642|8080|9119' || echo 'no listeners'; echo '---'; free -h | head -2; echo '---'; sudo systemctl status hermes-gateway open-webui --no-pager -l 2>/dev/null | head -30"
    ;;
  tools)
    # Dump the live tool registry — builtin + MCP — so CC can verify MCP
    # tools are actually present without guessing from journal logs.
    #
    # The Python is kept readable in a quoted heredoc (no shell expansion
    # inside it), then base64-encoded locally and decoded on the VM. This
    # is the only quoting scheme that survives the ssh → sudo → bash -c →
    # python nesting: base64 is a bare [A-Za-z0-9+/=] token, so nothing in
    # the source can break out through the layers of quotes.
    #
    # discover_mcp_tools() is the real entry point (load config → connect
    # → register); register_builtin_tools() alone never yields mcp_* names,
    # which is why the previous version always reported "MCP tools: 0".
    read -r -d '' PYSRC <<'PYEOF' || true
from tools.registry import ToolRegistry, register_builtin_tools
from tools.mcp_tool import discover_mcp_tools

reg = ToolRegistry()
builtin = register_builtin_tools(reg)
try:
    discover_mcp_tools(registry=reg)
except Exception as exc:
    print(f"MCP discovery failed: {exc!r}")

names = reg.get_all_tool_names()
mcp = sorted(n for n in names if n.startswith("mcp_"))
print(f"Builtin tools: {len(builtin)}")
print(f"Total registry tools: {len(names)}")
print(f"MCP tools: {len(mcp)}")
for m in mcp:
    print(f"  {m}")
PYEOF
    B64=$(printf '%s' "$PYSRC" | base64 | tr -d '\n')
    exec $SSH "sudo -u hermes bash -c 'cd /home/hermes/hermes-autonomaton-refactor && echo ${B64} | base64 -d | GROVE_DUMP_REQUESTS=1 timeout 30 .venv/bin/python - 2>/dev/null || echo \"Tool dump failed\"'"
    ;;
  mem)
    exec $SSH "free -h; echo '---'; ps aux --sort=-rss | head -8"
    ;;
  grep)
    GREP_PATTERN="${2:-}"
    if [ -z "$GREP_PATTERN" ]; then
      echo "Usage: $0 grep <pattern>" >&2
      exit 1
    fi
    exec $SSH "sudo journalctl -u ${SERVICE} --no-pager -n 200 | grep -i '${GREP_PATTERN}' | tail -30"
    ;;
  -f)
    FOLLOW="-f"
    shift
    ;;
  -n)
    LINES="${2:-50}"
    shift 2 || true
    ;;
  "")
    # defaults
    ;;
  *)
    echo "Usage: $0 [openwebui|status|tools|mem|grep <pattern>] [-f] [-n lines]" >&2
    exit 1
    ;;
esac

# Handle remaining flags after service selection
for arg in "$@"; do
  case "$arg" in
    -f) FOLLOW="-f" ;;
    -n) ;; # next arg is the count, handled below
    *) if [ "${prev:-}" = "-n" ]; then LINES="$arg"; fi ;;
  esac
  prev="$arg"
done

if [ -n "$FOLLOW" ]; then
  exec $SSH "sudo journalctl -u ${SERVICE} -f --no-pager"
else
  exec $SSH "sudo journalctl -u ${SERVICE} --no-pager -n ${LINES}"
fi
