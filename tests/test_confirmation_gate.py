"""confirmation-gate-grant-token-v1 P1 — server-side verification core.

Covers the SPEC P3 pins pulled forward to gate P1 correctness:
  * happy path (verify → consume → apply → provenance stamp, field-by-field),
  * alg-confusion (HS256-with-public-key, alg=none),
  * window (exp−iat>60, future iat, expired token, expired key),
  * replay (durable across store close/reopen),
  * StrictModes (service-user-writable file/parent refuses loudly),
  * tokenless six-type approve (RED-CLI-only refusal, regression-pinned),
  * wrong-binding (token for A submitted against B).

Token-verification tests pass an explicit ``registry=`` dict so they need no
filesystem anchor; the StrictModes tests exercise the real on-disk gate; the
wire-in tests provision a fixture registry (path monkeypatched, StrictModes
stubbed — a tmp file is owned by the test user and would fail the real gate,
which the dedicated StrictModes tests prove).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import types
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import grove.api.actions as actions
import grove.gate.registry as registry_mod
import grove.red_pending_store as rp
from grove.eval import proposal_queue
from grove.eval.proposal_queue import PROPOSAL_TYPE_DOCK_MUTATION
from grove.gate import (
    GrantVerificationError,
    verify_grant_token,
)
from grove.gate.registry import AuthorizedKey, RegistryError, load_registry
from grove.gate.verify import _AUDIENCE, _TYP

_OPERATOR = "operator@grove.example"


# ── crypto + token helpers ───────────────────────────────────────────────────


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _spki_fp(pub_pem):
    """Expected fingerprint (A3): SHA-256 over DER SubjectPublicKeyInfo."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )

    k = load_pem_public_key(pub_pem.encode("utf-8"))
    der = k.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _install_fake_stat(monkeypatch, *, euid=1001):
    """Install a synthetic os.stat over the /etc/grove/gate anchor chain so the
    StrictModes PASS boundary can be pinned without a real root-owned tree (A2).
    Real os.stat is used for every other path; os.lstat is left real so
    realpath still resolves. Returns (path, resolved, st_map) — mutate st_map to
    perturb a component before calling _assert_registry_secure. Defaults: file
    root:root 0644, all parents root:root 0755, service uid 1001, NOT
    sudo-capable."""
    path = "/etc/grove/gate/authorized_keys.yaml"
    resolved = Path(os.path.realpath(path))
    st_map = {str(resolved): (0, 0, 0o644)}
    for parent in resolved.parents:
        st_map[str(parent)] = (0, 0, 0o755)
    real_stat = os.stat

    def fake_stat(p, *a, **k):
        key = str(p)
        if key in st_map:
            uid, gid, mode = st_map[key]
            return types.SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)
        return real_stat(p, *a, **k)

    monkeypatch.setattr(registry_mod.os, "stat", fake_stat)
    monkeypatch.setattr(registry_mod.os, "geteuid", lambda: euid)
    monkeypatch.setattr(registry_mod.os, "getegid", lambda: euid)
    monkeypatch.setattr(registry_mod.os, "getgroups", lambda: [euid])
    monkeypatch.setattr(registry_mod, "_sudo_capable", lambda: False)
    return path, resolved, st_map


def _authorized_key(kid, pub_pem, *, not_after="2099-01-01T00:00:00+00:00"):
    return AuthorizedKey(
        kid=kid,
        public_key_pem=pub_pem,
        operator_identity=_OPERATOR,
        registered_at="2026-01-01T00:00:00+00:00",
        not_after=not_after,
    )


def _mint(
    priv_pem,
    *,
    kid="k1",
    proposal_id="sha256:abc",
    disposition="approve",
    aud=_AUDIENCE,
    iss=_OPERATOR,
    typ=_TYP,
    alg="ES256",
    iat=None,
    exp=None,
    jti=None,
    sign_key=None,
    claims_override=None,
):
    now = int(time.time())
    iat = now if iat is None else iat
    exp = (now + 30) if exp is None else exp
    claims = {
        "aud": aud,
        "iss": iss,
        "proposal_id": proposal_id,
        "disposition": disposition,
        "jti": jti or uuid.uuid4().hex,
        "iat": iat,
        "exp": exp,
    }
    if claims_override is not None:
        claims = claims_override
    return jwt.encode(
        claims,
        sign_key if sign_key is not None else priv_pem,
        algorithm=alg,
        headers={"kid": kid, "typ": typ},
    )


# ═════════════════════════════════ token verification ════════════════════════


def test_happy_path_verify_returns_grant():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    token = _mint(priv, proposal_id="sha256:p-happy")
    grant = verify_grant_token(
        token, expected_proposal_id="sha256:p-happy", registry=reg
    )
    assert grant.kid == "k1"
    assert grant.proposal_id == "sha256:p-happy"
    assert grant.disposition == "approve"
    assert grant.operator_identity == _OPERATOR
    assert grant.operator_key_fingerprint == _spki_fp(pub)


def _handcraft_hs256(claims, secret_bytes, *, kid="k1", typ=_TYP):
    """Hand-build an HS256 JWT (PyJWT's own encode() refuses a PEM key as an HMAC
    secret, which is the very attack — an attacker uses their own HMAC). This
    reproduces the ES→HS downgrade token our verifier's algorithms=['ES256']
    allow-list must refuse."""
    import base64
    import hashlib
    import hmac

    def _b64(d):
        return base64.urlsafe_b64encode(d).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "kid": kid, "typ": typ}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing_input = header + b"." + payload
    sig = _b64(hmac.new(secret_bytes, signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + sig).decode()


def test_alg_confusion_hs256_with_public_key_refused():
    """The classic ES→HS downgrade: forge an HS256 token using the PEM public
    key as the HMAC secret. The hardcoded algorithms=['ES256'] allow-list must
    refuse it."""
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    claims = {
        "aud": _AUDIENCE, "iss": _OPERATOR, "proposal_id": "sha256:p",
        "disposition": "approve", "jti": "j", "iat": now, "exp": now + 30,
    }
    forged = _handcraft_hs256(claims, pub.encode("utf-8"))
    with pytest.raises(GrantVerificationError):
        verify_grant_token(forged, expected_proposal_id="sha256:p", registry=reg)


def test_alg_none_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, alg="none", sign_key="", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_window_exceeds_server_max_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    tok = _mint(priv, iat=now, exp=now + 61, proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError, match="lifetime"):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_window_boundary_60_accepted():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    tok = _mint(priv, iat=now, exp=now + 60, proposal_id="sha256:p")
    grant = verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)
    assert grant.exp - grant.iat == 60


def test_future_iat_beyond_leeway_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    tok = _mint(priv, iat=now + 3600, exp=now + 3630, proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_expired_token_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    tok = _mint(priv, iat=now - 120, exp=now - 60, proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_expired_key_refused_names_iam_renewal():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub, not_after="2000-01-01T00:00:00+00:00")}
    tok = _mint(priv, proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError, match="IAM"):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_unknown_kid_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, kid="k-unknown", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError, match="[Uu]nknown kid"):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_wrong_typ_header_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, typ="JWT", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError, match="typ"):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_wrong_audience_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, aud="some-other-service", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_wrong_issuer_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, iss="attacker@evil", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_unknown_disposition_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, disposition="reject", proposal_id="sha256:p")
    with pytest.raises(GrantVerificationError, match="[Dd]isposition"):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_missing_required_claim_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    # No jti claim.
    claims = {
        "aud": _AUDIENCE,
        "iss": _OPERATOR,
        "proposal_id": "sha256:p",
        "disposition": "approve",
        "iat": now,
        "exp": now + 30,
    }
    tok = _mint(priv, claims_override=claims)
    with pytest.raises(GrantVerificationError):
        verify_grant_token(tok, expected_proposal_id="sha256:p", registry=reg)


def test_wrong_binding_refused():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    tok = _mint(priv, proposal_id="sha256:proposal-A")
    with pytest.raises(GrantVerificationError, match="different proposal"):
        verify_grant_token(
            tok, expected_proposal_id="sha256:proposal-B", registry=reg
        )


# ═════════════════════════════════ jti consumption ═══════════════════════════


def test_consume_jti_fresh_then_replay(tmp_path):
    store = rp.RedPendingStore(db_path=tmp_path / "red_pending.db")
    assert store.consume_jti("jti-1", "sha256:p", "k1") is True
    assert store.consume_jti("jti-1", "sha256:p", "k1") is False  # replay


def test_consume_jti_durable_across_reopen(tmp_path):
    db = tmp_path / "red_pending.db"
    store = rp.RedPendingStore(db_path=db)
    assert store.consume_jti("jti-durable", "sha256:p", "k1") is True
    del store  # close the handle; state is on disk
    reopened = rp.RedPendingStore(db_path=db)
    # The consumed jti survives a full store close+reopen — replay still refused.
    assert reopened.consume_jti("jti-durable", "sha256:p", "k1") is False


# ═════════════════════════════════ registry / StrictModes ════════════════════


def _reg_yaml(pub_pem, *, extra_fields="", drop=None, dup=False):
    entry = {
        "kid": "k1",
        "public_key_pem": pub_pem,
        "operator_identity": _OPERATOR,
        "registered_at": "2026-01-01T00:00:00+00:00",
        "not_after": "2099-01-01T00:00:00+00:00",
    }
    if drop:
        entry.pop(drop)
    import yaml

    keys = [entry]
    if dup:
        keys.append(dict(entry))
    doc = {"authorized_keys": keys}
    text = yaml.safe_dump(doc, sort_keys=False)
    if extra_fields:
        text = text.replace(
            "- kid: k1", f"- kid: k1\n  {extra_fields}"
        )
    return text


def test_load_registry_absent_refuses(tmp_path):
    with pytest.raises(RegistryError, match="absent"):
        load_registry(path=tmp_path / "nope.yaml")


def test_strictmodes_self_owned_file_refuses(tmp_path):
    """The tmp file is owned by the test (service) user — the owner can rewrite
    or unlink it, so the real StrictModes gate refuses loudly."""
    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub), encoding="utf-8")
    with pytest.raises(RegistryError, match="StrictModes"):
        load_registry(path=f)


def test_strictmodes_world_writable_refuses(tmp_path):
    import os

    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub), encoding="utf-8")
    os.chmod(f, 0o666)  # world-writable
    with pytest.raises(RegistryError, match="StrictModes"):
        load_registry(path=f)


def test_strict_parse_missing_field_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub, drop="not_after"), encoding="utf-8")
    with pytest.raises(RegistryError, match="missing required"):
        load_registry(path=f)


def test_strict_parse_unknown_field_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub, extra_fields="rogue: yes"), encoding="utf-8")
    with pytest.raises(RegistryError, match="unknown field"):
        load_registry(path=f)


def test_strict_parse_duplicate_kid_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub, dup=True), encoding="utf-8")
    with pytest.raises(RegistryError, match="duplicate kid"):
        load_registry(path=f)


def test_strict_parse_good_registry_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    _, pub = _keypair()
    f = tmp_path / "authorized_keys.yaml"
    f.write_text(_reg_yaml(pub), encoding="utf-8")
    keys = load_registry(path=f)
    assert set(keys) == {"k1"}
    assert keys["k1"].operator_identity == _OPERATOR


# ── A1: privilege-posture (sudo capability) ──────────────────────────────────


def test_sudo_capable_service_user_refuses(monkeypatch):
    """A1: modes clean, but the service user is sudo-capable → refuse loudly
    (the wall is a claim without a control). Subprocess never runs — the
    capability probe is monkeypatched True."""
    path, _resolved, _st_map = _install_fake_stat(monkeypatch)
    monkeypatch.setattr(registry_mod, "_sudo_capable", lambda: True)
    with pytest.raises(RegistryError, match="privilege posture"):
        registry_mod._assert_registry_secure(Path(path))


def test_not_sudo_capable_passes(monkeypatch):
    """A1 converse: modes clean AND not sudo-capable → PASS (no raise)."""
    path, _resolved, _st_map = _install_fake_stat(monkeypatch)
    # _install_fake_stat already pins _sudo_capable → False.
    registry_mod._assert_registry_secure(Path(path))


def test_sudo_probe_returns_false_on_missing_binary(monkeypatch):
    """A1 unit: _sudo_capable is fail-open-to-pass on exec failure (missing
    binary / OSError / timeout) — only an affirmative rc==0 is 'capable'."""
    def _boom(*a, **k):
        raise FileNotFoundError("no sudo here")

    monkeypatch.setattr("subprocess.run", _boom)
    assert registry_mod._sudo_capable() is False


# ── A2: StrictModes PASS/REFUSE boundary via faked stat ──────────────────────


def test_strictmodes_faked_root_owned_passes(monkeypatch):
    """A2: root:root 0755 parents + root:root 0644 file → PASS."""
    path, _resolved, _st_map = _install_fake_stat(monkeypatch)
    registry_mod._assert_registry_secure(Path(path))  # no raise


def test_strictmodes_faked_group_writable_parent_refuses(monkeypatch):
    """A2: one group-writable parent (owned by the service group) → REFUSE."""
    path, resolved, st_map = _install_fake_stat(monkeypatch, euid=1001)
    # Immediate parent dir: root-owned but group = service gid, group-writable.
    st_map[str(resolved.parent)] = (0, 1001, 0o775)
    with pytest.raises(RegistryError, match="StrictModes"):
        registry_mod._assert_registry_secure(Path(path))


def test_strictmodes_faked_file_owned_by_service_user_refuses(monkeypatch):
    """A2: the file itself owned by the service user → REFUSE (owner can rewrite
    or unlink regardless of mode)."""
    path, resolved, st_map = _install_fake_stat(monkeypatch, euid=1001)
    st_map[str(resolved)] = (1001, 0, 0o644)
    with pytest.raises(RegistryError, match="StrictModes"):
        registry_mod._assert_registry_secure(Path(path))


# ═════════════════════════════════ provenance stamp ══════════════════════════


def test_grant_provenance_stamp_fields():
    priv, pub = _keypair()
    reg = {"k1": _authorized_key("k1", pub)}
    now = int(time.time())
    tok = _mint(priv, iat=now, exp=now + 30, proposal_id="sha256:p-stamp")
    grant = verify_grant_token(
        tok, expected_proposal_id="sha256:p-stamp", registry=reg
    )
    stamp = actions._grant_provenance_stamp(grant)
    # A3: the stamped fingerprint is the DER-SPKI SHA-256, self-consistent with
    # the grant AND equal to the independently computed SPKI fingerprint.
    assert stamp["operator_key_fingerprint"] == grant.operator_key_fingerprint
    assert stamp["operator_key_fingerprint"] == _spki_fp(pub)
    assert stamp["token_jti"] == grant.jti
    assert stamp["token_iat"] == grant.iat
    assert stamp["token_exp"] == grant.exp
    assert stamp["proposal_id"] == "sha256:p-stamp"
    assert stamp["verification_method"] == "es256-jwt-v1"
    assert "approved_at" in stamp


# ═════════════════════════════════ wire-in (end to end) ══════════════════════


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    # Reset the process-level red_pending_store singleton so consume_jti binds to
    # THIS test's GROVE_HOME db, not a prior test's (torn-down) tmp dir.
    monkeypatch.setattr(rp, "_STORE", None)
    return tmp_path


@pytest.fixture
def provisioned_registry(tmp_path, monkeypatch):
    """A loadable fixture registry: path monkeypatched, StrictModes stubbed (a
    test-user-owned tmp file cannot pass the real gate — that is proven by the
    dedicated StrictModes tests)."""
    priv, pub = _keypair()
    reg_file = tmp_path / "gate_authorized_keys.yaml"
    reg_file.write_text(_reg_yaml(pub), encoding="utf-8")
    monkeypatch.setattr(registry_mod, "_REGISTRY_PATH", reg_file)
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    return priv, pub


@pytest.fixture
async def client(grove_home):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from grove.api import (
        init_substrate_singletons,
        portal_auth_middleware,
        register_portal_routes,
    )
    from grove.api.actions import register_action_routes
    from grove.api.fragments import _PORTAL_ASSETS, register_fragment_routes

    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    app.router.add_static("/portal/static", str(_PORTAL_ASSETS))
    register_fragment_routes(app)
    register_action_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


def _append_dock_mutation(pid):
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=pid,
        type=PROPOSAL_TYPE_DOCK_MUTATION,
        payload={"goal": {"id": "g-gate", "name": "Gate Test", "status": "staging"}},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
    ))


async def test_wire_tokenless_scope_defining_refused(client, grove_home):
    """Regression pin: a six-type approve WITHOUT a token keeps the current
    guarantee — 422, refused card, RED CLI named, proposal untouched in queue."""
    pid = f"{PROPOSAL_TYPE_DOCK_MUTATION}:tokenless"
    _append_dock_mutation(pid)
    r = await client.post(f"/portal/actions/proposals/{pid}/approve")
    assert r.status == 422
    body = await r.text()
    assert "refused" in body and "scope-defining" in body
    assert "flywheel approve" in body
    assert proposal_queue.read(pid) is not None  # not applied


async def test_wire_valid_token_applies_and_stamps(
    client, grove_home, provisioned_registry, monkeypatch
):
    priv, pub = provisioned_registry
    pid = f"{PROPOSAL_TYPE_DOCK_MUTATION}:applied"
    _append_dock_mutation(pid)
    token = _mint(priv, proposal_id=pid)

    captured = {}
    real = actions._record_kaizen_disposition

    def _spy(proposal, **kw):
        captured.update(kw)
        return real(proposal, **kw)

    monkeypatch.setattr(actions, "_record_kaizen_disposition", _spy)

    r = await client.post(
        f"/portal/actions/proposals/{pid}/approve", data={"token": token}
    )
    assert r.status == 200
    body = await r.text()
    assert "refused" not in body

    # Applied: the proposal left the queue and the machine dock file was written.
    assert proposal_queue.read(pid) is None
    assert (grove_home / "dock" / "dock.autonomaton.yaml").exists()

    # R-6 stamp — field-by-field on BOTH the applied_result and the event extra.
    fp = _spki_fp(pub)  # A3: DER-SPKI fingerprint
    applied = captured["applied_result"]
    extra = captured["extra"]
    for blob in (applied, extra):
        assert blob["operator_key_fingerprint"] == fp
        assert blob["token_jti"]  # present
        assert blob["proposal_id"] == pid
        assert blob["verification_method"] == "es256-jwt-v1"
        assert "token_iat" in blob and "token_exp" in blob
        assert "approved_at" in blob
    # applied_result also still carries the dock writer's own fields.
    assert applied["goal_id"] == "g-gate"


async def test_wire_replay_refused_durable(
    client, grove_home, provisioned_registry
):
    priv, _ = provisioned_registry
    pid = f"{PROPOSAL_TYPE_DOCK_MUTATION}:replay"
    _append_dock_mutation(pid)
    token = _mint(priv, proposal_id=pid, jti="fixed-replay-jti")

    r1 = await client.post(
        f"/portal/actions/proposals/{pid}/approve", data={"token": token}
    )
    assert r1.status == 200

    # Re-queue the same proposal id so the ONLY thing stopping a second apply is
    # the burned jti (not the already-dequeued proposal).
    _append_dock_mutation(pid)
    # Prove durability: drop the in-process store handle so the second submit
    # reads the consumed-jti row back from disk.
    rp._STORE = None
    r2 = await client.post(
        f"/portal/actions/proposals/{pid}/approve", data={"token": token}
    )
    assert r2.status == 409
    body = await r2.text()
    assert "already used" in body
    assert proposal_queue.read(pid) is not None  # second apply did NOT run


async def test_wire_wrong_binding_refused(
    client, grove_home, provisioned_registry
):
    priv, _ = provisioned_registry
    pid_a = f"{PROPOSAL_TYPE_DOCK_MUTATION}:bind-A"
    pid_b = f"{PROPOSAL_TYPE_DOCK_MUTATION}:bind-B"
    _append_dock_mutation(pid_b)
    token_for_a = _mint(priv, proposal_id=pid_a)  # bound to A
    r = await client.post(
        f"/portal/actions/proposals/{pid_b}/approve", data={"token": token_for_a}
    )
    assert r.status == 403
    body = await r.text()
    assert "rejected" in body
    assert proposal_queue.read(pid_b) is not None


async def test_wire_non_scope_type_unaffected_by_token_gate(
    client, grove_home, monkeypatch
):
    """A non-scope-defining type (routing_adjustment) approves with NO token and
    is NOT touched by the token gate. Handler stubbed (as in test_routing_approve)
    so the assertion is about the gate, not the routing apply environment."""
    class _Spy:
        apply_label_prefix = ""
        requires_source_patterns = False

        def apply_callback(self, proposal, *, machine_path=None):
            return ("stub-target", {"ok": True})

    monkeypatch.setattr(actions, "_handler_for", lambda t: _Spy())

    pid = "routing_adjustment:plain"
    proposal_queue.append(proposal_queue.RoutingProposal(
        proposal_id=pid,
        type="routing_adjustment",
        payload={"rule": "downward", "add_intents": ["greet"]},
        evidence=("turn_1",),
        eval_hash="hash1",
        created_at="2026-06-26T00:00:00Z",
        source_patterns=("cluster_1",),
    ))
    r = await client.post(f"/portal/actions/proposals/{pid}/approve")
    # 200 with no token — routing_adjustment is not in the refused six, so the
    # gate never engaged (no 422/403 refusal).
    assert r.status == 200
    assert "refused" not in (await r.text())
    assert proposal_queue.read(pid) is None
