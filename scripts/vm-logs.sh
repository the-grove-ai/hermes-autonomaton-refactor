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
    exec $SSH "sudo -u hermes bash -c 'cd /home/hermes/hermes-autonomaton-refactor && GROVE_DUMP_REQUESTS=1 timeout 30 .venv/bin/python -c \"
from tools.registry import ToolRegistry
from tools import register_builtin_tools
from tools.mcp_tool import register_mcp_servers
import time
reg = ToolRegistry()
register_builtin_tools(reg)
names = reg.get_all_tool_names()
mcp = [n for n in names if n.startswith(\"mcp_\")]
print(f\"Builtin tools: {len(names)}\")
print(f\"MCP tools: {len(mcp)}\")
for m in sorted(mcp): print(f\"  {m}\")
\" 2>/dev/null || echo \"Tool dump failed\"'"
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
