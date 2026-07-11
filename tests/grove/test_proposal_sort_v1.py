"""proposal-sort-v1 — render-only newest-first sort of the portal proposals feed.

Given proposals with distinct created_at in scrambled append order, the rendered
feed lists them newest-first; a proposal with a missing created_at lands last; the
RED bespoke card and generic cards both still render.
"""
from __future__ import annotations

import pytest

from grove.api import fragments
from grove.eval.proposal_queue import RoutingProposal
from grove.red_pending_store import RED_PENDING_PROPOSAL_TYPE, get_red_pending_store


def _rp(pid, created_at, ptype="routing_update"):
    # proposal-card-legibility-v1 follow-up: routing_update renders through the
    # approvable diff renderer, which requires a VALID routing rule — an empty
    # payload raised (Unknown routing rule: None) and rendered a DEFECT card
    # without the created_at meta this file asserts on.
    return RoutingProposal(
        proposal_id=pid, type=ptype,
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=(), eval_hash="e",
        created_at=created_at,
    )


class _StubAdapter:
    def __init__(self, k):
        self._api_key = k


class _StubReq:
    def __init__(self):
        self.query = {}  # proposal-proposer-attribution-v1 Move 2b reads ?view
        self.app = {
            "red_pending_store": get_red_pending_store(),
            "api_server_adapter": _StubAdapter("k"),
        }


@pytest.fixture(autouse=True)
def _grove_home(tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    yield


async def test_proposals_render_newest_first_missing_last(monkeypatch):
    red_pid = f"{RED_PENDING_PROPOSAL_TYPE}:redbare0001"
    # SCRAMBLED append order — the render must reorder to newest-first.
    props = [
        _rp("gen_old", "2026-07-01T00:00:00+00:00"),
        _rp("gen_none", ""),  # missing created_at -> must land LAST
        _rp("gen_new", "2026-07-08T00:00:00+00:00"),
        _rp(red_pid, "2026-07-09T00:00:00+00:00", RED_PENDING_PROPOSAL_TYPE),  # newest + RED
        _rp("gen_mid", "2026-07-04T00:00:00+00:00"),
    ]
    monkeypatch.setattr(fragments, "read_all_proposals", lambda: props)
    monkeypatch.setattr(fragments, "pending_memory_proposal_items", lambda: [])

    resp = await fragments.handle_proposals_pending(_StubReq())
    html = resp.text

    def _pos(pid):
        marker = f"proposal-{fragments._short_id(pid)}"
        assert marker in html, f"card missing for {pid}: {marker}"
        return html.index(marker)

    expected_newest_first = [red_pid, "gen_new", "gen_mid", "gen_old", "gen_none"]
    positions = [_pos(pid) for pid in expected_newest_first]
    assert positions == sorted(positions), (
        f"feed not newest-first: {list(zip(expected_newest_first, positions))}"
    )

    # the missing-created_at proposal is LAST
    assert _pos("gen_none") == max(positions)

    # RED bespoke card still renders (no store payload -> EXPIRED branch), and a
    # generic card still renders with its created_at line.
    assert f"proposal-{fragments._short_id(red_pid)}" in html
    assert "expired" in html.lower()
    assert "2026-07-08T00:00:00+00:00" in html


def test_sort_step_missing_created_at_sorts_last():
    # Unit-level: the exact sort key/reverse used in the handler.
    proposals = [
        {"proposal_id": "a", "created_at": "2026-07-01T00:00:00+00:00"},
        {"proposal_id": "b", "created_at": ""},
        {"proposal_id": "c", "created_at": "2026-07-09T00:00:00+00:00"},
        {"proposal_id": "d"},  # no created_at key at all
    ]
    proposals.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    ids = [p["proposal_id"] for p in proposals]
    assert ids[0] == "c"  # newest first
    assert set(ids[-2:]) == {"b", "d"}  # both missing/empty sink to the bottom
