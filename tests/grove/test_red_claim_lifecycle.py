"""capability-mutation-surface-v1 T3 — red claim lifecycle (banked FAILING).

CONTRACT under test (P2+ implementation target; Gate-A divergence 2 is the
defect): the approve-claim on a pending RED proposal must not be destroyed by
an execution failure.

  (a) approved claim -> writer raises/fails -> the claim SURVIVES in the store
      with the error surfaced -> a retry approve succeeds -> THEN popped.
      (Today: ``st.pop`` at grove/red_pending_store.py:512 consumes the entry
      BEFORE dispatch at :564 — "single-use ... cannot re-fire" :566.)
  (b) approved claim whose TARGET FILE drifted since propose-time -> loud CAS
      invalidation, claim withdrawn with a reason. CONTRACT: PendingRedProposal
      carries ``target_sha256`` (propose-time content hash of the write
      target, None for non-file effects); approve recomputes and refuses with
      reason ``"target_drift"`` on mismatch, withdrawing the claim.
  (c) success -> popped -> replay approve returns ``not_found`` and NO surface
      renders the misleading expiry copy ("Expired — re-propose") for a
      replay of a COMPLETED action.
  (d) the payload the portal renders for operator review byte-equals the
      payload the executor dispatches. CONTRACT: ``RedPendingStore.
      rendered_payload(proposal_id)`` returns exactly the content string the
      approve path re-dispatches.

Hermetic: store lives on a per-test tmp SQLite db; the approval registry's
dispatch is monkeypatched (no builtin tool registration, no real writes).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import grove
from grove.effect_signature import canonical_effect_signature
from grove.red_pending_store import (
    PendingRedProposal,
    RedPendingStore,
    action_proposal_id,
    approve_red_proposal,
    prepare_execute_arguments,
)


def _hermetic_store(tmp_path: Path) -> RedPendingStore:
    return RedPendingStore(db_path=tmp_path / "red_pending.db")


def _entry_for(tool_name: str, arguments: dict, **extra) -> PendingRedProposal:
    prepared = prepare_execute_arguments(tool_name, dict(arguments))
    sig = canonical_effect_signature(tool_name, prepared)
    return PendingRedProposal(
        proposal_id=action_proposal_id(sig),
        tool_name=tool_name,
        arguments=prepared,
        effect_signature=sig,
        description="test red action",
        rationale="lifecycle test",
        created_at="2026-07-21T00:00:00+00:00",
        **extra,
    )


def _neutralize_registry(monkeypatch, dispatch_fn):
    """Route approve_red_proposal's isolated registry to a controlled fake:
    no builtin registration, get_entry always resolves, dispatch is ours."""
    import tools.registry as registry_mod

    monkeypatch.setattr(registry_mod, "register_builtin_tools", lambda r: None)
    monkeypatch.setattr(
        registry_mod.ToolRegistry, "get_entry", lambda self, name: object()
    )
    monkeypatch.setattr(
        registry_mod.ToolRegistry,
        "dispatch",
        lambda self, name, args: dispatch_fn(name, args),
    )


def test_a_failed_execution_preserves_claim_then_retry_succeeds(
    tmp_path, monkeypatch
):
    store = _hermetic_store(tmp_path)
    entry = _entry_for(
        "write_file", {"path": str(tmp_path / "t.txt"), "content": "payload"}
    )
    store.put(entry)
    pid = entry.proposal_id

    # First approval: the writer fails loudly.
    _neutralize_registry(
        monkeypatch, lambda n, a: json.dumps({"error": "disk full"})
    )
    result = approve_red_proposal(pid, store=store)
    assert result["success"] is False
    assert result["reason"] == "execute_error"

    # CONTRACT (a): the claim SURVIVES an execution failure for retry.
    assert store.has(pid), (
        "CONTRACT: an approved claim whose execution FAILED must survive in "
        "the store for retry — today it is popped before dispatch and the "
        "failure destroys it (pop@512 / dispatch@564)"
    )

    # Retry: the writer now succeeds; the claim completes and is popped.
    _neutralize_registry(
        monkeypatch, lambda n, a: json.dumps({"success": True})
    )
    retry = approve_red_proposal(pid, store=store)
    assert retry["success"] is True, (
        f"CONTRACT: retry after transient failure must succeed, got {retry!r}"
    )
    assert not store.has(pid), "claim must be consumed after SUCCESSFUL execution"


def test_b_target_drift_invalidates_claim_loudly(tmp_path, monkeypatch):
    # CONTRACT (b) guard: the propose-time target-content anchor field.
    field_names = {f.name for f in dataclasses.fields(PendingRedProposal)}
    assert "target_sha256" in field_names, (
        "CONTRACT: PendingRedProposal must carry `target_sha256` (propose-time "
        "content hash of the write target) for the approve-time CAS check"
    )

    import hashlib

    target = tmp_path / "governed.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    propose_time_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    store = _hermetic_store(tmp_path)
    entry = _entry_for(
        "write_file",
        {"path": str(target), "content": "replacement: true\n"},
        target_sha256=propose_time_hash,
    )
    store.put(entry)
    pid = entry.proposal_id

    # Drift the target AFTER propose, BEFORE approve.
    target.write_text("drifted: true\n", encoding="utf-8")

    _neutralize_registry(
        monkeypatch, lambda n, a: json.dumps({"success": True})
    )
    result = approve_red_proposal(pid, store=store)
    assert result["success"] is False, (
        "CONTRACT: approval over a drifted target must refuse, not write"
    )
    assert result["reason"] == "target_drift", (
        f"CONTRACT: drift refusal reason must be 'target_drift', got "
        f"{result.get('reason')!r}"
    )
    assert "drift" in str(result.get("error", "")).lower(), (
        "CONTRACT: the CAS invalidation must be LOUD — error names the drift"
    )
    # Claim withdrawn WITH reason: the store no longer holds a live claim.
    assert not store.has(pid), (
        "CONTRACT: a drift-invalidated claim is withdrawn, not left approvable"
    )


def test_c_replay_after_success_is_not_found_without_expiry_copy(
    tmp_path, monkeypatch
):
    store = _hermetic_store(tmp_path)
    entry = _entry_for(
        "write_file", {"path": str(tmp_path / "t.txt"), "content": "x"}
    )
    store.put(entry)
    pid = entry.proposal_id

    _neutralize_registry(
        monkeypatch, lambda n, a: json.dumps({"success": True})
    )
    first = approve_red_proposal(pid, store=store)
    assert first["success"] is True
    assert not store.has(pid)

    replay = approve_red_proposal(pid, store=store)
    assert replay["success"] is False
    assert replay["reason"] == "not_found"
    err = str(replay.get("error", ""))
    assert "Expired" not in err and "re-propose" not in err, (
        "store-level replay copy must not claim expiry for a COMPLETED action"
    )

    # CONTRACT (c), portal layer: the confirm surface must not render the
    # misleading "Expired — re-propose. Nothing was written." copy on the
    # not_found/replay branch (grove/api/actions.py:742 today) — a replayed
    # confirm of an already-executed action is "already resolved", and after
    # (a)/(b) land, not_found never means "silently lost". Source-level pin,
    # same idiom as the writer-conformance basename scan.
    actions_src = (
        Path(grove.__file__).resolve().parents[1] / "grove" / "api" / "actions.py"
    ).read_text(encoding="utf-8")
    assert "Expired — re-propose" not in actions_src, (
        "CONTRACT: grove/api/actions.py still renders 'Expired — re-propose' "
        "on the confirm not_found branch — replay of a completed action must "
        "not read as expiry ('Nothing was written' is false there)"
    )


def test_d_portal_rendered_payload_byte_equals_dispatched_payload(tmp_path):
    store = _hermetic_store(tmp_path)
    content = "canonical payload bytes — exact\n"
    entry = _entry_for(
        "write_file", {"path": str(tmp_path / "t.txt"), "content": content}
    )
    store.put(entry)

    assert hasattr(store, "rendered_payload"), (
        "CONTRACT: RedPendingStore.rendered_payload(proposal_id) must expose "
        "the exact payload the operator reviews, so portal render and "
        "executor dispatch can never diverge"
    )
    rendered = store.rendered_payload(entry.proposal_id)
    assert rendered == entry.arguments["content"], (
        "CONTRACT: portal-rendered payload must byte-equal the "
        "executor-dispatched payload"
    )


class TestGovernedCardLegibility:
    """P7 micro-arc live Andon 3: the pending card rendered an admission
    claim with the .env credential template ("values hidden") — the operator
    could not see what they were approving. The credential mask is for .env
    ONLY; every other governed body renders target + bounded payload."""

    def test_describe_admission_claim_shows_payload_not_credential_mask(
        self, tmp_path
    ):
        from grove.red_pending_store import describe_red_action

        target = str(tmp_path / "capabilities" / "state" / "browser_read.yaml")
        body = "id: browser_read\nintents:\n- research\ntiers:\n- 2\n- 3\n"
        desc, is_opaque = describe_red_action(
            "propose_governance_change",
            {"target_file": target, "content": body, "rationale": "r"},
        )
        assert not is_opaque
        assert target in desc and "intents" in desc and "research" in desc
        assert "hidden" not in desc.lower()
        assert "credential" not in desc.lower()

    def test_describe_env_claim_stays_masked(self, tmp_path):
        from grove.red_pending_store import describe_red_action

        desc, _ = describe_red_action(
            "propose_governance_change",
            {
                "target_file": str(tmp_path / ".env"),
                "content": "HF_TOKEN=hf_secret_value\n", "rationale": "r",
            },
        )
        assert "hf_secret_value" not in desc
        assert "hidden" in desc.lower()

    def test_is_credential_write_keys_on_target_not_tool(self, tmp_path):
        from grove.effect_signature import canonical_effect_signature
        from grove.red_pending_store import (
            PendingRedProposal,
            RedPendingStore,
            action_proposal_id,
            prepare_execute_arguments,
            seal_red_claim,
        )

        store = RedPendingStore(db_path=tmp_path / "red.db")

        def _stage(target, body):
            args = prepare_execute_arguments("propose_governance_change", {
                "target_file": str(target), "content": body, "rationale": "r",
            })
            sealed = seal_red_claim("propose_governance_change", args)
            sig = canonical_effect_signature("propose_governance_change", args)
            e = PendingRedProposal(
                proposal_id=action_proposal_id(sig),
                tool_name="propose_governance_change", arguments=args,
                effect_signature=sig, description="d", rationale="r",
                created_at="2026-07-21T00:00:00+00:00", **sealed,
            )
            store.put(e)
            return e.proposal_id

        env_pid = _stage(tmp_path / ".env", "TOK=x\n")
        adm_pid = _stage(
            tmp_path / "capabilities" / "state" / "browser_read.yaml",
            "id: browser_read\nintents:\n- research\n",
        )
        assert store.is_credential_write(env_pid) is True
        assert store.is_credential_write(adm_pid) is False
