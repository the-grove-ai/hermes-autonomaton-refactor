"""Sprint P4 (portal-action-surface-v1) — portal write endpoints.

The portal becomes interactive: the operator approves/rejects/dismisses
proposals and toggles Dock goal status through the SAME apply logic the CLI and
conversation surfaces use. These tests pin:

* routing proposal approve — apply_callback called, proposal removed, disposition
  recorded "applied";
* routing proposal reject  — removed, disposition "rejected";
* memory proposal approve  — MemoryProposalHandler.apply called, record status
  flipped to "approved", file rewritten;
* memory proposal dismiss  — status flipped to "dismissed", NO disposition;
* unknown proposal id       — 404;
* dock goal status update   — dock.yaml rewritten, comments preserved;
* dock goal invalid status  — 400.

Substrate is isolated to a temp GROVE_HOME per test (mirrors the P1/P3.1
fixtures): synthetic proposals.jsonl / memory_proposals.jsonl / dock.yaml so
each test controls the exact record set.
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
from grove.api import actions as actions_mod
from grove.api.actions import register_action_routes
from grove.api.fragments import _PORTAL_ASSETS, register_fragment_routes
from grove.eval import proposal_queue
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_MEMORY_CONTEXT,
    compute_proposal_id,
)
from grove.memory import digest


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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
    register_action_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


def _append_routing(proposal_id, *, ptype="routing_adjustment", source_patterns=("cluster_1",)):
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=proposal_id,
        type=ptype,
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
        source_patterns=source_patterns,
        semantic_justification="cluster recurs across sessions",
    ))


def _memory_record(content, *, status="pending", session_id="s1"):
    return {
        "session_id": session_id,
        "status": status,
        "timestamp": "2026-06-26T01:00:00Z",
        "proposal": {
            "action": "create",
            "proposed_record": {
                "entity_type": "DomainFact",
                "content": content,
                "confidence": 0.8,
                "justification": "observed repeatedly",
            },
        },
    }


def _write_memory(home, records):
    (home / "memory_proposals.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


def _memory_pid(record):
    proposal = record["proposal"]
    session_id = record.get("session_id", "")
    evidence = (session_id,) if session_id else ()
    return compute_proposal_id(
        type=PROPOSAL_TYPE_MEMORY_CONTEXT, payload=proposal, evidence=evidence
    )


def _read_memory(home):
    lines = (home / "memory_proposals.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


_DOCK_YAML = """\
# Operator Dock — sovereign, hand-authored. Do not let a writer reflow this.
version: "1.0"
context_char_budget: 5000
goals:
  # The flagship goal — comment must survive a status toggle.
  - id: grove-foundation
    name: Grove Foundation
    vector: strategic
    status: accelerating   # currently pushing hard
    definition_of_done: Ship the reference implementation.
    context_sources: []
    keywords: [grove, foundation]
    unlocked_skills: []
"""


def _write_dock(home):
    (home / "dock").mkdir(parents=True, exist_ok=True)
    (home / "dock" / "dock.yaml").write_text(_DOCK_YAML, encoding="utf-8")


class _SpyHandler:
    """Stand-in ProposalHandler — records the apply_callback invocation."""

    apply_label_prefix = "routing → "
    requires_source_patterns = True
    strict_gate = None

    def __init__(self):
        self.called_with = None

    def apply_callback(self, proposal, *, machine_path):
        self.called_with = (proposal, machine_path)
        return ("downward", {"merged": True})


# ---------------------------------------------------------------------------
# 1. Routing proposal approve
# ---------------------------------------------------------------------------


async def test_routing_approve(client, grove_home, monkeypatch):
    _append_routing("routing_adjustment:abc123")
    spy = _SpyHandler()
    dispositions = []
    monkeypatch.setattr(actions_mod, "_handler_for", lambda t: spy)
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition",
        lambda p, **kw: dispositions.append(kw),
    )

    r = await client.post("/portal/actions/proposals/routing_adjustment:abc123/approve")
    assert r.status == 200
    body = await r.text()
    assert "approved" in body

    # apply_callback was called…
    assert spy.called_with is not None
    # …the proposal was removed from the queue…
    assert proposal_queue.read("routing_adjustment:abc123") is None
    # …and an "applied" disposition was recorded.
    assert dispositions and dispositions[0]["disposition"] == "applied"


# ---------------------------------------------------------------------------
# 2. Routing proposal reject
# ---------------------------------------------------------------------------


async def test_routing_reject(client, grove_home, monkeypatch):
    _append_routing("routing_adjustment:def456")
    dispositions = []
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition",
        lambda p, **kw: dispositions.append(kw),
    )

    r = await client.post("/portal/actions/proposals/routing_adjustment:def456/reject")
    assert r.status == 200
    assert "rejected" in (await r.text())
    assert proposal_queue.read("routing_adjustment:def456") is None
    assert dispositions and dispositions[0]["disposition"] == "rejected"


# ---------------------------------------------------------------------------
# 3. Memory proposal approve
# ---------------------------------------------------------------------------


async def test_memory_approve(client, grove_home, monkeypatch):
    rec = _memory_record("Grove is sovereign.")
    _write_memory(grove_home, [rec])
    pid = _memory_pid(rec)

    applied = []
    monkeypatch.setattr(
        digest.MemoryProposalHandler, "apply",
        lambda self, proposal: applied.append(proposal) or True,
    )
    dispositions = []
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition",
        lambda p, **kw: dispositions.append(kw),
    )

    r = await client.post(f"/portal/actions/proposals/{pid}/approve")
    assert r.status == 200
    assert "approved" in (await r.text())

    assert applied, "MemoryProposalHandler.apply was not called"
    records = _read_memory(grove_home)
    assert records[0]["status"] == "approved"
    assert dispositions and dispositions[0]["disposition"] == "applied"


# ---------------------------------------------------------------------------
# 4. Memory proposal dismiss — soft, NO disposition
# ---------------------------------------------------------------------------


async def test_memory_dismiss_records_no_disposition(client, grove_home, monkeypatch):
    rec = _memory_record("Tentative observation.")
    _write_memory(grove_home, [rec])
    pid = _memory_pid(rec)

    dispositions = []
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition",
        lambda p, **kw: dispositions.append(kw),
    )

    r = await client.post(f"/portal/actions/proposals/{pid}/dismiss")
    assert r.status == 200
    assert "dismissed" in (await r.text())

    records = _read_memory(grove_home)
    assert records[0]["status"] == "dismissed"
    assert dispositions == [], "dismiss must NOT record a kaizen disposition"


# ---------------------------------------------------------------------------
# 5. Proposal not found
# ---------------------------------------------------------------------------


async def test_proposal_not_found(client, grove_home):
    r = await client.post("/portal/actions/proposals/sha256:doesnotexist/approve")
    assert r.status == 404
    assert "not found" in (await r.text())


# ---------------------------------------------------------------------------
# 6. Dock goal status update — rewrite + comment preservation
# ---------------------------------------------------------------------------


async def test_dock_goal_status_update(client, grove_home):
    _write_dock(grove_home)

    r = await client.patch(
        "/portal/actions/dock/goals/grove-foundation", data={"status": "paused"}
    )
    assert r.status == 200
    body = await r.text()
    assert 'id="goal-grove-foundation"' in body
    assert "paused" in body

    raw = (grove_home / "dock" / "dock.yaml").read_text(encoding="utf-8")
    assert "status: paused" in raw
    # Comment preservation is mandatory — the sovereign file's comments survive.
    assert "# Operator Dock — sovereign" in raw
    assert "# The flagship goal" in raw
    assert "# currently pushing hard" in raw


# ---------------------------------------------------------------------------
# 7. Dock goal invalid status — 400
# ---------------------------------------------------------------------------


async def test_dock_goal_invalid_status(client, grove_home):
    _write_dock(grove_home)

    r = await client.patch(
        "/portal/actions/dock/goals/grove-foundation", data={"status": "active"}
    )
    assert r.status == 400
    assert "Invalid status" in (await r.text())
    # The file is untouched — the rejected write never reached the writer.
    raw = (grove_home / "dock" / "dock.yaml").read_text(encoding="utf-8")
    assert "status: accelerating" in raw


# ---------------------------------------------------------------------------
# 8. Dock goal not found — 404
# ---------------------------------------------------------------------------


async def test_dock_goal_not_found(client, grove_home):
    _write_dock(grove_home)

    r = await client.patch(
        "/portal/actions/dock/goals/no-such-goal", data={"status": "paused"}
    )
    assert r.status == 404
    assert "not found" in (await r.text())
