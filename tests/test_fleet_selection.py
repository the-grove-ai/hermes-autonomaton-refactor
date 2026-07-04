"""fleet-pipeline-v1 P4 — single-unit selection (skip-already-staged + rank + one).

Generic + config-driven: the resolver reads order_by/select_one/skip_already_staged
off input_state and the staging_dir off the worker's record — blind to field
meaning. GROVE_HOME is per-test isolated (autouse conftest).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.fleet import resolvers
from grove.fleet.errors import FleetWorkerAndon
from hermes_constants import get_hermes_home

_ORDER_BY = [{"field": "Fit Score", "direction": "desc"}, {"field": "id", "direction": "asc"}]
_IST = {"select_one": True, "skip_already_staged": True, "order_by": _ORDER_BY}


def _forge_sink() -> Path:
    # matches the forge record's governance.write_zone.staging_dir
    return Path(get_hermes_home()) / "forge" / "pending_review"


def _stage(slug: str, row_id: str):
    d = _forge_sink() / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"row_id": row_id, "slug": slug}))
    (d / "resume.md").write_text("R")


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


# ── non-recursive glob + malformed-meta Andon ────────────────────────────────


def test_glob_is_one_level_ignores_tmp_and_nested():
    _stage("260704-real", "pg1")
    # a .tmp sibling (mid-atomic-write) must be ignored by the */meta.json glob
    (_forge_sink() / "260704-real" / "meta.json.tmp").write_text("{partial")
    # a nested meta.json two levels deep must NOT be matched (non-recursive)
    nested = _forge_sink() / "260704-real" / "sub"
    nested.mkdir()
    (nested / "meta.json").write_text(json.dumps({"row_id": "pgNESTED"}))
    staged = resolvers._staged_row_ids("forge")
    assert staged == {"pg1"}  # only the one-level final meta.json


def test_malformed_meta_fails_loud_not_treated_unstaged():
    d = _forge_sink() / "260704-bad"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{ this is not json")
    with pytest.raises(FleetWorkerAndon) as ei:
        resolvers._staged_row_ids("forge")
    assert ei.value.check == "staged_meta_unreadable"


def test_no_sink_means_nothing_staged():
    assert resolvers._staged_row_ids("forge") == set()  # sink absent -> empty


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
