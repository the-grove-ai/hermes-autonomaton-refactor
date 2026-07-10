"""promoted-artifact-persistence-v1 P2 S2+S3 — four-state read + preview.

Pins the TWO ruled deltas as intentional (phantom removal, crash-window heal),
the ordering inversion (hold-open reads needs_review), the grandfather
invariants (ledger rule 2 untouched for pre-persistence terminals), and the
canonical-side preview.

Local: GROVE_HOME → tmp_path (the C2 smoke discipline); real capability
records; no network, no model.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _forge_cap():
    from grove.capability_registry import load_capabilities
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def _units(cap):
    from grove.api.portal import _list_fleet_units
    return {r["unit_id"]: r for r in _list_fleet_units(cap)}


def _stage_meta_only(home, slug, meta):
    d = home / "forge" / "pending_review" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _canonical_pkg(home, slug):
    d = home / "forge" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.md").write_text("canonical resume body", encoding="utf-8")
    (d / "cover-letter.md").write_text("canonical cover body", encoding="utf-8")
    return d


def _proposal(slug, unit_id):
    from grove.eval import proposal_queue
    pid, _ = proposal_queue.file_agentless(
        type=proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
        payload={"slug": slug, "row_id": unit_id,
                 "skill_id": "skill.fleet.forge-jobsearch"},
        evidence=(unit_id,), justification="test",
        proposer="skill.fleet.forge-jobsearch",
    )
    return pid


def _ledger_event(home, disposition, unit_id, slug=None, line_suffix="a"):
    d = home / ".kaizen_ledger"
    d.mkdir(parents=True, exist_ok=True)
    ar = {"unit_id": unit_id}
    if slug:
        ar["slug"] = slug
    ev = {"event_type": "kaizen_disposition",
          "proposal_type": "forge_artifact_pending",
          "disposition": disposition, "applied_result": ar}
    with (d / f"sess-{line_suffix}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


# ── S2: the ruled deltas ─────────────────────────────────────────────────


def test_holdopen_meta_staging_plus_canonical_is_needs_review(grove_home):
    """ORDERING INVERSION (Verdict B): staged meta + canonical subdir + open
    proposal = the hold-open window → needs_review; NO out-of-band autoclose."""
    slug, uid = "260101-acme-pm", "row-1"
    _stage_meta_only(grove_home, slug, {"row_id": uid, "slug": slug,
                                        "company": "Acme", "role": "PM"})
    _canonical_pkg(grove_home, slug)
    pid = _proposal(slug, uid)

    rows = _units(_forge_cap())
    assert rows[uid]["governance_state"] == "needs_review"
    from grove.eval import proposal_queue
    assert proposal_queue.read(pid) is not None  # NOT auto-closed


def test_crash_window_heals_to_promoted_out_of_band(grove_home):
    """RULED DELTA 2: open proposal + canonical subdir + NO staged dir =
    delivered-but-unfinalized (P1 archives only after publish success) →
    promoted, proposal auto-closed promoted_out_of_band."""
    slug, uid = "260101-acme-pm", "row-1"
    _canonical_pkg(grove_home, slug)
    pid = _proposal(slug, uid)

    rows = _units(_forge_cap())
    assert rows[uid]["governance_state"] == "promoted"
    assert rows[uid]["filename"] == slug  # the canonical subdir joins the row
    from grove.eval import proposal_queue
    assert proposal_queue.read(pid) is None  # healed: auto-closed to terminal


def test_post_p1_promoted_reads_from_subdir_with_ledger_join(grove_home):
    """Normal post-P1 terminal: ledger applied (slug map) + canonical subdir →
    promoted keyed on unit_id, filename = slug dir (the detail-view join)."""
    slug, uid = "260101-acme-pm", "row-1"
    _canonical_pkg(grove_home, slug)
    _ledger_event(grove_home, "applied", uid, slug=slug)

    rows = _units(_forge_cap())
    assert rows[uid]["governance_state"] == "promoted"
    assert rows[uid]["filename"] == slug


def test_flat_file_in_remote_sink_is_not_a_unit(grove_home):
    """RULED DELTA 1 (phantom correction): remote-sink enumeration is
    subdir-only — a flat file (career-corpus.md shape) produces NO unit."""
    (grove_home / "forge").mkdir(parents=True)
    (grove_home / "forge" / "career-corpus.md").write_text("corpus")

    assert _units(_forge_cap()) == {}


def test_pre_persistence_terminals_unchanged(grove_home):
    """GRANDFATHER: pre-P1 terminals (no canonical subdir) resolve via ledger
    rule 2 exactly as before — promoted with lingering staged, rejected, and
    legacy (staged, no proposal) all unchanged."""
    # pre-P1 promoted: ledger applied, staged dir LINGERS (pre-P1 shape)
    _stage_meta_only(grove_home, "260101-old-promoted",
                     {"row_id": "row-old", "slug": "260101-old-promoted"})
    (grove_home / "forge" / "pending_review" / "260101-old-promoted"
     / "resume.md").write_text("old")
    _ledger_event(grove_home, "applied", "row-old", line_suffix="b")
    # pre-P1 rejected (ledger only)
    _ledger_event(grove_home, "rejected", "row-rej", line_suffix="c")
    # legacy: staged content, no proposal, no ledger
    d = grove_home / "forge" / "pending_review" / "260101-legacy"
    d.mkdir(parents=True)
    (d / "resume.md").write_text("legacy draft")
    (d / "meta.json").write_text(json.dumps({"row_id": "row-leg"}))

    rows = _units(_forge_cap())
    assert rows["row-old"]["governance_state"] == "promoted"
    assert rows["row-rej"]["governance_state"] == "rejected"
    assert rows["row-leg"]["governance_state"] == "legacy"


def test_rejected_with_orphaned_canonical_subdir_stays_rejected(grove_home):
    """A reject during the hold-open window orphans the canonical subdir (P1
    residual 3): the ledger's reject terminal outranks the undelivered local
    copy — never promoted."""
    slug, uid = "260101-acme-pm", "row-1"
    _canonical_pkg(grove_home, slug)
    _ledger_event(grove_home, "rejected", uid, slug=slug)

    rows = _units(_forge_cap())
    assert rows[uid]["governance_state"] == "rejected"


def test_subdir_without_ledger_or_proposal_uses_slug_as_uid(grove_home):
    slug = "260101-orphan-unit"
    _canonical_pkg(grove_home, slug)
    rows = _units(_forge_cap())
    assert rows[slug]["governance_state"] == "promoted"


def test_empty_canonical_subdir_is_not_a_unit(grove_home):
    (grove_home / "forge" / "260101-empty").mkdir(parents=True)
    assert _units(_forge_cap()) == {}


# ── S3: preview (canonical-side content) ─────────────────────────────────


def _render(unit_name, pid=None):
    from grove.api.fragments import _render_unit_detail
    resp = _render_unit_detail(_forge_cap(), "forge-jobsearch", unit_name, pid)
    return resp.status, resp.text


def test_holdopen_detail_renders_canonical_content(grove_home):
    """Verdict B second half: during the hold-open window (staged dir is
    meta-only) the detail view renders the CANONICAL content files."""
    slug, uid = "260101-acme-pm", "row-1"
    _stage_meta_only(grove_home, slug, {"row_id": uid, "slug": slug,
                                        "company": "Acme", "role": "PM"})
    _canonical_pkg(grove_home, slug)
    _proposal(slug, uid)

    status, body = _render(slug)
    assert status == 200
    assert "canonical resume body" in body
    assert "canonical cover body" in body
    assert "Acme" in body  # identity still staged-side (meta.json)


def test_promoted_detail_renders_full_text(grove_home):
    """A promoted forge unit's full text is readable from the portal —
    declaration-driven rendering over the canonical subdir, no new render
    code. The publish affordance is absent (nothing left to publish)."""
    slug, uid = "260101-acme-pm", "row-1"
    _canonical_pkg(grove_home, slug)
    _ledger_event(grove_home, "applied", uid, slug=slug)

    status, body = _render(slug)
    assert status == 200
    assert "canonical resume body" in body
    assert "canonical cover body" in body
    assert "forge-publish" not in body  # no affordance, no 'unavailable' noise


def test_staged_package_still_renders_staging_side(grove_home):
    """Pre-promote drafts render staging-side exactly as before (set-diff)."""
    slug, uid = "260101-acme-pm", "row-1"
    d = _stage_meta_only(grove_home, slug, {"row_id": uid, "slug": slug,
                                            "company": "Acme", "role": "PM"})
    (d / "resume.md").write_text("staged resume body", encoding="utf-8")
    _proposal(slug, uid)

    status, body = _render(slug)
    assert status == 200
    assert "staged resume body" in body
