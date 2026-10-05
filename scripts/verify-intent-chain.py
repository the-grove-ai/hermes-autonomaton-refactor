#!/usr/bin/env python3
"""verify-intent-chain.py — check the intent store's per-session hash chains.

    scripts/verify-intent-chain.py                 # the live store
    scripts/verify-intent-chain.py path/to.jsonl   # any copy

Read-only. Reports, per session, how many records are chained, how many
predate the chain, and every break: an altered record, a removed / inserted /
reordered record, or response content that no longer matches its digest.
Exit status 0 when no problems are found, 1 otherwise.

Limit: removing a session's NEWEST records leaves nothing later that points
back at them, so that cannot be detected from the file alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from grove.intent_store import verify_chain

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "intent_records.jsonl"
    if not path.exists():
        print(f"ERROR: no intent store at {path}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as fh:
        report = verify_chain(fh)
    chained_sessions = sum(1 for s in report["sessions"].values() if s["chained"])
    print(f"store            : {path}")
    print(f"records          : {report['records']}")
    print(f"chained          : {report['chained']} across {chained_sessions} session(s)")
    print(f"predate the chain: {report['unchained']}")
    if not report["problems"]:
        print("result           : OK — every chained record verifies")
        return 0
    print(f"result           : {len(report['problems'])} PROBLEM(S)")
    for p in report["problems"]:
        print(f"  line {p['line']}  {p['turn_id'] or '-'}  {p['problem']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
