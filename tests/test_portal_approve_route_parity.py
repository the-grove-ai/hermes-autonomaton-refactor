"""portal-approve-route-parity — route 7 refuses the six scope-defining types.

The portal approve route (POST ``/portal/actions/proposals/{id}/approve``,
"route 7") is loopback/mesh-gated but carries NO operator identity. Before this
sprint it applied any routing proposal inline — including the six scope-defining
types that write dock/routing/capability files, which are supposed to apply ONLY
via the operator's RED CLI (``autonomaton flywheel approve``). That was C13 / the
route-7 hole in ``portal-action-checkpoint-parity``.

``_apply_routing`` now refuses those six types at the top of the approve branch,
BEFORE any handler is resolved — the same six the agent tool refuses in
``tools/flywheel_review_tool.py``. Each type must yield HTTP 422, a refused card
naming the RED CLI, and — critically — leave THAT TYPE'S OWN scope-defining
target untouched. Because the check runs before handler resolution, no writer
runs; this test proves it per type against the file each type would actually
write:

* dock_mutation / goal_attachment / dock_goal_status / dock_detach → ``dock/dock.yaml``
  (``_approve_dock_*`` / ``_approve_goal_attachment`` writers),
* exploration_nudge → ``$GROVE_HOME/routing.config.yaml`` (the routing_writer
  ``swap_tier_model`` writer),
* model_binding → a capability record under ``$GROVE_HOME/capabilities/``
  (``capability_registry.set_model_binding``) — asserted by proving NO record is
  minted (the writer never runs), since a scope-defining approve is refused
  before the payload is even inspected.

Route 8 (``/confirm``, the RED two-step) and the RED-prefixed dispatch are a
DIFFERENT, already-gated code path (intercepted in ``_dispatch_proposal_action``
before the generic routing dispatch). They are covered by the existing suite and
are not retested here.
"""

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
    PROPOSAL_TYPE_DOCK_DETACH,
    PROPOSAL_TYPE_DOCK_GOAL_STATUS,
    PROPOSAL_TYPE_DOCK_MUTATION,
    PROPOSAL_TYPE_EXPLORATION_NUDGE,
    PROPOSAL_TYPE_GOAL_ATTACHMENT,
    PROPOSAL_TYPE_MODEL_BINDING,
)

# The six scope-defining proposal types — mirrors both
# tools/flywheel_review_tool.py::_SCOPE_DEFINING_REFUSED_TYPES and the new
# grove/api/actions.py::_SCOPE_DEFINING_REFUSED_TYPES.
SCOPE_DEFINING_TYPES = [
    PROPOSAL_TYPE_EXPLORATION_NUDGE,
    PROPOSAL_TYPE_MODEL_BINDING,
    PROPOSAL_TYPE_DOCK_MUTATION,
    PROPOSAL_TYPE_GOAL_ATTACHMENT,
    PROPOSAL_TYPE_DOCK_GOAL_STATUS,
    PROPOSAL_TYPE_DOCK_DETACH,
]

# The four that write the operator dock (dock/dock.yaml).
DOCK_TYPES = frozenset({
    PROPOSAL_TYPE_DOCK_MUTATION,
    PROPOSAL_TYPE_GOAL_ATTACHMENT,
    PROPOSAL_TYPE_DOCK_GOAL_STATUS,
    PROPOSAL_TYPE_DOCK_DETACH,
})


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


# A sovereign, hand-authored dock — the target the four dock types write.
_DOCK_YAML = """\
# Operator Dock — sovereign, hand-authored. Do not let a writer reflow this.
version: "1.0"
context_char_budget: 5000
goals:
  - id: grove-foundation
    name: Grove Foundation
    vector: strategic
    status: accelerating
    definition_of_done: Ship the reference implementation.
    context_sources: []
    keywords: [grove, foundation]
    unlocked_skills: []
"""

# A sovereign, hand-authored operator routing config — the target exploration_nudge
# writes (via routing_writer.swap_tier_model). Comment-dense on purpose: a writer
# run would reflow it.
_ROUTING_CONFIG_YAML = """\
# Operator routing config — sovereign, hand-authored (portal-model-swap-v1).
routing:
  tier_preferences:
    T1:
      - anthropic/claude-haiku-4.5   # cheap cognition
    T2:
      - anthropic/claude-sonnet-4.6
    T3:
      - anthropic/claude-opus-4.6    # apex cognition
"""


def _write_dock(home):
    (home / "dock").mkdir(parents=True, exist_ok=True)
    (home / "dock" / "dock.yaml").write_text(_DOCK_YAML, encoding="utf-8")


def _snapshot(path):
    """A comparable fingerprint of a scope-defining target. A single file →
    (bytes, mtime_ns). A records directory → the sorted (name, bytes) of every
    file under it. Absent → None (a stable sentinel — any writer run would mint a
    file and change it)."""
    if path.is_dir():
        return sorted((p.name, p.read_bytes()) for p in path.rglob("*") if p.is_file())
    if path.exists():
        return (path.read_bytes(), path.stat().st_mtime_ns)
    return None


def _append(pid, ptype):
    """Append a routing-queue proposal of ``ptype`` under content id ``pid``.
    All six scope-defining types are RoutingProposal-family, so each is reached
    by ``_apply_routing`` via the generic ``proposal_queue.read`` dispatch."""
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=pid,
        type=ptype,
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
        source_patterns=("cluster_1",),
        semantic_justification="cluster recurs across sessions",
    ))


def _target_for(home, ptype):
    """This type's OWN scope-defining target, pre-seeded where it pre-exists."""
    if ptype in DOCK_TYPES:
        _write_dock(home)
        return home / "dock" / "dock.yaml"
    if ptype == PROPOSAL_TYPE_EXPLORATION_NUDGE:
        target = home / "routing.config.yaml"
        target.write_text(_ROUTING_CONFIG_YAML, encoding="utf-8")
        return target
    if ptype == PROPOSAL_TYPE_MODEL_BINDING:
        # set_model_binding mints/writes a record under $GROVE_HOME/capabilities/.
        # Fresh GROVE_HOME has none; the refusal must leave it that way.
        return home / "capabilities"
    raise AssertionError(f"unmapped scope-defining type {ptype}")


@pytest.mark.parametrize("ptype", SCOPE_DEFINING_TYPES)
async def test_portal_approve_refuses_scope_defining_type(client, grove_home, ptype, monkeypatch):
    target = _target_for(grove_home, ptype)
    before = _snapshot(target)

    # A correct governance refusal is NOT a fault: it must NOT reach the operator's
    # channel (Telegram) nor file a portal_action_failure Kaizen proposal — those
    # are for genuine failures, and firing them on every refusal is alarm fatigue.
    broadcasts = []

    async def _record_broadcast(content, **kw):
        broadcasts.append(content)

    monkeypatch.setattr(actions_mod, "broadcast_to_operator", _record_broadcast)

    pid = f"{ptype}:scope-{ptype}"
    _append(pid, ptype)

    r = await client.post(f"/portal/actions/proposals/{pid}/approve")

    # Door refuses: 422, refused card, RED CLI named.
    assert r.status == 422
    body = await r.text()
    assert "refused" in body
    assert "scope-defining" in body
    assert "flywheel approve" in body

    # This type's OWN target is untouched — its writer never ran.
    assert _snapshot(target) == before

    # Refused, not applied: the original proposal is still in the queue.
    assert proposal_queue.read(pid) is not None

    # QUIET: no operator broadcast, no Kaizen fault filed for a deliberate refusal.
    assert broadcasts == [], f"refusal broadcast to operator: {broadcasts}"
    pafs = [p for p in proposal_queue.read_all() if p.type == "portal_action_failure"]
    assert pafs == [], f"refusal filed portal_action_failure proposals: {[p.proposal_id for p in pafs]}"
