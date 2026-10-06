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

    from grove.decision_work import config_for_goal, reset_work

    # The reset itself lives in grove.decision_work, shared with the portal's
    # Reset button, so the two can never disagree.
    try:
        cfg = config_for_goal(args.goal)
        plan = reset_work(cfg, args.label, apply=False)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"goal          : {args.goal}")
    print(f"current run   : {plan['previous_run'] or 'none yet'}"
          + (f" ({plan['previous_label']})" if plan["previous_label"] else ""))
    print(f"decisions in it: {plan['decisions']} of {plan['queued']} queued items")
    print(f"kegs to revoke : {len(plan['kegs_revoked'])}"
          + "".join(f"\n    {pid} ({status})" for pid, status in plan["kegs_revoked"]))
    print(f"proposals to withdraw: {plan['proposals_withdrawn']}")
    print(f"backlog items to take out of the queue: {plan['backlog_removed']}")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to open a new run.")
        return 0
    done = reset_work(cfg, args.label, surface="cli")
    print(f"✓ opened run {done['run']} ({done['label']}); the queue starts "
          f"again at {done['first_item'] or 'nothing'}.")
    print("Start a fresh chat session with /new before coding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
