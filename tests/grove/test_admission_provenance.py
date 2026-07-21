"""capability-mutation-surface-v1 T6 — provenance stamp (banked FAILING).

Gate P0 ruling A-3: every admission-field state write carries a required
``provenance`` block — exactly ``{approval_id, timestamp, surface,
write_class}``, all non-empty strings — and the sanctioned writer refuses
stampless or partially-stamped scope-defining writes. The loader
(``_read_state_file``) must round-trip the block (allowlist admission is
asserted in T4; here the shape survives a full write -> read cycle).

Hermetic: per-test tmp state dirs only.
"""

from __future__ import annotations

import pytest
import yaml

import grove.capability_registry as capreg

_STAMP = {
    "approval_id": "red-1234abcd",
    "timestamp": "2026-07-21T12:00:00+00:00",
    "surface": "portal_confirm",
    "write_class": "capability_admission",
}


def _writer():
    fn = getattr(capreg, "write_admission_state", None)
    assert fn is not None, (
        "CONTRACT: sanctioned admission writer "
        "grove.capability_registry.write_admission_state is not implemented "
        "(see T1 pin / T4 semantics)"
    )
    return fn


def test_writer_emits_full_provenance_stamp(tmp_path):
    writer = _writer()
    writer(
        "browser_read", intents=["research_request"],
        provenance=dict(_STAMP), state_dir=tmp_path,
    )
    files = sorted(tmp_path.glob("*.yaml"))
    assert len(files) == 1
    doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert doc.get("provenance") == _STAMP, (
        f"CONTRACT: the emitted stamp must round-trip exactly; got "
        f"{doc.get('provenance')!r}"
    )


@pytest.mark.parametrize("missing_key", sorted(_STAMP))
def test_writer_rejects_partial_stamp(tmp_path, missing_key):
    writer = _writer()
    partial = {k: v for k, v in _STAMP.items() if k != missing_key}
    with pytest.raises(ValueError):
        writer(
            "browser_read", intents=["research_request"],
            provenance=partial, state_dir=tmp_path,
        )
    assert not list(tmp_path.glob("*.yaml")), (
        f"a stamp missing {missing_key!r} must be refused with no file written"
    )


def test_loader_roundtrips_provenance_block(tmp_path):
    state = tmp_path / "browser_read.yaml"
    state.write_text(
        yaml.safe_dump(
            {
                "id": "browser_read",
                "intents": ["research_request"],
                "tiers": [3],
                "provenance": dict(_STAMP),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    try:
        rid, doc = capreg._read_state_file(state)
    except capreg._StateFileInvalid as exc:
        pytest.fail(
            "CONTRACT: _read_state_file must admit the canonical admission "
            f"keys (intents/tiers/provenance); it raised: {exc}"
        )
    assert rid == "browser_read"
    assert doc["provenance"] == _STAMP


def test_admission_executor_emits_written_ledger_event(tmp_path, monkeypatch):
    """P7 live-verify pin: the admission adapter emits the paired
    disposition="written" governance_change event (item-5 ledger continuity
    — every governed writer, not just env/routing/dock)."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("GROVE_SESSION_ID", "admission_ledger_pin")
    from grove.effect_signature import canonical_effect_signature
    from grove.kaizen_ledger import KaizenLedger
    from grove.red_pending_store import (
        PendingRedProposal,
        RedPendingStore,
        action_proposal_id,
        approve_red_proposal,
        prepare_execute_arguments,
        seal_red_claim,
    )

    target = tmp_path / "capabilities" / "state" / "browser_read.yaml"
    body = "id: browser_read\nintents:\n- research\ntiers:\n- 2\n- 3\n"
    args = prepare_execute_arguments("propose_governance_change", {
        "target_file": str(target), "content": body, "rationale": "pin",
    })
    sealed = seal_red_claim("propose_governance_change", args)
    assert sealed["writer_name"] == "write_admission_state"
    sig = canonical_effect_signature("propose_governance_change", args)
    store = RedPendingStore(db_path=tmp_path / "red.db")
    entry = PendingRedProposal(
        proposal_id=action_proposal_id(sig),
        tool_name="propose_governance_change", arguments=args,
        effect_signature=sig, description="d", rationale="r",
        created_at="2026-07-21T00:00:00+00:00", **sealed,
    )
    store.put(entry)
    res = approve_red_proposal(entry.proposal_id, store=store)
    assert res["success"] is True, res
    events = KaizenLedger("admission_ledger_pin").events_by_type(
        "governance_change"
    )
    written = [e for e in events if e.get("disposition") == "written"]
    assert len(written) == 1
    assert written[0]["approval_id"] == entry.proposal_id
