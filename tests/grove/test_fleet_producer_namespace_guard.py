"""fleet-receipt-custody-v1 P3b — the producer-namespace disjointness guard.

Two namespaces share ~/.grove/flywheel/producer_pauses.yaml with no separator:
the FLEET worker ids (keys of config/fleet_workers.yaml, paused by P3b's breaker)
and the DORMANCY producer names (the strings passed to
dispatcher._run_guarded_producer, paused by the recurrence card). If a name ever
belonged to both, pausing one would silently pause the other. This pin discovers
both sets and asserts they are DISJOINT — no hand-listing, so a new worker or a
new dormancy producer enrols at collection time.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from grove.fleet.config import load_fleet_workers

pytestmark = pytest.mark.guard

_GROVE = pathlib.Path(__file__).resolve().parents[2] / "grove"


def _dormancy_producer_names():
    """Every string literal passed as the first arg to _run_guarded_producer,
    across grove/ — the dormancy producers that consult the shared pause file."""
    names = set()
    for py in sorted(_GROVE.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_run_guarded_producer"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def test_fleet_worker_ids_and_dormancy_producers_are_disjoint():
    fleet_ids = set(load_fleet_workers().keys())
    dormancy = _dormancy_producer_names()

    # Vacuity — both legs: a broken discovery must not pass by finding nothing.
    assert fleet_ids, "discovered no fleet worker ids — config scan broke"
    assert dormancy, "discovered no dormancy producers — _run_guarded_producer scan broke"

    collision = fleet_ids & dormancy
    assert not collision, (
        f"producer-namespace collision {sorted(collision)}: a name is BOTH a fleet "
        "worker and a dormancy producer, so pausing one silently pauses the other "
        "in the shared producer_pauses.yaml. Rename one."
    )
