"""fleet-receipt-custody-v1 P4b-0 — the fleet plane does not import the API plane.

The ledger disposition projection lives in a shared fleet-plane module so both
eligibility (resolvers, P4a) and emission (P4b) import it FORWARD. A fleet module
reaching backward into grove.api is the coupling this closes — and the guard
keeps it closed: a new backward import fails at collection time.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.guard

_FLEET = pathlib.Path(__file__).resolve().parents[2] / "grove" / "fleet"


def _fleet_modules_importing_api():
    offenders: dict = {}
    for py in sorted(_FLEET.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(py.relative_to(_FLEET.parent))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
                "grove.api"
            ):
                offenders.setdefault(rel, []).append(f"from {node.module} import …")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("grove.api"):
                        offenders.setdefault(rel, []).append(f"import {alias.name}")
    return offenders


def test_no_fleet_module_imports_grove_api():
    modules = list(_FLEET.rglob("*.py"))
    assert modules, "vacuity: no fleet modules discovered — scan broke"
    offenders = _fleet_modules_importing_api()
    assert not offenders, (
        "the fleet plane imports the API plane (backward coupling): "
        + "; ".join(f"{m}: {ims}" for m, ims in offenders.items())
    )


def test_disposition_projection_is_a_fleet_plane_module():
    import json

    from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING
    from grove.fleet.dispositions import (
        _ledger_slug_to_uid,
        _ledger_terminal_dispositions,
    )
    from grove.kaizen_ledger import default_ledger_dir

    assert _ledger_terminal_dispositions() == {}  # empty ledger -> empty

    d = default_ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "disp.jsonl").write_text(json.dumps({
        "event_type": "kaizen_disposition",
        "proposal_type": PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
        "disposition": "rejected",
        "applied_result": {"unit_id": "u1", "slug": "s1"},
    }) + "\n")
    assert _ledger_terminal_dispositions() == {"u1": "rejected"}
    assert _ledger_slug_to_uid() == {"s1": "u1"}
    # (the shared module's freedom from grove.api imports is enforced structurally
    # by test_no_fleet_module_imports_grove_api above — an AST scan, not a
    # substring match that would trip on the docstring naming the boundary.)
