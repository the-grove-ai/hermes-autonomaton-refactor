#!/usr/bin/env python3
"""Checkpoints from the command line: save the node's records at one moment,
list what is saved, and put a saved moment back.

    scripts/checkpoint.py save before-month-3 --note "months 1 and 2 done"
    scripts/checkpoint.py list
    scripts/checkpoint.py restore before-month-3          # applied when the gateway restarts
    scripts/checkpoint.py restore before-month-3 --now    # only with the gateway STOPPED

Run as the user that owns the node's home. Nothing here writes to any goal's
records; saves and restores go in the checkpoints admin log.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grove import checkpoints  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    save = sub.add_parser("save")
    save.add_argument("name")
    save.add_argument("--note", default="")
    save.add_argument("--replace", action="store_true",
                      help="keep the earlier checkpoint of this name in the archive")
    sub.add_parser("list")
    restore = sub.add_parser("restore")
    restore.add_argument("name")
    restore.add_argument("--now", action="store_true",
                         help="apply at once; ONLY with the gateway stopped")
    args = parser.parse_args(argv)
    try:
        if args.verb == "save":
            saved = checkpoints.save(args.name, args.note, replace=args.replace)
            print(f"SAVED {saved['name']} at {saved['saved_at']}")
            for goal in saved.get("goals") or []:
                print("  ", json.dumps(goal))
        elif args.verb == "list":
            for m in checkpoints.listing():
                print(f"{m.get('name'):<28} saved {str(m.get('saved_at'))[:19]}  {m.get('note') or ''}")
        elif args.now:
            reasons = checkpoints.in_flight()
            if reasons:
                raise checkpoints.CheckpointRefused(
                    "Nothing was restored: " + "; ".join(reasons) + ".")
            result = checkpoints.restore_now(args.name)
            print(json.dumps(result, indent=1))
            return 0 if result["identical"] and result["audit_check"]["result"] == "intact" else 1
        else:
            checkpoints.request_restore(args.name)
            print(f"REQUESTED restore of {args.name}. It is applied when the gateway next "
                  f"starts: restart the gateway now.")
    except checkpoints.CheckpointRefused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
