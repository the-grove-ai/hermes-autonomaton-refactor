#!/usr/bin/env python3
"""kaizen-redraft.py — hand a stopped line back to Kaizen.

    scripts/kaizen-redraft.py --goal gl-invoice-coding            # dry run
    scripts/kaizen-redraft.py --goal gl-invoice-coding --apply

For a goal whose keg is HALTED after a miss with no proposal waiting — the
operator sent the revision back and nothing came of it, or a redraft was lost
— this raises the event again through the andon handler, so Kaizen redrafts
with the corrected case and every piece of feedback the operator has given.
Whatever comes back is a proposal for signature (or, if no tier can draft it,
a request for the operator to write the condition). It changes no standard
work itself.

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
    parser.add_argument("--apply", action="store_true", help="raise the event")
    args = parser.parse_args()

    from hermes_cli.env_loader import load_hermes_dotenv

    load_hermes_dotenv(project_env=REPO_ROOT / ".env")   # model credentials, as the CLI does

    from grove import keg as keg_mod
    from grove.andon import raise_andon
    from grove.decision_work import DECISION_CORRECT, DecisionWork, config_for_goal
    from grove.eval import proposal_queue
    from grove.flywheel_cli import keg_feedback_history
    from grove.kaizen.standard_work import goal_kegs
    from grove.pattern_cache import STATUS_HALTED

    try:
        work = DecisionWork(config_for_goal(args.goal))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    halted = goal_kegs(work, (STATUS_HALTED,))
    waiting = [
        p for p in proposal_queue.read_all()
        if ((p.payload or {}).get("keg") or {}).get("dock_goal") == args.goal
        or (p.payload or {}).get("goal") == args.goal
    ]
    misses = [
        c for c in work.history()
        if c["served_by_keg"] and c["decision"] == DECISION_CORRECT
    ]
    run = work.log.current_run() or {}
    feedback = keg_feedback_history(work.config.keg.name, lineage=run.get("run_id")) \
        if work.config.keg else []

    print(f"goal            : {args.goal}")
    print(f"halted keg      : {halted[-1].pattern_id if halted else 'none'}")
    print(f"corrected case  : {misses[-1]['ref'] if misses else 'none'}"
          + (f" ({misses[-1]['served']} -> {misses[-1]['confirmed']})" if misses else ""))
    print(f"operator feedback: {feedback or 'none'}")
    print(f"waiting proposals: {[p.type for p in waiting] or 'none'}")
    if not halted or not misses:
        print("Nothing to redraft: no halted keg with a corrected case in this run.")
        return 0
    if waiting:
        print("A proposal for this goal is already waiting; nothing raised.")
        return 0
    if not args.apply:
        print("dry run — nothing raised. Re-run with --apply.")
        return 0

    miss = misses[-1]
    spec = keg_mod.keg_of(halted[-1]) or {}
    event = raise_andon(
        keg_mod.FLAG_ANOMALY,
        detector="operator_feedback" if feedback else "correction",
        goal=args.goal,
        summary=(
            f"redraft for the halted keg {spec.get('name')} v{spec.get('version')}: "
            f"{miss['ref']} was corrected {miss['served']} -> {miss['confirmed']}"
        ),
        evidence=[{"item_id": miss["ref"], "turn_id": miss.get("turn_id")}],
        details={
            "item_id": miss["ref"], "inputs": miss["inputs"],
            "served": miss["served"], "corrected": miss["confirmed"],
            "feedback": " | ".join(feedback),
            # No "keg" key: the keg is already halted; this only asks Kaizen
            # for its answer, it does not stop anything further.
        },
        matched_skill=halted[-1].pattern_id,
        context={"work": work},
    )
    answer = event.get("answer") or {}
    print(f"✓ andon {event['andon_id'][:8]} raised; Kaizen answered: "
          f"{answer.get('kind')} — {answer.get('summary')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
