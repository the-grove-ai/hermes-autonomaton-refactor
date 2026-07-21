"""propose-approve-deadlock-v1 Phase 1b-ii — portal RED surface proof.

Exercises the mint-capable endpoint end-to-end through a real aiohttp test app:

  * NONCE — valid approve-nonce accepted; tampered / wrong-step / expired-bucket
    rejected.
  * TWO-STEP — /approve issues a Confirm card + confirm-nonce and does NOT mint or
    write; /confirm with the issued nonce runs the callback → .env written once.
  * STEP-JUMP — POST /confirm without a valid confirm-nonce is rejected (403), no
    write.
  * REPLAY — a second /confirm after a successful one finds nothing (claim pop) →
    fails clean, no double-write.
  * ORPHAN — a durable queue row whose in-memory payload is gone renders EXPIRED,
    no live approve.
  * ESCAPING — the masked description renders through _esc (no raw HTML).
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from grove.api import init_substrate_singletons, register_portal_routes
from grove.api.actions import register_action_routes
from grove.api.fragments import _PORTAL_ASSETS, register_fragment_routes
from grove.api.portal import portal_auth_middleware
from grove.api.red_nonce import red_nonce, verify_red_nonce
from grove.effect_signature import canonical_effect_signature
from grove.eval import proposal_queue
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
    PendingRedProposal,
    action_proposal_id,
    describe_red_action,
    get_red_pending_store,
    prepare_execute_arguments,
)

_KEY = b"testkey"


class _StubAdapter:
    def __init__(self, key: str) -> None:
        self._api_key = key


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    return tmp_path


@pytest.fixture
async def client(grove_home):
    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)          # injects app["red_pending_store"]
    app["api_server_adapter"] = _StubAdapter("testkey")
    register_portal_routes(app)
    app.router.add_static("/portal/static", str(_PORTAL_ASSETS))
    register_fragment_routes(app)
    register_action_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


def _stage(grove_home, *, content="HF_TOKEN=hf_x\n", description=None):
    """Put a live RED proposal into the store + the opaque durable queue row.

    red-action-store-pending-v1 Phase A — the store now holds a generalized
    ``(tool_name, arguments)`` ToolIntent keyed by ``action_proposal_id(sig)``
    (the effect-signature anchor), not the 1a ``.env``-only shape. The secret
    ``.env`` body rides ONLY in ``arguments`` (in-memory), never on the durable
    queue row nor the ``description`` (which is the masked operator-facing copy).
    """
    env = grove_home / ".env"
    args = prepare_execute_arguments(
        "propose_governance_change",
        {"target_file": str(env), "content": content, "rationale": "r"},
    )
    sig = canonical_effect_signature("propose_governance_change", args)
    bare = action_proposal_id(sig)          # store key + integrity anchor
    full_pid = f"{RED_PENDING_PROPOSAL_TYPE}:{bare}"
    if description is None:
        # The exact masked string the portal renders — names the key, hides value.
        description, _ = describe_red_action("propose_governance_change", args)
    # capability-mutation-surface-v1 P4 — staged entries SEAL, mirroring the
    # live propose path (unsealed governance claims are legacy_shape).
    from grove.red_pending_store import seal_red_claim
    _sealed = seal_red_claim("propose_governance_change", args)
    get_red_pending_store().put(PendingRedProposal(
        proposal_id=bare, tool_name="propose_governance_change", arguments=args,
        effect_signature=sig, description=description, rationale="r",
        created_at="2026-07-07T00:00:00+00:00",
        target_sha256=_sealed["target_sha256"], writer_name=_sealed["writer_name"],
        writer_payload=_sealed["writer_payload"], sealed_target=_sealed["sealed_target"],
    ))
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=full_pid, type=RED_PENDING_PROPOSAL_TYPE, payload={"zone": "red"},
        evidence=(), eval_hash=bare, created_at="2026-07-07T00:00:00+00:00",
    ))
    return env, bare, full_pid


# ── NONCE ────────────────────────────────────────────────────────────────────
class TestNonce:
    def test_accept_reject_stepjump_expiry(self):
        pid = "governance_env_pending:abc"
        n = red_nonce(pid, "approve", _KEY)
        assert verify_red_nonce(pid, "approve", n, _KEY) is True         # valid
        assert verify_red_nonce(pid, "confirm", n, _KEY) is False        # wrong step
        assert verify_red_nonce("other", "approve", n, _KEY) is False    # wrong id
        # tamper — flip the last hex char to a guaranteed-different value (a fixed
        # "0" is a no-op ~1/16 of the time-buckets when the nonce already ends "0").
        assert verify_red_nonce(
            pid, "approve", n[:-1] + ("1" if n[-1] == "0" else "0"), _KEY
        ) is False
        assert verify_red_nonce(pid, "approve", "", _KEY) is False       # empty
        # expired: a nonce from 2 buckets ago is outside [now, now-1]
        import grove.api.red_nonce as rn
        now = int(__import__("time").time() // rn.RED_NONCE_TTL)
        stale = red_nonce(pid, "approve", _KEY, now - 2)
        assert verify_red_nonce(pid, "approve", stale, _KEY) is False


# ── TWO-STEP + STEP-JUMP + REPLAY ─────────────────────────────────────────────
class TestTwoStep:
    async def test_approve_issues_confirm_no_write(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home)
        n = red_nonce(full_pid, "approve", _KEY)
        r = await client.post(
            f"/portal/actions/proposals/{full_pid}/approve", data={"nonce": n}
        )
        assert r.status == 200
        body = await r.text()
        assert "Confirm RED write" in body            # step-2 card rendered
        assert "/confirm" in body                      # points to the mint route
        assert not env.exists()                        # NO write at step 1
        assert get_red_pending_store().has(bare) is True   # still pending

    async def test_confirm_writes_once(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home, content="HF_TOKEN=hf_real\n")
        cn = red_nonce(full_pid, "confirm", _KEY)
        r = await client.post(
            f"/portal/actions/proposals/{full_pid}/confirm", data={"nonce": cn}
        )
        assert r.status == 200
        assert "Written" in await r.text()
        assert env.read_text() == "HF_TOKEN=hf_real\n"      # written ONCE
        assert get_red_pending_store().has(bare) is False   # consumed

    async def test_bad_approve_nonce_rejected(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home)
        r = await client.post(
            f"/portal/actions/proposals/{full_pid}/approve", data={"nonce": "forged"}
        )
        assert r.status == 403
        assert not env.exists()
        assert get_red_pending_store().has(bare) is True    # untouched

    async def test_step_jump_confirm_without_nonce_rejected(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home)
        r = await client.post(
            f"/portal/actions/proposals/{full_pid}/confirm", data={"nonce": "forged"}
        )
        assert r.status == 403
        assert not env.exists()                             # NO write
        assert get_red_pending_store().has(bare) is True

    async def test_replay_confirm_no_double_write(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home, content="HF_TOKEN=hf_once\n")
        cn = red_nonce(full_pid, "confirm", _KEY)
        r1 = await client.post(
            f"/portal/actions/proposals/{full_pid}/confirm", data={"nonce": cn}
        )
        assert r1.status == 200 and "Written" in await r1.text()
        # replay with a still-valid confirm nonce → claim pop finds nothing
        r2 = await client.post(
            f"/portal/actions/proposals/{full_pid}/confirm", data={"nonce": cn}
        )
        assert r2.status == 200
        # capability-mutation-surface-v1 P4 (M4) — the expired-lie is dead:
        # a replay of a CONSUMED claim reads "Already resolved", never
        # "Expired — re-propose" (T3c).
        _body = await r2.text()
        assert "Already resolved" in _body                  # fail-clean, no re-fire
        assert "Expired — re-propose" not in _body
        assert env.read_text() == "HF_TOKEN=hf_once\n"       # exactly once


# ── ORPHAN + ESCAPING (render) ────────────────────────────────────────────────
class TestRender:
    async def test_orphan_renders_expired(self, client, grove_home):
        env, bare, full_pid = _stage(grove_home)
        get_red_pending_store().pop(bare)   # simulate restart: payload gone, row stays
        r = await client.get("/portal/fragments/proposals/pending")
        body = await r.text()
        assert "expired" in body.lower()
        assert "Approve" not in body        # NO live approve for an orphan
        assert "/confirm" not in body

    async def test_masked_description_escaped(self, client, grove_home):
        # A crafted description with HTML must render escaped (no raw injection).
        _stage(grove_home, description="Persist <script>alert(1)</script> — hidden.")
        r = await client.get("/portal/fragments/proposals/pending")
        body = await r.text()
        assert "<script>alert(1)</script>" not in body     # not raw
        assert "&lt;script&gt;" in body                    # escaped via _esc

    async def test_live_card_masks_value(self, client, grove_home):
        _stage(grove_home, content="HF_TOKEN=SUPERSECRETVALUE\n")
        r = await client.get("/portal/fragments/proposals/pending")
        body = await r.text()
        assert "SUPERSECRETVALUE" not in body              # secret NEVER rendered
        assert "•••• (masked)" in body
        assert "Approve" in body                           # live two-step approve
