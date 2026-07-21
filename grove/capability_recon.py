"""capability-mutation-surface-v1 P6 (M7) — deploy-time admission recon.

READ-ONLY reconciliation of the capability admission surface: renders the
base↔overlay diff (definition values vs overlay values for the canonical
admission keys), overlay provenance, and orphaned overlay slugs, as plain
text for the deploy transcript (``scripts/deploy.sh`` invokes
``python -m grove.capability_recon`` post-deploy).

F6 RULING (do not violate): this module NEVER deletes, flushes, or rewrites
overlay state — upgrades cannot cross the sovereignty line. Orphans ALERT;
they are never removed here. The no-flush pin
(tests/grove/test_admission_recon.py) asserts this source contains no
deletion primitives.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml


def _definition_docs(definitions_dirs: Optional[List[Path]] = None) -> Dict[str, dict]:
    """Raw definition docs keyed by id — PURE base values (no state compose),
    read straight from the definition YAMLs (repo-bundled + GROVE_HOME mints
    by default)."""
    if definitions_dirs is None:
        from grove.capability_registry import (
            default_capabilities_dir,
            grove_home_capabilities_dir,
        )

        definitions_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    docs: Dict[str, dict] = {}
    for d in definitions_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("id"), str):
                docs.setdefault(doc["id"], doc)
    return docs


def render_admission_recon(
    definitions_dirs: Optional[List[Path]] = None,
    state_dir: Optional[Path] = None,
) -> str:
    """The base↔overlay diff, per slug, plus orphan ALERTs. Pure read."""
    from grove.capability_registry import (
        _StateFileInvalid,
        _read_state_file,
        capability_state_dir,
        orphaned_state_slugs,
    )

    sd = Path(state_dir) if state_dir is not None else capability_state_dir()
    defs = _definition_docs(definitions_dirs)
    lines: List[str] = ["=== capability admission recon (base <-> overlay) ==="]

    if not sd.is_dir():
        lines.append("no state overlay directory — nothing to reconcile")
        return "\n".join(lines)

    orphan_pairs = orphaned_state_slugs(defs, state_dir=sd)
    orphan_ids = {rid for (_p, rid) in orphan_pairs}

    overlay_files = sorted(sd.glob("*.yaml"))
    shown = 0
    for path in overlay_files:
        try:
            rid, state = _read_state_file(path)
        except _StateFileInvalid as exc:
            lines.append(f"INVALID (R-B1) {path.name}: {exc}")
            continue
        if rid in orphan_ids:
            continue  # rendered in the ALERT block below
        base = defs.get(rid) or {}
        base_intents = ((base.get("trigger") or {}).get("intents")) or []
        base_tiers = ((base.get("tier_rule") or {}).get("eligible")) or []
        base_pref = (base.get("tier_rule") or {}).get("preferred")
        prov = state.get("provenance") or {}
        prov_note = (
            f"[approval {prov.get('approval_id')}, {prov.get('timestamp')}]"
            if prov else "[NO PROVENANCE — pre-canonical file]"
        )
        touched = False
        lines.append(f"slug {rid} ({path.name}):")
        if "intents" in state:
            lines.append(
                f"  intents: definition {base_intents!r} -> overlay "
                f"{state['intents']!r} {prov_note}"
            )
            touched = True
        if "tiers" in state:
            lines.append(
                f"  tiers: definition {base_tiers!r} -> overlay "
                f"{state['tiers']!r} {prov_note}"
            )
            touched = True
            # D1 marker — a restriction excluding the definition preferred
            # re-anchors preferred at merge (derived, never operator-set).
            if base_pref is not None and base_pref not in state["tiers"]:
                lines.append(
                    f"  preferred: {base_pref} -> {max(state['tiers'])} "
                    "(derived — re-anchored by merge)"
                )
        if "added_intents" in state:
            lines.append(
                "  LEGACY: added_intents present (loader-honored; the "
                "canonical writer never emits it)"
            )
            touched = True
        if not touched:
            lines.append("  (no admission-field overlay keys)")
        shown += 1

    for _path, rid in orphan_pairs:
        lines.append(
            f"ALERT: orphaned overlay slug {rid!r} ({_path.name}) — no "
            "definition carries this id. NOT deleted (F6: deploy never "
            "flushes overlay state) — reconcile by hand or via the portal."
        )

    lines.append(
        f"total: {len(overlay_files)} overlay file(s), {shown} reconciled, "
        f"{len(orphan_pairs)} orphan(s)"
    )
    return "\n".join(lines)


def main() -> int:
    print(render_admission_recon())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
