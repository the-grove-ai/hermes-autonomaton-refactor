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


# ---------------------------------------------------------------------------
# portal-action-error-surfacing-v1 P3 — _loud_action_failure + wiring
# ---------------------------------------------------------------------------


def _paf(home=None):
    """The portal_action_failure proposals filed in the (temp) queue."""
    return [
        p for p in proposal_queue.read_all()
        if p.type == "portal_action_failure"
    ]


# — Helper contract: fail-safe, card+banner, status preserved ————————


async def test_loud_helper_returns_card_and_banner_when_kaizen_raises(monkeypatch):
    sent = []

    async def _fake_broadcast(content, **kw):
        sent.append(content)
        return {"logged": True}

    def _boom(**kw):
        raise RuntimeError("queue down")

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _fake_broadcast)
    monkeypatch.setattr(proposal_queue, "file_agentless_proposal", _boom)

    resp = await actions_mod._loud_action_failure(
        '<div class="card" id="inline-x">inline</div>',
        failure_class="fc", action="act", message="it broke", status=422,
    )
    # Status preserved; card + banner both in the body despite the kaizen throw.
    assert resp.status == 422
    body = resp.text
    assert 'id="inline-x">inline' in body            # inline card preserved
    assert 'id="alert-banner"' in body               # banner appended
    assert 'hx-swap-oob="true"' in body
    assert "it broke" in body
    # Broadcast still fired even though filing raised (P1 discipline in the helper).
    assert sent == ["Portal action 'act' failed: it broke"]


async def test_loud_helper_file_kaizen_false_skips_filing(monkeypatch):
    calls = []

    async def _noop_broadcast(content, **kw):
        return {}

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _noop_broadcast)
    monkeypatch.setattr(
        proposal_queue, "file_agentless_proposal",
        lambda **kw: calls.append(kw),
    )

    resp = await actions_mod._loud_action_failure(
        "<div>x</div>", failure_class="fc", action="act", message="m",
        status=400, file_kaizen=False,
    )
    assert resp.status == 400
    assert 'id="alert-banner"' in resp.text
    assert calls == []  # filing gated off → nothing queued


# — Per-branch wiring: status + banner + failure_class/action ——————————


def _capture_broadcast(monkeypatch):
    """Record broadcast_to_operator calls (the loud always-on leg)."""
    sent = []

    async def _rec(content, **kw):
        sent.append(content)
        return {"logged": True}

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _rec)
    return sent


# — SUPPRESSED sites (file_kaizen=False): loud everywhere, but NOT queued ——
# UI structurally cannot produce these, so a Kaizen proposal has no fix to
# recommend and would be queue noise. Broadcast + banner + 4xx stay unconditional.


async def test_dock_invalid_status_loud_but_not_filed(client, grove_home, monkeypatch):
    sent = _capture_broadcast(monkeypatch)
    _write_dock(grove_home)
    r = await client.patch(
        "/portal/actions/dock/goals/grove-foundation", data={"status": "bogus"}
    )
    assert r.status == 400
    body = await r.text()
    assert 'id="alert-banner"' in body and 'hx-swap-oob="true"' in body
    assert "Invalid status" in body           # inline card preserved
    assert len(sent) == 1 and "dock_update" in sent[0]   # broadcast fired (loud)
    assert _paf() == []                       # SUPPRESSED — nothing queued


async def test_tier_swap_unknown_tier_loud_but_not_filed(client, grove_home, monkeypatch):
    sent = _capture_broadcast(monkeypatch)
    r = await client.post(
        "/portal/actions/routing/swap", data={"tier": "BOGUS", "model_slug": "x/y"}
    )
    assert r.status == 400
    assert 'id="alert-banner"' in (await r.text())
    assert len(sent) == 1 and "tier_swap" in sent[0]
    assert _paf() == []                       # SUPPRESSED


async def test_tier_revert_unknown_tier_loud_but_not_filed(client, grove_home, monkeypatch):
    sent = _capture_broadcast(monkeypatch)
    r = await client.post("/portal/actions/routing/revert", data={"tier": "BOGUS"})
    assert r.status == 400
    assert 'id="alert-banner"' in (await r.text())
    assert len(sent) == 1 and "tier_revert" in sent[0]
    assert _paf() == []                       # SUPPRESSED


async def test_tier_swap_same_model_is_noop_info_not_error(client, grove_home, monkeypatch):
    # ledger-eventtype-hygiene-v1 Change 3 — swapping a tier to the model it
    # already holds is a success-class NO-OP: HTTP 200 with an info line (not the
    # 422 error surface), nothing written, no failure broadcast/queue entry.
    import shutil
    from pathlib import Path

    import grove.config.routing_writer as rw

    repo_root = Path(__file__).resolve().parents[1]
    cfg = grove_home / "routing.config.yaml"
    shutil.copy(repo_root / "config" / "routing.config.yaml", cfg)
    # rebind the cached writer singleton to THIS test's GROVE_HOME config.
    monkeypatch.setattr(rw, "_writer", None)

    bytes_before = cfg.read_bytes()
    mtime_before = cfg.stat().st_mtime_ns
    sent = _capture_broadcast(monkeypatch)

    r = await client.post(
        "/portal/actions/routing/swap",
        data={"tier": "T2", "model_slug": "anthropic/claude-sonnet-4.6"},
    )
    assert r.status == 200                        # success-class, NOT 422
    body = await r.text()
    assert "no change" in body.lower()            # the info copy
    assert 'class="meta info"' in body            # info styling
    assert 'class="meta error"' not in body       # NOT the error surface
    # PIN: the no-op wrote nothing — bytes + mtime unchanged, no .bak.
    assert cfg.read_bytes() == bytes_before
    assert cfg.stat().st_mtime_ns == mtime_before
    assert not cfg.with_suffix(cfg.suffix + ".bak").exists()
    assert sent == []                             # no failure broadcast
    assert _paf() == []                           # nothing filed


async def test_forge_no_draft_dir(client, grove_home):
    r = await client.post("/portal/actions/forge/nope/publish")
    assert r.status == 404
    assert 'id="alert-banner"' in (await r.text())
    assert any(
        p.payload == {"failure_class": "forge_no_draft_dir", "action": "forge_publish"}
        for p in _paf()
    )


# — Dedup at call sites ————————————————————————————————————————————


async def test_forge_no_dir_dedups_on_repeat(client, grove_home):
    # Same failure_class + action across 3 taps → ONE queue entry (flood-guard).
    for _ in range(3):
        r = await client.post("/portal/actions/forge/nope/publish")
        assert r.status == 404
    paf = _paf()
    assert len(paf) == 1
    assert paf[0].payload == {
        "failure_class": "forge_no_draft_dir", "action": "forge_publish"
    }


async def test_proposal_not_found_files_one_group_per_action(client, grove_home):
    # SAME proposal id across all three actions — the id is ephemeral (excluded
    # from the dedup key), so the action alone splits them into 3 groups.
    for action in ("approve", "reject", "dismiss"):
        r = await client.post(f"/portal/actions/proposals/ghost/{action}")
        assert r.status == 404
        assert 'id="alert-banner"' in (await r.text())
    groups = {(p.payload["failure_class"], p.payload["action"]) for p in _paf()}
    assert groups == {
        ("proposal_not_found", "proposal_approve"),
        ("proposal_not_found", "proposal_reject"),
        ("proposal_not_found", "proposal_dismiss"),
    }
    assert len(_paf()) == 3


# — Success-path non-regression ————————————————————————————————————


async def test_success_path_has_no_banner(client, grove_home, monkeypatch):
    # The async _apply_routing refactor must not perturb the 200 success path:
    # a routing reject still returns its resolved card, no banner, no filing.
    _append_routing("routing_adjustment:ok999")
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition", lambda p, **kw: None
    )
    r = await client.post("/portal/actions/proposals/routing_adjustment:ok999/reject")
    assert r.status == 200
    body = await r.text()
    assert "rejected" in body
    assert "alert-banner" not in body
    assert _paf() == []


# ---------------------------------------------------------------------------
# P3.6 — portal pending-proposals card gates Approve for render-only types
# ---------------------------------------------------------------------------


async def test_portal_card_omits_approve_for_render_only_type(client, grove_home):
    # A portal_action_failure proposal surfaces in the pending list, but WITHOUT
    # an Approve button (approve dead-ends at _handler_for). Reject + Dismiss stay
    # — the portal reject/dismiss path is honored for every type.
    proposal_queue.file_agentless_proposal(
        failure_class="forge_notion_cold", action="forge_publish",
        evidence="e", justification="Notion cold at publish.",
    )
    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    assert "portal_action_failure" in body        # it surfaces (unfiltered)
    assert "btn-reject" in body and "btn-dismiss" in body
    assert "btn-approve" not in body              # dead affordance gated out


async def test_portal_card_keeps_approve_for_memory(client, grove_home):
    # REGRESSION GUARD: memory cards keep Approve — memory applies via its own
    # registry (not PROPOSAL_HANDLERS); the resolver's bridge branch returns True.
    _write_memory(grove_home, [_memory_record("a recurring domain fact")])
    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    body = await r.text()
    assert "memory_context" in body
    assert "btn-approve" in body


async def test_portal_card_keeps_approve_for_routing(client, grove_home):
    _append_routing("routing_adjustment:keepapprove")
    r = await client.get("/portal/fragments/proposals/pending")
    assert r.status == 200
    assert "btn-approve" in (await r.text())


async def test_portal_reject_honored_for_render_only(client, grove_home):
    pid, _ = proposal_queue.file_agentless_proposal(
        failure_class="forge_notion_cold", action="forge_publish",
        evidence="e", justification="cold",
    )
    r = await client.post(f"/portal/actions/proposals/{pid}/reject")
    assert r.status == 200                         # honored, not a dead-end
    assert "rejected" in (await r.text())
    assert proposal_queue.read(pid) is None        # dequeued


async def test_portal_dismiss_honored_for_render_only(client, grove_home):
    pid, _ = proposal_queue.file_agentless_proposal(
        failure_class="forge_notion_cold", action="forge_publish",
        evidence="e", justification="cold",
    )
    r = await client.post(f"/portal/actions/proposals/{pid}/dismiss")
    assert r.status == 200
    assert "dismissed" in (await r.text())
    assert proposal_queue.read(pid) is None
