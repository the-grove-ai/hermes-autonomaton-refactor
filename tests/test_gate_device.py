"""confirmation-gate-grant-token-v1 P2 — operator device tool + key lifecycle.

Covers the P3 pins folded forward: rendering fidelity (F4), full-id comparison,
tampered fetch, key hygiene (0600 + O_EXCL), signer/verifier round-trip (the
shared-constants proof), registry admin (service-user refuse, duplicate kid, bad
PEM, tmp+rename), and the raw read route.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import grove.gate.admin as admin
import grove.gate.client as client
import grove.gate.keyfile as keyfile
import grove.gate.registry as registry_mod
from grove.eval.proposal_queue import compute_proposal_id
from grove.gate.constants import spki_fingerprint
from grove.gate.registry import AuthorizedKey
from grove.gate.verify import verify_grant_token


def _pub_pem(curve=ec.SECP256R1()):
    priv = ec.generate_private_key(curve)
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


# ═══════════════════ signing-surface render (G1 strengthened) ═══════════════


def test_render_shows_only_digested_fields_omits_annotations():
    """P4/G1: the signing surface shows ONLY type/payload/evidence. Annotation
    fields (semantic_justification, proposer, detail, source_patterns, lease) are
    OMITTED ENTIRELY — no banner, not present at all."""
    record = {
        "type": "model_binding",
        "payload": {"skill": "wiki", "new_binding": {"model": "attacker/evil"}},
        "evidence": ["t1"],
        "semantic_justification": "benign logging tweak",
        "proposer": "fleet-worker",
        "detail": {"x": 1},
        "source_patterns": ["c1"],
        "lease": {"holder": "op"},
    }
    out = client.render_for_approval(record)
    # digested content present:
    assert '"model": "attacker/evil"' in out
    assert "model_binding" in out
    # every annotation absent — and no banner survives:
    for annotation in ("benign logging tweak", "fleet-worker", "source_patterns",
                       "lease", "UNVERIFIED", "banner"):
        assert annotation not in out


# ═════════════════════════ content-digest commitment ════════════════════════


def test_content_digest_is_domain_separated_and_stable():
    from grove.gate.constants import _DIGEST_DOMAIN

    assert _DIGEST_DOMAIN == b"grove-gate-v1:content_digest:"  # pinned literal
    rec = {"type": "dock_mutation", "payload": {"goal": {"id": "g"}},
           "evidence": ["t1"]}
    assert client.content_digest(rec) == client.content_digest(dict(rec))


@pytest.mark.parametrize("payload", [
    {"n": 5},                       # int
    {"n": 5.0},                     # float (distinct from int)
    {"s": "café ☕ 日本語"},          # unicode
    {"nested": {"b": [1, {"c": 2}], "a": True, "z": None}},  # nested + types
])
def test_canonicalization_survives_json_wire(payload):
    """device digest (pre-wire, in-memory) == server digest (post-JSON-wire)."""
    from grove.gate.constants import canonical_digest

    record = {"type": "dock_mutation", "payload": payload, "evidence": ["a", "b"]}
    device = client.content_digest(record)
    # simulate the raw-route JSON wire round-trip the server reloads through:
    server = canonical_digest(json.loads(json.dumps(record)))
    assert device == server


def test_int_and_float_digests_differ():
    from grove.gate.constants import canonical_digest

    a = canonical_digest({"type": "t", "payload": {"n": 5}, "evidence": []})
    b = canonical_digest({"type": "t", "payload": {"n": 5.0}, "evidence": []})
    assert a != b


# ═════════════════════════════ key hygiene ═══════════════════════════════════


def test_mint_writes_0600_and_bundle(tmp_path):
    out = keyfile.mint("op@grove", directory=tmp_path)
    p = Path(out["path"])
    assert p.exists()
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert out["kid"].startswith("gk-")
    assert "BEGIN PUBLIC KEY" in out["public_key_pem"]


def test_mint_refuses_overwrite_o_excl(tmp_path, monkeypatch):
    """A second mint onto an existing key file refuses (O_EXCL). Fixed kid so the
    two mints target the same path."""
    monkeypatch.setattr(keyfile, "derive_kid", lambda pub: "gk-fixed")
    keyfile.mint("op@grove", directory=tmp_path)
    with pytest.raises(keyfile.GateKeyError, match="already exists"):
        keyfile.mint("op2@grove", directory=tmp_path)


def test_group_readable_key_refuses_to_sign(tmp_path):
    out = keyfile.mint("op@grove", directory=tmp_path)
    os.chmod(out["path"], 0o640)  # group-readable
    with pytest.raises(keyfile.GateKeyError, match="unsafe perms"):
        keyfile.load_signing_key(directory=tmp_path)


def test_ambiguous_key_without_kid_refuses(tmp_path, monkeypatch):
    kids = iter(["gk-a", "gk-b"])
    monkeypatch.setattr(keyfile, "derive_kid", lambda pub: next(kids))
    keyfile.mint("op@grove", directory=tmp_path)
    keyfile.mint("op@grove", directory=tmp_path)
    with pytest.raises(keyfile.GateKeyError, match="multiple device keys"):
        keyfile.load_signing_key(directory=tmp_path)


# ═════════════════════════ signer/verifier round-trip ═══════════════════════


def test_client_token_verifies_under_p1_verifier(tmp_path):
    """THE shared-constants proof: a token the client mints verifies under P1's
    verify_grant_token against a fixture registry built from the minted key.
    Both sides bind grove.gate.constants — aud/typ/alg/window/claims agree."""
    out = keyfile.mint("operator@grove", directory=tmp_path)
    priv, kid, operator = keyfile.load_signing_key(directory=tmp_path)
    pid = "sha256:roundtrip"
    digest = client.content_digest(
        {"type": "dock_mutation", "payload": {"goal": {"id": "g"}}, "evidence": ["t1"]}
    )
    token = client.build_token(pid, digest, priv, kid, operator)

    reg = {
        kid: AuthorizedKey(
            kid=kid,
            public_key_pem=out["public_key_pem"],
            operator_identity=operator,
            registered_at="2026-01-01T00:00:00+00:00",
            not_after="2099-01-01T00:00:00+00:00",
        )
    }
    grant = verify_grant_token(token, expected_proposal_id=pid, registry=reg)
    assert grant.kid == kid
    assert grant.proposal_id == pid
    assert grant.content_digest == digest  # commitment carried through
    assert grant.operator_identity == operator
    # Fingerprint agreement (A3 SPKI) across signer key and verifier stamp.
    pub = serialization.load_pem_public_key(out["public_key_pem"].encode())
    assert grant.operator_key_fingerprint == spki_fingerprint(pub)
    # Server-enforced window is exactly MAX_WINDOW (shared constant).
    assert grant.exp - grant.iat == 60


# ═════════════════════════════ registry admin ═══════════════════════════════


@pytest.fixture
def operator_shell(monkeypatch):
    """Simulate an operator (non-service) shell: service user is some other
    account and SUDO_USER is cleared so the invoker is the test user."""
    monkeypatch.setattr(admin, "_service_username", lambda: "grove-svc-acct")
    monkeypatch.delenv("SUDO_USER", raising=False)


def test_add_as_service_user_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "_service_username", lambda: "hermes")
    monkeypatch.setenv("SUDO_USER", "hermes")
    with pytest.raises(admin.GateAdminError, match="service user"):
        admin.add_key("gk-1", _pub_pem(), "op@grove",
                      registry_path=tmp_path / "authorized_keys.yaml")


def test_add_writes_tmp_rename_and_loads(tmp_path, operator_shell, monkeypatch):
    reg = tmp_path / "authorized_keys.yaml"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    entry = admin.add_key("gk-w", _pub_pem(), "op@grove",
                          registry_path=reg, now=now)
    assert reg.exists()
    assert not (tmp_path / "authorized_keys.yaml.tmp").exists()
    # not_after = registered_at + 90d.
    assert entry["not_after"] == datetime(2026, 9, 29, tzinfo=timezone.utc).isoformat()
    # loads under the P1 loader (StrictModes + sudo stubbed for the fixture read).
    monkeypatch.setattr(registry_mod, "_assert_registry_secure", lambda p: None)
    monkeypatch.setattr(registry_mod, "_sudo_capable", lambda: False)
    keys = registry_mod.load_registry(path=reg)
    assert set(keys) == {"gk-w"}


def test_add_duplicate_kid_refused(tmp_path, operator_shell):
    reg = tmp_path / "authorized_keys.yaml"
    admin.add_key("gk-dup", _pub_pem(), "op@grove", registry_path=reg)
    with pytest.raises(admin.GateAdminError, match="already registered"):
        admin.add_key("gk-dup", _pub_pem(), "op@grove", registry_path=reg)


def test_add_bad_pem_refused(tmp_path, operator_shell):
    with pytest.raises(admin.GateAdminError, match="does not parse"):
        admin.add_key("gk-x", "-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n",
                      "op@grove", registry_path=tmp_path / "a.yaml")


def test_add_non_p256_refused(tmp_path, operator_shell):
    p384 = _pub_pem(ec.SECP384R1())
    with pytest.raises(admin.GateAdminError, match="P-256"):
        admin.add_key("gk-384", p384, "op@grove", registry_path=tmp_path / "a.yaml")


def test_revoke_removes_and_missing_refuses(tmp_path, operator_shell):
    reg = tmp_path / "authorized_keys.yaml"
    admin.add_key("gk-r", _pub_pem(), "op@grove", registry_path=reg)
    assert admin.revoke_key("gk-r", registry_path=reg) == 1
    with pytest.raises(admin.GateAdminError, match="no registered key"):
        admin.revoke_key("gk-r", registry_path=reg)


@pytest.mark.parametrize(
    "content",
    [
        "this: is: not: valid: yaml: [",          # unparseable
        "just a scalar string",                    # present, non-mapping
        "- a\n- b\n",                              # present, top-level list
        "some_other_key: 1\n",                     # mapping, no authorized_keys
        "authorized_keys: not-a-list\n",           # authorized_keys wrong type
        "",                                         # present but empty (0 bytes)
    ],
)
def test_add_malformed_registry_refuses_byte_identical(
    tmp_path, operator_shell, content
):
    """A PRESENT-but-unparseable / wrong-shape / empty registry must REFUSE LOUD
    with zero writes — never read-as-empty (which a tmp+rename would then
    silently overwrite). The file is byte-identical after the refusal."""
    reg = tmp_path / "authorized_keys.yaml"
    reg.write_text(content, encoding="utf-8")
    before = reg.read_bytes()
    with pytest.raises(admin.GateAdminError, match="refuse"):
        admin.add_key("gk-new", _pub_pem(), "op@grove", registry_path=reg)
    assert reg.read_bytes() == before  # untouched — no destructive overwrite
    assert not (tmp_path / "authorized_keys.yaml.tmp").exists()


def test_add_absent_registry_creates_first_entry(tmp_path, operator_shell):
    """Absent (not present) → the first registration creates the file."""
    reg = tmp_path / "authorized_keys.yaml"
    assert not reg.exists()
    admin.add_key("gk-first", _pub_pem(), "op@grove", registry_path=reg)
    assert reg.exists()


def test_add_present_empty_list_registry_appends(tmp_path, operator_shell):
    """A well-formed, deliberately-empty registry (authorized_keys: []) appends
    normally — the refusal is for malformed/ambiguous files, not empty lists."""
    reg = tmp_path / "authorized_keys.yaml"
    reg.write_text("authorized_keys: []\n", encoding="utf-8")
    admin.add_key("gk-ok", _pub_pem(), "op@grove", registry_path=reg)
    import grove.gate.registry as _rm

    # loadable with exactly the one appended key.
    import pytest as _pytest  # local, avoid top-level churn
    _mp = _pytest.MonkeyPatch()
    _mp.setattr(_rm, "_assert_registry_secure", lambda p: None)
    _mp.setattr(_rm, "_sudo_capable", lambda: False)
    try:
        assert set(_rm.load_registry(path=reg)) == {"gk-ok"}
    finally:
        _mp.undo()


# ═════════════════════════════ raw read route ═══════════════════════════════


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    (tmp_path / "wiki" / "pages").mkdir(parents=True)
    return tmp_path


@pytest.fixture
async def portal_client(grove_home):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from grove.api import (
        init_substrate_singletons,
        portal_auth_middleware,
        register_portal_routes,
    )

    app = web.Application(middlewares=[portal_auth_middleware])
    init_substrate_singletons(app)
    register_portal_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_raw_route_returns_stored_record(portal_client, grove_home):
    from grove.eval import proposal_queue

    pid = compute_proposal_id(
        type="dock_mutation", payload={"goal": {"id": "g-raw"}}, evidence=("t1",)
    )
    rec = proposal_queue.RoutingProposal(
        proposal_id=pid,
        type="dock_mutation",
        payload={"goal": {"id": "g-raw"}},
        evidence=("t1",),
        eval_hash="h1",
        created_at="2026-07-01T00:00:00Z",
        proposer="detector",
        semantic_justification="should not gate the hash",
    )
    proposal_queue.append(rec)
    r = await portal_client.get(f"/api/substrate/proposals/{pid}/raw")
    assert r.status == 200
    body = await r.json()
    # verbatim stored record — the hashed fields recompute to the SAME id the
    # device tool fetched by (the P2 raw-route contract).
    assert body["type"] == "dock_mutation"
    assert body["payload"] == {"goal": {"id": "g-raw"}}
    assert compute_proposal_id(
        type=body["type"], payload=body["payload"],
        evidence=tuple(body["evidence"]),
    ) == pid


async def test_raw_route_404_unknown(portal_client, grove_home):
    r = await portal_client.get("/api/substrate/proposals/sha256:nope/raw")
    assert r.status == 404
