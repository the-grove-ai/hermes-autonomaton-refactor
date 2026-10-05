#!/usr/bin/env python3
"""remine-memory-sessions.py — recover sessions whose memory extraction was lost.

    scripts/remine-memory-sessions.py                      # dry run: list them
    scripts/remine-memory-sessions.py --since 2026-10-01   # narrow the list
    scripts/remine-memory-sessions.py --since 2026-10-01 --apply

Why: before memory-extraction-retry-v1 the Context Persistence Detector wrote a
``processing`` lock, called the model, and — when the reply could not be parsed
— left the lock in place forever. Those sessions were never mined and can never
come back through the normal sweep (their turns are long finalized).

This finds every session whose ONLY record in ``memory_proposals`` is that bare
lock, and with ``--apply`` releases the lock (a ``failed`` record, reason
``stale_lock_recovery``) and runs the detector on the stored transcript. New
proposals are STAGED for operator review exactly as a normal sweep would stage
them — nothing is written to memory here.

Caveat: a bare lock is also what a genuine "nothing worth remembering" result
left behind, so some listed sessions may simply stage nothing again. Each
re-mine is one or two model calls on the Telemetry tier. Run as the user that
owns ``$GROVE_HOME``, with the gateway's environment loaded.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", default=None,
                        help="only sessions locked on/after this date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true",
                        help="release the locks and re-run extraction")
    args = parser.parse_args()

    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    from grove.memory.detector import ContextPersistenceDetector
    from grove.memory.dispositions import EXTRACTION_FAILED_STATUS
    from grove.memory.lifecycle import load_active_dock_goal_dicts
    from grove.memory.store import MemoryStore

    base = Path(get_hermes_home())
    detector = ContextPersistenceDetector(store=MemoryStore(base_dir=base), base_dir=base)

    by_session: dict = {}
    for rec in detector._read_records():
        sid = rec.get("session_id")
        if sid:
            by_session.setdefault(sid, []).append(rec)

    stuck = []
    for sid, recs in by_session.items():
        statuses = {r.get("status") for r in recs}
        if statuses != {"processing"}:
            continue  # staged, disposed, or already released — not a bare lock
        locked_at = max(str(r.get("timestamp") or "") for r in recs)
        if args.since and locked_at[:10] < args.since:
            continue
        stuck.append((locked_at, sid))
    stuck.sort()

    print(f"proposals file : {detector.proposals_path}")
    print(f"bare-lock sessions{' since ' + args.since if args.since else ''}: {len(stuck)}")
    for locked_at, sid in stuck:
        print(f"  {sid}  locked {locked_at[:19]}")
    if not stuck:
        return 0
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to re-mine them.")
        return 0

    session_db = SessionDB()
    goals = load_active_dock_goal_dicts()
    total = 0
    for _locked_at, sid in stuck:
        transcript = session_db.get_messages_as_conversation(sid)
        if not transcript:
            print(f"  {sid}: no stored transcript — left as is")
            continue
        detector._append_record({
            "session_id": sid,
            "status": EXTRACTION_FAILED_STATUS,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "stale_lock_recovery",
            "attempt": 1,
        })
        staged = detector.detect_and_stage(sid, transcript, goals)
        total += staged
        print(f"  {sid}: staged {staged} proposal(s)")
    print(f"✓ re-mined {len(stuck)} session(s); {total} proposal(s) staged for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
