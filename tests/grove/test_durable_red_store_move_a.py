"""durable-red-store-v1 Move A — durability + exactly-once proofs.

The SQLite store is now the SOLE source of truth (the in-memory ``_by_id`` map is
retired). This suite proves the two properties the durable backing exists for:

  * RESTART-SURVIVAL — a pending proposal written by one store instance resolves +
    approves through a genuinely fresh instance on the same on-disk DB (a gateway
    restart drops the process singleton but NOT the payload).
  * DOUBLE-CLAIM EXACTLY-ONCE — two approves of one proposal_id: exactly one
    executes; the second gets zero rows back from the ``DELETE … RETURNING`` claim.

Plus: the secret-bearing DB is owner-only (0600), and the Phase-B classification
metadata (is_opaque / pattern_key) survives the durable boundary — the reason the
schema carries those two columns.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from grove.effect_signature import canonical_effect_signature
from grove.red_pending_store import (
    PendingRedProposal,
    action_proposal_id,
    approve_red_proposal,
    get_red_pending_store,
    prepare_execute_arguments,
)


@pytest.fixture(autouse=True)
def _grove_home(tmp_path, monkeypatch):
    """Redirect $GROVE_HOME to a tmp dir (never touch the real ~/.grove) and reset
    the store singleton so each test opens a fresh on-disk DB."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)
    return tmp_path


def _reset_singleton(monkeypatch):
    """Simulate a gateway restart: drop the process handle; the on-disk DB stays."""
    import grove.red_pending_store as rps

    monkeypatch.setattr(rps, "_STORE", None)


def _propose(env: Path, content: str) -> tuple[str, dict, str]:
    args = prepare_execute_arguments(
        "propose_governance_change",
        {"target_file": str(env), "content": content, "rationale": "r"},
    )
    sig = canonical_effect_signature("propose_governance_change", args)
    return action_proposal_id(sig), args, sig


def test_restart_survival_masked_description_and_approve(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    pid, args, sig = _propose(env, "HF_TOKEN=hf_restart\n")
    s1 = get_red_pending_store()
    s1.put(PendingRedProposal(
        proposal_id=pid, tool_name="propose_governance_change", arguments=args,
        effect_signature=sig,
        description="Persist credential(s) to ~/.grove/.env: HF_TOKEN — values hidden.",
        rationale="r", created_at="2026-07-08T00:00:00+00:00",
    ))

    _reset_singleton(monkeypatch)
    s2 = get_red_pending_store()
    assert s2 is not s1                              # genuinely fresh instance
    assert s2.masked_description(pid) is not None     # payload SURVIVED the "restart"

    res = approve_red_proposal(pid, s2)               # approve through the new instance
    assert res["success"] is True and res["reason"] == "written", res
    assert env.read_text() == "HF_TOKEN=hf_restart\n"
    assert s2.has(pid) is False                        # consumed


def test_double_claim_exactly_once(tmp_path):
    env = tmp_path / ".env"
    pid, args, sig = _propose(env, "TOK=once\n")
    store = get_red_pending_store()
    store.put(PendingRedProposal(
        proposal_id=pid, tool_name="propose_governance_change", arguments=args,
        effect_signature=sig, description="d", rationale="r",
        created_at="2026-07-08T00:00:00+00:00",
    ))

    r1 = approve_red_proposal(pid, store)              # first claim wins
    r2 = approve_red_proposal(pid, store)              # second claim finds zero rows

    assert r1["success"] is True and r1["reason"] == "written", r1
    assert r2["success"] is False and r2["reason"] == "not_found", r2
    assert env.read_text() == "TOK=once\n"             # written EXACTLY once
    assert store.has(pid) is False


def test_db_and_sidecars_are_owner_only(tmp_path):
    env = tmp_path / ".env"
    pid, args, sig = _propose(env, "K=v\n")
    store = get_red_pending_store()
    store.put(PendingRedProposal(                      # forces WAL/SHM creation
        proposal_id=pid, tool_name="propose_governance_change", arguments=args,
        effect_signature=sig, description="d", rationale="r",
        created_at="2026-07-08T00:00:00+00:00",
    ))
    saw_db = False
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(store.path) + suffix)
        if p.exists():
            mode = stat.S_IMODE(os.stat(p).st_mode)
            assert mode == 0o600, (p.name, oct(mode))  # NOT world/group-readable
            saw_db = saw_db or suffix == ""
    assert saw_db, "expected the DB file to exist"


def test_classification_metadata_survives_restart(tmp_path, monkeypatch):
    # The reason the schema carries is_opaque + pattern_key: the Phase-B OPAQUE
    # banner + per-type card title must resolve from SQLite (the sole SoT now).
    args = {"command": "echo $(whoami)"}
    sig = canonical_effect_signature("terminal", args)
    pid = action_proposal_id(sig)
    get_red_pending_store().put(PendingRedProposal(
        proposal_id=pid, tool_name="terminal", arguments=args, effect_signature=sig,
        description="Opaque dynamic command — effect not statically resolved.",
        rationale="r", created_at="2026-07-08T00:00:00+00:00",
        is_opaque=True, pattern_key="opacity:substitution",
    ))

    _reset_singleton(monkeypatch)
    s2 = get_red_pending_store()
    assert s2.is_opaque(pid) is True                   # OPAQUE banner survives
    assert s2.card_title(pid) == "RED — opaque command"
    got = s2.get(pid)
    assert got.pattern_key == "opacity:substitution" and got.is_opaque is True
