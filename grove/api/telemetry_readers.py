"""Operator Portal — telemetry data readers (Sprint P3, portal-dashboard-v1).

Pure functions that read the substrate telemetry stores and return plain
dicts/lists. No HTML, no SVG, no aiohttp. The dashboard fragment routes
(``grove/api/dashboard_fragments.py``) call these, then hand the results to
the SVG generators (``grove/api/svg_charts.py``).

NO SILENT DEGRADATION. A malformed JSONL line is skipped with a logged
warning (never dropped without trace); an empty or absent store returns an
empty/zero structure the caller renders as an explicit "No data" message,
never an omitted chart.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

# Canonical Cognitive Router tiers (Pattern v1.3). Always present in the
# returned tier_counts so a zero tier renders as an explicit 0 bar, never an
# omitted one.
_TIERS = ("T0", "T1", "T2", "T3")

# Intent taxonomy is open-ended; the dashboard shows the heaviest few and
# folds the long tail into a single "other" bucket so a 16-class taxonomy
# stays legible.
_TOP_INTENTS = 10


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------


def _parse_ts(value) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    Tolerates a trailing ``Z`` and normalizes naive timestamps to UTC so
    window comparisons never raise on a mixed naive/aware pair.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window_start(days: int, now: Optional[datetime]) -> datetime:
    """Inclusive lower bound for the ``days``-day lookback window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - timedelta(days=days)


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield each well-formed JSON object from a JSONL file.

    A2 — malformed lines are skipped with a logged WARNING (loud, traceable),
    never silently dropped. A missing file yields nothing (the caller renders
    "No data"); a non-dict line is skipped the same way as a parse error.
    """
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[telemetry_readers] malformed JSONL line %d in %s: %r",
                    line_no, path, exc,
                )
                continue
            if not isinstance(record, dict):
                logger.warning(
                    "[telemetry_readers] non-object JSONL line %d in %s: %r",
                    line_no, path, type(record).__name__,
                )
                continue
            yield record


def _fmt_records(store, now: Optional[datetime], start: datetime) -> Iterator:
    """Yield IntentRecords whose timestamp falls within the window."""
    for rec in store.records():
        ts = _parse_ts(getattr(rec, "timestamp", None))
        if ts is None or ts < start:
            continue
        yield rec, ts


# ---------------------------------------------------------------------------
# Reader 1 — intent summary (IntentStore)
# ---------------------------------------------------------------------------


def read_intent_summary(store, days: int = 7, now: Optional[datetime] = None) -> dict:
    """Summarize IntentStore records within the last ``days`` days.

    ``store`` is an :class:`grove.intent_store.IntentStore` (or any object with a
    ``records()`` iterator); handlers pass ``get_store()``. A1 — the store has
    no time-range filter, so the window is applied here over ``records()``.

    Returns a dict with:
      total_turns          int — records in window
      tier_counts          {T0,T1,T2,T3} — always all four keys
      intent_class_counts  top 10 classes by count, long tail folded to "other"
      daily_effort         [{date, api_calls, duration_ms}] sorted ascending
    """
    start = _window_start(days, now)
    tier_counts = {t: 0 for t in _TIERS}
    intent_counter: Counter = Counter()
    daily: dict = defaultdict(lambda: {"api_calls": 0, "duration_ms": 0.0})
    total = 0

    for rec, ts in _fmt_records(store, now, start):
        total += 1
        tier = getattr(rec, "tier_selected", None)
        if tier in tier_counts:
            tier_counts[tier] += 1
        intent_counter[getattr(rec, "intent_class", None) or "unknown"] += 1
        day = ts.date().isoformat()
        daily[day]["api_calls"] += int(getattr(rec, "api_calls", 0) or 0)
        daily[day]["duration_ms"] += float(getattr(rec, "duration_ms", 0.0) or 0.0)

    top = dict(intent_counter.most_common(_TOP_INTENTS))
    tail = sum(v for k, v in intent_counter.items() if k not in top)
    if tail:
        top["other"] = tail

    daily_effort = [
        {
            "date": day,
            "api_calls": daily[day]["api_calls"],
            "duration_ms": round(daily[day]["duration_ms"], 1),
        }
        for day in sorted(daily)
    ]

    return {
        "total_turns": total,
        "tier_counts": tier_counts,
        "intent_class_counts": top,
        "daily_effort": daily_effort,
    }


# ---------------------------------------------------------------------------
# Reader 2 — Flywheel activity (kaizen ledger + memory proposals)
# ---------------------------------------------------------------------------


def read_flywheel_activity(
    ledger_dir, proposals_path, days: int = 7, now: Optional[datetime] = None
) -> dict:
    """Count Flywheel/Andon activity from the kaizen ledger + proposal queue.

    ``ledger_dir`` is ``~/.grove/.kaizen_ledger`` (one JSONL per session);
    ``proposals_path`` is ``~/.grove/memory_proposals.jsonl``. Ledger events are
    filtered to the window; proposal status is point-in-time (the queue is the
    current pending set, not an event log) so it is counted unfiltered.

    Returns a dict with:
      disposition_counts     andon_disposition values (always/session/deny)
      event_type_counts      every ledger event_type
      proposal_status_counts memory-proposal status values
    """
    start = _window_start(days, now)
    ledger_dir = Path(ledger_dir)
    event_type_counts: Counter = Counter()
    disposition_counts: Counter = Counter()

    if ledger_dir.is_dir():
        for fp in sorted(ledger_dir.glob("*.jsonl")):
            for record in _iter_jsonl(fp):
                ts = _parse_ts(record.get("timestamp"))
                if ts is None or ts < start:
                    continue
                event_type = record.get("event_type")
                if event_type:
                    event_type_counts[event_type] += 1
                disposition = record.get("disposition")
                if disposition:
                    disposition_counts[disposition] += 1

    proposal_status_counts: Counter = Counter()
    for record in _iter_jsonl(Path(proposals_path)):
        status = record.get("status")
        if status:
            proposal_status_counts[status] += 1

    return {
        "disposition_counts": dict(disposition_counts),
        "event_type_counts": dict(event_type_counts),
        "proposal_status_counts": dict(proposal_status_counts),
    }


# ---------------------------------------------------------------------------
# Reader 3 — connector health (in-memory MCP breaker + configured servers)
# ---------------------------------------------------------------------------


def read_connector_health(
    get_failures: Optional[Callable[[], dict]] = None,
    get_status: Optional[Callable[[], list]] = None,
) -> list:
    """Report per-MCP-server health: ``{name, status}`` for every configured
    server, healthy ones included.

    Connector health is in-memory only (the breaker resets on restart), so this
    reads live process state: ``get_connect_failures()`` (name -> "reauth" |
    "unreachable") and ``get_mcp_status()`` (all configured servers + a
    ``connected`` flag). Both default to the real ``tools.mcp_tool`` accessors,
    imported lazily so importing this module has no MCP side effects; tests
    inject stand-ins. A server with a breaker signature takes that signature;
    otherwise it is "ok" if connected, "unreachable" if not.
    """
    if get_failures is None or get_status is None:
        from tools.mcp_tool import get_connect_failures, get_mcp_status

        get_failures = get_failures or get_connect_failures
        get_status = get_status or get_mcp_status

    failures = get_failures() or {}
    statuses = get_status() or []

    health = []
    seen = set()
    for entry in statuses:
        name = entry.get("name")
        if not name:
            continue
        seen.add(name)
        signature = failures.get(name)
        if signature in ("reauth", "unreachable"):
            status = signature
        elif entry.get("connected"):
            status = "ok"
        else:
            status = "unreachable"
        health.append({"name": name, "status": status})

    # A failed server missing from the status list must still surface — never
    # drop a known breach because the status accessor didn't enumerate it.
    for name, signature in failures.items():
        if name in seen:
            continue
        status = signature if signature in ("reauth", "unreachable") else "unreachable"
        health.append({"name": name, "status": status})

    health.sort(key=lambda item: item["name"])
    return health
