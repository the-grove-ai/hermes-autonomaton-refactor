"""fleet-receipt-custody-v1 P4b-1 — card emission binds to derived state.

A per-tick scan is the SINGLE artifact-card authority (it replaces the per-run
one-shot emit at the reap instant). A unit that reads **Needs you** and carries
no live card emits exactly ONE card; Done / Working / Dead-lettered emit
nothing; a unit already carrying a live card is SKIPPED (emit-once-and-skip,
never attempt-and-dedup). Age is irrelevant — the reconciler's ``.classified``
sidecar and 7-day window are NOT inherited (R3).

The precondition that makes UNIT-grain keying safe — a completed unit has at
most ONE non-superseded success run (forge redrafts in-process on one run_id;
every worker is ``skip_already_staged`` so a Needs-you unit is never re-selected
into a second success) — is asserted STRUCTURALLY: two live success runs fail
LOUD rather than silently pick one. If a producer is ever changed so a unit can
carry two live success runs, this pin fires and flags that Option B's grain is
now wrong (see fleet-emission-grain-coupling).
"""

from __future__ import annotations

import inspect
import json

import pytest

from grove.fleet import manager as manager_mod, paths
from grove.eval import proposal_queue as pq
from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as FT
from grove.kaizen_ledger import default_ledger_dir

_W = "forge"
_SKILL = "skill.fleet.forge-jobsearch"


def _dispatch(unit_id, run_id, w=_W):
    p = paths.dispatch_path(w, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "unit_id": unit_id, "worker_id": w}))


def _success(unit_id, run_id, w=_W, slug=None, **fields):
    """One completed success run for a unit: a dispatch record + a success event."""
    _dispatch(unit_id, run_id, w)
    p = paths.event_path(w, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    ev = {"run_id": run_id, "worker_id": w, "skill": _SKILL, "status": "success",
          "slug": slug or unit_id, "row_id": unit_id}
    ev.update(fields)
    p.write_text(json.dumps(ev))


def _dispose(unit_id, disp):
    d = default_ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"disp-{unit_id}-{disp}.jsonl").write_text(json.dumps({
        "event_type": "kaizen_disposition",
        "proposal_type": FT, "disposition": disp,
        "applied_result": {"unit_id": unit_id, "slug": unit_id},
    }) + "\n")


@pytest.fixture
def captured(monkeypatch):
    emits, andons = [], []
    monkeypatch.setattr(pq, "file_agentless",
                        lambda **kw: (emits.append(kw), ("sha256:x", True))[1])
    monkeypatch.setattr(manager_mod, "surface_fleet_andon",
                        lambda wid, run_id, msg, **kw: andons.append(kw.get("check")))
    return emits, andons


# ── the core binding: Needs-you + no live card -> exactly one card ───────────


def test_needs_you_no_card_emits_one(captured):
    emits, andons = captured
    _success("u1", "r1")
    manager_mod.FleetManager()._emit_state_cards()
    assert len(emits) == 1 and andons == []
    assert emits[0]["type"] == FT
    assert emits[0]["payload"]["slug"] == "u1"


# ── the excluded states emit nothing ─────────────────────────────────────────


def test_done_by_applied_emits_nothing(captured):
    emits, andons = captured
    _success("u1", "r1")
    _dispose("u1", "applied")  # terminal disposition -> Done (A-P4b-1)
    manager_mod.FleetManager()._emit_state_cards()
    assert emits == []


def test_done_by_rejected_emits_nothing(captured):
    emits, andons = captured
    _success("u1", "r1")
    _dispose("u1", "rejected")  # terminal disposition -> Done
    manager_mod.FleetManager()._emit_state_cards()
    assert emits == []


def test_working_emits_nothing(captured):
    emits, andons = captured
    _dispatch("u1", "r1")  # dispatch, no receipt -> Working
    manager_mod.FleetManager()._emit_state_cards()
    assert emits == []


# ── emit-once-and-skip (R2): a carded unit is skipped, across ticks ──────────


def test_emit_once_and_skip_across_ticks():
    """A REAL queue write: the first scan emits, the second reads the queue once
    and skips (state prevents double-carding — the content-hash is the backstop,
    not the mechanism). Two ticks -> one card, not two."""
    _success("u1", "r1")
    m = manager_mod.FleetManager()
    m._emit_state_cards()
    m._emit_state_cards()
    live = [p for p in pq.read_all() if p.type == FT]
    assert len(live) == 1


# ── the precondition pin: at most ONE non-superseded success run ─────────────


def test_two_success_runs_fail_loud_no_card(captured):
    """The invariant unit-grain depends on. Two live success runs for one unit is
    the grain violation: fail LOUD, emit no card — never silently pick a run."""
    emits, andons = captured
    _success("u1", "r1")
    _success("u1", "r2")  # a SECOND live success run for the same unit
    manager_mod.FleetManager()._emit_state_cards()
    assert emits == []
    assert "emission_grain_violation" in andons


def test_single_success_run_emits(captured):
    """Vacuity companion to the grain pin — one success run is the normal path."""
    emits, andons = captured
    _success("u1", "r1")
    manager_mod.FleetManager()._emit_state_cards()
    assert len(emits) == 1
    assert "emission_grain_violation" not in andons


# ── R3: no inherited suppressors (age / window / .classified) ────────────────


def test_classified_sidecar_does_not_suppress_emission(captured):
    """The reconciler's ``.classified`` sidecar is a boot-reconciliation guard,
    NOT an emission gate — a Needs-you unit whose event is classified still cards."""
    emits, andons = captured
    _success("u1", "r1")
    manager_mod._classified_marker_path(paths.event_path(_W, "r1")).touch()
    manager_mod.FleetManager()._emit_state_cards()
    assert len(emits) == 1


def test_emission_path_carries_no_inherited_suppressor():
    """R3, structural — the emission path carries no age/timestamp/sidecar gate.
    Checked over CODE tokens only (comments and strings stripped) so the prose
    naming the boundary — 'no .classified gate' — does not itself trip the pin."""
    import io
    import tokenize

    src = (
        inspect.getsource(manager_mod.FleetManager._emit_state_cards)
        + "\n"
        + inspect.getsource(manager_mod.FleetManager._success_run_for_unit)
        + "\n"
        + inspect.getsource(manager_mod.FleetManager._emit_artifact_card)
    )
    code_names = {
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.NAME
    }
    forbidden = {
        "_classified_marker_path", "_mark_classified",  # the .classified sidecar
        "_RECONCILE_WINDOW_DAYS",                        # the 7-day boot window
        "st_mtime", "getmtime",                         # any age read
    }
    leaked = forbidden & code_names
    assert not leaked, f"inherited suppressor(s) leaked into the emission path: {leaked}"
