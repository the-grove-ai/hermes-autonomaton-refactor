"""fleet-receipt-custody-v1 P4a — eligibility binds to the derivation.

The resolver stops asking "is there a staged directory" and asks "what state is
this unit in." Excluded from selection: Working, Needs you, Dead-lettered. Not
excluded: Done, Waiting. Both inputs (disposed, terminal_skip) are assembled
from the existing committed readers — no meta.json glob, no second ledger parse.
"""

from __future__ import annotations

import inspect
import json

from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING
from grove.fleet import paths, resolvers
from grove.forge import feedback_store
from grove.kaizen_ledger import default_ledger_dir

_W = "forge"
_IS = {"skip_already_staged": True}


def _dispatch(unit_id, run_id):
    p = paths.dispatch_path(_W, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "unit_id": unit_id, "worker_id": _W}))


def _event(run_id, status, check=None):
    p = paths.event_path(_W, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "status": status, "check": check}))


def _dispose(unit_id, disp):
    d = default_ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"disp-{unit_id}-{disp}.jsonl").write_text(json.dumps({
        "event_type": "kaizen_disposition",
        "proposal_type": PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
        "disposition": disp,
        "applied_result": {"unit_id": unit_id, "slug": unit_id},
    }) + "\n")


def _terminal_skip(unit_id):
    feedback_store.write(_W, unit_id, "guidance")
    feedback_store.set_terminal_skip(_W, unit_id)


def _select(*unit_ids):
    rows = [{"id": u} for u in unit_ids]
    return [r["id"] for r in resolvers._select_units(rows, _IS, _W)]


# ── the excluded states ─────────────────────────────────────────────────────


def test_working_unit_is_not_selected():
    _dispatch("u1", "r1")  # dispatch, no receipt -> Working
    assert _select("u1") == []


def test_needs_you_unit_is_not_selected():
    _dispatch("u1", "r1")
    _event("r1", "success")  # success receipt, no disposition -> Needs you
    assert _select("u1") == []


def test_dead_lettered_by_retry_cap_is_not_selected():
    for r in ("r1", "r2", "r3"):
        _dispatch("u1", r)
        _event(r, "failed", "no_package")  # 3 retry failures -> cap
    assert _select("u1") == []


def test_dead_lettered_by_terminal_skip_is_not_selected():
    _terminal_skip("u1")  # won't-converge -> Dead-lettered
    assert _select("u1") == []


# ── the NOT-excluded states ─────────────────────────────────────────────────


def test_a_rejected_unit_under_the_revision_cap_is_selected():
    _dispose("u1", "rejected")  # terminal disposition -> Done (re-draftable)
    feedback_store.write(_W, "u1", "one revision")  # count 1 < cap, NOT terminal_skip
    assert _select("u1") == ["u1"]


def test_an_applied_unit_is_excluded_by_the_tracker_filter_not_by_state():
    _dispose("u1", "applied")  # Done — the STATE filter does not exclude it
    assert _select("u1") == ["u1"]  # passes state; the Notion filter is the brake


def test_a_fresh_unit_is_selected():
    assert _select("fresh") == ["fresh"]  # no runs -> Waiting -> eligible


# ── structural pins ─────────────────────────────────────────────────────────


def test_no_meta_json_glob_remains_in_the_selection_path():
    src = (
        inspect.getsource(resolvers._select_units)
        + inspect.getsource(resolvers._select_file_units)
        + inspect.getsource(resolvers._build_unit_state_context)
        + inspect.getsource(resolvers._derived_unit_state)
    )
    assert "meta.json" not in src, "a disk glob survives in the selection path"
    # and the removed authorities are gone from the module entirely
    mod = inspect.getsource(resolvers)
    assert "_staged_row_ids" not in mod
    assert "_staged_unit_ids" not in mod


def test_disposed_derives_from_the_committed_reader_not_a_second_parse():
    src = inspect.getsource(resolvers._build_unit_state_context)
    assert "_ledger_terminal_dispositions" in src, (
        "disposed must reuse the committed reader"
    )
    # no second parse of the ledger event type in the resolver
    assert "kaizen_disposition" not in inspect.getsource(resolvers)


def test_both_dead_lettered_causes_exclude_unconditionally_others_gated():
    """Dead-lettered is a VERDICT — excluded with skip_already_staged false, by
    either cause. Working and Needs-you are the in-flight / pending states the
    flag governs, so they are NOT excluded when the flag is off."""
    _terminal_skip("dl_ts")  # won't-converge -> Dead-lettered
    for r in ("a1", "a2", "a3"):
        _dispatch("dl_rc", r)
        _event(r, "failed", "no_package")  # retry cap -> Dead-lettered
    _dispatch("wk", "wk-r")  # dispatch, no receipt -> Working
    _dispatch("ny", "ny-r")
    _event("ny-r", "success")  # success, no disposition -> Needs you

    candidates = [{"id": u} for u in ("dl_ts", "dl_rc", "wk", "ny", "fresh")]
    no_flag: dict = {}  # skip_already_staged ABSENT

    for select in (resolvers._select_units, resolvers._select_file_units):
        got = {r["id"] for r in select(candidates, no_flag, _W)}
        # both Dead-lettered causes excluded, unconditionally
        assert "dl_ts" not in got, select.__name__
        assert "dl_rc" not in got, select.__name__
        # Working / Needs-you NOT excluded without the flag (gated states)
        assert {"wk", "ny", "fresh"} <= got, select.__name__
