"""Sprint 32 Phase 2 — queue type discriminator + CLI dispatch tests.

Covers:

* Backward compat: legacy entries without ``type``, and Sprint 47
  ``routing_update`` entries, both round-trip cleanly.
* routing_adjustment approval writes to ``routing.autonomaton.yaml``.
* Unknown proposal types are rejected without clearing the queue.

zones-v2-scope-keying P2 (D1): the ``zone_promotion`` proposal type is
RETIRED end-to-end — no proposer (``build_zone_promotion_proposal``), no
renderer, no apply handler (``save_zone_rule`` / ``grove.zone_rules`` /
``grove.kaizen_promotion`` are deleted). The former mixed-type-queue,
zone-promotion-diff, and zones-schema-write tests are removed with the
machinery they exercised. The ``PROPOSAL_TYPE_ZONE_PROMOTION`` string
constant still exists in ``proposal_queue.py`` but has no handler; a
queued zone_promotion now approves as an unknown type (covered by
``test_unknown_type_returns_nonzero``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)


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


# ── Approve dispatch (Sprint 32 2c) ──────────────────────────────────


class TestApproveDispatch:
    @pytest.fixture(autouse=True)
    def _seed_operational_sibling(self, tmp_path: Path) -> None:
        # routing-v2-machine-overlay-migration-v1 (ANDON 3 — approval-is-activation):
        # seed the operational sibling the guard validates against. Standing sinks are
        # BINDING-ONLY (target_tier, disabled); the approved diff activates them.
        (tmp_path / "routing.operational.yaml").write_text(
            'schema_version: "2.0"\n'
            "routing_rules:\n"
            "  downward: {target_tier: T1}\n"
            "  upward: {target_tier: T3}\n",
            encoding="utf-8",
        )

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
        intents = cfg["routing_rules"]["downward"]["match"]["intents"]
        assert intents == ["conversation"]
        assert cfg["routing_rules"]["downward"]["enabled"] is True
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
