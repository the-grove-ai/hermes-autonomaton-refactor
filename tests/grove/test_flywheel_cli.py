"""Sprint 47 — operator review surface tests (GRV-008 § IV)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)


def _make_proposal(
    *,
    rule: str = "downward",
    intents=("conversation",),
    evidence=("t_a", "t_b", "t_c"),
    source_patterns=("cluster:sha256:test",),
) -> RoutingProposal:
    # B2 — routing_adjustment/routing_update now requires a non-empty
    # source_patterns to approve (the no-cluster-no-proposal gate), so the
    # default carries a cluster id. Approve tests rely on this default; the
    # empty-cluster refusal is asserted explicitly in test_flywheel_b2.py.
    payload = {"rule": rule, "add_intents": list(intents)}
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_update",
            payload=payload,
            evidence=tuple(evidence),
        ),
        type="routing_update",
        payload=payload,
        evidence=tuple(evidence),
        eval_hash="sha256:gated",
        created_at="2026-05-30T00:00:00+00:00",
        source_patterns=tuple(source_patterns),
    )


# ── cli_list ─────────────────────────────────────────────────────────


class TestCliList:
    def test_empty_queue_prints_message(self, tmp_path: Path, capsys) -> None:
        rc = flywheel_cli.cli_list(queue_path=tmp_path / "proposals.jsonl")
        out = capsys.readouterr().out
        assert rc == 0
        assert "No pending Flywheel proposals" in out

    def test_lists_pending_proposals(self, tmp_path: Path, capsys) -> None:
        queue = tmp_path / "proposals.jsonl"
        p = _make_proposal()
        append(p, path=queue)
        rc = flywheel_cli.cli_list(queue_path=queue)
        out = capsys.readouterr().out
        assert rc == 0
        assert "pending proposal" in out
        assert "downward" in out
        assert p.proposal_id.split(":")[-1][:12] in out


# ── cli_show ─────────────────────────────────────────────────────────


class TestCliShow:
    def test_shows_payload_evidence_diff(self, tmp_path: Path, capsys) -> None:
        queue = tmp_path / "proposals.jsonl"
        p = _make_proposal()
        append(p, path=queue)
        rc = flywheel_cli.cli_show(p.proposal_id, queue_path=queue)
        out = capsys.readouterr().out
        assert rc == 0
        assert p.proposal_id in out
        # kaizen-offerings — the lead is the composer's bare pull form (the
        # per-type _LEAD dict folded into compose_offering), not the old generic.
        assert flywheel_cli.compose_offering(p, is_push=False) in out
        assert "Here's a routing change I'd recommend" not in out
        assert "your review before anything changes" in out
        assert "Here's what I'd put in place:" in out
        assert "What changes if you approve" in out
        # The substantive payload/diff content is preserved verbatim.
        assert "downward" in out
        assert "conversation" in out

    def test_short_prefix_resolves(self, tmp_path: Path, capsys) -> None:
        queue = tmp_path / "proposals.jsonl"
        p = _make_proposal()
        append(p, path=queue)
        short = p.proposal_id.split(":")[-1][:10]
        rc = flywheel_cli.cli_show(short, queue_path=queue)
        assert rc == 0

    def test_unknown_id_returns_nonzero(self, tmp_path: Path, capsys) -> None:
        queue = tmp_path / "proposals.jsonl"
        rc = flywheel_cli.cli_show("sha256:zzzz", queue_path=queue)
        err = capsys.readouterr().err
        assert rc == 1
        assert "No proposal matches" in err


# ── cli_approve ──────────────────────────────────────────────────────


class TestCliApprove:
    def test_approves_and_updates_machine_file(self, tmp_path: Path) -> None:
        """Mandatory scenario 4: operator approves → routing.autonomaton.yaml
        updated."""
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"
        p = _make_proposal(intents=("conversation",))
        append(p, path=queue)
        assert not machine.exists()
        rc = flywheel_cli.cli_approve(
            p.proposal_id,
            queue_path=queue,
            machine_path=machine,
        )
        assert rc == 0
        assert machine.exists()
        cfg = yaml.safe_load(machine.read_text(encoding="utf-8"))
        intents = cfg["routing"]["routing_rules"]["downward"]["match"]["intents"]
        assert intents == ["conversation"]

    def test_approval_removes_from_queue(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"
        p = _make_proposal()
        append(p, path=queue)
        flywheel_cli.cli_approve(
            p.proposal_id, queue_path=queue, machine_path=machine,
        )
        assert read_all(path=queue) == []

    def test_two_approvals_union_intents(self, tmp_path: Path) -> None:
        """Mandatory scenario 6 (through CLI): the SET-UNION list merge
        means both operator-pre-existing AND new approvals survive on
        the machine side."""
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"
        a = _make_proposal(intents=("conversation",), evidence=("t_a",))
        b = _make_proposal(intents=("creative_writing",), evidence=("t_b",))
        append(a, path=queue)
        append(b, path=queue)
        flywheel_cli.cli_approve(
            a.proposal_id, queue_path=queue, machine_path=machine,
        )
        flywheel_cli.cli_approve(
            b.proposal_id, queue_path=queue, machine_path=machine,
        )
        cfg = yaml.safe_load(machine.read_text(encoding="utf-8"))
        intents = cfg["routing"]["routing_rules"]["downward"]["match"]["intents"]
        assert sorted(intents) == ["conversation", "creative_writing"]

    def test_machine_file_never_touches_operator_config(
        self, tmp_path: Path,
    ) -> None:
        """GRV-008 § III invariant: the approve handler never touches
        routing.config.yaml, only routing.autonomaton.yaml."""
        queue = tmp_path / "proposals.jsonl"
        op = tmp_path / "routing.config.yaml"
        op.write_text("# operator file — sentinel\nrouting:\n  default_tier: T2\n")
        op_mtime_before = op.stat().st_mtime
        machine = tmp_path / "routing.autonomaton.yaml"
        p = _make_proposal()
        append(p, path=queue)
        flywheel_cli.cli_approve(
            p.proposal_id, queue_path=queue, machine_path=machine,
        )
        # Operator file unchanged (same mtime, same content).
        op_mtime_after = op.stat().st_mtime
        assert op_mtime_after == op_mtime_before
        assert op.read_text(encoding="utf-8") == (
            "# operator file — sentinel\nrouting:\n  default_tier: T2\n"
        )


# ── cli_reject ───────────────────────────────────────────────────────


class TestCliReject:
    def test_rejects_and_removes_from_queue(self, tmp_path: Path) -> None:
        """Mandatory scenario 5: operator rejects → removed, no config
        change."""
        queue = tmp_path / "proposals.jsonl"
        machine = tmp_path / "routing.autonomaton.yaml"
        p = _make_proposal()
        append(p, path=queue)
        rc = flywheel_cli.cli_reject(p.proposal_id, queue_path=queue)
        assert rc == 0
        assert read_all(path=queue) == []
        # No machine file was created — rejection produces no config change.
        assert not machine.exists()

    def test_reject_with_reason_logs(self, tmp_path: Path, caplog) -> None:
        import logging
        queue = tmp_path / "proposals.jsonl"
        p = _make_proposal()
        append(p, path=queue)
        with caplog.at_level(logging.INFO, logger="grove.flywheel_cli"):
            rc = flywheel_cli.cli_reject(
                p.proposal_id, reason="not the right pattern yet",
                queue_path=queue,
            )
        assert rc == 0
        assert "not the right pattern yet" in caplog.text

    def test_reject_unknown_returns_nonzero(self, tmp_path: Path, capsys) -> None:
        queue = tmp_path / "proposals.jsonl"
        rc = flywheel_cli.cli_reject("sha256:nope", queue_path=queue)
        err = capsys.readouterr().err
        assert rc == 1
        assert "No proposal matches" in err
