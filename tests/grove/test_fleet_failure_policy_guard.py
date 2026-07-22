"""fleet-receipt-custody-v1 P3a — the failure-policy coverage guard.

Every failure class that can land on a fleet RECEIPT must appear in the policy
map, or `default` must be explicit. The scan is auto-enrolling: a new receipt
`check=` literal enrols at collection time, and if it is neither mapped nor a
known-unmapped exception the guard fails and forces a ruling — the map never
drifts silently behind the code.

RESIDUAL FENCE (named, not hidden): the scan sees `check=` LITERALS on the
receipt-writing calls (`write_synthetic_receipt`, `_event`) and on
`FleetWorkerAndon` raised in `worker_entry` (which the `main()` catch-all turns
into a receipt). It does NOT see dynamically-computed check values — a variable
`check=` threaded through `_close_genesis`, `_uncaught_check(exc)`, the
`nonzero_exit` fallback, or the approval-deferred handler. Those rely on
`default: retry`. Andon-only checks (surface_fleet_andon in the manager /
resolvers / reap / config — never a receipt) are out of scope by construction.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from grove.fleet.unit_state import load_failure_policy

pytestmark = pytest.mark.guard

_FLEET = pathlib.Path(__file__).resolve().parents[2] / "grove" / "fleet"

# Guaranteed-literal synthetic receipt classes — the vacuity anchor. If the scan
# stops finding these, its surface broke and every downstream assertion is moot.
_ANCHORS = frozenset({"wall_clock_exceeded", "catastrophic_no_event", "reaped_at_restart"})

# A-P3-1: receipt check classes emitted but NOT in the policy map, carried by
# `default: retry` pending an operator ruling. RATCHET (shrink-only): a class
# ruled into the map MUST be removed here; a NEW unmapped receipt class must not
# appear without a ruling. Every entry is a FleetWorkerAndon raised in
# worker_entry that reaches a receipt via the main() catch-all.
_KNOWN_UNMAPPED = frozenset({
    "bad_skill_id",
    "inbox_missing",
    "model_binding_malformed_slug",
    "no_archive_location",
    "no_declared_sink",
    "no_grove_home",
    "no_routing_config",
    "record_not_found",
    "record_not_skill",
})


def _check_literal(call):
    for kw in call.keywords:
        if kw.arg == "check" and isinstance(kw.value, ast.Constant) and isinstance(
            kw.value.value, str
        ):
            return kw.value.value
    return None


def _discover_receipt_check_classes():
    discovered = set()
    for py in sorted(_FLEET.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            fn = node.func.id
            if fn in ("write_synthetic_receipt", "_event") or (
                fn == "FleetWorkerAndon" and py.name == "worker_entry.py"
            ):
                lit = _check_literal(node)
                if lit:
                    discovered.add(lit)
    return discovered


def test_every_receipt_check_class_is_mapped_or_known_unmapped():
    discovered = _discover_receipt_check_classes()

    # Vacuity — both legs: the scan found classes AND it still sees the anchors.
    assert discovered, "receipt-check scan found nothing — the surface broke"
    missing_anchors = _ANCHORS - discovered
    assert not missing_anchors, (
        f"scan lost sight of guaranteed receipt classes {sorted(missing_anchors)} "
        "— extractor regression"
    )

    policy = load_failure_policy()
    # The escape clause: unmapped classes are covered ONLY because default exists.
    assert policy.default_disposition, "no explicit `default` — unmapped classes unhandled"

    mapped = set(policy.failure_policy)
    diff = discovered - mapped

    # No NEW unmapped receipt class without a ruling.
    new = diff - _KNOWN_UNMAPPED
    assert not new, (
        "unmapped receipt check class(es) with no ruling: "
        f"{sorted(new)} — rule each (map it, or add to _KNOWN_UNMAPPED with a note)"
    )
    # Shrink-only: a class ruled INTO the map must be dropped from the allowlist.
    stale = _KNOWN_UNMAPPED - diff
    assert not stale, (
        f"allowlisted class(es) no longer unmapped — remove from _KNOWN_UNMAPPED "
        f"so the ratchet only tightens: {sorted(stale)}"
    )
