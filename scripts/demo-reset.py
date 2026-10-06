#!/usr/bin/env python3
"""demo-reset.py — start a goal's decision work over, without deleting anything.

    scripts/demo-reset.py --goal gl-invoice-coding            # dry run
    scripts/demo-reset.py --goal gl-invoice-coding --apply    # write

What it does: opens a new, clearly marked RUN in the goal's decision log. The
queue then starts again at its first item, and everything that reads "the
current run" (the evidence rule, the verifier's run count, the scorecard) stops
seeing earlier decisions. Earlier records stay in the log, and the audit trail
is never touched.

It also retires the previous run's standard work for this goal: any keg that
was drafted, serving or halted is revoked (it stops serving and stays on
record as revoked), and any keg proposal still waiting is withdrawn from the
queue with a recorded disposition. The new run's first keg is v1 again.

After a reset, start a fresh chat session with /new before coding: an isolated
goal only records decisions in a session that began with its own work.

Run as the user that owns ``$GROVE_HOME``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--goal", required=True, help="the Dock goal id")
    parser.add_argument("--label", default="", help="a note stored on the new run")
    parser.add_argument("--apply", action="store_true", help="write the new run")
    args = parser.parse_args()

    from grove.decision_work import KIND_PROPOSED, DecisionWork, config_for_goal

    try:
        work = DecisionWork(config_for_goal(args.goal))
        run = work.log.current_run()
        decided = [r for r in work.log.run_records() if r.get("kind") == KIND_PROPOSED]
        queued = len(work.queue_items())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"goal          : {args.goal}")
    print(f"decision log  : {work.log.path}")
    print(f"current run   : {run.get('run_number') if run else 'none yet'}"
          + (f" ({run['label']})" if run and run.get("label") else ""))
    print(f"decisions in it: {len(decided)} of {queued} queued items")

    from grove import keg as keg_mod
    from grove.eval import proposal_queue
    from grove.pattern_cache import (
        PatternCacheStore, STATUS_ACTIVE, STATUS_DEMOTED, STATUS_HALTED,
        STATUS_SUSPENDED,
    )

    store = PatternCacheStore()
    kegs = [
        entry for entry in store.all()
        if (keg_mod.keg_record(entry).get("keg") or {}).get("dock_goal") == args.goal
        and entry.status in (STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED)
    ]
    waiting = [
        p for p in proposal_queue.read_all()
        if ((p.payload or {}).get("keg") or {}).get("dock_goal") == args.goal
    ]
    print(f"kegs to revoke : {len(kegs)}"
          + "".join(f"\n    {e.pattern_id} ({e.status})" for e in kegs))
    print(f"proposals to withdraw: {len(waiting)}")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to open a new run.")
        return 0
    from grove.flywheel_cli import _record_kaizen_disposition

    for proposal in waiting:
        proposal_queue.remove(proposal.proposal_id)
        _record_kaizen_disposition(
            proposal, disposition="withdrawn", reason=args.label or "demo reset",
        )
    for entry in kegs:
        store.set_status(entry.pattern_id, STATUS_DEMOTED)
    new = work.log.start_run(args.label or "demo reset")
    print(f"✓ opened run {new['run_number']} ({new['label']}); the queue starts "
          f"again at {work.next_item().stem if work.next_item() else 'nothing'}.")
    print("Start a fresh chat session with /new before coding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
