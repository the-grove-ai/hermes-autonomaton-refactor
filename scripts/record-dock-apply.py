#!/usr/bin/env python3
"""record-dock-apply.py — put a hand-applied dock.yaml change on the record.

    scripts/record-dock-apply.py --before OLD_DOCK_YAML

Run right after the operator replaces ``$GROVE_HOME/dock/dock.yaml`` by hand
(the Dock is the operator's own file: the agent can never write it, and the
operator edits it directly). It does two things:

1. Writes one hash-chained ledger event, ``operator_applied``: the file, its
   digest before and after, and which fields changed. Custody of a change that
   no proposal carried is then provable.

2. For every goal whose SESSION RULE is not signed as it now stands — because
   it is new, or because this edit changed it — files that rule in the portal
   for signature and says so. A session rule acts directly (no keg stands
   between it and what the system does), so an edit to one is a draft: it is
   not in force until the operator signs it. Everything else in a goal's
   declaration reaches execution only through a keg, and a keg is signed.

Run as the user that owns ``$GROVE_HOME``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _flatten(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list) and all(isinstance(i, dict) and "id" in i for i in node):
        for item in node:
            yield from _flatten(item, f"{prefix}[{item['id']}]")
    else:
        yield prefix, node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--before", required=True, help="the dock.yaml that was replaced")
    args = parser.parse_args()

    import yaml
    from hermes_constants import get_hermes_home

    from grove import decision_work as dw
    from grove.dock import load_dock
    from grove.kaizen import session_rule
    from grove.kaizen_ledger import KaizenLedger

    home = Path(get_hermes_home())
    after_path = home / "dock" / "dock.yaml"
    before_path = Path(args.before)
    before_bytes = before_path.read_bytes() if before_path.exists() else b""
    after_bytes = after_path.read_bytes()
    before = dict(_flatten(yaml.safe_load(before_bytes) or {}))
    after = dict(_flatten(yaml.safe_load(after_bytes) or {}))
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))

    rules = []
    dock = load_dock()
    for goal in dock.goals:
        cfg = dw.load_config(goal)
        if cfg is None or not cfg.isolated:
            continue
        signed = dw.session_rule_grant(cfg) is not None
        entry = {"goal": cfg.goal_id, "digest": dw.session_rule_digest(cfg), "signed": signed}
        if not signed:
            entry["proposal_id"] = session_rule.propose_rule(
                dw.DecisionWork(cfg), {"andon_id": None})
        rules.append(entry)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    event = KaizenLedger(f"operator-{stamp}").record(
        "operator_applied",
        file="dock/dock.yaml",
        before_sha256=hashlib.sha256(before_bytes).hexdigest(),
        after_sha256=hashlib.sha256(after_bytes).hexdigest(),
        changed=changed,
        session_rules=rules,
        applied_by="operator",
    )
    print(f"RECORDED operator_applied ({len(changed)} field(s) changed; "
          f"record {event['record_hash'][:12]})")
    for rule in rules:
        if rule["signed"]:
            print(f"  session rule for {rule['goal']}: signed and in force")
        else:
            print(f"  session rule for {rule['goal']}: NOT IN FORCE until you sign it "
                  f"in the portal (proposal {rule['proposal_id'][-12:]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
