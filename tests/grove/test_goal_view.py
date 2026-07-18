"""goal-spine-v1 P4 — goal detail fragment + detach surface tests.

Pins: entries render with excerpt + rationale + hash-route artifact link,
honest empty state, dangling-goal tolerance (R-9), detach round-trip (reason
required — refusal writes nothing), N:N detach independence, peer-auth on the
new routes (prefix pin + middleware 403), and the list-card hash link.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from grove.api.actions import handle_attachment_detach
from grove.api.fragments import handle_goal_detail, render_goal_card
from grove.api.portal import portal_auth_middleware
from grove.dock.attachment_store import (
    attached_pairs,
    mint_attachment,
)
from grove.kaizen_ledger import default_ledger_dir

AID1 = "a" * 16
AID2 = "b" * 16

_DETACH_PATH = "/portal/actions/dock/goals/{goal_id}/attachments/{artifact_id}/detach"
_FRAGMENT_PATH = "/portal/fragments/goal/{goal_id}"


def _goal(goal_id="goal-alpha", name="Goal Alpha"):
    return SimpleNamespace(
        id=goal_id,
        name=name,
        vector="strategic",
        status="accelerating",
        definition_of_done="Alpha shipped.",
        keywords=("alpha", "spine"),
        extra={},
    )


def _dock(*goals):
    return SimpleNamespace(goals=tuple(goals))


def _seed_artifact_written(*artifact_ids) -> None:
    ledger_dir = default_ledger_dir()
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with (ledger_dir / "seed.jsonl").open("a", encoding="utf-8") as fh:
        for aid in artifact_ids:
            fh.write(
                json.dumps(
                    {
                        "event_type": "artifact_written",
                        "session_id": "seed",
                        "timestamp": "2026-07-18T00:00:00+00:00",
                        "artifact_id": aid,
                        "path": f"/tmp/{aid}.md",
                        "turn_id": "s#1",
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def _mint(aid, goal_id="goal-alpha"):
    return mint_attachment(
        aid,
        goal_id,
        proposal_id="prop-1",
        rationale="moves the goal forward",
        excerpt="quoted evidence",
        dock=_dock(_goal("goal-alpha"), _goal("goal-beta", "Goal Beta")),
        excerpt_cap=600,
    )


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    return home


@pytest.fixture
def dock(monkeypatch):
    """One two-goal dock, patched into BOTH consuming modules (each imported
    load_dock into its own namespace at module import)."""
    dock = _dock(_goal("goal-alpha"), _goal("goal-beta", "Goal Beta"))
    monkeypatch.setattr("grove.api.fragments.load_dock", lambda: dock)
    monkeypatch.setattr("grove.api.actions.load_dock", lambda: dock)
    return dock


@pytest.fixture
async def client(grove_home, dock):
    app = web.Application(middlewares=[portal_auth_middleware])
    app.router.add_get(_FRAGMENT_PATH, handle_goal_detail)
    app.router.add_post(_DETACH_PATH, handle_attachment_detach)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── fragment rendering ──────────────────────────────────────────────────────


async def test_fragment_renders_attached_entries(client, grove_home):
    _seed_artifact_written(AID1)
    _mint(AID1)
    resp = await client.get(f"/portal/fragments/goal/goal-alpha")
    assert resp.status == 200
    body = await resp.text()
    assert '<div id="goal-detail">' in body
    assert "Goal Alpha" in body and "Alpha shipped." in body
    # Entry: hash-route artifact link + excerpt + rationale + detach control.
    assert f'href="/portal#fragments/artifact/{AID1}"' in body
    assert "quoted evidence" in body
    assert "moves the goal forward" in body
    assert f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach" in body
    assert 'name="reason"' in body  # the K2 reason-capture affordance


async def test_empty_state_is_honest(client):
    resp = await client.get("/portal/fragments/goal/goal-beta")
    assert resp.status == 200
    body = await resp.text()
    assert "Nothing attached yet" in body
    assert "error" not in body.lower()


async def test_dangling_goal_renders_not_found_never_raises(client):
    resp = await client.get("/portal/fragments/goal/auto-pruned-goal")
    assert resp.status == 404
    body = await resp.text()
    assert "No such goal" in body
    assert '<div id="goal-detail">' in body  # plain body, not a blank panel


# ── detach round-trip ───────────────────────────────────────────────────────


async def test_detach_round_trip(client, grove_home):
    _seed_artifact_written(AID1)
    _mint(AID1)
    assert (AID1, "goal-alpha") in attached_pairs()

    resp = await client.post(
        f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach",
        data={"reason": "wrong goal entirely"},
    )
    assert resp.status == 200
    body = await resp.text()
    # Success re-renders the fragment WITHOUT the pair.
    assert '<div id="goal-detail">' in body
    assert AID1 not in body
    assert "Nothing attached yet" in body
    # The store agrees, and the reason rode the event.
    assert attached_pairs() == {}
    detach_events = [
        json.loads(line)
        for p in sorted(default_ledger_dir().glob("*.jsonl"))
        for line in p.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "artifact_goal_detached"
    ]
    assert len(detach_events) == 1
    assert detach_events[0]["reason"] == "wrong goal entirely"


async def test_detach_requires_reason_and_refusal_writes_nothing(
    client, grove_home
):
    _seed_artifact_written(AID1)
    _mint(AID1)
    resp = await client.post(
        f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach",
        data={"reason": "   "},
    )
    assert resp.status == 400
    assert "Detach reason is empty" in await resp.text()
    # Refusal wrote nothing: still attached, no detach event on disk.
    assert (AID1, "goal-alpha") in attached_pairs()
    for p in sorted(default_ledger_dir().glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event_type"] != "artifact_goal_detached"


async def test_detach_unattached_pair_is_loud_404(client, grove_home):
    resp = await client.post(
        f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach",
        data={"reason": "does not belong"},
    )
    assert resp.status == 404
    assert "not attached" in await resp.text()


# ── N:N independence ────────────────────────────────────────────────────────


async def test_n_to_n_detach_independence(client, grove_home):
    _seed_artifact_written(AID1)
    _mint(AID1, "goal-alpha")
    _mint(AID1, "goal-beta")

    # Visible under BOTH goals.
    for gid in ("goal-alpha", "goal-beta"):
        body = await (await client.get(f"/portal/fragments/goal/{gid}")).text()
        assert f'href="/portal#fragments/artifact/{AID1}"' in body

    # Detach from alpha only.
    resp = await client.post(
        f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach",
        data={"reason": "belongs to beta, not alpha"},
    )
    assert resp.status == 200

    alpha = await (await client.get("/portal/fragments/goal/goal-alpha")).text()
    beta = await (await client.get("/portal/fragments/goal/goal-beta")).text()
    assert AID1 not in alpha
    assert f'href="/portal#fragments/artifact/{AID1}"' in beta
    assert attached_pairs() == {
        (AID1, "goal-beta"): attached_pairs()[(AID1, "goal-beta")]
    }


# ── auth ────────────────────────────────────────────────────────────────────


def test_routes_sit_under_the_middleware_prefix():
    # portal_auth_middleware gates by the /portal path prefix — both new
    # routes must live under it (K1 pin).
    assert _FRAGMENT_PATH.startswith("/portal/")
    assert _DETACH_PATH.startswith("/portal/")


async def test_detach_route_is_auth_gated():
    async def handler(request):
        return web.Response(text="ok")

    gated = make_mocked_request(
        "POST",
        f"/portal/actions/dock/goals/goal-alpha/attachments/{AID1}/detach",
    )
    resp = await portal_auth_middleware(gated, handler)
    assert resp.status == 403  # mocked request has no peer → denied


async def test_goal_fragment_route_is_auth_gated():
    async def handler(request):
        return web.Response(text="ok")

    gated = make_mocked_request("GET", "/portal/fragments/goal/goal-alpha")
    resp = await portal_auth_middleware(gated, handler)
    assert resp.status == 403


# ── list-card link ──────────────────────────────────────────────────────────


def test_list_card_links_into_detail():
    card = render_goal_card(
        SimpleNamespace(
            id="goal-alpha",
            name="Goal Alpha",
            vector="strategic",
            status="accelerating",
            definition_of_done="Alpha shipped.",
            keywords=("alpha",),
            extra={},
        )
    )
    assert 'href="/portal#fragments/goal/goal-alpha"' in card
