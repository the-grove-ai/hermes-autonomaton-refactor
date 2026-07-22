"""fleet-pipeline-v1 P4 — single-unit selection (skip-already-staged + rank + one).

Generic + config-driven: the resolver reads order_by/select_one/skip_already_staged
off input_state and the staging_dir off the worker's record — blind to field
meaning. GROVE_HOME is per-test isolated (autouse conftest).
"""

from __future__ import annotations

import json

from grove.fleet import paths, resolvers

_ORDER_BY = [{"field": "Fit Score", "direction": "desc"}, {"field": "id", "direction": "asc"}]
_IST = {"select_one": True, "skip_already_staged": True, "order_by": _ORDER_BY}


def _stage(_slug: str, row_id: str):
    """fleet-receipt-custody-v1 P4a — "already staged" is no longer a directory on
    disk; it is a derived state. A drafted-and-pending unit has a success receipt
    with no disposition → Needs you → excluded from re-selection. Simulate that."""
    run_id = f"{row_id}-run"
    dp = paths.dispatch_path("forge", run_id)
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(json.dumps({"run_id": run_id, "unit_id": row_id, "worker_id": "forge"}))
    ep = paths.event_path("forge", run_id)
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(json.dumps({"run_id": run_id, "status": "success", "check": None}))


# ── ranking determinism (ties + nulls) ──────────────────────────────────────


def test_order_by_ties_broken_by_id_asc():
    rows = [{"id": "b", "Fit Score": 91}, {"id": "a", "Fit Score": 91}, {"id": "c", "Fit Score": 95}]
    out = resolvers._select_units(rows, {"order_by": _ORDER_BY}, "forge")
    assert [r["id"] for r in out] == ["c", "a", "b"]  # 95 first; 91-tie -> id asc


def test_order_by_nulls_sort_last_not_arbitrary():
    rows = [{"id": "d", "Fit Score": None}, {"id": "c", "Fit Score": 95},
            {"id": "e", "Fit Score": 76}]
    out = resolvers._select_units(rows, {"order_by": _ORDER_BY}, "forge")
    assert [r["id"] for r in out] == ["c", "e", "d"]  # None LAST, regardless of desc


def test_null_fit_reversed_input_still_last():
    # determinism independent of input order
    rows = [{"id": "c", "Fit Score": 95}, {"id": "d", "Fit Score": None}]
    rows2 = list(reversed(rows))
    a = [r["id"] for r in resolvers._select_units(rows, {"order_by": _ORDER_BY}, "forge")]
    b = [r["id"] for r in resolvers._select_units(rows2, {"order_by": _ORDER_BY}, "forge")]
    assert a == b == ["c", "d"]


# ── skip-already-staged + select_one (the 46 -> 1 case) ──────────────────────


def test_skips_staged_and_yields_one_top_fit():
    rows = [{"id": f"pg{i}", "Fit Score": 50 + i} for i in range(46)]  # pg45 has top fit 95
    _stage("260704-top", "pg45")  # the top-fit row is already staged
    out = resolvers._select_units(rows, _IST, "forge")
    assert len(out) == 1
    assert out[0]["id"] == "pg44"  # next-highest UN-staged (fit 94)


def test_all_staged_returns_empty():
    rows = [{"id": "pg1", "Fit Score": 91}, {"id": "pg2", "Fit Score": 80}]
    _stage("s1", "pg1")
    _stage("s2", "pg2")
    assert resolvers._select_units(rows, _IST, "forge") == []


def test_select_one_off_yields_all_ranked():
    rows = [{"id": "b", "Fit Score": 80}, {"id": "a", "Fit Score": 95}]
    out = resolvers._select_units(rows, {"order_by": _ORDER_BY, "select_one": False}, "forge")
    assert [r["id"] for r in out] == ["a", "b"]  # ranked, not truncated


# (fleet-receipt-custody-v1 P4a — the non-recursive-glob + malformed-meta Andon
# tests retired with the disk glob: eligibility no longer reads meta.json. The
# malformed-staged-meta path and its staged_meta_unreadable Andon are gone with
# _staged_row_ids/_staged_unit_ids. The derivation exclusion is covered by
# tests/test_fleet_eligibility.py.)


# ── full resolve path (generic, mocked MCP) ──────────────────────────────────


def test_resolve_notion_query_yields_one_unstaged_topfit(monkeypatch):
    rows = [{"id": "pg1", "Fit Score": 95}, {"id": "pg2", "Fit Score": 91},
            {"id": "pg3", "Fit Score": 80}]
    monkeypatch.setattr(resolvers, "_mcp_call",
                        lambda *a, **k: {"result": json.dumps({"results": rows})})
    _stage("260704-top", "pg1")  # top-fit already staged
    out = resolvers.resolve_input_state(
        {"type": "notion_query", "data_source": "5eb5630d-x",
         "filter": {"Status": "To Apply"}, **_IST},
        "forge",
    )
    assert out is not None
    assert len(out["rows"]) == 1 and out["rows"][0]["id"] == "pg2"  # next un-staged
