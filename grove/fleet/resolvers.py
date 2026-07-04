"""input_state resolvers — generic work-detection at the ticker boundary (Phase 3).

The ticker never knows a skill's shape; it evaluates a worker's ``input_state``
predicate through a resolver dispatched on the predicate ``type``. A resolver
returns the resolved input payload when work exists, ``None`` for no work (the
one quiet path), or raises ``FleetWorkerAndon`` when it cannot tell — a cold or
unreachable source is an Andon, never a silent skip.

The gateway BROKERS the read here (the worker holds no MCP). ``notion_query``
reads via the gateway's WARM MCP session through the existing tool handler, which
inherits the circuit breaker AND is warm-session-only — an unconnected server
returns an error immediately rather than triggering a blocking cold connect, so a
cold read never stalls the 60s tick.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from grove.fleet.errors import FleetWorkerAndon

# Resolver registry: predicate type -> callable(input_state, worker_id) -> payload|None
_RESOLVERS: Dict[str, Callable[[Dict[str, Any], str], Optional[Any]]] = {}


def register_resolver(
    ptype: str, fn: Callable[[Dict[str, Any], str], Optional[Any]]
) -> None:
    _RESOLVERS[ptype] = fn


def resolve_input_state(input_state: Dict[str, Any], worker_id: str) -> Optional[Any]:
    """Dispatch on ``input_state['type']``.

    Returns the resolved payload (work exists), or None (no work). Raises
    FleetWorkerAndon on a missing/unknown type or an unresolvable read.
    """
    if not isinstance(input_state, dict) or not input_state.get("type"):
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: input_state missing a 'type'",
            worker_id=worker_id,
            check="resolver_failed",
        )
    ptype = input_state["type"]
    resolver = _RESOLVERS.get(ptype)
    if resolver is None:
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: no resolver for input_state type {ptype!r} "
            f"(known: {sorted(_RESOLVERS)})",
            worker_id=worker_id,
            check="resolver_failed",
        )
    return resolver(input_state, worker_id)


# ── notion_query ─────────────────────────────────────────────────────────────

NOTION_SERVER = "notion"
# Pinned against live mcp.notion.com in Phase 5 (first live read). The tool is
# HYPHENATED and takes a SQL-mode payload wrapped under a top-level `data` key:
#   {"data": {"mode": "sql",
#             "data_source_urls": ["collection://<id>"],
#             "query": 'SELECT * FROM "collection://<id>" WHERE "Col" = ?',
#             "params": ["<value>"]}}
# The result is DOUBLE-ENCODED: {"result": "<json string of {\"results\": [rows]}>"}
# and each row is FLAT (properties are direct keys, no "properties" wrapper).
# Overridable via input_state.tool.
NOTION_QUERY_TOOL = "notion-query-data-sources"
_RESOLVER_TIMEOUT_SECS = 30.0


def _collection_url(data_source: str) -> str:
    """Notion SQL mode addresses a data source as ``collection://<id>``."""
    ds = str(data_source).strip()
    return ds if ds.startswith("collection://") else f"collection://{ds}"


def _mcp_call(server: str, tool: str, args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """Call a warm MCP tool via the runtime's existing handler and parse the
    JSON result. Module-level so tests can monkeypatch it. The handler inherits
    the circuit breaker and returns ``{"error": ...}`` for an unconnected server
    (no blocking cold connect)."""
    from tools.mcp_tool import _make_tool_handler

    handler = _make_tool_handler(server, tool, timeout)
    raw = handler(args)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise FleetWorkerAndon(
            f"MCP tool {server}.{tool} returned non-JSON: {raw!r} ({exc})",
            check="resolver_failed",
        ) from exc


def _build_sql(ds_url: str, filter_: Dict[str, Any]) -> "tuple[str, list]":
    """Build a parameterized SELECT for the data source from an equality filter.

    Column names are quoted (Notion columns contain spaces, e.g. "Fit Score").
    Values are bound as ``?`` params (SQL-injection-safe). An empty filter
    returns every row. Checkbox columns want "__YES__"/"__NO__" as the value —
    the caller supplies those; equality on select/text uses the literal string.
    """
    if not filter_:
        return f'SELECT * FROM "{ds_url}"', []
    clauses, params = [], []
    for col, val in filter_.items():
        clauses.append(f'"{col}" = ?')
        params.append(val)
    return f'SELECT * FROM "{ds_url}" WHERE ' + " AND ".join(clauses), params


def resolve_notion_query(input_state: Dict[str, Any], worker_id: str) -> Optional[Any]:
    """Read the declared Notion data_source/filter via the warm MCP session.

    Returns ``{"rows": [...], "data_source": ..., "filter": ...}`` when matching
    rows exist, ``None`` for no work. A cold/unreachable server or an error
    result raises an Andon (routed to the observed-event bus by the manager),
    never a silent skip.
    """
    data_source = input_state.get("data_source")
    if not data_source:
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: notion_query input_state missing 'data_source'",
            worker_id=worker_id,
            check="resolver_failed",
        )
    filter_ = input_state.get("filter") or {}
    server = input_state.get("server", NOTION_SERVER)
    tool = input_state.get("tool", NOTION_QUERY_TOOL)
    ds_url = _collection_url(data_source)
    query, params = _build_sql(ds_url, filter_)

    result = _mcp_call(
        server,
        tool,
        {"data": {"mode": "sql", "data_source_urls": [ds_url], "query": query, "params": params}},
        _RESOLVER_TIMEOUT_SECS,
    )
    if isinstance(result, dict) and result.get("error"):
        # Warm-session-only handler: an error here is a cold/unreachable server
        # or a call-time breaker trip — surface loudly, do not block the tick.
        raise FleetWorkerAndon(
            f"notion_query read failed for worker {worker_id!r}: {result['error']}",
            worker_id=worker_id,
            check="resolver_cold_mcp",
        )

    # Server-side WHERE already filtered; rows are the matches.
    rows = _extract_rows(result)
    if not rows:
        return None  # legitimate no_work
    return {"rows": rows, "data_source": ds_url, "filter": filter_}


def _extract_rows(result: Any) -> List[Dict[str, Any]]:
    """Pull the flat row list out of a notion-query-data-sources result.

    The handler wraps the tool output as ``{"result": <text>}``; the text is a
    JSON STRING of ``{"results": [ {flat row}, ... ]}`` (double-encoded). Parse
    the string, then read ``results``. Defensive across the near-shapes.
    """
    payload = result.get("result", result) if isinstance(result, dict) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(payload, dict):
        for key in ("results", "rows", "pages", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


register_resolver("notion_query", resolve_notion_query)
