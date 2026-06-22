"""consolidation-ratchet-v1 — Stage 2 detector + two-file atomic apply tests.

The ConsolidationRatchet reads machine sink entries, verifies LONG-term
stability against the intent feed (n≥20, ≥90% success, zero Andons), and
proposes graduating the intent to permanent operator policy. Approval performs
a two-file atomic write to routing.config.yaml + routing.autonomaton.yaml with
ruamel comment preservation, sandbox validation, backup/restore, and a
router hot-reload.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from grove.eval.consolidation_ratchet import ConsolidationRatchet
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_CONSOLIDATION,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    RoutingProposal,
    compute_proposal_id,
)
from grove.intent_store import IntentRecord, IntentStore

_REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "routing.config.yaml"
_PATTERN = "f" * 64


# ── fixtures / helpers ───────────────────────────────────────────────────


def _write_machine(path: Path, sink_intents: dict) -> None:
    """sink_intents: {sink_rule_name: [intent_class, ...]}."""
    rules = {
        name: {
            "enabled": True,
            "match": {"intents": list(ints)},
            "target_tier": {"ratchet_promoted_t1": "T1",
                            "ratchet_promoted_t2": "T2",
                            "ratchet_promoted_t3": "T3"}[name],
        }
        for name, ints in sink_intents.items()
    }
    path.write_text(yaml.safe_dump({"routing": {"routing_rules": rules}}))


def _feed(path: Path, specs) -> None:
    """specs: list of (intent_class, outcome, count). One record per turn."""
    store = IntentStore(path)
    idx = 0
    for intent_class, outcome, count in specs:
        for _ in range(count):
            store.append(IntentRecord(
                timestamp=datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
                session_id="s_c",
                turn_id=f"{intent_class}_{idx}",
                user_message_stem="probe",
                pattern_hash=_PATTERN,
                intent_class=intent_class,
                register_class="casual",
                complexity_signal="simple",
                confidence=0.9,
                outcome=outcome,
                tier_selected="T2",
            ))
            idx += 1


def _proposal(intent="code_generation", tier="T2", sink="ratchet_promoted_t2",
              n=25, rate=0.96):
    payload = {
        "action": "consolidate", "intent_class": intent, "target_tier": tier,
        "source_sink": sink, "stats": {"n": n, "success_rate": rate, "andons": 0},
    }
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_CONSOLIDATION,
            payload={"intent_class": intent, "target_tier": tier, "source_sink": sink},
            evidence=(),
        ),
        type=PROPOSAL_TYPE_CONSOLIDATION, payload=payload, evidence=(),
        eval_hash="", created_at="2026-06-01T00:00:00+00:00",
    )


# ── M1 detection ─────────────────────────────────────────────────────────


def test_detect_stable_intent_proposed(tmp_path):
    _write_machine(tmp_path / "m.yaml", {"ratchet_promoted_t2": ["code_generation"]})
    _feed(tmp_path / "i.jsonl",
          [("code_generation", "success", 24), ("code_generation", "correction", 1)])

    proposals = ConsolidationRatchet().detect(tmp_path / "m.yaml", tmp_path / "i.jsonl")
    assert len(proposals) == 1
    p = proposals[0]
    assert p == {
        "action": "consolidate", "intent_class": "code_generation",
        "target_tier": "T2", "source_sink": "ratchet_promoted_t2",
        "stats": {"n": 25, "success_rate": 0.96, "andons": 0},
    }


def test_detect_below_sample_threshold_no_proposal(tmp_path):
    _write_machine(tmp_path / "m.yaml", {"ratchet_promoted_t2": ["code_generation"]})
    _feed(tmp_path / "i.jsonl", [("code_generation", "success", 15)])  # n=15 < 20
    assert ConsolidationRatchet().detect(tmp_path / "m.yaml", tmp_path / "i.jsonl") == []


def test_detect_below_success_rate_no_proposal(tmp_path):
    _write_machine(tmp_path / "m.yaml", {"ratchet_promoted_t2": ["code_generation"]})
    # 20 success + 5 correction = 25 obs, 80% success < 90%
    _feed(tmp_path / "i.jsonl",
          [("code_generation", "success", 20), ("code_generation", "correction", 5)])
    assert ConsolidationRatchet().detect(tmp_path / "m.yaml", tmp_path / "i.jsonl") == []


def test_detect_andon_disqualifies(tmp_path):
    _write_machine(tmp_path / "m.yaml", {"ratchet_promoted_t2": ["code_generation"]})
    # 24 success + 1 governance_terminated: success_rate 0.96 but andons=1.
    _feed(tmp_path / "i.jsonl",
          [("code_generation", "success", 24),
           ("code_generation", "governance_terminated", 1)])
    assert ConsolidationRatchet().detect(tmp_path / "m.yaml", tmp_path / "i.jsonl") == []


def test_detect_intent_not_in_sink_no_proposal(tmp_path):
    # Great stats for 'analysis', but only 'code_generation' is in the sink.
    _write_machine(tmp_path / "m.yaml", {"ratchet_promoted_t2": ["code_generation"]})
    _feed(tmp_path / "i.jsonl", [("analysis", "success", 30)])
    assert ConsolidationRatchet().detect(tmp_path / "m.yaml", tmp_path / "i.jsonl") == []


# ── M3 two-file atomic apply ─────────────────────────────────────────────


def _operator_and_machine(tmp_path):
    op = tmp_path / "routing.config.yaml"
    shutil.copy(_REPO_CONFIG, op)
    mac = tmp_path / "routing.autonomaton.yaml"
    _write_machine(mac, {"ratchet_promoted_t2": ["code_generation", "analysis"]})
    return op, mac


def test_apply_writes_rule_to_operator_config(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op, mac = _operator_and_machine(tmp_path)

    _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                           reload_fn=lambda: None)

    data = yaml.safe_load(op.read_text())
    rule = data["routing"]["routing_rules"]["code_generation"]
    assert rule == {"enabled": True, "match": {"intents": ["code_generation"]},
                    "target_tier": "T2"}


def test_apply_removes_intent_from_machine_sink(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op, mac = _operator_and_machine(tmp_path)

    _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                           reload_fn=lambda: None)

    sink = yaml.safe_load(mac.read_text())["routing"]["routing_rules"]
    # 'analysis' remains; 'code_generation' graduated out (sink not emptied).
    assert sink["ratchet_promoted_t2"]["match"]["intents"] == ["analysis"]


def test_apply_removes_empty_sink_rule(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op = tmp_path / "routing.config.yaml"
    shutil.copy(_REPO_CONFIG, op)
    mac = tmp_path / "routing.autonomaton.yaml"
    _write_machine(mac, {"ratchet_promoted_t2": ["code_generation"]})  # sole intent

    _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                           reload_fn=lambda: None)

    rules = yaml.safe_load(mac.read_text())["routing"]["routing_rules"]
    assert "ratchet_promoted_t2" not in rules  # emptied sink pruned (A6)


def test_apply_preserves_operator_comments(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op, mac = _operator_and_machine(tmp_path)
    assert "# OWNERSHIP" in op.read_text()  # precondition

    _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                           reload_fn=lambda: None)

    after = op.read_text()
    assert "# OWNERSHIP" in after  # ruamel round-trip kept the comments
    assert "THE FOUR TIERS" in after


def test_apply_restores_both_files_on_failure(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op, mac = _operator_and_machine(tmp_path)
    op_before, mac_before = op.read_text(), mac.read_text()

    def _boom():
        raise RuntimeError("reload blew up")

    with pytest.raises(RuntimeError, match="reload blew up"):
        _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                               reload_fn=_boom)

    # Both files rolled back; the new rule did NOT survive.
    assert op.read_text() == op_before
    assert mac.read_text() == mac_before
    assert "code_generation" not in yaml.safe_load(op.read_text())["routing"][
        "routing_rules"]
    assert op.with_suffix(".yaml.bak").exists()  # backup left for forensics


def test_apply_calls_hot_reload(tmp_path):
    from grove.flywheel_cli import _approve_consolidation
    op, mac = _operator_and_machine(tmp_path)
    called = []

    _approve_consolidation(_proposal(), machine_path=mac, operator_path=op,
                           reload_fn=lambda: called.append(True))
    assert called == [True]


# ── M2 rendering + registration ──────────────────────────────────────────


def test_summary_renderer_natural_language():
    from grove.flywheel_cli import _summary_consolidation
    text = _summary_consolidation(_proposal(n=25, rate=0.96))
    assert "code_generation" in text and "T2" in text
    assert "25 obs" in text and "96% success" in text and "zero halts" in text
    assert "Promote to permanent routing policy?" in text
    # No schema leak.
    for leak in ("consolidate", "source_sink", "mem_", "[", "{"):
        assert leak not in text


def test_push_frame_distinct():
    consolidation = _proposal()
    routing = RoutingProposal(
        proposal_id="sha256:x", type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload={"rule": "ratchet_promoted_t1", "add_intents": ["x"]},
        evidence=(), eval_hash="", created_at="2026-06-01T00:00:00+00:00",
    )
    assert consolidation.push_body("CORE") == \
        "I'm recommending a routing policy change — CORE"
    assert routing.push_body("CORE") == "I noticed I could CORE"


def test_type_registration_end_to_end():
    from grove.flywheel_cli import compose_offering, get_renderer
    assert callable(get_renderer(PROPOSAL_TYPE_CONSOLIDATION))
    note = compose_offering(_proposal(), is_push=True)
    assert "I'm recommending a routing policy change" in note
    assert "Promote to permanent routing policy?" in note
    assert "approve" in note.lower() and "dismiss" in note.lower()


def test_regression_routing_and_memory_unaffected():
    from grove.flywheel_cli import _PUSH_PRIORITY, get_renderer
    assert _PUSH_PRIORITY[PROPOSAL_TYPE_ROUTING_ADJUSTMENT] == 2
    assert _PUSH_PRIORITY["memory_context"] == 1
    assert _PUSH_PRIORITY[PROPOSAL_TYPE_CONSOLIDATION] < 2  # between memory & routing
    assert callable(get_renderer(PROPOSAL_TYPE_ROUTING_ADJUSTMENT))
    assert callable(get_renderer("memory_context"))


def test_stage_proposals_dedups(tmp_path):
    q = tmp_path / "proposals.jsonl"
    from grove.eval import proposal_queue

    # Point the queue at a temp path via the public append path arg by staging
    # through a thin wrapper: ConsolidationRatchet.stage_proposals uses the
    # default queue, so exercise append idempotency directly on our proposal.
    p = _proposal()
    assert proposal_queue.append(p, path=q) is True
    assert proposal_queue.append(p, path=q) is False  # same id dedups
