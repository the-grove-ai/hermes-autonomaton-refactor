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

import functools
import json
from pathlib import Path
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
    # Single-unit selection (fleet-pipeline-v1 P4) — generic, blind to field
    # meaning: skip rows already staged, rank by the declared order_by, yield one.
    rows = _select_units(rows, input_state, worker_id)
    if not rows:
        return None  # every matching row already has a staged draft -> no_work
    payload = {"rows": rows, "data_source": ds_url, "filter": filter_}
    # suggest-revision-verb-v1 P3 — host-side revision fold. Surface the selected
    # row's accumulated operator guidance as an explicit, framed revision_directive
    # so the re-draft satisfies it (the worker prompt lifts it OUT of the json blob).
    # FAIL-LOUD: a corrupt store entry raises (never draft feedback-blind).
    directive = _revision_directive(rows[0].get("id"), worker_id)
    if directive:
        payload["revision_directive"] = directive
    return payload


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


# ── single-unit selection (fleet-pipeline-v1 P4, generic) ────────────────────
#
# All three steps are DRIVEN BY CONFIG and blind to meaning: the resolver does not
# know "Fit Score" is a fitness or "id" is a Notion page — it globs the worker's
# DECLARED staging_dir for staged row ids, filters, sorts by the declared order_by,
# and yields one. No skill name appears here.


def _worker_staging_dir(worker_id: str) -> Optional[Path]:
    """Resolve the worker's DECLARED staging sink (governance.write_zone.staging_dir
    on its capability record) to an absolute path — the same resolution the worker
    stages into. None when the worker / record / sink cannot be resolved."""
    from grove.capability_registry import load_capabilities
    from grove.fleet.config import load_fleet_workers
    from grove.utils.fs_utils import _grove_home_realpath, _grove_subdir_realpath

    cfg = load_fleet_workers().get(worker_id)
    if cfg is None:
        return None
    cap = load_capabilities().get(cfg.skill)
    if cap is None:
        return None
    gov = cap.governance if isinstance(cap.governance, dict) else {}
    staging = ((gov.get("write_zone") or {}).get("staging_dir"))
    grove = _grove_home_realpath()
    if not staging or grove is None:
        return None
    return Path(_grove_subdir_realpath(staging, grove))


def _staged_row_ids(worker_id: str) -> set:
    """The set of row_ids that already have a staged draft. Non-recursive glob of
    ``staging_dir/*/meta.json`` (the watcher.py:151 shape — one level; the atomic
    tmp->rename stage means the glob matches only a FINAL meta.json, never a
    ``.tmp``). A bare read is safe (rename is atomic); an unreadable/malformed
    meta.json fails LOUD — we must NOT silently treat its row as un-staged, which
    would re-draft a row that IS staged."""
    sink = _worker_staging_dir(worker_id)
    if sink is None or not sink.is_dir():
        return set()  # no sink yet -> nothing staged
    staged: set = set()
    for meta_path in sorted(sink.glob("*/meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise FleetWorkerAndon(
                f"worker {worker_id!r}: staged meta.json unreadable/malformed at "
                f"{meta_path} ({exc}) — cannot tell if its row is staged; refusing "
                f"to risk re-drafting a staged row",
                worker_id=worker_id,
                check="staged_meta_unreadable",
            )
        rid = data.get("row_id") if isinstance(data, dict) else None
        if rid:
            staged.add(rid)
    return staged


def _order_by_key(order_by: List[Dict[str, Any]]):
    """A cmp_to_key sort key honoring a multi-field order_by with per-field
    direction and NULLS-LAST (always, regardless of direction) so a missing value
    has a defined position, never an arbitrary one."""

    def _cmp(a: Dict[str, Any], b: Dict[str, Any]) -> int:
        for spec in order_by:
            field = spec.get("field")
            desc = spec.get("direction", "asc") == "desc"
            va, vb = a.get(field), b.get(field)
            if va is None and vb is None:
                continue
            if va is None:
                return 1  # nulls last
            if vb is None:
                return -1
            if va == vb:
                continue
            c = -1 if va < vb else 1
            return -c if desc else c
        return 0

    return functools.cmp_to_key(_cmp)


def _read_feedback_or_andon(row_id: Optional[str], worker_id: str):
    """``feedback_store.read(row_id)`` with a corrupt entry converted to a LOUD
    Andon (B7): a present-but-unreadable revision entry must NEVER be swallowed into
    a feedback-blind re-draft. Returns the entry dict or None."""
    if not row_id:
        return None
    from grove.forge import feedback_store

    try:
        return feedback_store.read(row_id)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise FleetWorkerAndon(
            f"revision feedback store unreadable for row {row_id!r} ({exc}) — refusing "
            f"to re-draft feedback-blind",
            worker_id=worker_id,
            check="revision_store_unreadable",
        ) from exc


def _has_revision_priority(row_id: Optional[str], worker_id: str) -> bool:
    """True iff row_id carries NON-TERMINAL operator revision guidance — the row that
    jumps the fresh-fit queue. terminal_skip rows (P4) do NOT get priority, so P4's
    exclusion composes cleanly on top of this ``not terminal_skip`` gate."""
    entry = _read_feedback_or_andon(row_id, worker_id)
    return bool(entry and not entry.get("terminal_skip") and entry.get("history"))


def _is_terminal_skip(row_id: Optional[str], worker_id: str) -> bool:
    """True iff row_id's store entry is terminal_skip (won't-converge, the N-breaker
    terminal state). Reads via the fail-loud path (corrupt -> Andon)."""
    entry = _read_feedback_or_andon(row_id, worker_id)
    return bool(entry and entry.get("terminal_skip"))


def _revision_directive(row_id: Optional[str], worker_id: str) -> Optional[str]:
    """Build the framed revision directive for row_id from the Path-B store, or None
    when there is no non-terminal guidance. Operator feedback is DELIMITED (<<< >>>)
    as guidance the fresh, corpus-only worker must satisfy — it has no prior draft in
    context, so this is draft-fresh-with-constraints, NOT a diff-edit. Accumulated
    revisions are chronological with the LATEST authoritative and priors as context,
    so a contradictory accumulation cannot crowd out the latest directive (B2).
    FAIL-LOUD on a corrupt entry (via _read_feedback_or_andon)."""
    entry = _read_feedback_or_andon(row_id, worker_id)
    if not entry or entry.get("terminal_skip"):
        return None
    notes = [
        h.get("revision_note")
        for h in (entry.get("history") or [])
        if h.get("revision_note")
    ]
    if not notes:
        return None
    directive = (
        "The operator reviewed a prior draft and rejected it with this guidance: "
        f"<<<{notes[-1]}>>>. Produce a NEW draft that satisfies this guidance."
    )
    if len(notes) > 1:
        priors = "; ".join(notes[:-1])
        directive += (
            " Earlier revision guidance, for context only (the guidance above is "
            f"authoritative if any conflict): <<<{priors}>>>."
        )
    return directive


def _select_units(rows: List[Dict[str, Any]], input_state: Dict[str, Any], worker_id: str):
    """Apply the declared skip-already-staged filter, order_by ranking, the
    revision-priority tier, and select_one — read from input_state (P0 config),
    applied blind. suggest-revision-verb-v1 P3: rows carrying non-terminal operator
    revision guidance sort BEFORE the fresh-fit order_by tier, so a re-draft-with-
    guidance is serviced ahead of a never-drafted row (stable within each tier)."""
    if input_state.get("skip_already_staged"):
        staged = _staged_row_ids(worker_id)  # may raise a loud Andon
        rows = [r for r in rows if r.get("id") not in staged]
    if not rows:
        return []
    order_by = input_state.get("order_by") or []
    if order_by:
        rows = sorted(rows, key=_order_by_key(order_by))
    # suggest-revision-verb-v1 P4 — N-breaker terminal EXCLUSION: a won't-converge
    # (terminal_skip) row is removed from re-selection ENTIRELY, not merely
    # de-prioritized (the placebo-livelock fix). Composes with the P3 `not
    # terminal_skip` priority gate: a terminal_skip row is neither prioritized nor
    # selected.
    rows = [r for r in rows if not _is_terminal_skip(r.get("id"), worker_id)]
    # Revision-priority tier — stable partition, revision-pending first (order_by
    # preserved within each tier). Empty pending -> rows unchanged (byte-identical).
    pending, rest = [], []
    for r in rows:
        (pending if _has_revision_priority(r.get("id"), worker_id) else rest).append(r)
    rows = pending + rest
    if input_state.get("select_one"):
        return rows[:1]
    return rows


register_resolver("notion_query", resolve_notion_query)
