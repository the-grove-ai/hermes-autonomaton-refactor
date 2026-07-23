"""fleet-pipeline-v1 P2 — proposal type + verb renderer + generalized emitter.

Covers: _event additive fields + _row_identity sourcing; the artifact-card
emitter mints a forge_artifact_pending proposal ONLY for an action_surface_publish
success, reading fields OFF the event (no_work + failures silent), gated on
approval_handoff.mode; the generalized single agentless emission path; render-only
status w.r.t. the generic approve machinery; and the verb-iterating portal buttons.

fleet-receipt-custody-v1 P4b-1 note: card emission is now driven by the per-tick
state scan, so the card-contract tests call ``_emit_artifact_card`` directly (the
card LOGIC's new home); the reap-instant path (``_classify_terminal``) no longer
cards — it only fires the armed unattended publish. The no_work / failure /
ingest_post SILENCE tests still drive ``_classify_terminal`` (nothing to emit
there, unchanged).
"""

from __future__ import annotations

import json

import pytest

from grove.fleet import manager as manager_mod, worker_entry as we
from grove.eval import proposal_queue as pq
from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as FT


# ── _event additive fields (A1) ─────────────────────────────────────────────


def test_event_has_additive_fields():
    ev = we._event("forge", "r", "skill.fleet.forge-jobsearch", "success",
                   slug="260704-acme", row_id="pg1", fit_score=91)
    assert ev["slug"] == "260704-acme" and ev["row_id"] == "pg1" and ev["fit_score"] == 91
    # unset -> None, and status still present (reap keys on presence-of-status)
    ev2 = we._event("w", "r", "s", "no_work")
    assert ev2["slug"] is None and ev2["row_id"] is None and ev2["status"] == "no_work"


def test_row_identity_sources_from_meta_and_payload():
    meta = json.dumps({"row_id": "pg1", "company": "Acme"})
    package = {"slug": "s", "files": {"meta.json": meta, "resume.md": "..."}}
    payload = {"rows": [{"id": "pg1", "Fit Score": 91}, {"id": "pg2", "Fit Score": 5}]}
    assert we._row_identity(package, payload) == ("pg1", 91)
    # tolerant: no meta / no rows -> (None, None), never raises
    assert we._row_identity({"files": {}}, None) == (None, None)


# ── emitter: success emits, no_work + failure silent (gated on approval_handoff) ─


@pytest.fixture
def captured(monkeypatch):
    emits, andons = [], []
    monkeypatch.setattr(pq, "file_agentless",
                        lambda **kw: (emits.append(kw), ("sha256:x", True))[1])
    monkeypatch.setattr(manager_mod, "surface_fleet_andon",
                        lambda wid, run_id, msg, **kw: andons.append(kw.get("check")))
    return emits, andons


def _handle(run_id="r"):
    class _H:
        worker_id = "forge"
        wall_clock_secs = 900
        pgid = 1
    h = _H(); h.run_id = run_id
    return h


def _success_event(skill="skill.fleet.forge-jobsearch", **over):
    ev = {"worker_id": "forge", "run_id": "r", "skill": skill, "status": "success",
          "slug": "260704-acme", "row_id": "pg1", "fit_score": 91, "staged": ["x"]}
    ev.update(over)
    return ev


def test_success_emits_complete_payload(captured):
    emits, andons = captured
    m = manager_mod.FleetManager()
    # fleet-receipt-custody-v1 P4b-1 — card emission moved off the reap instant to
    # the per-tick state scan; the card LOGIC lives in _emit_artifact_card (the
    # payload contract below is unchanged; only the trigger moved).
    m._emit_artifact_card("forge", "r", _success_event())
    assert len(emits) == 1 and andons == []
    kw = emits[0]
    assert kw["type"] == FT
    # forge-publish-meta-hotfix-v1 P1 — a CLEAN draft's payload is byte-identical
    # to the pre-sprint shape (no meta_defect key); the content-addressed
    # proposal_id is unchanged.
    assert kw["payload"] == {
        "slug": "260704-acme", "row_id": "pg1",
        "skill_id": "skill.fleet.forge-jobsearch", "fit_score": 91,
    }


def test_defect_marked_success_andons_and_marks_card(captured):
    """forge-publish-meta-hotfix-v1 P1 — a success event carrying a meta_defect
    STILL mints the promote proposal (surface-regardless) but ALSO fires the loud
    operator Andon and threads the marker into the card payload."""
    emits, andons = captured
    m = manager_mod.FleetManager()
    m._emit_artifact_card("forge", "r", _success_event(meta_defect="missing:role"))
    assert len(emits) == 1  # draft is NOT withheld
    assert emits[0]["payload"]["meta_defect"] == "missing:role"
    assert "forge_meta_incomplete" in andons  # loud operator signal fired


def test_no_work_emits_nothing(captured):
    emits, andons = captured
    m = manager_mod.FleetManager()
    m._classify_terminal("forge", _handle(), 0,
                         {"status": "no_work", "skill": "skill.fleet.forge-jobsearch"},
                         killed=False)
    assert emits == [] and andons == []


def test_failure_branches_emit_nothing(captured):
    emits, andons = captured
    m = manager_mod.FleetManager()
    # exit-0 + failed status
    m._classify_terminal("forge", _handle(), 0,
                         {"status": "failed", "detail": "boom", "check": "no_package",
                          "skill": "skill.fleet.forge-jobsearch"}, killed=False)
    # nonzero exit
    m._classify_terminal("forge", _handle(), 1, None, killed=False)
    # wall-clock kill
    m._classify_terminal("forge", _handle(), -9, _success_event(), killed=True)
    assert emits == []  # no emit on ANY failure path
    assert andons  # failures DO andon


def test_ingest_post_worker_does_not_emit(captured):
    emits, andons = captured
    m = manager_mod.FleetManager()
    # scout is approval_handoff.mode=ingest_post -> no operator-promote proposal
    m._classify_terminal("scout", _handle(), 0,
                         _success_event(skill="skill.fleet.scout"), killed=False)
    assert emits == [] and andons == []


def test_success_without_slug_andons(captured):
    emits, andons = captured
    m = manager_mod.FleetManager()
    m._emit_artifact_card("forge", "r", _success_event(slug=None))
    assert emits == [] and "event_missing_slug" in andons


# ── generalized emission path (one path, not a fork) ─────────────────────────


def test_one_emission_path(tmp_path):
    q = tmp_path / "q.jsonl"
    # portal_action_failure flows through the SAME file_agentless
    pid, ap = pq.file_agentless_proposal(
        failure_class="fc", action="a", evidence="e", justification="j", path=q)
    assert ap and pq.read(pid, path=q).type == "portal_action_failure"
    pid2, ap2 = pq.file_agentless(
        type=FT, payload={"slug": "s"}, evidence=("s",), path=q)
    assert ap2 and pq.read(pid2, path=q).type == FT


def test_forge_type_is_render_only():
    # NOT a PROPOSAL_HANDLERS row -> generic SYNC approve is suppressed.
    assert pq._type_offers_approve(FT) is False


# ── verb-iterating portal buttons ────────────────────────────────────────────
# fleet-ui-reconciliation-v1 C3: the helper no longer renders in the PENDING
# FEED (artifact types partition into the Fleet cross-link card) — its live
# consumer is the promote-failure card (actions._forge_promote_error_card).


def test_verb_actions_render_promote_and_reject():
    from grove.api.fragments import _verb_actions_html
    from grove.eval.proposal_queue import PROPOSAL_VERBS
    html = _verb_actions_html("sha256:abc", "abc", PROPOSAL_VERBS[FT])
    assert "Promote" in html and "Reject" in html
    assert "/portal/actions/proposals/sha256:abc/promote" in html
    assert "/portal/actions/proposals/sha256:abc/reject" in html
    assert "Approve" not in html  # not the generic approve affordance


# ── proposals-page partition (fleet-ui-reconciliation-v1 C3) ─────────────────
# Artifact-pending types never reach the pending feed's card renderer — they
# partition into the Fleet cross-link card. The verb affordances live on the
# Fleet review surface (C3 component); the disposition ROUTES are unchanged.


def test_artifact_types_partition_out_of_feed():
    from grove.api.fragments import _partition_proposals
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING
    mixed = [
        {"type": FT, "proposal_id": "sha256:a"},
        {"type": PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "proposal_id": "sha256:b"},
        {"type": "routing_threshold", "proposal_id": "sha256:c"},
    ]
    artifact, other = _partition_proposals(mixed)
    assert [p["proposal_id"] for p in artifact] == ["sha256:a", "sha256:b"]
    assert [p["proposal_id"] for p in other] == ["sha256:c"]
