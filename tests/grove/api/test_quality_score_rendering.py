"""drafter-quality-checks-v1 P4 — score rendering pins.

The event fields remain the canonical channel; P4 surfaces them on the two
operator-facing reads:

* SUMMARY — quality_score interpolation into the fleet_artifact_pending
  one-liner (the fit_score idiom), including the skipped_oversize annotation;
  an ungated payload renders byte-identically to pre-P4.
* CHIP — the unit detail header's score chip (the rev-chip idiom), fed by the
  manager → proposal payload → unit dict threading.
* RIDER — the manager reads the four quality fields OFF the event into the
  fleet_artifact_pending payload; the portal unit join carries them onto the
  needs_review unit.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from grove.api import fragments as F
from grove.kaizen.rendering import _summary_fleet_artifact_pending

_BASE_PAYLOAD = {
    "slug": "moon-bot",
    "unit_id": "moon-bot",
    "skill_id": "skill.fleet.drafter",
    "canonical_sink": "drafter",
}


def _proposal(payload):
    return SimpleNamespace(payload=payload, semantic_justification="")


# ── SUMMARY interpolation (fit_score idiom) ──────────────────────────────────


def test_summary_interpolates_score():
    pl = dict(_BASE_PAYLOAD, quality_score=0.85, rubric_version="1.0",
              redraft_count=0, evaluator_model="m/x")
    s = _summary_fleet_artifact_pending(_proposal(pl))
    assert s == "fleet draft staged for review: moon-bot (unit moon-bot) (quality 0.85)"


def test_summary_annotates_skipped_oversize():
    pl = dict(_BASE_PAYLOAD, quality_score=None, rubric_version="1.0",
              redraft_count=0, evaluator_model=None)
    s = _summary_fleet_artifact_pending(_proposal(pl))
    assert s == (
        "fleet draft staged for review: moon-bot (unit moon-bot) "
        "(quality skipped: oversize)"
    )


def test_summary_ungated_byte_identical():
    """An ungated payload (null rider or pre-P4 shape) renders the pre-P4 line."""
    expected = "fleet draft staged for review: moon-bot (unit moon-bot)"
    nulled = dict(_BASE_PAYLOAD, quality_score=None, rubric_version=None,
                  redraft_count=None, evaluator_model=None)
    assert _summary_fleet_artifact_pending(_proposal(nulled)) == expected
    assert _summary_fleet_artifact_pending(_proposal(dict(_BASE_PAYLOAD))) == expected


# ── CHIP (rev-chip idiom) ────────────────────────────────────────────────────


def test_chip_renders_score():
    html = F._quality_chip({"quality_score": 0.85})
    assert html == ' <span class="state-chip">quality 0.85</span>'


def test_chip_annotates_skipped():
    html = F._quality_chip({"quality_score": None, "quality_rubric_version": "1.0"})
    assert "quality skipped (oversize)" in html


def test_chip_empty_for_ungated_unit():
    assert F._quality_chip({}) == ""
    assert F._quality_chip({"quality_score": None}) == ""


def test_chip_escapes_malformed_score():
    """A read-side view must not 500 (or inject) on one malformed payload value."""
    html = F._quality_chip({"quality_score": "<b>0.9</b>"})
    assert "<b>" not in html and "&lt;b&gt;" in html


# ── RIDER: manager event → payload; portal payload → unit dict ──────────────


def test_manager_threads_quality_fields_off_event(monkeypatch):
    from grove.eval import proposal_queue
    from grove.fleet.manager import FleetManager

    captured = {}
    monkeypatch.setattr(
        proposal_queue, "file_agentless",
        lambda **kw: (captured.update(kw), ("sha256:q", True))[1],
    )
    event = {
        "skill": "skill.fleet.drafter", "status": "success",
        "slug": "moon-bot", "unit_id": "moon-bot",
        "quality_score": 0.85, "rubric_version": "1.0",
        "redraft_count": 1, "evaluator_model": "prov/model-x",
    }
    # fleet-receipt-custody-v1 P4b-1 — the card LOGIC lives in _emit_artifact_card
    # (emission is now driven by the per-tick state scan; the payload is unchanged).
    FleetManager()._emit_artifact_card("drafter", "run1", event)
    pl = captured["payload"]
    assert pl["quality_score"] == 0.85
    assert pl["rubric_version"] == "1.0"
    assert pl["redraft_count"] == 1
    assert pl["evaluator_model"] == "prov/model-x"


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def test_unit_dict_carries_quality_from_open_proposal(grove_home):
    """End-to-end join (the C2 smoke harness shape): staged unit + open
    fleet_artifact_pending proposal WITH the quality rider → the needs_review
    unit dict carries the chip fields."""
    from grove.api import portal
    from grove.capability_registry import load_capabilities
    from grove.eval import proposal_queue
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T

    d = grove_home / "drafter" / "pending_review" / "moon-bot"
    d.mkdir(parents=True)
    (d / "draft-moon-bot.md").write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"unit_id": "moon-bot", "slug": "moon-bot"}), encoding="utf-8"
    )
    proposal_queue.file_agentless(
        type=T,
        payload=dict(_BASE_PAYLOAD, quality_score=0.72, rubric_version="1.0",
                     redraft_count=0, evaluator_model="m/x"),
        evidence=("moon-bot",),
        justification="test",
        proposer="skill.fleet.drafter",
    )
    cap = load_capabilities()["skill.fleet.drafter"]
    rows = portal._list_fleet_units(cap)
    r = {row["unit_id"]: row for row in rows}["moon-bot"]
    assert r["governance_state"] == "needs_review"
    assert r["quality_score"] == 0.72
    assert r["quality_rubric_version"] == "1.0"
    # and the chip renders from exactly this dict:
    assert "quality 0.72" in F._quality_chip(r)


def test_package_unit_detail_header_carries_chip(grove_home):
    """P5c — the C2 PACKAGE unit detail header renders the score chip (the
    placement gap the first live gated unit exposed: P4's chip sat on the C4
    flat-artifact header only)."""
    from grove.api import fragments as frag
    from grove.capability_registry import load_capabilities
    from grove.eval import proposal_queue
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T

    d = grove_home / "drafter" / "pending_review" / "moon-bot"
    d.mkdir(parents=True)
    (d / "draft-moon-bot.md").write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"unit_id": "moon-bot", "slug": "moon-bot"}), encoding="utf-8"
    )
    proposal_queue.file_agentless(
        type=T,
        payload=dict(_BASE_PAYLOAD, quality_score=0.72, rubric_version="1.0",
                     redraft_count=0, evaluator_model="m/x"),
        evidence=("moon-bot",),
        justification="test",
        proposer="skill.fleet.drafter",
    )
    cap = load_capabilities()["skill.fleet.drafter"]
    resp = frag._render_unit_detail(cap, "drafter", "moon-bot", None)
    assert resp.status == 200
    assert "quality 0.72" in resp.text


def test_unit_dict_omits_quality_when_ungated(grove_home):
    from grove.api import portal
    from grove.capability_registry import load_capabilities
    from grove.eval import proposal_queue
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T

    d = grove_home / "drafter" / "pending_review" / "moon-bot"
    d.mkdir(parents=True)
    (d / "draft-moon-bot.md").write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"unit_id": "moon-bot", "slug": "moon-bot"}), encoding="utf-8"
    )
    proposal_queue.file_agentless(
        type=T,
        payload=dict(_BASE_PAYLOAD, quality_score=None, rubric_version=None,
                     redraft_count=None, evaluator_model=None),
        evidence=("moon-bot",),
        justification="test",
        proposer="skill.fleet.drafter",
    )
    cap = load_capabilities()["skill.fleet.drafter"]
    rows = portal._list_fleet_units(cap)
    r = {row["unit_id"]: row for row in rows}["moon-bot"]
    assert "quality_score" not in r
    assert F._quality_chip(r) == ""
