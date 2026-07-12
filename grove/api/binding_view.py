"""binding-governance-surfaces-v1 Phase 3 — effective-binding read helper.

DATA ONLY (the ``_fleet_index_rows`` discipline): no HTML, no layout hints —
the binding page fragment and the pin/unpin action re-render both inherit
this row. Resolution is PURE (GATE-A D7): a row derives from the loaded
capability record + the live routing config, never from worker state — the
effective binding is queryable without booting a worker.

Plane semantics (GATE-A F15/FLAG-7, stated plainly on every row):

* ``type=model`` pin — fleet workers honor it (worker boot swaps the tier's
  model for the pinned slug); the interactive agent refuses it and runs at
  the turn tier.
* ``type=tier_override`` — the interactive agent honors it at skill
  invocation; a fleet worker boots at the record's preferred tier regardless.
* no binding — the skill inherits its preferred tier's current model.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Plane-qualification copy — one source, reused by every renderer of the row.
PLANE_NOTE_PIN = (
    "Fleet workers honor this pin; the interactive agent runs at the turn tier."
)
PLANE_NOTE_TIER_OVERRIDE = (
    "The interactive agent honors this override; fleet workers boot at the "
    "record's preferred tier."
)
PLANE_NOTE_INHERIT = "Pinning applies to fleet worker runs only."


def _tier_model(prefs: Dict[str, Any], tier: str) -> Optional[str]:
    entry = prefs.get(tier)
    if isinstance(entry, dict):
        return entry.get("model")
    return None


def _row_for(name: str, cap: Any, registry: Dict[str, tuple],
             prefs: Dict[str, Any]) -> Dict[str, Any]:
    from grove.api.fragments import _PRODUCER_MODE
    from grove.capability_registry import _binding_to_dict

    mode = ((cap.governance.get("approval_handoff") or {}).get("mode"))
    binding = _binding_to_dict(getattr(cap, "model_binding", None))
    preferred = f"T{cap.tier_rule.preferred}"
    tier_model = _tier_model(prefs, preferred)

    if binding is None:
        state = f"inherits {preferred} (currently {tier_model or '(unbound)'})"
        plane_note = PLANE_NOTE_INHERIT
    elif binding.get("type") == "model":
        state = f"pinned: {binding.get('model')}"
        plane_note = PLANE_NOTE_PIN
    elif binding.get("type") == "tier_override":
        ot = binding.get("tier")
        state = (
            f"tier override {ot} "
            f"(currently {_tier_model(prefs, ot) or '(unbound)'})"
        )
        plane_note = PLANE_NOTE_TIER_OVERRIDE
    else:  # specialty — validated-but-no-op, reserved
        state = f"{binding.get('type')} binding (no-op)"
        plane_note = ""

    worker = None
    reg = registry.get(cap.id)
    if reg is not None:
        wid, cfg = reg
        worker = {"id": wid, "enabled": cfg.enabled, "cadence": cfg.cadence}

    return {
        "skill": name,
        "record_id": cap.id,
        "group": "producer" if mode == _PRODUCER_MODE else "observer",
        "worker": worker,
        "binding": binding,
        "preferred_tier": preferred,
        "tier_model": tier_model,
        "pinned": bool(binding and binding.get("type") == "model"),
        "state": state,
        "plane_note": plane_note,
    }


def binding_rows() -> List[Dict[str, Any]]:
    """One row per fleet capability (kind=skill with a governance block),
    joined with the operational worker registry — the Auxiliary group census.
    Fresh read every call (N2 discipline: render reflects post-write state)."""
    from grove.api.portal import _fleet_skill_records, _fleet_worker_registry
    from grove.api.fragments import _live_tier_preferences

    records = _fleet_skill_records()
    registry = _fleet_worker_registry()
    prefs = _live_tier_preferences()
    return [
        _row_for(name, records[name], registry, prefs)
        for name in sorted(records)
    ]


def binding_row(skill: str) -> Optional[Dict[str, Any]]:
    """The fresh-read row for ONE skill name (post-write re-render), or None
    when no fleet record carries that name."""
    from grove.api.portal import _fleet_skill_records, _fleet_worker_registry
    from grove.api.fragments import _live_tier_preferences

    records = _fleet_skill_records()
    cap = records.get(skill)
    if cap is None:
        return None
    return _row_for(skill, cap, _fleet_worker_registry(), _live_tier_preferences())
