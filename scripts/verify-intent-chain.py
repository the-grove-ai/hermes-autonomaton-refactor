#!/usr/bin/env python3
"""verify-intent-chain.py — check the intent store's per-session hash chains.

    scripts/verify-intent-chain.py                 # the live store
    scripts/verify-intent-chain.py path/to.jsonl   # any copy (file checks only)

Read-only. Every record carries the hash of the record before it in its
session, plus its own. This recomputes both for every record and reports any
break: an altered record, a removed / inserted / reordered record, or response
content that no longer matches its digest.

For the live store it also compares each session's latest hash against the one
the Dispatcher recorded in the session database (``state_meta``), which catches
removal of a session's NEWEST records — invisible from the file alone.

Exit status 0 when the chain is intact, 1 otherwise.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _read_anchors(db_path: Path) -> dict:
    """``{session_id: latest record_hash}`` from the session database, opened
    read-only. A missing database is not an error (nothing anchored yet); an
    unreadable one is — fail loud, never report a truncation check that did
    not run."""
    from grove.intent_store import CHAIN_HEAD_KEY_PREFIX

    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?",
            (CHAIN_HEAD_KEY_PREFIX + "%",),
        ).fetchall()
    finally:
        conn.close()
    return {k[len(CHAIN_HEAD_KEY_PREFIX):]: v for k, v in rows}


def main() -> int:
    from grove.intent_store import verify_chain

    live = len(sys.argv) <= 1
    if live:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        path = home / "intent_records.jsonl"
    else:
        path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: no intent store at {path}", file=sys.stderr)
        return 1
    anchors = _read_anchors(home / "telemetry.db") if live else {}
    with open(path, encoding="utf-8") as fh:
        report = verify_chain(fh, anchors=anchors)

    chained_sessions = sum(1 for s in report["sessions"].values() if s["chained"])
    problems = report["problems"]
    bar = "=" * 62
    print(bar)
    print("  AUDIT CHAIN CHECK")
    print(bar)
    print(f"  Store                      {path}")
    print(f"  Records checked            {report['records']:,}")
    print(f"  Hash-chained records       {report['chained']:,}  "
          f"in {chained_sessions:,} session(s)")
    print(f"  Older, pre-chain records   {report['unchained']:,}  (counted, not chained)")
    if live:
        print(f"  Sessions tail-checked      {report['anchored']:,}  "
              "(newest record still present)")
    else:
        print("  Sessions tail-checked      not run (needs the live session database)")
    print(bar)
    if not problems:
        if report["chained"]:
            print("  RESULT: CHAIN INTACT — no record altered, removed or reordered")
        else:
            print("  RESULT: NOTHING TO VERIFY — no chained records in this store yet")
        print(bar)
        return 0
    print(f"  RESULT: CHAIN BROKEN — {len(problems)} problem(s)")
    print(bar)
    for p in problems:
        where = f"line {p['line']}" if p.get("line") else f"session {p['session_id']}"
        print(f"  {where}  {p['turn_id'] or ''}  {p['problem']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
