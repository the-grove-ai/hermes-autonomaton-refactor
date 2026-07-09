"""proposal-proposer-attribution-v1 (Move 2) — proofs.

  (1) DEDUP-PARITY  — proposal_id + eval_hash are byte-identical with vs without a
      proposer, for otherwise-identical content (id-exclusion; no legacy fork).
  (2) PRODUCER STAMPS — every one of the 14 producers stamps its exact proposer.
  (3) LEGACY DESERIALIZE — a record with no proposer key → "unattributed"; renders.
  (4) GROUPED VIEW — groups by proposer, ordered by most-recent; flat feed present.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from grove.eval.proposal_queue import (
    RoutingProposal, compute_proposal_id, file_agentless, file_agentless_proposal,
    read_all,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]


# ── (1) DEDUP-PARITY ──────────────────────────────────────────────────────────
def test_compute_proposal_id_has_no_proposer_param():
    assert "proposer" not in inspect.signature(compute_proposal_id).parameters


def test_dedup_parity_proposer_never_forks_identity(tmp_path):
    q = tmp_path / "q.jsonl"
    kw = dict(type="portal_action_failure",
              payload={"failure_class": "x", "action": "y"}, evidence=("sig",))
    pid1, appended1 = file_agentless(**kw, proposer="portal_failure", path=q)
    # SAME content, DIFFERENT proposer -> SAME id, and the second dedups away.
    pid2, appended2 = file_agentless(**kw, proposer="tier_ratchet", path=q)
    assert pid1 == pid2                      # id-exclusion: proposer not in the seed
    assert appended1 is True and appended2 is False   # dedup still collapses
    # eval_hash is report-derived / passed-in — never proposer-derived.
    a = RoutingProposal(proposal_id=pid1, type="t", payload={}, evidence=(),
                        eval_hash="sha256:z", created_at="2026-07-08", proposer="a")
    b = RoutingProposal(proposal_id=pid1, type="t", payload={}, evidence=(),
                        eval_hash="sha256:z", created_at="2026-07-08", proposer="b")
    assert a.eval_hash == b.eval_hash
    # to_dict: an UNATTRIBUTED proposal omits the key (byte-identical to legacy).
    leg = RoutingProposal(proposal_id="p", type="t", payload={}, evidence=(),
                          eval_hash="", created_at="t")
    assert "proposer" not in leg.to_dict()
    assert a.to_dict()["proposer"] == "a"


# ── (2) PRODUCER STAMPS — each of the 14 stamps its exact value ───────────────
_PRODUCER_STAMPS = {
    "grove/fleet/manager.py": "proposer=skill_id",                 # #1 (dynamic)
    "grove/memory/digest.py": 'proposer="memory_digest"',          # #3
    "grove/kaizen/synthesizer.py": 'proposer="skill_synthesis"',   # #4
    "gateway/run.py": 'proposer="skill_promotion"',                # #6
    "grove/eval/pattern_compiler.py": 'proposer="pattern_compiler"',  # #8
    "grove/eval/tier_ratchet.py": 'proposer="tier_ratchet"',       # #9
    "grove/eval/consolidation_ratchet.py": 'proposer="consolidation_ratchet"',  # #10
    "grove/eval/disposition_promotion.py": 'proposer="disposition_promotion"',  # #11
    "grove/kaizen_promotion.py": 'proposer="kaizen_promotion"',    # #12
    "grove/dock/detector.py": 'proposer="dock_detector"',          # #13
    "grove/eval/proposal_queue.py": 'proposer="portal_failure"',   # #14
}
# dispatcher.py carries three distinct stamps (#2, #5, #7).
_DISPATCHER_STAMPS = ['proposer="governance"', 'proposer="skill_promotion"',
                      'proposer="pattern_demotion"']


def test_every_producer_stamps_its_proposer():
    for rel, literal in _PRODUCER_STAMPS.items():
        src = (_REPO / rel).read_text()
        assert literal in src, f"{rel} is missing its proposer stamp {literal!r}"
    disp = (_REPO / "grove/dispatcher.py").read_text()
    for literal in _DISPATCHER_STAMPS:
        assert literal in disp, f"dispatcher.py missing {literal!r}"


def test_file_agentless_proposal_stamps_portal_failure(tmp_path):
    q = tmp_path / "q.jsonl"
    file_agentless_proposal(failure_class="fc", action="ac", evidence="ev",
                            justification="j", path=q)
    (prop,) = read_all(path=q)
    assert prop.proposer == "portal_failure"


# ── (3) LEGACY DESERIALIZE ────────────────────────────────────────────────────
def test_legacy_record_without_proposer_key_binds_unattributed(tmp_path):
    q = tmp_path / "q.jsonl"
    q.write_text(json.dumps({
        "proposal_id": "sha256:legacy", "type": "routing_update", "payload": {},
        "evidence": [], "eval_hash": "", "created_at": "2026-07-01T00:00:00+00:00",
    }) + "\n")
    (prop,) = read_all(path=q)
    assert prop.proposer == "unattributed"


# ── (4) GROUPED VIEW + flat coexistence ───────────────────────────────────────
class _StubAdapter:
    def __init__(self, k): self._api_key = k


class _StubReq:
    def __init__(self, view=None):
        from grove.red_pending_store import get_red_pending_store
        self.query = {"view": view} if view else {}
        self.app = {"red_pending_store": get_red_pending_store(),
                    "api_server_adapter": _StubAdapter("k")}


@pytest.fixture(autouse=True)
def _grove_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    import grove.red_pending_store as rps
    monkeypatch.setattr(rps, "_STORE", None)
    yield


def _rp(pid, created_at, proposer, ptype="routing_update"):
    return RoutingProposal(proposal_id=pid, type=ptype, payload={}, evidence=(),
                           eval_hash="", created_at=created_at, proposer=proposer)


async def test_grouped_view_groups_ordered_by_most_recent_and_flat_present(monkeypatch):
    from grove.api import fragments
    props = [
        _rp("dock_old", "2026-07-02T00:00:00+00:00", "dock_detector"),
        _rp("tier_new", "2026-07-09T00:00:00+00:00", "tier_ratchet"),   # newest overall
        _rp("dock_mid", "2026-07-03T00:00:00+00:00", "dock_detector"),
        _rp("legacy", "2026-07-01T00:00:00+00:00", "unattributed"),
        _rp("tier_old", "2026-07-05T00:00:00+00:00", "tier_ratchet"),
    ]
    monkeypatch.setattr(fragments, "read_all_proposals", lambda: props)
    monkeypatch.setattr(fragments, "pending_memory_proposal_items", lambda: [])

    # GROUPED
    html = (await fragments.handle_proposals_pending(_StubReq(view="grouped"))).text
    # sections present, one per proposer
    assert 'data-proposer="tier_ratchet"' in html
    assert 'data-proposer="dock_detector"' in html
    assert 'data-proposer="unattributed"' in html
    # groups ordered by MOST-RECENT: tier_ratchet (07-09) before dock_detector (07-03)
    # before unattributed (07-01)
    assert (html.index('data-proposer="tier_ratchet"')
            < html.index('data-proposer="dock_detector"')
            < html.index('data-proposer="unattributed"'))
    # within tier_ratchet group: newest first (tier_new before tier_old)
    assert html.index("proposal-tier_new") < html.index("proposal-tier_old")
    # toggle present, no crash
    assert "view-toggle" in html

    # FLAT (default) still present + NOT grouped into sections
    flat = (await fragments.handle_proposals_pending(_StubReq())).text
    assert "proposer-group" not in flat          # flat feed has no sections
    assert "view-toggle" in flat                  # toggle available in both
    assert flat.index("proposal-tier_new") < flat.index("proposal-tier_old")  # Move-1 sort intact
