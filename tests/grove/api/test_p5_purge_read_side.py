"""promoted-artifact-persistence-v1 P5 S4 — purge suppression (ANDON ruling).

PIN (PM-ruled): purge-manifest evidence suppresses the unit from the
four-state surface — absence, not a fifth state — ranking above BOTH rule 5
(terminal_skip → rejected) and rule 2 (ledger terminal authority). The
redraft-after-purge caveat (staged + open proposal) beats suppression.

Both empirical cases from the S4 Andon probe are pinned as EXITING.
Local: GROVE_HOME + GROVE_WIKI_PATH → tmp; real capability records.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    return home


def _units(skill_id):
    from grove.api.portal import _list_fleet_units
    from grove.capability_registry import load_capabilities
    return {r["unit_id"]: r["governance_state"]
            for r in _list_fleet_units(load_capabilities()[skill_id])}


def _ledger_applied(home, uid, slug):
    d = home / ".kaizen_ledger"
    d.mkdir(exist_ok=True)
    ev = {"event_type": "kaizen_disposition",
          "proposal_type": "forge_artifact_pending", "disposition": "applied",
          "applied_result": {"unit_id": uid, "slug": slug}}
    (d / "s.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")


def test_purged_file_producer_unit_exits(grove_home):
    """Andon probe case A pinned: promoted → PURGED → absent (was: rejected
    via rule-5 terminal_skip). Suppression outranks rule 5."""
    from tools.fleet_lifecycle_tool import fleet_purge

    f = grove_home / "drafter" / "draft-2026-01-01-moon.md"
    f.parent.mkdir(parents=True)
    f.write_text("D")
    assert _units("skill.fleet.drafter") == {"2026-01-01-moon": "promoted"}

    fleet_purge("drafter", "2026-01-01-moon")
    assert _units("skill.fleet.drafter") == {}  # EXITED — not rejected


def test_purged_remote_sink_unit_exits(grove_home):
    """Andon probe case B pinned: ledger-applied promoted → PURGED → absent
    (was: phantom 'promoted' via rule-2 ledger authority). Suppression
    outranks rule 2; the slug→uid ledger join keys the manifest."""
    from tools.fleet_lifecycle_tool import fleet_purge

    slug, uid = "260101-acme-pm", "row-1"
    d = grove_home / "forge" / slug
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    _ledger_applied(grove_home, uid, slug)
    assert _units("skill.fleet.forge-jobsearch") == {uid: "promoted"}

    fleet_purge("forge-jobsearch", slug, unit_id=uid)
    assert _units("skill.fleet.forge-jobsearch") == {}  # EXITED — no phantom


def test_redraft_after_purge_surfaces_needs_review(grove_home):
    """The ruled caveat: staged + open proposal BEATS suppression — a
    legitimate redraft is reviewable, never hidden by the old purge."""
    from grove.eval import proposal_queue
    from tools.fleet_lifecycle_tool import fleet_purge

    slug, uid = "260101-acme-pm", "row-1"
    d = grove_home / "forge" / slug
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    _ledger_applied(grove_home, uid, slug)
    fleet_purge("forge-jobsearch", slug, unit_id=uid)
    assert _units("skill.fleet.forge-jobsearch") == {}

    # redraft: freshly staged with an OPEN proposal
    sd = grove_home / "forge" / "pending_review" / slug
    sd.mkdir(parents=True)
    (sd / "resume.md").write_text("R2")
    (sd / "meta.json").write_text(json.dumps(
        {"row_id": uid, "slug": slug, "company": "Acme", "role": "PM"}))
    proposal_queue.file_agentless(
        type=proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
        payload={"slug": slug, "row_id": uid,
                 "skill_id": "skill.fleet.forge-jobsearch"},
        evidence=(uid,), justification="redraft",
        proposer="skill.fleet.forge-jobsearch",
    )
    assert _units("skill.fleet.forge-jobsearch") == {uid: "needs_review"}


def test_unreadable_manifest_never_suppresses(grove_home):
    """Garbage must not hide a unit: a corrupt purge manifest is skipped and
    the unit stays visible."""
    f = grove_home / "drafter" / "draft-2026-01-01-moon.md"
    f.parent.mkdir(parents=True)
    f.write_text("D")
    bad = grove_home / "drafter" / ".archive" / "2026-01-01-moon-20260101T000000Z"
    bad.mkdir(parents=True)
    (bad / "purge-manifest.json").write_text("{not json", encoding="utf-8")

    assert _units("skill.fleet.drafter") == {"2026-01-01-moon": "promoted"}


def test_reject_style_archive_without_manifest_never_suppresses(grove_home):
    """Reject/revision archives carry NO manifest — only the purge core writes
    one. A rejected unit's archive residue does not suppress anything."""
    f = grove_home / "drafter" / "draft-2026-01-01-moon.md"
    f.parent.mkdir(parents=True)
    f.write_text("D")
    rej = grove_home / "drafter" / ".archive" / "other-unit-20260101T000000Z"
    rej.mkdir(parents=True)
    (rej / "draft-old.md").write_text("rejected residue")

    assert _units("skill.fleet.drafter") == {"2026-01-01-moon": "promoted"}
