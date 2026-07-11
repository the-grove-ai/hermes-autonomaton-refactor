"""Parity harness — unified feed vs the legacy kaizen ledger (GRV-009 E3 C2).

Proves, over a time window, that the capability feed (``capability_feed.py``)
captured the same EXECUTED tool invocations the legacy kaizen ledger recorded —
so the legacy capability-usage writers can be retired (C4) without losing
coverage.

Locked D4 design:
  * MATCH KEY: ``(session_id, tool_name)`` multiset. The lock named
    ``(session, turn, tool_name)``; the kaizen ``tool_batch_executed`` event
    carries no ``turn_id`` column and a turn ordinal reconstructed from
    ``tool_selection`` boundaries desyncs on no-tool turns, so turn-granular
    cross-sink matching is not reliably available. The multiset key is
    order-independent (safe for concurrent batches whose completion order
    differs from submission order) and is the strongest key both sinks support.
    The feed's ``turn_id`` is carried on each invocation for itemization only.
  * FIELD MAPPING old -> feed:
      kaizen ``tool_batch_executed`` -> expand ``intents[].tool_name`` to one
      invocation each; ``session_id`` from the event; ``ts`` from the event
      ``timestamp``. (turn_id is unavailable on the ledger side — None.)
  * NORMALIZATION (a mismatch any rule explains is a PASS):
      1. Timestamp skew: the feed enqueues at invoke-start, the ledger writes at
         batch-end — up to ~seconds apart. A record is in-window if its ts is
         within ``TS_SKEW_S`` of the window, so a boundary invocation is not
         counted on one side only.
      2. Intent store excluded: it is a STAYS sink with by-design late
         finalization (the Implicit Success Sweep); not read here.
      3. Carve-out tools: the disclosure-meta pull tools never execute through
         ``_invoke_tool`` (excluded from the feed by contract) and
         halted/blocked invocations live in ``andon_halt`` (this sprint), not
         ``tool_batch_executed`` — so reading only ``tool_batch_executed`` makes
         them absent on both sides. ``CARVE_OUT_TOOLS`` drops the pull tools
         explicitly in case a future event surfaces them.
      4. Restart/SIGKILL boundary: records queued-but-unflushed at SIGKILL are
         lost from the feed. A ledger-only invocation within ``TS_SKEW_S`` of a
         supplied ``restart_boundaries`` timestamp is classified
         ``restart-explainable`` rather than a real miss.

Run for GATE-PARITY:  python -m grove.capability_feed_parity --since <iso> --until <iso>
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Disclosure-meta tools that never reach the executor (excluded by contract).
CARVE_OUT_TOOLS = frozenset({"read_tool_schema", "read_goal_context"})

# Feed-enqueue (invoke-start) vs ledger-write (batch-end) skew tolerance.
TS_SKEW_S = 2.0


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass(frozen=True)
class Invocation:
    session_id: str
    tool_name: str
    ts: Optional[datetime]
    turn_id: Optional[str]
    source: str  # "feed" | "ledger"

    @property
    def key(self) -> Tuple[str, str]:
        return (self.session_id, self.tool_name)


@dataclass
class ParityReport:
    window_start: datetime
    window_end: datetime
    feed_count: int = 0
    ledger_count: int = 0
    matched: int = 0
    feed_only: List[dict] = field(default_factory=list)     # in feed, not ledger
    ledger_only: List[dict] = field(default_factory=list)   # in ledger, not feed

    @property
    def real_mismatches(self) -> List[dict]:
        return [m for m in (self.feed_only + self.ledger_only)
                if m["classification"] == "real"]

    @property
    def match_rate(self) -> float:
        denom = max(self.feed_count, self.ledger_count)
        return 1.0 if denom == 0 else self.matched / denom

    def summary(self) -> str:
        lines = [
            f"Parity window {self.window_start.isoformat()} .. {self.window_end.isoformat()}",
            f"  feed invocations  : {self.feed_count}",
            f"  ledger invocations: {self.ledger_count}",
            f"  matched           : {self.matched}  (match_rate={self.match_rate:.4f})",
            f"  feed-only         : {len(self.feed_only)}  "
            f"({sum(1 for m in self.feed_only if m['classification']=='real')} real)",
            f"  ledger-only       : {len(self.ledger_only)}  "
            f"({sum(1 for m in self.ledger_only if m['classification']=='real')} real)",
            f"  REAL mismatches   : {len(self.real_mismatches)}",
        ]
        for m in self.real_mismatches:
            lines.append(f"    ! {m['source']}-only REAL: session={m['session_id']} "
                         f"tool={m['tool_name']} ts={m['ts']} turn={m['turn_id']}")
        return "\n".join(lines)


def _in_window(ts: Optional[datetime], start: datetime, end: datetime) -> bool:
    # Rule 1: ±TS_SKEW_S grace at both boundaries. A record with no parseable
    # ts is included (surfaced, never silently dropped).
    if ts is None:
        return True
    return (start - timedelta(seconds=TS_SKEW_S)) <= ts <= (end + timedelta(seconds=TS_SKEW_S))


def read_feed_invocations(feed_dir: Path, start: datetime, end: datetime) -> List[Invocation]:
    out: List[Invocation] = []
    for path in sorted(feed_dir.glob("feed*.jsonl")):
        for line in _iter_jsonl(path):
            tool = line.get("tool_name")
            if not tool or tool in CARVE_OUT_TOOLS:
                continue
            ts = _parse_ts(line.get("ts"))
            if not _in_window(ts, start, end):
                continue
            out.append(Invocation(
                session_id=str(line.get("session_id") or ""),
                tool_name=str(tool), ts=ts,
                turn_id=line.get("turn_id"), source="feed",
            ))
    return out


def read_ledger_invocations(ledger_dir: Path, start: datetime, end: datetime) -> List[Invocation]:
    out: List[Invocation] = []
    for path in sorted(ledger_dir.glob("*.jsonl")):
        for line in _iter_jsonl(path):
            if line.get("event_type") != "tool_batch_executed":
                continue
            ts = _parse_ts(line.get("timestamp"))
            if not _in_window(ts, start, end):
                continue
            session_id = str(line.get("session_id") or path.stem)
            for intent in (line.get("intents") or []):
                tool = (intent or {}).get("tool_name")
                if not tool or tool in CARVE_OUT_TOOLS:
                    continue
                out.append(Invocation(
                    session_id=session_id, tool_name=str(tool), ts=ts,
                    turn_id=None, source="ledger",
                ))
    return out


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except Exception:
                    logger.debug("[parity] skipping malformed line in %s", path)
    except FileNotFoundError:
        return


def _classify_only(inv_dict: dict, restart_boundaries: List[datetime]) -> str:
    # Rule 4: a one-sided record near a restart boundary is explainable.
    ts = _parse_ts(inv_dict.get("ts"))
    if ts is not None:
        for b in restart_boundaries:
            if abs((ts - b).total_seconds()) <= TS_SKEW_S:
                return "restart-explainable"
    return "real"


def compare(
    feed_invs: List[Invocation],
    ledger_invs: List[Invocation],
    start: datetime,
    end: datetime,
    restart_boundaries: Optional[List[datetime]] = None,
) -> ParityReport:
    rb = restart_boundaries or []
    rep = ParityReport(window_start=start, window_end=end,
                       feed_count=len(feed_invs), ledger_count=len(ledger_invs))

    feed_ct: Counter = Counter(i.key for i in feed_invs)
    ledger_ct: Counter = Counter(i.key for i in ledger_invs)
    # Representative invocation per key for itemization.
    feed_by_key: Dict[Tuple[str, str], Invocation] = {}
    for i in feed_invs:
        feed_by_key.setdefault(i.key, i)
    ledger_by_key: Dict[Tuple[str, str], Invocation] = {}
    for i in ledger_invs:
        ledger_by_key.setdefault(i.key, i)

    for key in set(feed_ct) | set(ledger_ct):
        f, g = feed_ct.get(key, 0), ledger_ct.get(key, 0)
        rep.matched += min(f, g)
        if f > g:
            ex = feed_by_key[key]
            for _ in range(f - g):
                d = _item(ex)
                d["classification"] = _classify_only(d, rb)
                rep.feed_only.append(d)
        elif g > f:
            ex = ledger_by_key[key]
            for _ in range(g - f):
                d = _item(ex)
                d["classification"] = _classify_only(d, rb)
                rep.ledger_only.append(d)
    return rep


def _item(inv: Invocation) -> dict:
    return {
        "source": inv.source, "session_id": inv.session_id,
        "tool_name": inv.tool_name,
        "ts": inv.ts.isoformat() if inv.ts else None,
        "turn_id": inv.turn_id,
    }


def run_parity(
    start: datetime,
    end: datetime,
    *,
    feed_dir: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
    restart_boundaries: Optional[List[datetime]] = None,
) -> ParityReport:
    if feed_dir is None:
        from grove.capability_feed import feed_dir as _fd
        feed_dir = _fd()
    if ledger_dir is None:
        from grove.kaizen_ledger import default_ledger_dir
        ledger_dir = default_ledger_dir()
    feed_invs = read_feed_invocations(Path(feed_dir), start, end)
    ledger_invs = read_ledger_invocations(Path(ledger_dir), start, end)
    return compare(feed_invs, ledger_invs, start, end, restart_boundaries)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Capability-feed parity harness (GRV-009 E3).")
    ap.add_argument("--since", required=True, help="window start, ISO-8601")
    ap.add_argument("--until", required=True, help="window end, ISO-8601")
    ap.add_argument("--restart", action="append", default=[],
                    help="restart boundary ISO-8601 (repeatable)")
    args = ap.parse_args(argv)
    start, end = _parse_ts(args.since), _parse_ts(args.until)
    if start is None or end is None:
        ap.error("--since/--until must be ISO-8601")
    rb = [b for b in (_parse_ts(x) for x in args.restart) if b]
    # kaizen-ledger-retention-v1 P5 — completeness advisory. Retention prunes
    # window-bounded telemetry older than retention_days to the archive, so a
    # parity window reaching past that cutoff reads an incomplete LIVE ledger:
    # ledger-side invocations there may sit in the archive, not missing.
    # Advisory only — the run proceeds unchanged.
    import sys
    from grove.ledger_retention import default_archive_dir, load_retention_config
    retention_days = load_retention_config().retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    if start < cutoff:
        print(
            f"WARNING: --since {start.isoformat()} predates the retention "
            f"cutoff — the live ledger may be incomplete before "
            f"{cutoff.date().isoformat()} (retention_days={retention_days}); "
            f"pruned events are archived at {default_archive_dir()}",
            file=sys.stderr,
        )
    rep = run_parity(start, end, restart_boundaries=rb)
    print(rep.summary())
    return 0 if not rep.real_mismatches else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
