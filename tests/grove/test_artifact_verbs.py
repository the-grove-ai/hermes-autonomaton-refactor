"""artifact-continuation-v1 P3 — continuation verbs + lineage render.

POST-only (405 pin), in-flight cap (429 pin), POST-time parent validation
(400, turn never mints), dispatch wiring → template-locked result fragment,
lineage render both directions, legacy-event resilience, verb panel outside
the model-content demarcation (structural pin).
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import grove.api.artifacts as artifacts_mod
import grove.continuation as continuation_mod
from grove.api.artifacts import (
    handle_artifact,
    handle_artifact_compose,
    handle_artifact_fragment,
    handle_artifact_refine,
    resolve_artifact_roots,
)
from grove.api.portal import portal_auth_middleware
from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.kaizen_ledger import KaizenLedger


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(home / "wiki"))
    (home / "wiki" / "pages").mkdir(parents=True)
    return home


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    app["artifact_roots"] = resolve_artifact_roots(config={})
    app["_artifact_index"] = {}
    app.router.add_get("/artifact/{artifact_id}", handle_artifact)
    app.router.add_get(
        "/portal/fragments/artifact/{artifact_id}", handle_artifact_fragment
    )
    app.router.add_post(
        "/portal/actions/artifact/{artifact_id}/refine", handle_artifact_refine
    )
    app.router.add_post(
        "/portal/actions/artifact/{artifact_id}/compose", handle_artifact_compose
    )
    async with TestClient(TestServer(app)) as c:
        yield c


def _emit(path_str: str, parents=None, session="verbs-test") -> str:
    canonical = canonical_artifact_path(path_str)
    aid = artifact_id(canonical)
    KaizenLedger(session).record(
        "artifact_written", path=canonical, artifact_id=aid, turn_id="t#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
        parent_artifact_ids=list(parents or []),
    )
    return aid


def _write_md(grove_home, name, parents=None):
    f = grove_home / name
    f.write_text(f"# {name}\n", encoding="utf-8")
    return _emit(str(f), parents=parents)


# ── verb endpoints: method + validation + cap ────────────────────────────────


async def test_get_on_verb_endpoints_is_405(client, grove_home):
    aid = _write_md(grove_home, "a.md")
    for verb in ("refine", "compose"):
        resp = await client.get(f"/portal/actions/artifact/{aid}/{verb}")
        assert resp.status == 405  # POST-only, pinned (GATE-B cond. 2)


async def test_unknown_parent_400_turn_never_mints(client, grove_home, monkeypatch):
    called = {"n": 0}

    def _no_dispatch(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(
        continuation_mod, "dispatch_continuation_turn", _no_dispatch,
    )
    resp = await client.post(
        "/portal/actions/artifact/" + "e" * 16 + "/refine",
        data={"instruction": "x"},
    )
    assert resp.status == 400
    assert called["n"] == 0  # the turn never minted


async def test_stale_compose_target_400(client, grove_home):
    aid = _write_md(grove_home, "a.md")
    resp = await client.post(
        f"/portal/actions/artifact/{aid}/compose",
        data={"instruction": "merge", "target_id": "f" * 16},
    )
    assert resp.status == 400


async def test_empty_instruction_400(client, grove_home):
    aid = _write_md(grove_home, "a.md")
    resp = await client.post(
        f"/portal/actions/artifact/{aid}/refine", data={"instruction": "  "},
    )
    assert resp.status == 400


async def test_cap_429(client, grove_home, monkeypatch):
    aid = _write_md(grove_home, "a.md")
    # Saturate the in-flight counter (cap mocked via the counter itself).
    monkeypatch.setattr(
        artifacts_mod, "_inflight_turns", artifacts_mod._MAX_INFLIGHT_TURNS,
    )
    resp = await client.post(
        f"/portal/actions/artifact/{aid}/refine", data={"instruction": "go"},
    )
    assert resp.status == 429
    # Slot released only by completed dispatches — saturated stays saturated.
    assert artifacts_mod._inflight_turns == artifacts_mod._MAX_INFLIGHT_TURNS


async def test_dispatch_wiring_and_result_fragment(client, grove_home, monkeypatch):
    parent = _write_md(grove_home, "parent.md")
    calls = []

    def _fake_dispatch(instruction, parent_ids):
        calls.append((instruction, parent_ids))
        return {
            "turn_id": "portal_x#1",
            "response_text": "Refined & saved.",
            "halted": True,
            "pending_items": [
                {"proposal_id": "p1", "tool": "write_file", "zone": "yellow"},
            ],
            "artifact_ids_written": ["9" * 16],
        }

    monkeypatch.setattr(
        continuation_mod, "dispatch_continuation_turn", _fake_dispatch,
    )
    resp = await client.post(
        f"/portal/actions/artifact/{parent}/refine",
        data={"instruction": "Sharpen it."},
    )
    assert resp.status == 200
    body = await resp.text()
    assert calls == [("Sharpen it.", [parent])]
    # Response text inside a model-content container.
    assert '<div class="model-content">' in body
    assert "Refined &amp; saved." in body
    # Artifact link, hash-route.
    assert f'href="/portal#fragments/artifact/{"9" * 16}"' in body
    # Pending items + link to the pending fragment.
    assert "1 action(s) await your approval" in body
    assert 'href="/portal#fragments/proposals/pending"' in body
    # Cap slot released after dispatch.
    assert artifacts_mod._inflight_turns == 0


async def test_compose_passes_both_parents(client, grove_home, monkeypatch):
    a = _write_md(grove_home, "a.md")
    b = _write_md(grove_home, "b.md")
    calls = []

    def _fake_dispatch(instruction, parent_ids):
        calls.append(parent_ids)
        return {"turn_id": "t", "response_text": "ok", "halted": False,
                "pending_items": [], "artifact_ids_written": []}

    monkeypatch.setattr(
        continuation_mod, "dispatch_continuation_turn", _fake_dispatch,
    )
    resp = await client.post(
        f"/portal/actions/artifact/{a}/compose",
        data={"instruction": "merge", "target_id": b},
    )
    assert resp.status == 200
    assert calls == [[a, b]]


# ── lineage render ───────────────────────────────────────────────────────────


async def test_lineage_renders_both_directions(client, grove_home):
    parent = _write_md(grove_home, "root.md")
    child = _write_md(grove_home, "child.md", parents=[parent])
    # Parent's fragment shows the continuation (child).
    body = await (
        await client.get(f"/portal/fragments/artifact/{parent}")
    ).text()
    assert "continuations:" in body
    assert f'href="/portal#fragments/artifact/{child}"' in body
    assert "child.md" in body
    # Child's fragment shows the parent.
    body = await (
        await client.get(f"/portal/fragments/artifact/{child}")
    ).text()
    assert "derived from:" in body
    assert f'href="/portal#fragments/artifact/{parent}"' in body


async def test_legacy_events_render_clean(client, grove_home):
    # A legacy event (no parent_artifact_ids field at all) → no lineage
    # section, no error.
    f = grove_home / "legacy.md"
    f.write_text("# L\n", encoding="utf-8")
    canonical = canonical_artifact_path(str(f))
    aid = artifact_id(canonical)
    KaizenLedger("legacy-test").record(
        "artifact_written", path=canonical, artifact_id=aid, turn_id="t#1",
        active_primary_skill_slug=None, intent_class=None, tool="write_file",
    )
    resp = await client.get(f"/portal/fragments/artifact/{aid}")
    assert resp.status == 200
    body = await resp.text()
    assert "derived from:" not in body
    assert "continuations:" not in body


# ── verb panel: presence + structural containment pin ────────────────────────


async def test_verb_panel_present_outside_demarcation(client, grove_home):
    other = _write_md(grove_home, "other.md")
    aid = _write_md(grove_home, "main.md")
    body = await (
        await client.get(f"/portal/fragments/artifact/{aid}")
    ).text()
    assert 'class="verb-panel"' in body
    assert f'hx-post="/portal/actions/artifact/{aid}/refine"' in body
    assert f'hx-post="/portal/actions/artifact/{aid}/compose"' in body
    # Compose select lists the other artifact (basename + short id), never self.
    assert "other.md" in body and other[:8] in body
    assert f'<option value="{aid}"' not in body
    # STRUCTURAL PIN: the panel sits OUTSIDE the model-content container —
    # between the container's opening div and the panel, opens == closes.
    mc = body.index('<div class="model-content">')
    panel = body.index('<div class="verb-panel">')
    assert mc < panel
    between = body[mc:panel]  # container + its children, panel excluded
    assert between.count("<div") == between.count("</div>")
