"""Sprint 32 Phase 2 — queue type discriminator + CLI dispatch tests.

Covers:

* Backward compat: legacy entries without ``type``, and Sprint 47
  ``routing_update`` entries, both round-trip cleanly.
* Mixed-type queues: routing_adjustment + zone_promotion proposals
  coexist; list / show / approve all dispatch correctly.
* Zone promotion approval writes to ``zones.schema.yaml`` via
  ``save_zone_rule`` (preserved by the existing Sprint 22 path) —
  not to ``routing.autonomaton.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)
from grove.kaizen_promotion import build_zone_promotion_proposal


def _routing_proposal(rule="downward", intents=("conversation",)):
    payload = {"rule": rule, "add_intents": list(intents)}
    evidence = ("t_a", "t_b", "t_c", "t_d", "t_e")
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
            payload=payload,
            evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        payload=payload,
        evidence=evidence,
        eval_hash="sha256:eval",
        created_at="2026-05-30T11:00:00+00:00",
        # B2 — routing_adjustment must cite a cluster to approve.
        source_patterns=("cluster:sha256:test",),
    )


def _zone_proposal(
    tool="terminal",
    command="python3 /x/.grove/skills/foo/run.py",
    turn_id="s_001#1",
):
    proposal, _ = build_zone_promotion_proposal(
        tool_name=tool,
        command_string=command,
        evidence_turn_id=turn_id,
    )
    return proposal


# ── Backward compat (Sprint 32 2a) ───────────────────────────────────


class TestQueueBackwardCompat:
    def test_legacy_entry_without_type_defaults_to_routing_adjustment(
        self, tmp_path: Path,
    ) -> None:
        """A pre-Sprint-32 queue entry that doesn't carry the ``type``
        field MUST load with ``type=routing_adjustment``."""
        queue = tmp_path / "proposals.jsonl"
        # Write a record without 'type' — simulates a pre-Sprint-32
        # queue file the operator may have on disk.
        legacy = {
            "proposal_id": "sha256:legacy",
            "payload": {"rule": "downward", "add_intents": ["x"]},
            "evidence": ["t1"],
            "eval_hash": "",
            "created_at": "2026-05-30T00:00:00+00:00",
        }
        queue.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        loaded = read_all(path=queue)
        assert len(loaded) == 1
        assert loaded[0].type == PROPOSAL_TYPE_ROUTING_ADJUSTMENT

    def test_legacy_routing_update_string_round_trips(
        self, tmp_path: Path,
    ) -> None:
        """A Sprint 47 record with ``type=routing_update`` MUST load
        as-is; the CLI accepts both spellings."""
        queue = tmp_path / "proposals.jsonl"
        legacy = {
            "proposal_id": "sha256:rupd",
            "type": "routing_update",
            "payload": {"rule": "downward", "add_intents": ["conversation"]},
            "evidence": ["t1"],
            "eval_hash": "",
            "created_at": "2026-05-30T00:00:00+00:00",
        }
        queue.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        loaded = read_all(path=queue)
        assert loaded[0].type == "routing_update"


# ── Mixed-type queue (Sprint 32 2c) ──────────────────────────────────


class TestMixedTypeQueue:
    def test_list_displays_both_proposal_types(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        queue = tmp_path / "proposals.jsonl"
        append(_routing_proposal(), path=queue)
        append(_zone_proposal(), path=queue)
        rc = flywheel_cli.cli_list(queue_path=queue)
        out = capsys.readouterr().out
        assert rc == 0
        assert "2 pending proposal(s)" in out
        assert PROPOSAL_TYPE_ROUTING_ADJUSTMENT in out
        assert PROPOSAL_TYPE_ZONE_PROMOTION in out

    def test_show_renders_zone_promotion_diff(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _zone_proposal()
        append(prop, path=queue)
        rc = flywheel_cli.cli_show(prop.proposal_id, queue_path=queue)
        out = capsys.readouterr().out
        assert rc == 0
        # The diff section MUST surface the tool_zones key and the
        # green rule that would be written.
        assert "tool_zones" in out
        assert "terminal" in out
        assert "green" in out
        assert ".grove/skills/foo" in out


# ── Approve dispatch (Sprint 32 2c) ──────────────────────────────────


class TestApproveDispatch:
    def test_routing_adjustment_writes_to_machine_config(
        self, tmp_path: Path,
    ) -> None:
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"
        prop = _routing_proposal()
        append(prop, path=queue)
        rc = flywheel_cli.cli_approve(
            prop.proposal_id, queue_path=queue, machine_path=machine,
        )
        assert rc == 0
        assert machine.exists()
        cfg = yaml.safe_load(machine.read_text(encoding="utf-8"))
        intents = cfg["routing"]["routing_rules"]["downward"]["match"]["intents"]
        assert intents == ["conversation"]
        assert read_all(path=queue) == []

    def test_legacy_routing_update_approves_via_routing_path(
        self, tmp_path: Path,
    ) -> None:
        """The Sprint 47 spelling ``routing_update`` MUST continue to
        approve through the routing_adjustment writer."""
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"

        payload = {"rule": "downward", "add_intents": ["x"]}
        legacy_prop = RoutingProposal(
            proposal_id="sha256:legacy",
            type="routing_update",
            payload=payload,
            evidence=("t_a",),
            eval_hash="",
            created_at="2026-05-30T00:00:00+00:00",
            # B2 — even the legacy spelling must cite a cluster to approve.
            source_patterns=("cluster:sha256:test",),
        )
        append(legacy_prop, path=queue)
        rc = flywheel_cli.cli_approve(
            legacy_prop.proposal_id,
            queue_path=queue,
            machine_path=machine,
        )
        assert rc == 0
        assert machine.exists()

    def test_zone_promotion_writes_to_zones_schema(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A zone_promotion proposal MUST route to save_zone_rule —
        writing the green rule to zones.schema.yaml — and MUST NOT
        touch routing.autonomaton.yaml."""
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"

        # Capture the save_zone_rule call to verify the dispatch
        # without writing to the operator's real ~/.grove file.
        captured = {}

        def _fake_save(tool_id, pattern, zone, reason):
            captured["tool_id"] = tool_id
            captured["pattern"] = pattern
            captured["zone"] = zone
            captured["reason"] = reason

        monkeypatch.setattr(
            "grove.zone_rules.save_zone_rule", _fake_save,
        )

        prop = _zone_proposal(
            tool="terminal",
            command="python3 /x/.grove/skills/cal/run.py today",
            turn_id="t_cal",
        )
        append(prop, path=queue)
        rc = flywheel_cli.cli_approve(
            prop.proposal_id, queue_path=queue, machine_path=machine,
        )
        assert rc == 0
        # save_zone_rule was called with the proposal's payload values.
        assert captured == {
            "tool_id": "terminal",
            "pattern": r".*\.grove/skills/cal/.*",
            "zone": "green",
            "reason": "Operator approved: allow cal to execute via terminal.",
        }
        # Machine routing file MUST NOT be touched on a zone_promotion
        # approval.
        assert not machine.exists()
        # Queue cleared after approval.
        assert read_all(path=queue) == []

    def test_unknown_type_returns_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        queue = tmp_path / "proposals.jsonl"
        weird = RoutingProposal(
            proposal_id="sha256:weird",
            type="unknown_type",
            payload={},
            evidence=(),
            eval_hash="",
            created_at="2026-05-30T00:00:00+00:00",
        )
        append(weird, path=queue)
        rc = flywheel_cli.cli_approve(weird.proposal_id, queue_path=queue)
        err = capsys.readouterr().err
        assert rc == 1
        assert "unknown_type" in err
        # Queue NOT cleared — unknown type means the proposal stays
        # for the operator to manually handle.
        assert len(read_all(path=queue)) == 1
