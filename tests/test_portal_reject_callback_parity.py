"""portal-reject-callback-parity-v1 — the portal reject/dismiss branch must
dispatch the type's reject_callback (tombstone / suppression / pattern
re-activation), matching flywheel_cli.cli_reject.

Before this sprint, _apply_routing's reject/dismiss branch removed the proposal
and recorded the disposition but never called handler.reject_callback, so a
portal reject left no tombstone (proposal re-surfaced) and — for pattern_demotion
— silently failed to re-activate a pattern the operator meant to keep.

Exercised through the REAL portal route (aiohttp TestClient → handle_proposal_*
→ _dispatch_proposal_action → _apply_routing), GROVE_HOME-isolated per test.
"""
import dataclasses
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


def _append(ptype, pid, payload, **extra):
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=pid, type=ptype, payload=payload, evidence=(),
        eval_hash="", created_at="2026-07-19T00:00:00Z", **extra,
    ))


# 1 — portal reject exploration_nudge → own-namespace tombstone, binding untouched


async def test_portal_reject_exploration_nudge_writes_tombstone(client, grove_home):
    _append("exploration_nudge", "exploration_nudge:t1", {"slug": "prov/x", "tier": "T2"})
    r = await client.post("/portal/actions/proposals/exploration_nudge:t1/reject")
    assert r.status == 200
    assert proposal_queue.read("exploration_nudge:t1") is None
    from grove.eval.exploration_scan import _load_tombstones
    assert [t["slug"] for t in _load_tombstones()] == ["prov/x"]
    # OWN namespace — the binding tombstone store is untouched (F-4).
    assert not (grove_home / "binding_tombstones.json").exists()


# 2 — portal reject model_binding → binding tombstone


async def test_portal_reject_model_binding_writes_tombstone(client, grove_home):
    _append(
        "model_binding", "model_binding:t2",
        {"skill": "alpha",
         "proposed_binding": {"type": "model", "model": "prov/y"},
         "previous_binding": {"type": "model", "model": "prov/base"}},
    )
    r = await client.post("/portal/actions/proposals/model_binding:t2/reject")
    assert r.status == 200
    assert proposal_queue.read("model_binding:t2") is None
    tomb = grove_home / "binding_tombstones.json"
    assert tomb.exists()
    entries = json.loads(tomb.read_text(encoding="utf-8"))["tombstones"]
    assert any(e["skill"] == "alpha" and e["proposed_model"] == "prov/y" for e in entries)


# 3 — portal reject pattern_demotion → pattern re-activated (the functional case)


async def test_portal_reject_pattern_demotion_reactivates(client, grove_home, monkeypatch):
    from grove import pattern_cache
    calls = []
    monkeypatch.setattr(
        pattern_cache.PatternCacheStore, "set_status",
        lambda self, pid, status, **kw: calls.append((pid, status)),
    )
    _append(
        "pattern_demotion", "pattern_demotion:t3",
        {"pattern_id": "sha256:pat1", "intent_class": "weather",
         "cacheable_type": "static", "suggested_action": "demote",
         "trigger": "correction_drift", "correction_turn_id": "t9"},
    )
    r = await client.post("/portal/actions/proposals/pattern_demotion:t3/reject")
    assert r.status == 200
    assert proposal_queue.read("pattern_demotion:t3") is None
    from grove.pattern_cache import STATUS_ACTIVE
    assert ("sha256:pat1", STATUS_ACTIVE) in calls  # re-activated, not left suspended


# 4 — portal DISMISS exploration_nudge → tombstone too (both verbs, R-1)


async def test_portal_dismiss_exploration_nudge_writes_tombstone(client, grove_home):
    _append("exploration_nudge", "exploration_nudge:t4", {"slug": "prov/z", "tier": "T2"})
    r = await client.post("/portal/actions/proposals/exploration_nudge:t4/dismiss")
    assert r.status == 200
    assert proposal_queue.read("exploration_nudge:t4") is None
    from grove.eval.exploration_scan import _load_tombstones
    assert "prov/z" in [t["slug"] for t in _load_tombstones()]


# 5 — unknown / handler-less type → dequeue still succeeds, no raise


async def test_portal_reject_handlerless_type_dequeues(client, grove_home):
    _append("does_not_exist", "does_not_exist:t5", {"x": 1})
    r = await client.post("/portal/actions/proposals/does_not_exist:t5/reject")
    assert r.status == 200  # tolerant resolution — no strict 422 on reject
    assert proposal_queue.read("does_not_exist:t5") is None


# 6 — raising callback → WARNING logged, dequeue succeeds, disposition recorded


async def test_portal_reject_raising_callback_still_dequeues(
    client, grove_home, monkeypatch, caplog
):
    from grove import flywheel_cli

    def _boom(p):
        raise RuntimeError("kaboom")

    row = flywheel_cli.PROPOSAL_HANDLERS["exploration_nudge"]
    monkeypatch.setitem(
        flywheel_cli.PROPOSAL_HANDLERS, "exploration_nudge",
        dataclasses.replace(row, reject_callback=_boom),
    )
    disp = []
    monkeypatch.setattr(
        actions_mod, "_record_kaizen_disposition",
        lambda p, **kw: disp.append(kw),
    )
    _append("exploration_nudge", "exploration_nudge:t6", {"slug": "prov/b", "tier": "T2"})
    with caplog.at_level("WARNING"):
        r = await client.post("/portal/actions/proposals/exploration_nudge:t6/reject")
    assert r.status == 200
    assert proposal_queue.read("exploration_nudge:t6") is None          # dequeue survived
    assert any("reject_callback failed" in rec.message for rec in caplog.records)
    assert disp and disp[0]["disposition"] == "rejected"               # disposition recorded
    from grove.eval.exploration_scan import _load_tombstones
    assert "prov/b" not in [t["slug"] for t in _load_tombstones()]      # callback raised pre-write
