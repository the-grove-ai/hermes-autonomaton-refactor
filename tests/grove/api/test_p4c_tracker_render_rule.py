"""fleet-receipt-custody-v1 P4c — the view stops mislabeling published work.

A forge unit published by the standalone ``/publish`` tap left a folder_link-only
ledger entry (no joinable ``unit_id``) and a lingering staged dir. Rule 3 read that
dir as ``legacy``/``needs_review``. The tracker is the authority: a Notion row whose
Status is a published-state OR that carries an Application Package renders **promoted**.

The rule is a STRICT FALLBACK — after rule 2 (ledger wins over the lingering dir,
portal.py:996-1006) and before rule 3 (staged, :1009) — reached ONLY when the ledger
join fails (``uid not in ledger``). A unit both ledger-disposed and tracker-published
hits rule 2 first and never arrives. The discriminator is the tracker signal, never
the lingering dir (both cohorts carry one). A cold/failed Notion read degrades to
today's rendering and never blocks the view.
"""

from __future__ import annotations

import json

import pytest

from grove.api import portal
from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as FT


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _forge_cap():
    from grove.capability_registry import load_capabilities
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def _stage(home, unit, row_id):
    d = home / "forge" / "pending_review" / unit
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.md").write_text("r", encoding="utf-8")
    (d / "cover-letter.md").write_text("c", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"row_id": row_id, "slug": unit, "company": "C", "role": "R"}),
        encoding="utf-8",
    )
    return d


def _ledger(home, disposition, unit_id):
    d = home / ".kaizen_ledger"
    d.mkdir(parents=True, exist_ok=True)
    ev = {"event_type": "kaizen_disposition", "proposal_type": FT,
          "disposition": disposition, "applied_result": {"unit_id": unit_id}}
    (d / "s.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")


def _by_unit(rows):
    return {r["unit_id"]: r for r in rows}


def _tracker(monkeypatch, published):
    monkeypatch.setattr(portal, "_tracker_published_uids",
                        lambda worker, uids: set(published))


# ── the rule: tracker says published -> promoted (only when not in ledger) ──


def test_not_in_ledger_tracker_published_renders_promoted(grove_home, monkeypatch):
    _stage(grove_home, "260629-idc", "ROW-IDC")
    _tracker(monkeypatch, {"ROW-IDC"})   # Status Drafted / Application Package present
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-IDC"]
    assert r["governance_state"] == "promoted"


def test_not_in_ledger_not_published_falls_through_to_legacy(grove_home, monkeypatch):
    _stage(grove_home, "260705-dataiku", "ROW-DK")
    _tracker(monkeypatch, set())          # To Apply, no Package -> genuinely pending
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-DK"]
    assert r["governance_state"] == "legacy"   # pending, never promoted


def test_lingering_dir_is_not_the_discriminator(grove_home, monkeypatch):
    """Both units carry a lingering staged dir; the tracker signal alone decides."""
    _stage(grove_home, "260629-idc", "ROW-PUB")       # tracker: published
    _stage(grove_home, "260705-dataiku", "ROW-PEND")  # tracker: pending
    _tracker(monkeypatch, {"ROW-PUB"})
    by = _by_unit(portal._list_fleet_units(_forge_cap()))
    assert by["ROW-PUB"]["governance_state"] == "promoted"
    assert by["ROW-PEND"]["governance_state"] == "legacy"


# ── precedence: rule 2 (ledger) wins; the new rule never overrides it ────────


def test_in_ledger_rejected_wins_over_tracker_published(grove_home, monkeypatch):
    _stage(grove_home, "260618-sirion", "ROW-SIR")
    _ledger(grove_home, "rejected", "ROW-SIR")
    _tracker(monkeypatch, {"ROW-SIR"})    # tracker would say promoted — must NOT win
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-SIR"]
    assert r["governance_state"] == "rejected"


def test_in_ledger_applied_renders_promoted_via_rule2(grove_home, monkeypatch):
    _stage(grove_home, "260707-x", "ROW-X")
    _ledger(grove_home, "applied", "ROW-X")
    _tracker(monkeypatch, set())          # ledger, not tracker, is the authority
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-X"]
    assert r["governance_state"] == "promoted"


# ── graceful degradation: cold/failed Notion renders exactly as today ───────


def test_cold_notion_renders_as_today_no_error(grove_home, monkeypatch):
    _stage(grove_home, "260629-idc", "ROW-IDC")

    def boom(worker, uids):
        raise RuntimeError("notion cold")

    monkeypatch.setattr(portal, "_tracker_published_uids", boom)
    rows = portal._list_fleet_units(_forge_cap())   # MUST NOT raise
    assert _by_unit(rows)["ROW-IDC"]["governance_state"] == "legacy"  # today's result


# ── the batched read: Status/Package -> published set, and it never raises ──


def test_tracker_read_maps_status_and_package(grove_home, monkeypatch):
    from grove.fleet import resolvers
    payload = {"result": json.dumps({"results": [
        {"id": "ROW-DRAFTED", "Status": "Drafted", "Application Package": ""},
        {"id": "ROW-APPLIED", "Status": "Applied", "Application Package": ""},
        {"id": "ROW-PACKAGE", "Status": "Saved", "Application Package": "https://drive/x"},
        {"id": "ROW-TOAPPLY", "Status": "To Apply", "Application Package": ""},
    ]})}
    monkeypatch.setattr(resolvers, "_mcp_call", lambda *a, **k: payload)
    got = portal._tracker_published_uids(
        "forge", {"ROW-DRAFTED", "ROW-APPLIED", "ROW-PACKAGE", "ROW-TOAPPLY"})
    assert got == {"ROW-DRAFTED", "ROW-APPLIED", "ROW-PACKAGE"}  # To-Apply-no-package out


def test_tracker_read_cold_session_returns_empty(grove_home, monkeypatch):
    from grove.fleet import resolvers
    monkeypatch.setattr(resolvers, "_mcp_call", lambda *a, **k: {"error": "cold"})
    assert portal._tracker_published_uids("forge", {"ROW-1"}) == set()


def test_tracker_read_exception_returns_empty(grove_home, monkeypatch):
    from grove.fleet import resolvers

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(resolvers, "_mcp_call", boom)
    assert portal._tracker_published_uids("forge", {"ROW-1"}) == set()


def test_tracker_read_no_worker_or_no_units_is_empty(grove_home):
    assert portal._tracker_published_uids(None, {"ROW-1"}) == set()
    assert portal._tracker_published_uids("forge", set()) == set()
