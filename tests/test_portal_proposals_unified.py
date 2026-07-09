"""Sprint P3.1 (portal-reader-contract-fix) — unified proposals review surface.

The portal's proposals panel must read BOTH backing files:

* ``proposals.jsonl``        — routing proposals (RoutingProposal records)
* ``memory_proposals.jsonl`` — memory_context crystallizations staged by the
  detector as ``{session_id, status, timestamp, proposal}`` records.

Before this sprint the portal read only the routing file, so 59 pending
memory crystallizations rendered as "No pending proposals". These tests pin
the dual-read for both the JSON endpoint and the HTMX fragment, and the
graceful (logged, non-crashing) handling of an empty or missing memory file.

Substrate is isolated to a temp GROVE_HOME per test, mirroring the P2 portal
fixtures: a couple of synthetic proposals.jsonl / memory_proposals.jsonl files
written directly so each test controls the exact record set.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.api.fragments import _PORTAL_ASSETS, register_fragment_routes
from grove.eval import proposal_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_routing_proposal(home):
    """One routing proposal in ~/.grove/proposals.jsonl via the real writer."""
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id="routing_update:abc123",
        type="routing_update",
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
    ))


def _memory_record(content, *, status="pending", confidence=0.8, session_id="s1"):
    """A detector-shaped memory_proposals.jsonl record (create action)."""
    return {
        "session_id": session_id,
        "status": status,
        "timestamp": "2026-06-26T01:00:00Z",
        "proposal": {
            "action": "create",
            "proposed_record": {
                "entity_type": "DomainFact",
                "content": content,
                "confidence": confidence,
                "justification": "observed repeatedly",
            },
        },
    }


def _write_memory_proposals(home, records):
    """Write detector-shaped records to ~/.grove/memory_proposals.jsonl."""
    path = home / "memory_proposals.jsonl"
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    app.router.add_static("/portal/static", str(_PORTAL_ASSETS))
    register_fragment_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# JSON endpoint: /api/substrate/proposals/pending
# ---------------------------------------------------------------------------


async def test_json_endpoint_unions_routing_and_memory(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Grove is sovereign.")])

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 2
    types = {item["type"] for item in body["data"]}
    assert types == {"routing_update", "memory_context"}


async def test_json_endpoint_empty_memory_file_returns_only_routing(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [])  # exists but empty

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 1
    assert body["data"][0]["type"] == "routing_update"


async def test_json_endpoint_missing_memory_file_returns_only_routing(client, grove_home):
    _write_routing_proposal(grove_home)
    # No memory_proposals.jsonl written at all.
    assert not (grove_home / "memory_proposals.jsonl").exists()

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    assert body["meta"]["count"] == 1
    assert body["data"][0]["type"] == "routing_update"


async def test_json_endpoint_filters_memory_to_pending(client, grove_home):
    _write_memory_proposals(grove_home, [
        _memory_record("Pending fact.", status="pending"),
        _memory_record("Approved fact.", status="approved"),
        _memory_record("Rejected fact.", status="rejected"),
    ])

    r = await client.get("/api/substrate/proposals/pending")
    assert r.status == 200
    body = await r.json()
    memory_items = [i for i in body["data"] if i["type"] == "memory_context"]
    assert len(memory_items) == 1
    blob = json.dumps(body["data"])
    assert "Pending fact." in blob
    assert "Approved fact." not in blob
    assert "Rejected fact." not in blob


# ---------------------------------------------------------------------------
# HTMX fragment: /portal/fragments/proposals/pending
# ---------------------------------------------------------------------------


async def test_fragment_renders_both_routing_and_memory_cards(client, grove_home):
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Grove runs on sovereignty.")])

    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    assert 'id="proposals-listing"' in body
    # Routing card still rendered.
    assert "routing_update" in body
    # Memory card: type badge + the summary_renderer content.
    assert "memory_context" in body
    assert "Grove runs on sovereignty." in body
    # The empty-state placeholder must NOT appear when proposals exist.
    assert "No pending proposals" not in body


# ---------------------------------------------------------------------------
# fleet-ui-reconciliation-v1 C3 — one review surface: artifact partition
# ---------------------------------------------------------------------------


def _file_artifact_proposal(ptype, slug):
    """One live artifact-pending proposal via the real agentless writer."""
    proposal_queue.file_agentless(
        type=ptype,
        payload={"slug": slug, "unit_id": slug,
                 "skill_id": "skill.fleet.drafter", "canonical_sink": "drafter"},
        evidence=(slug,), justification="t", proposer="skill.fleet.drafter",
    )


async def test_artifact_proposals_partition_into_xlink_card(client, grove_home):
    """Mixed queue: artifact-pending types render as ONE cross-link card (N=2),
    zero artifact cards below; routing + memory cards unchanged."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Pending fact.")])
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, "260709-acme")

    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    # ONE cross-link card with the artifact count, linking into Fleet.
    assert body.count('class="card xlink"') == 1
    assert "2 fleet artifact(s) awaiting review" in body
    assert 'href="/portal#fragments/fleet/"' in body
    # ZERO artifact cards: neither type badge nor Promote affordance renders.
    assert "fleet_artifact_pending" not in body
    assert "forge_artifact_pending" not in body
    assert "/promote" not in body
    # Non-artifact cards unchanged.
    assert "routing_update" in body and "Pending fact." in body


async def test_no_artifact_proposals_no_xlink_card(client, grove_home):
    _write_routing_proposal(grove_home)
    r = await client.get("/portal/fragments/proposals/pending")
    body = await r.text()
    assert 'class="card xlink"' not in body


async def test_grouped_view_also_partitions_artifacts(client, grove_home):
    _write_routing_proposal(grove_home)
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    r = await client.get("/portal/fragments/proposals/pending?view=grouped")
    body = await r.text()
    assert body.count('class="card xlink"') == 1
    assert "fleet_artifact_pending" not in body
    # the artifact proposer never gets a section of its own
    assert 'data-proposer="skill.fleet.drafter"' not in body


async def test_proposals_nav_badge_matches_page_card_count(client, grove_home):
    """F3 — badge N == rendered card N, by the same partition: 1 routing + 1
    memory = 2; the 2 artifact proposals never count here (they count in the
    Fleet badge, C2)."""
    _write_routing_proposal(grove_home)
    _write_memory_proposals(grove_home, [_memory_record("Pending fact.")])
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, "moon-bot")
    _file_artifact_proposal(
        proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, "260709-acme")

    r = await client.get("/portal/fragments/nav/proposals")
    assert r.status == 200
    body = await r.text()
    assert 'href="/portal#fragments/proposals/pending"' in body
    assert '<span class="nav-badge hot">2</span>' in body

    # and the page renders exactly 2 disposition-bearing cards
    page = await (await client.get("/portal/fragments/proposals/pending")).text()
    assert page.count('class="proposal-actions"') == 2
