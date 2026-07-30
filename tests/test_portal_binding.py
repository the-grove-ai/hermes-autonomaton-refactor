"""binding-governance-surfaces-v1 Phase 3 — binding page + pin/unpin actions.

Proves:

* HELPER — binding_view rows derive pinned / inherited / tier-override state
  purely from the record + routing config (no worker boot), with the plane
  qualification stated plainly on every row.
* PAGE — the grouped fragment renders Telemetry / Primary / Auxiliary, with
  producer/observer sub-groups and catalog-only dropdowns.
* ACTIONS — pin/unpin follow the tier-swap template: happy path re-renders
  the row from a FRESH read (200); off-catalog model → 400 loud; writer
  refusal (unresolvable skill) → 422 loud.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import grove.capability_registry as reg
from grove.api import actions as actions_mod
from grove.api.actions import register_action_routes
from grove.api.fragments import register_fragment_routes
from grove.api.portal import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)
from grove.capability import Capability, ModelBinding
from tests.grove.routing_fixtures import to_v2_split

from tests.grove.test_capability_binding_writer import _mint, _skill_cap

_CATALOG = [
    {"slug": "z-ai/glm-5.2", "display_name": "GLM 5.2", "provider": "openrouter",
     "input_cost_per_mtok": 1, "output_cost_per_mtok": 2},
    {"slug": "anthropic/claude-haiku-4.5", "display_name": "Haiku 4.5",
     "provider": "openrouter", "input_cost_per_mtok": 1, "output_cost_per_mtok": 2},
]

_ROUTING_YAML = """\
routing:
  tier_preferences:
    T1:
      model: deepseek/deepseek-v4-pro
    T2:
      model: anthropic/claude-sonnet-4.6
    T3:
      model: anthropic/claude-opus-4.6
    Telemetry:
      model: google/gemini-2.5-flash
"""


def _fleet_cap(cap_id: str, *, mode: str = "action_surface_publish",
               model_binding: ModelBinding | None = None) -> Capability:
    cap = _skill_cap(cap_id, model_binding=model_binding)
    cap.governance = {
        "approval_handoff": {"mode": mode},
        "write_zone": {"staging_dir": "fleet/x/staging",
                       "canonical_dir": "fleet/x/canonical"},
    }
    return cap


@pytest.fixture
def caps_env(tmp_path, monkeypatch):
    """Hermetic registry + routing config + catalog + ledger home."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    # GRV-001 v2.0 — write the operational + authority split via the shared
    # migrator transform (Task 5a); the binding view reads the operational tiers.
    _op_text, _auth_text = to_v2_split(_ROUTING_YAML)
    (tmp_path / "routing.operational.yaml").write_text(_op_text, encoding="utf-8")
    (tmp_path / "routing.authority.yaml").write_text(_auth_text, encoding="utf-8")
    repo_caps = tmp_path / "repo_caps"
    repo_caps.mkdir()
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: repo_caps)
    monkeypatch.setattr(
        reg, "grove_home_capabilities_dir", lambda: tmp_path / "capabilities"
    )
    monkeypatch.setattr(
        "grove.config.model_catalog.load_catalog", lambda: list(_CATALOG)
    )
    monkeypatch.setattr(actions_mod, "load_catalog", lambda: list(_CATALOG))
    return repo_caps


@pytest.fixture
async def client(caps_env):
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    register_fragment_routes(app)
    register_action_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── helper state derivation ──────────────────────────────────────────────────


def test_helper_inherited_state(caps_env):
    from grove.api.binding_view import PLANE_NOTE_INHERIT, binding_row

    _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    row = binding_row("bindui-alpha")
    assert row is not None
    # preferred=2 in the test cap → inherits T2 (currently the routing model).
    assert row["state"] == "inherits T2 (currently anthropic/claude-sonnet-4.6)"
    assert row["pinned"] is False
    assert row["plane_note"] == PLANE_NOTE_INHERIT
    assert row["group"] == "producer"


def test_helper_pinned_state(caps_env):
    from grove.api.binding_view import PLANE_NOTE_PIN, binding_row

    _mint(caps_env, _fleet_cap(
        "skill.fleet.bindui-beta",
        model_binding=ModelBinding(type="model", model="z-ai/glm-5.2"),
    ))
    row = binding_row("bindui-beta")
    assert row["state"] == "pinned: z-ai/glm-5.2"
    assert row["pinned"] is True
    assert row["plane_note"] == PLANE_NOTE_PIN
    # The plane qualification says plainly where the pin applies.
    assert "Fleet workers honor this pin" in row["plane_note"]
    assert "interactive agent" in row["plane_note"]


def test_helper_tier_override_state(caps_env):
    from grove.api.binding_view import PLANE_NOTE_TIER_OVERRIDE, binding_row

    _mint(caps_env, _fleet_cap(
        "skill.fleet.bindui-gamma", mode="other",
        model_binding=ModelBinding(type="tier_override", tier="T3"),
    ))
    row = binding_row("bindui-gamma")
    assert row["state"] == "tier override T3 (currently anthropic/claude-opus-4.6)"
    assert row["pinned"] is False
    assert row["plane_note"] == PLANE_NOTE_TIER_OVERRIDE
    assert row["group"] == "observer"


def test_helper_unknown_skill_returns_none(caps_env):
    from grove.api.binding_view import binding_row
    # Registry must be non-empty (an empty registry fails loud by design);
    # the unknown NAME within a loaded registry returns None.
    _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    assert binding_row("bindui-nonexistent") is None


# ── page fragment ─────────────────────────────────────────────────────────────


async def test_binding_panel_renders_groups(client, caps_env):
    _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    _mint(caps_env, _fleet_cap(
        "skill.fleet.bindui-beta", mode="other",
        model_binding=ModelBinding(type="model", model="z-ai/glm-5.2"),
    ))
    r = await client.get("/portal/fragments/binding/panel")
    assert r.status == 200
    body = await r.text()
    for heading in ("Telemetry", "Primary", "Auxiliary", "Producers", "Observers"):
        assert f"<h3>{heading}</h3>" in body or f"<h4>{heading}</h4>" in body
    assert 'id="tier-T2"' in body                       # Primary tier card
    assert 'id="tier-Telemetry"' in body                # Telemetry tier card
    assert 'id="binding-bindui-alpha"' in body          # producer row
    assert 'id="binding-bindui-beta"' in body           # observer row
    assert "pinned: z-ai/glm-5.2" in body
    assert "inherits T2" in body
    assert "Fleet workers honor this pin" in body       # plane note rendered
    # Catalog-only modal: every option is a catalog slug or display name.
    assert 'value="z-ai/glm-5.2"' in body
    assert "GLM 5.2" in body


# ── actions: happy + 400 + 422 ───────────────────────────────────────────────


async def test_pin_happy_path_files_proposal(client, caps_env):
    # portal-action-checkpoint-parity — pin FILES a model_binding proposal; the
    # portal writes NOTHING. The binding lands only when the operator runs
    # `flywheel approve` (the RED CLI apply → set_model_binding).
    path = _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    r = await client.post(
        "/portal/actions/binding/pin",
        data={"skill": "bindui-alpha", "model_slug": "z-ai/glm-5.2"},
    )
    assert r.status == 200
    body = await r.text()
    assert "proposed" in body                           # pending, not applied
    assert "flywheel approve" in body                   # the operator apply command
    assert "bindui-alpha" in body
    assert 'id="alert-banner"' not in body              # no failure banner
    # The portal wrote nothing — the binding is unchanged until approve.
    from grove.capability_registry import load_capabilities

    assert load_capabilities()["skill.fleet.bindui-alpha"].model_binding is None


async def test_unpin_happy_path(client, caps_env):
    # portal-action-checkpoint-parity — unpin FILES a model_binding proposal with
    # proposed_binding=None (the writer clears on approve). Portal writes nothing.
    path = _mint(caps_env, _fleet_cap(
        "skill.fleet.bindui-beta",
        model_binding=ModelBinding(type="model", model="z-ai/glm-5.2"),
    ))
    r = await client.post(
        "/portal/actions/binding/unpin", data={"skill": "bindui-beta"}
    )
    assert r.status == 200
    body = await r.text()
    assert "proposed" in body
    assert "flywheel approve" in body
    assert "bindui-beta" in body
    # Portal wrote nothing — the seed pin is still in place until approve.
    from grove.capability_registry import load_capabilities

    assert load_capabilities()["skill.fleet.bindui-beta"].model_binding.model == (
        "z-ai/glm-5.2"
    )


async def test_pin_off_catalog_model_400(client, caps_env, monkeypatch):
    sent = []

    async def _rec(content, **kw):
        sent.append(content)
        return {"logged": True}

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _rec)
    path = _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    original = path.read_bytes()
    r = await client.post(
        "/portal/actions/binding/pin",
        data={"skill": "bindui-alpha", "model_slug": "fake/not-a-model"},
    )
    assert r.status == 400
    body = await r.text()
    assert 'id="alert-banner"' in body
    assert "not in the catalog" in body
    assert path.read_bytes() == original                # nothing written
    assert len(sent) == 1 and "binding_pin" in sent[0]


async def test_pin_unresolvable_skill_files_proposal(client, caps_env, monkeypatch):
    # portal-action-checkpoint-parity — file-time validation is skill-PRESENCE +
    # catalog membership; skill RESOLVABILITY is enforced at apply time by the
    # writer (set_model_binding raises → the flywheel approve surfaces it, and the
    # proposal stays queued). So a cataloged model + a non-empty skill name FILES a
    # proposal; the portal writes nothing.
    async def _noop(content, **kw):
        return {"logged": True}

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _noop)
    _mint(caps_env, _fleet_cap("skill.fleet.bindui-alpha"))
    r = await client.post(
        "/portal/actions/binding/pin",
        data={"skill": "bindui-nonexistent", "model_slug": "z-ai/glm-5.2"},
    )
    assert r.status == 200
    body = await r.text()
    assert "proposed" in body
    assert "flywheel approve" in body
