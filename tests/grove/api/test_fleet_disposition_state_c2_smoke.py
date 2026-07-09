"""State-matrix smoke for fleet-review-unification-v1 C2 — the four-state artifact
disposition API (/api/substrate/fleet/{skill}/), replacing the two-state topology flag.

Joins filesystem topology (mv-sink authoritative) + the live proposal store + the
per-(worker, unit_id) feedback store + the kaizen_disposition ledger (forge terminal
authoritative). Reconciles on read: out-of-band mv → promoted_out_of_band auto-close.

VERDICT A — forge disposition flow no-drift: reading a needs_review unit does NOT
mutate its proposal; the read-side auto-close only fires on genuine out-of-band drift.
VERDICT B — every state resolves correctly, incl. the forge-promoted-via-ledger row.

Local: GROVE_HOME → tmp_path; no network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _drafter_cap():
    from grove.capability_registry import load_capabilities
    return load_capabilities()["skill.fleet.drafter"]


def _forge_cap():
    from grove.capability_registry import load_capabilities
    return load_capabilities()["skill.fleet.forge-jobsearch"]


def _stage(home, sink, unit, content_name, meta):
    d = home / sink / "pending_review" / unit
    d.mkdir(parents=True, exist_ok=True)
    (d / content_name).write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _canonical(home, sink, filename):
    (home / sink).mkdir(parents=True, exist_ok=True)
    p = home / sink / filename
    p.write_text("---\ntitle: X\n---\nbody", encoding="utf-8")
    return p


def _file_proposal(ptype, payload, unit_id):
    from grove.eval import proposal_queue
    pid, _ = proposal_queue.file_agentless(
        type=ptype, payload=payload, evidence=(unit_id,),
        justification="test", proposer=payload["skill_id"],
    )
    return pid


def _ledger_event(home, proposal_type, disposition, unit_id):
    d = home / ".kaizen_ledger"
    d.mkdir(parents=True, exist_ok=True)
    ev = {"event_type": "kaizen_disposition", "proposal_type": proposal_type,
          "disposition": disposition, "applied_result": {"unit_id": unit_id}}
    (d / "sess.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")


def _by_unit(rows):
    return {r["unit_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# VERDICT B — the state matrix
# ---------------------------------------------------------------------------


def test_needs_review_staged_with_open_proposal(grove_home):
    from grove.api import portal
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T
    _stage(grove_home, "drafter", "moon-bot", "draft-moon-bot.md",
           {"unit_id": "moon-bot", "slug": "moon-bot"})
    pid = _file_proposal(T, {"slug": "moon-bot", "unit_id": "moon-bot",
                             "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
                         "moon-bot")
    rows = portal._list_fleet_units(_drafter_cap())
    r = _by_unit(rows)["moon-bot"]
    assert r["governance_state"] == "needs_review"
    assert r["proposal_id"] == pid and r["proposal_type"] == T
    assert r["producer"] == "drafter" and r["revision_count"] == 0


def test_promoted_canonical_present(grove_home):
    from grove.api import portal
    _canonical(grove_home, "drafter", "draft-moon-bot.md")
    rows = portal._list_fleet_units(_drafter_cap())
    r = _by_unit(rows)["moon-bot"]
    assert r["governance_state"] == "promoted" and r["filename"] == "draft-moon-bot.md"


def test_promoted_out_of_band_autocloses_open_proposal(grove_home):
    """Topological supremacy: staged draft + open proposal BUT the artifact already
    sits in canonical (out-of-band mv) → promoted + the proposal is auto-closed."""
    from grove.api import portal
    from grove.eval import proposal_queue
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T
    _stage(grove_home, "drafter", "moon-bot", "draft-moon-bot.md",
           {"unit_id": "moon-bot", "slug": "moon-bot"})
    _canonical(grove_home, "drafter", "draft-moon-bot.md")
    pid = _file_proposal(T, {"slug": "moon-bot", "unit_id": "moon-bot",
                             "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
                         "moon-bot")
    assert proposal_queue.read(pid) is not None  # open before read
    rows = portal._list_fleet_units(_drafter_cap())
    assert _by_unit(rows)["moon-bot"]["governance_state"] == "promoted"
    assert proposal_queue.read(pid) is None  # auto-closed promoted_out_of_band


def test_revision_requested_feedback_pending_no_draft(grove_home):
    """suggest_revision recorded (feedback non-terminal), draft archived → the redraft
    window: revision_requested + revision_count + directive echo, no artifact file."""
    from grove.api import portal
    from grove.forge import feedback_store
    feedback_store.write("drafter", "moon-bot", "tighten the open")
    rows = portal._list_fleet_units(_drafter_cap())
    r = _by_unit(rows)["moon-bot"]
    assert r["governance_state"] == "revision_requested"
    assert r["revision_count"] == 1
    assert r["directive_echo"] == "tighten the open"
    assert "filename" not in r  # archived — no current artifact


def test_rejected_wont_converge_terminal_skip(grove_home):
    from grove.api import portal
    from grove.forge import feedback_store
    feedback_store.write("drafter", "dead-unit", "g")
    feedback_store.set_terminal_skip("drafter", "dead-unit")
    r = _by_unit(portal._list_fleet_units(_drafter_cap()))["dead-unit"]
    assert r["governance_state"] == "rejected"


def test_grandfathered_staged_no_proposal_is_legacy(grove_home):
    from grove.api import portal
    _stage(grove_home, "drafter", "legacy-unit", "draft-legacy-unit.md",
           {"unit_id": "legacy-unit", "slug": "legacy-unit"})
    r = _by_unit(portal._list_fleet_units(_drafter_cap()))["legacy-unit"]
    assert r["governance_state"] == "legacy"


def test_forge_promoted_via_ledger_not_legacy(grove_home):
    """Sink-authority rule: forge's staged dir LINGERS post-publish (no local
    canonical). With no open proposal, the ledger 'applied' event makes it promoted —
    NOT misclassified as legacy."""
    from grove.api import portal
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as T
    _stage(grove_home, "forge", "260707-sirion", "resume.md",
           {"row_id": "ROW-SIR", "slug": "260707-sirion", "company": "S", "role": "R"})
    _ledger_event(grove_home, T, "applied", "ROW-SIR")  # unit_id == row_id
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-SIR"]
    assert r["governance_state"] == "promoted"


def test_forge_lingering_staged_no_ledger_is_legacy(grove_home):
    """Contrast: forge staged dir, no proposal, NO ledger terminal → genuinely
    grandfathered/orphan → legacy (the ledger is what distinguishes promoted)."""
    from grove.api import portal
    _stage(grove_home, "forge", "260707-orphan", "resume.md",
           {"row_id": "ROW-ORPH", "slug": "260707-orphan", "company": "S", "role": "R"})
    r = _by_unit(portal._list_fleet_units(_forge_cap()))["ROW-ORPH"]
    assert r["governance_state"] == "legacy"


def test_empty_producer_zero_count(grove_home):
    from grove.api import portal
    assert portal._list_fleet_units(_drafter_cap()) == []


def test_index_aggregates_needs_review(grove_home):
    from grove.api import portal
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING as T
    _stage(grove_home, "drafter", "u1", "draft-u1.md", {"unit_id": "u1", "slug": "u1"})
    _file_proposal(T, {"slug": "u1", "unit_id": "u1", "skill_id": "skill.fleet.drafter",
                       "canonical_sink": "drafter"}, "u1")
    _stage(grove_home, "drafter", "u2", "draft-u2.md", {"unit_id": "u2", "slug": "u2"})  # legacy
    counts = {}
    import asyncio
    from types import SimpleNamespace
    resp = asyncio.run(portal.handle_fleet_index(SimpleNamespace(match_info={})))
    body = json.loads(resp.text)
    drafter = next(s for s in body["data"]["skills"] if s["name"] == "drafter")
    assert drafter["needs_review_count"] == 1
    assert drafter["state_counts"].get("legacy") == 1


# ---------------------------------------------------------------------------
# VERDICT A — forge disposition-flow no-drift
# ---------------------------------------------------------------------------


def test_read_does_not_mutate_open_proposal(grove_home):
    """A read of a needs_review unit (no out-of-band drift) leaves the proposal OPEN —
    the read-side auto-close fires ONLY on genuine topological drift, never on the
    normal needs_review path."""
    from grove.api import portal
    from grove.eval import proposal_queue
    from grove.eval.proposal_queue import PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING as T
    _stage(grove_home, "forge", "260707-x", "resume.md",
           {"row_id": "ROW-X", "slug": "260707-x", "company": "S", "role": "R"})
    pid = _file_proposal(T, {"slug": "260707-x", "row_id": "ROW-X",
                             "skill_id": "skill.fleet.forge-jobsearch"}, "ROW-X")
    portal._list_fleet_units(_forge_cap())
    portal._list_fleet_units(_forge_cap())  # idempotent — twice
    assert proposal_queue.read(pid) is not None  # still OPEN — no drift, no mutation


def test_forge_promote_reject_disposition_outcome_unchanged(grove_home, monkeypatch):
    """VERDICT A — the applied_result enrichment (unit_id/slug) is additive LEDGER
    telemetry: the forge disposition OUTCOME (proposal popped, status, reason) is
    byte-identical. Capture the finalize call and assert the load-bearing args are
    unchanged, with the identity fields added to applied_result only."""
    from grove.api import actions
    from grove.eval import proposal_queue
    captured = {}
    monkeypatch.setattr(proposal_queue, "finalize_proposal_state",
                        lambda pid, status, ar=None, **k: captured.update(
                            {"pid": pid, "status": status, "ar": ar, "kw": k}) or True)
    monkeypatch.setattr(actions, "_archive_forge_slug", lambda p: "/home/x/forge/.archive/260707-x-TS")
    from types import SimpleNamespace
    import asyncio
    # reject path (via _apply_routing)
    proposal = SimpleNamespace(type=proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
                               proposal_id="sha256:abc",
                               payload={"slug": "260707-x", "row_id": "ROW-X"},
                               to_dict=lambda: {"semantic_justification": ""},
                               source_patterns=[])
    monkeypatch.setattr(actions, "broadcast_to_operator", lambda m: _async_none())
    asyncio.run(actions._apply_routing(proposal, "reject", "sha256:abc", "abc", "why"))
    assert captured["status"] == "rejected" and captured["pid"] == "sha256:abc"
    assert captured["kw"].get("reason") == "why"  # disposition semantics unchanged
    assert captured["ar"]["unit_id"] == "ROW-X" and captured["ar"]["archive_path"]  # additive


async def _async_none():
    return None
