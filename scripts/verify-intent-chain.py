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

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    # The check itself lives in grove.audit, shared with the portal's Audit
    # page, so the two can never disagree. This is the INDEPENDENT way to run
    # it: straight off the files, without asking the running gateway.
    from grove.audit import chain_report, format_chain_report

    try:
        report = chain_report(store_path=Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("\n".join(format_chain_report(report)))
    return 0 if report["result"] != "broken" else 1


if __name__ == "__main__":
    raise SystemExit(main())
