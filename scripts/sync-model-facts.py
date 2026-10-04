#!/usr/bin/env python3
"""sync-model-facts.py — copy MISSING model_facts entries from the repo template
into the operator's live routing config.

    scripts/sync-model-facts.py            # dry run: show what would be added
    scripts/sync-model-facts.py --apply    # write

Why: a model bound to a tier needs a ``model_facts`` entry in
``$GROVE_HOME/routing.operational.yaml`` (context window, tool schema, cost).
Without one the router assumes an 8,192-token context window and warns on every
session. The live file is operator-owned and is never synced by deploy, so new
facts shipped in ``config/routing.operational.yaml`` do not reach it on their
own.

Additive only: an entry the operator already has is NEVER touched. The write
goes through ``RoutingConfigWriter.apply_mutation`` — backup, sandbox-validate,
atomic replace — the same sanctioned writer the portal model swap uses. Run it
as the user that owns ``$GROVE_HOME``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    import yaml

    from grove.config import routing_writer

    template_path = REPO_ROOT / "config" / "routing.operational.yaml"
    template = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    template_facts = template.get("model_facts") or {}
    if not isinstance(template_facts, dict) or not template_facts:
        print(f"ERROR: no model_facts mapping in {template_path}", file=sys.stderr)
        return 1

    live_path = routing_writer._default_config_path()
    if not live_path.exists():
        print(f"ERROR: live routing config not found at {live_path}", file=sys.stderr)
        return 1
    live = yaml.safe_load(live_path.read_text(encoding="utf-8")) or {}
    live_facts = live.get("model_facts") or {}
    if not isinstance(live_facts, dict):
        print(f"ERROR: model_facts in {live_path} is not a mapping", file=sys.stderr)
        return 1

    missing = [slug for slug in template_facts if slug not in live_facts]
    print(f"live config : {live_path}")
    print(f"template    : {template_path}")
    print(f"already declared live ({len(live_facts)}): {', '.join(live_facts) or '-'}")
    if not missing:
        print("nothing to add — every template entry is already declared live.")
        return 0
    print(f"missing live ({len(missing)}):")
    for slug in missing:
        f = template_facts[slug]
        print(
            f"  + {slug}  context_window={f.get('context_window')} "
            f"cost={f.get('cost_per_mtok_input')}/{f.get('cost_per_mtok_output')}"
        )
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply to add them.")
        return 0

    def mutate(data) -> None:
        facts = data.get("model_facts")
        if facts is None:
            data["model_facts"] = {}
            facts = data["model_facts"]
        for slug in missing:
            if slug not in facts:  # additive only
                facts[slug] = dict(template_facts[slug])

    routing_writer.get_writer().apply_mutation(
        mutate, label=f"sync model_facts (+{len(missing)})"
    )
    print(f"✓ added {len(missing)} model_facts entr{'y' if len(missing) == 1 else 'ies'}; "
          f"backup at {live_path}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
