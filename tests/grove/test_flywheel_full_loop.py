"""B3 flywheel-surface-v1 — the full-loop integration gate.

The end-to-end Flywheel loop, ZERO mocks, via routing_adjustment:
  observe (IntentStore) → detect (run_tier_ratchet_scan) → propose (queue, with
  source_patterns cluster) → approve (the B1 registry gate) → apply (the REAL
  routing.autonomaton.yaml write).

Proven through BOTH entrypoints — the CLI (cli_approve) AND the surface-agnostic
tool (approve_proposal) — to show the operator-drivable loop closes, not just the
CLI. Plus: the tool routes through (does not bypass) the B2 no-cluster gate; the
zones are correct; and the C2 repoint mini-loop (skill_synthesis → approve via
the tool → .andon/ + proposed record).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)
from grove.intent_store import IntentRecord, IntentStore
from tools.flywheel_review_tool import (
    approve_proposal,
    reject_proposal,
    review_proposals,
)


def _r(*, intent_class, outcome, idx):
    return IntentRecord(
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        session_id=f"s{idx % 3}",
        turn_id=f"t_{intent_class}_{idx}",
        user_message_stem="probe",
        pattern_hash="a" * 64,
        intent_class=intent_class,
        register_class="technical",
        complexity_signal="moderate",
        confidence=0.9,
        outcome=outcome,
        tier_selected="T1",
    )


def _seed_correction_spike(store, intent_class="date_arithmetic"):
    """5 turns, 3 corrections (rate 0.6 ≥ 0.30) → one upward routing_adjustment."""
    for idx, outcome in enumerate(
        ["success", "success", "correction", "correction", "correction"]
    ):
        store.append(_r(intent_class=intent_class, outcome=outcome, idx=idx))


def _detect_one(store, queue):
    """Run the detector; return the single queued routing_adjustment."""
    new, dup = flywheel_cli.run_tier_ratchet_scan(
        store=store, current_routing_rules={}, queue_path=queue,
    )
    assert (new, dup) == (1, 0)
    proposals = read_all(path=queue)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.type == PROPOSAL_TYPE_ROUTING_ADJUSTMENT
    assert p.source_patterns  # cluster bound (B2)
    return p


def _assert_machine_has_intent(machine: Path, intent_class: str) -> None:
    cfg = yaml.safe_load(machine.read_text(encoding="utf-8"))
    intents = cfg["routing"]["routing_rules"]["ratchet_promoted_t3"]["match"]["intents"]
    assert intent_class in intents


# ── the full loop, via the CLI (zero mocks) ──────────────────────────


def test_full_loop_via_cli_approve(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "routing.autonomaton.yaml"
    store = IntentStore(store_path=tmp_path / "intent.jsonl")

    _seed_correction_spike(store)
    p = _detect_one(store, queue)

    rc = flywheel_cli.cli_approve(
        p.proposal_id, queue_path=queue, machine_path=machine,
    )
    assert rc == 0
    assert machine.exists()
    _assert_machine_has_intent(machine, "date_arithmetic")
    assert read_all(path=queue) == []


# ── the SAME loop, via the surface-agnostic tool ─────────────────────


def test_full_loop_via_approve_proposal_tool(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "routing.autonomaton.yaml"
    store = IntentStore(store_path=tmp_path / "intent.jsonl")

    _seed_correction_spike(store)
    p = _detect_one(store, queue)

    result = json.loads(
        approve_proposal(p.proposal_id, queue_path=queue, machine_path=machine)
    )
    assert result["success"] is True
    assert machine.exists()
    _assert_machine_has_intent(machine, "date_arithmetic")
    assert read_all(path=queue) == []


def test_review_proposals_lists_pending(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    store = IntentStore(store_path=tmp_path / "intent.jsonl")
    _seed_correction_spike(store)
    _detect_one(store, queue)

    result = json.loads(review_proposals(queue_path=queue))
    assert result["success"] is True
    assert result["pending_count"] == 1
    # Reuses flywheel_cli._format_summary — the line carries type + body.
    assert "routing_adjustment" in result["proposals"][0]
    assert "date_arithmetic" in result["proposals"][0]


def test_reject_proposal_tool_removes(tmp_path: Path) -> None:
    queue = tmp_path / "proposals.jsonl"
    store = IntentStore(store_path=tmp_path / "intent.jsonl")
    _seed_correction_spike(store)
    p = _detect_one(store, queue)

    result = json.loads(reject_proposal(p.proposal_id, queue_path=queue))
    assert result["success"] is True
    assert read_all(path=queue) == []


# ── the tool ROUTES through the gate, never bypasses it ──────────────


def test_tool_respects_b2_no_cluster_gate(tmp_path: Path) -> None:
    """A routing_adjustment with empty source_patterns is refused at the gate —
    the tool surfaces the refusal, it does not force the apply."""
    queue = tmp_path / "proposals.jsonl"
    machine = tmp_path / "routing.autonomaton.yaml"
    payload = {"rule": "upward", "add_intents": ["x"]}
    p = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=("t1",),
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=("t1",),
        eval_hash="", created_at="2026-06-01T00:00:00+00:00",
        source_patterns=(),  # empty — the B2 gate must refuse
    )
    append(p, path=queue)
    result = json.loads(
        approve_proposal(p.proposal_id, queue_path=queue, machine_path=machine)
    )
    assert result["success"] is False
    assert "no source_patterns" in result["message"]
    assert len(read_all(path=queue)) == 1  # retained, non-destructive
    assert not machine.exists()


# ── zones: the tool is itself governed ───────────────────────────────


def test_tool_zones_are_governed() -> None:
    import grove.zones as z

    repo = Path(__file__).resolve().parents[2] / "config" / "zones.schema.yaml"
    z.initialize(schema_path=repo)
    assert z.classify("review_proposals").zone == "green"   # read-only
    assert z.classify("approve_proposal").zone == "yellow"  # governed self-mod
    assert z.classify("reject_proposal").zone == "yellow"


# ── C2 repoint mini-loop: synthesis → approve via the tool ───────────

_VALID_SKILL_MD = (
    "---\n"
    "name: prep-meeting-brief\n"
    "description: Pull a GitHub repo's recent activity before a meeting.\n"
    "---\n"
    "## When to use\n"
    "Before a meeting about {repo}.\n\n"
    "## Procedure\n"
    "1. Fetch {repo} activity.\n"
    "2. Summarize it.\n"
)


def test_synthesis_offer_approves_through_the_tool() -> None:
    """The repointed offer's target: a skill_synthesis proposal approved via the
    tool stages to .andon/ + mints the proposed record (GROVE_HOME-isolated)."""
    from grove.capability_registry import skill_record_id_for_name
    from grove.kaizen.synthesizer import stage_proposal
    from grove.skills import proposal_path

    pid = stage_proposal(
        {"tool_sequence": ("a", "b"), "evidence_turns": ["t#1"]},
        _VALID_SKILL_MD,
    )
    skill_name = "prep-meeting-brief"
    assert not proposal_path(skill_name).exists()

    result = json.loads(approve_proposal(pid))
    assert result["success"] is True
    assert proposal_path(skill_name).exists()
    assert skill_record_id_for_name(skill_name) is not None
    assert read_all() == []
