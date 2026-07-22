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
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.unit_state import DEAD_LETTERED, NEEDS_YOU, WORKING

logger = logging.getLogger(__name__)

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
    # fleet-review-unification-v1 C1b-1 — surface the selected unit's stable identity
    # so the WORKER RUNTIME seam (manager) can fold the revision_directive by unit_id.
    # For notion_query unit_id == row_id (rows[0]["id"] — the single selected row).
    # The directive fold itself MOVED to the manager (gated on action_surface_publish);
    # the resolver no longer injects it here. Selection-time priority / terminal_skip
    # reads remain in _select_units (row-selection, not directive injection).
    payload["unit_id"] = rows[0].get("id")
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
# All steps are DRIVEN BY CONFIG and blind to meaning: the resolver does not know
# "Fit Score" is a fitness or "id" is a Notion page — it asks the derivation what
# STATE each unit is in (fleet-receipt-custody-v1 P4a — disk presence is never a
# state signal), filters the excluded states, sorts by the declared order_by, and
# yields one. No skill name appears here.


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


def _read_feedback_or_andon(unit_id: Optional[str], worker_id: str):
    """``feedback_store.read(worker_id, unit_id)`` with a corrupt entry converted to a
    LOUD Andon (B7): a present-but-unreadable revision entry must NEVER be swallowed
    into a feedback-blind re-draft. Returns the entry dict or None. (C1b-1: keyed on
    the generalized (worker, unit_id) store; for notion_query unit_id == row_id.)"""
    if not unit_id:
        return None
    from grove.forge import feedback_store

    try:
        return feedback_store.read(worker_id, unit_id)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise FleetWorkerAndon(
            f"revision feedback store unreadable for unit {unit_id!r} ({exc}) — refusing "
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


def _build_unit_state_context(worker_id: str) -> Dict[str, Any]:
    """Assemble the shared ``derive_unit_state`` inputs ONCE per selection.

    One scandir of ``dispatch/`` + ``events/`` (run_id filenames), one read of
    each receipt, one unit_id→runs grouping over the dispatch records, and the
    committed ``_ledger_terminal_dispositions`` projection for ``disposed`` — the
    single disposition authority, never a second parse of the ledger. ``reset/``
    does not exist yet (P5), so ``forgiven`` is empty. ``terminal_skip`` is read
    per-unit at derive time via the existing feedback-store reader.
    """
    from grove.api.portal import _ledger_terminal_dispositions
    from grove.fleet import paths
    from grove.fleet.unit_state import load_failure_policy

    ddir, edir = paths.dispatch_dir(worker_id), paths.events_dir(worker_id)
    dispatched = {p.stem for p in ddir.glob("*.json")} if ddir.is_dir() else set()
    received = {p.stem for p in edir.glob("*.json")} if edir.is_dir() else set()
    events: Dict[str, Dict[str, Any]] = {}
    for rid in received:
        try:
            events[rid] = json.loads(
                paths.event_path(worker_id, rid).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue  # a torn/missing receipt is not a state signal
    unit_runs: Dict[Any, List[str]] = {}
    for rid in dispatched:
        try:
            rec = json.loads(
                paths.dispatch_path(worker_id, rid).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        unit_runs.setdefault(rec.get("unit_id"), []).append(rid)
    return {
        "dispatched": dispatched,
        "received": received,
        "events": events,
        "unit_runs": unit_runs,
        "disposed": set(_ledger_terminal_dispositions()),
        "forgiven": frozenset(),
        "policy": load_failure_policy(),
        "worker_id": worker_id,
    }


def _derived_unit_state(unit_id: Optional[str], ctx: Dict[str, Any]) -> str:
    """The unit's state, from the assembled context + its per-unit terminal_skip."""
    from grove.fleet.unit_state import derive_unit_state

    return derive_unit_state(
        unit_runs=ctx["unit_runs"].get(unit_id, ()),
        dispatched=ctx["dispatched"],
        received=ctx["received"],
        forgiven=ctx["forgiven"],
        events=ctx["events"],
        disposed=unit_id in ctx["disposed"],
        producer=ctx["worker_id"],
        policy=ctx["policy"],
        terminal_skip=_is_terminal_skip(unit_id, ctx["worker_id"]),
    )


def _eligibility_excluded(unit_id: Optional[str], ctx: Dict[str, Any], skip: bool) -> bool:
    """Is this unit excluded from re-selection? (fleet-receipt-custody-v1 P4a.)

    Dead-lettered is a VERDICT — a won't-converge (terminal_skip) or retry-cap
    poison-pill unit is never dispatched, UNCONDITIONALLY. Working (in-flight) and
    Needs you (pending operator disposition) are the states ``skip_already_staged``
    governs, so they exclude only when the flag is set. Done and Waiting are
    always eligible (an applied unit is out of the tracker filter; a rejected one
    is re-draftable). The revision-cap guarantee never rides on a staging flag.
    """
    state = _derived_unit_state(unit_id, ctx)
    if state == DEAD_LETTERED:
        return True
    if skip and state in (WORKING, NEEDS_YOU):
        return True
    return False


def _select_units(rows: List[Dict[str, Any]], input_state: Dict[str, Any], worker_id: str):
    """Apply the derivation-based eligibility filter, order_by ranking, the
    revision-priority tier, and select_one — read from input_state (P0 config),
    applied blind. suggest-revision-verb-v1 P3: rows carrying non-terminal operator
    revision guidance sort BEFORE the fresh-fit order_by tier, so a re-draft-with-
    guidance is serviced ahead of a never-drafted row (stable within each tier)."""
    # P4a — eligibility is a projection over durable records, not disk presence.
    # Dead-lettered (either cause) is excluded UNCONDITIONALLY; Working / Needs you
    # exclude only under skip_already_staged. No disk glob, no fallback. The ctx
    # build is unconditional (~2ms) — the terminal-skip verdict must not depend on
    # the flag.
    ctx = _build_unit_state_context(worker_id)
    skip = bool(input_state.get("skip_already_staged"))
    rows = [r for r in rows if not _eligibility_excluded(r.get("id"), ctx, skip)]
    if not rows:
        return []
    order_by = input_state.get("order_by") or []
    if order_by:
        rows = sorted(rows, key=_order_by_key(order_by))
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


# ── file_source ────────────────────────────────────────────────────────────
# fleet-review-unification-v1 C1b-2 — the file-producer analog of notion_query.
# A worker whose upstream is a fleet SINK (drafter ← ~/.grove/researcher/ briefs,
# cultivator ← ~/.grove/scout/ digests) detects work by globbing that dir. The
# unit_id is the STABLE SEMANTIC SLUG derived from the source filename with
# timestamps/versions stripped (the ``slug_regex`` capture group) — an upstream
# re-date/refresh CONTINUES the same unit, so feedback + revision count persist
# (the C1b-1 store is keyed on that unit_id). No fold code here: the manager seam
# folds the revision_directive (gated on action_surface_publish), exactly as for
# notion_query.


def _grove_source_dir(source_dir: str) -> Path:
    """``~/.grove/<source_dir>`` — the upstream fleet sink this worker consumes."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / source_dir


def _unit_id_from_source(filename: str, slug_regex: str, worker_id: str) -> str:
    """The STABLE unit_id = ``slug_regex`` capture group over the source *filename*.

    ``brief-\\d{4}-\\d{2}-\\d{2}-(.+)`` over ``brief-2026-07-09-moon-bot.json`` →
    ``moon-bot`` — a re-dated brief for the same topic maps to the SAME unit_id, so
    the disposition/feedback history persists across the upstream refresh. A source
    name that does NOT match its declared regex is a LOUD Andon (never a silent skip
    that would leave real work undetected)."""
    m = re.match(slug_regex, filename)
    if not m or not m.group(1):
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: source file {filename!r} does not match the "
            f"declared slug_regex {slug_regex!r}; cannot derive a stable unit_id",
            worker_id=worker_id,
            check="file_source_bad_name",
        )
    return m.group(1)


def _select_file_units(units: List[Dict[str, Any]], input_state: Dict[str, Any], worker_id: str):
    """File-unit selection — the ``_select_units`` contract for file sources:
    exclude by derived state (Working, Needs you, Dead-lettered — terminal_skip
    among the causes), float revision-pending units ahead of fresh ones (stable
    within each tier), select_one. Deterministic input order is the caller's
    filename sort (no order_by for file sources)."""
    # P4a — same eligibility projection as _select_units: Dead-lettered excludes
    # unconditionally, Working / Needs you only under skip_already_staged.
    ctx = _build_unit_state_context(worker_id)
    skip = bool(input_state.get("skip_already_staged"))
    units = [u for u in units if not _eligibility_excluded(u.get("id"), ctx, skip)]
    if not units:
        return []
    pending, rest = [], []
    for u in units:
        (pending if _has_revision_priority(u.get("id"), worker_id) else rest).append(u)
    units = pending + rest
    if input_state.get("select_one"):
        return units[:1]
    return units


# ── one_shot request lifecycle (researcher-fleet-worker-v1 P2) ───────────────
# Generic REQUEST semantics for the file_source lane, keyed on the DECLARATIVE
# ``lifecycle: one_shot`` input_state flag (absent = refresh, byte-identical to
# the pre-P2 lane). A request file is consumed exactly once: claimed into
# ``.processing/`` at dispatch, disposed to ``.done/`` / ``.failed/`` at reap
# (the manager holds the claim), dead-lettered to ``.rejected/`` when malformed
# at resolve — never fail-in-place, so a bad file can never crash-loop the tick.
# Dot-prefixed subdirs are invisible to the resolver's non-recursive glob. This
# block is a MESH PRIMITIVE: no worker identities, ever (pinned by test).

_REQUEST_ORIGINS = frozenset({"operator", "agent"})
_PROCESSING_DIR = ".processing"
_DONE_DIR = ".done"
_FAILED_DIR = ".failed"
_REJECTED_DIR = ".rejected"


def _record_request_rejected(
    worker_id: str, source_dir: str, request_name: str, reason: str
) -> None:
    """File the worker-agnostic ``fleet_request_rejected`` ledger event.

    Defensive: filing must never crash a dispatch tick; the WARNING log floor in
    the caller stands regardless."""
    try:
        from grove.kaizen_ledger import KaizenLedger

        KaizenLedger(session_id=f"fleet:{worker_id}:resolve").record(
            "fleet_request_rejected",
            source="fleet_resolver",
            worker_id=worker_id,
            source_dir=source_dir,
            request=request_name,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the ticker
        logger.error(
            "[fleet.resolver] fleet_request_rejected filing failed: %r "
            "(worker=%s request=%s)", exc, worker_id, request_name,
        )


def _reject_request(
    path: Path, base: Path, worker_id: str, source_dir: str, reason: str
) -> None:
    """Dead-letter a malformed request: mv → ``.rejected/`` + ledger event."""
    dest_dir = base / _REJECTED_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, dest_dir / path.name)
    except OSError as exc:
        # Cannot move — leaving it in place WOULD re-reject every tick; loud.
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: cannot dead-letter malformed request "
            f"{path.name!r} ({exc})",
            worker_id=worker_id,
            check="request_reject_failed",
        )
    logger.warning(
        "[fleet.resolver] worker %s rejected request %s → %s/: %s",
        worker_id, path.name, _REJECTED_DIR, reason,
    )
    _record_request_rejected(worker_id, source_dir, path.name, reason)


def _screen_request_files(
    files: List[Path], base: Path, input_state: Dict[str, Any], worker_id: str
) -> List[Path]:
    """Validate one_shot request files; dead-letter failures, return survivors.

    Checks, in order: filename matches the declared ``slug_regex`` (a bad name
    dead-letters instead of Andon-looping); parses as a JSON object; ``origin``
    ∈ operator|agent; every declared ``required_keys`` key present."""
    source_dir = input_state.get("source_dir")
    slug_regex = input_state.get("slug_regex")
    required = input_state.get("required_keys") or []
    keep: List[Path] = []
    for p in files:
        m = re.match(slug_regex, p.name)
        if not m or not m.group(1):
            _reject_request(
                p, base, worker_id, source_dir,
                f"filename does not match slug_regex {slug_regex!r}",
            )
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _reject_request(
                p, base, worker_id, source_dir, f"unreadable/invalid JSON ({exc})"
            )
            continue
        if not isinstance(data, dict):
            _reject_request(
                p, base, worker_id, source_dir, "request is not a JSON object"
            )
            continue
        origin = data.get("origin")
        if origin not in _REQUEST_ORIGINS:
            _reject_request(
                p, base, worker_id, source_dir,
                f"origin {origin!r} not in {sorted(_REQUEST_ORIGINS)}",
            )
            continue
        missing = [k for k in required if k not in data]
        if missing:
            _reject_request(
                p, base, worker_id, source_dir,
                f"missing required keys {missing}",
            )
            continue
        keep.append(p)
    return keep


def _claim_request(path: Path, base: Path, worker_id: str) -> Path:
    """Atomically claim *path* into ``.processing/`` at dispatch (one_shot)."""
    dest_dir = base / _PROCESSING_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    try:
        os.replace(path, dest)
    except OSError as exc:
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: cannot claim request {path.name!r} ({exc})",
            worker_id=worker_id,
            check="request_claim_failed",
        )
    return dest


def dispose_request_claim(claim: Dict[str, Any], *, success: bool) -> None:
    """Reap-side disposition: success → ``.done/``, anything else → ``.failed/``.

    Defensive — a disposition failure logs and never crashes the reap; the
    claimed file stays visible in ``.processing/`` for operator rescue."""
    try:
        src = Path(claim["path"])
        root = Path(claim["root"])
        dest_dir = root / (_DONE_DIR if success else _FAILED_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if src.exists():
            os.replace(src, dest_dir / src.name)
    except Exception as exc:  # noqa: BLE001 — never crash the reap
        logger.error(
            "[fleet.resolver] request-claim disposition failed: %r (claim=%r)",
            exc, claim,
        )


def restore_request_claim(claim: Dict[str, Any]) -> None:
    """Un-claim after a dispatch failure: mv back so the next tick retries."""
    try:
        src = Path(claim["path"])
        root = Path(claim["root"])
        if src.exists():
            os.replace(src, root / src.name)
    except Exception as exc:  # noqa: BLE001 — never crash the dispatch surfacer
        logger.error(
            "[fleet.resolver] request-claim restore failed: %r (claim=%r)",
            exc, claim,
        )


def resolve_file_source(input_state: Dict[str, Any], worker_id: str) -> Optional[Any]:
    """Detect work from an upstream fleet sink dir (C1b-2).

    Returns ``{"units": [...], "source_dir", "source_path", "source_name",
    "unit_id"}`` for the selected unit, or ``None`` for no work. An ABSENT or empty
    source dir is a graceful no-op (the upstream producer has not run yet — idle is
    correct, not an Andon). Missing config is a loud Andon."""
    source_dir = input_state.get("source_dir")
    pattern = input_state.get("pattern")
    slug_regex = input_state.get("slug_regex")
    if not (source_dir and pattern and slug_regex):
        raise FleetWorkerAndon(
            f"worker {worker_id!r}: file_source input_state needs 'source_dir', "
            f"'pattern', and 'slug_regex'",
            worker_id=worker_id,
            check="resolver_failed",
        )
    base = _grove_source_dir(source_dir)
    if not base.is_dir():
        return None  # upstream producer has not run yet — graceful idle
    files = sorted(p for p in base.glob(pattern) if p.is_file())
    if not files:
        return None  # empty source — idle
    one_shot = input_state.get("lifecycle") == "one_shot"
    if one_shot:
        files = _screen_request_files(files, base, input_state, worker_id)
        if not files:
            return None  # every request dead-lettered — idle, never fail-in-place
    units = [
        {"id": _unit_id_from_source(p.name, slug_regex, worker_id),
         "source_path": str(p), "source_name": p.name}
        for p in files
    ]
    units = _select_file_units(units, input_state, worker_id)
    if not units:
        return None  # all staged / terminal — no work
    sel = units[0]
    payload = {
        "units": units,
        "source_dir": source_dir,
        "source_path": sel["source_path"],
        "source_name": sel["source_name"],
        "unit_id": sel["id"],
    }
    if one_shot:
        # Atomic claim at dispatch: the selected request leaves the glob surface
        # NOW; the manager stashes the claim and disposes it at reap.
        claimed = _claim_request(Path(sel["source_path"]), base, worker_id)
        payload["source_path"] = str(claimed)
        payload["request_claim"] = {"path": str(claimed), "root": str(base)}
    return payload


register_resolver("file_source", resolve_file_source)
