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
import json
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


def _run_reports(home: Path, store_path: Path) -> tuple:
    """For each goal's decision log: the current run, how many decisions it
    holds and whether every one of them has its turn in the audit trail.
    Returns ``(lines, problems)``. A decision whose turn record is missing is
    an audit gap and is reported as a problem."""
    from grove.decision_work import KIND_PROPOSED, DecisionLog

    directory = home / "decisions"
    if not directory.is_dir():
        return [], []
    uids = set()
    with open(store_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                uids.add(json.loads(line).get("turn_uid"))
            except ValueError:
                continue  # verify_chain already reports an unreadable line
    lines, problems = [], []
    for log_path in sorted(directory.glob("*.jsonl")):
        log = DecisionLog(log_path.stem, directory=directory)
        try:
            run = log.current_run()
            proposed = [r for r in log.run_records() if r.get("kind") == KIND_PROPOSED]
        except ValueError as exc:
            problems.append({"line": None, "turn_id": log_path.name, "problem": str(exc)})
            continue
        if run is None:
            continue
        found = sum(1 for r in proposed if r.get("turn_uid") in uids)
        label = f" ({run['label']})" if run.get("label") else ""
        lines.append(
            f"  Run {run.get('run_number', '?')}{label} · {log_path.stem}\n"
            f"    Decisions in this run    {len(proposed):,}\n"
            f"    With a turn on record    {found:,} of {len(proposed):,}"
        )
        for r in proposed:
            if r.get("turn_uid") not in uids:
                problems.append({
                    "line": None, "turn_id": r.get("turn_id") or r.get("item_id"),
                    "problem": f"decision {r.get('item_id')} has no turn in the audit trail",
                })
    return lines, problems


def _ledger_report(home: Path) -> tuple:
    """Verify every Kaizen ledger file's provenance chain (the ledger that
    carries flags, andon events, Kaizen's answers and signatures). Lines the
    retention engine archived are read back in, so a pruned ledger still
    verifies whole. Returns ``(summary line, problems)``."""
    from grove.kaizen_ledger import verify_ledger_chain

    directory = home / ".kaizen_ledger"
    if not directory.is_dir():
        return None, []
    files = chained = unchained = 0
    problems = []
    for path in sorted(directory.glob("*.jsonl")):
        archive = home / ".kaizen_ledger_archive" / path.name
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        archived = []
        if archive.exists():
            with open(archive, encoding="utf-8") as fh:
                archived = fh.read().splitlines()
        report = verify_ledger_chain(lines, archived)
        files += 1
        chained += report["chained"]
        unchained += report["unchained"]
        for p in report["problems"]:
            problems.append({
                "line": p.get("line"), "turn_id": p.get("event_type") or "",
                "problem": p["problem"], "where": f"ledger {path.name}",
            })
    line = (
        f"  Kaizen ledger events       {chained:,} chained in {files:,} file(s)"
        + (f"; {unchained:,} older, pre-chain" if unchained else "")
    )
    return line, problems


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
    problems = list(report["problems"])
    run_lines, run_problems = _run_reports(home, path) if live else ([], [])
    problems += run_problems
    ledger_line, ledger_problems = _ledger_report(home) if live else (None, [])
    problems += ledger_problems
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
    if ledger_line:
        print(ledger_line)
    for block in run_lines:
        print(block)
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
        where = (
            p["where"] + (f" line {p['line']}" if p.get("line") else "") if p.get("where")
            else f"line {p['line']}" if p.get("line")
            else f"session {p['session_id']}" if p.get("session_id")
            else "decision log"
        )
        print(f"  {where}  {p['turn_id'] or ''}  {p['problem']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
