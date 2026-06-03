"""Sprint 53.2 Phase 4 — --strict skill promotion gate.

* the Kaizen ledger now accepts the additive quarantine_skill_disposition
  event type (GATE-A decision 2);
* ``_has_successful_quarantine_execution`` scans all session ledgers;
* ``cli_approve(strict=True)`` enforces logged-execution + confirmation
  before promoting; ``strict=False`` does not.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_SKILL_PROMOTION,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)
from grove.kaizen_ledger import KaizenLedger


def _skill_proposal(name="my-skill") -> RoutingProposal:
    payload = {
        "skill_name": name,
        "skill_path": f"/Users/op/.grove/skills/.andon/{name}",
        "execution_turn_id": "s#1",
        "suggested_action": "promote",
    }
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_SKILL_PROMOTION, payload=payload, evidence=("s#1",),
        ),
        type=PROPOSAL_TYPE_SKILL_PROMOTION,
        payload=payload,
        evidence=("s#1",),
        eval_hash="sha256:cafe",
        created_at="2026-06-03T00:00:00+00:00",
    )


def _log_execution(home: Path, name="my-skill") -> None:
    led = KaizenLedger("sess1", ledger_dir=home / ".kaizen_ledger")
    led.record(
        "quarantine_skill_disposition",
        skill_name=name,
        skill_path=f"/Users/op/.grove/skills/.andon/{name}",
        disposition="once",
    )


# ── ledger accepts the new event type ─────────────────────────────────


def test_ledger_accepts_quarantine_disposition_event(tmp_path: Path) -> None:
    led = KaizenLedger("s", ledger_dir=tmp_path)
    event = led.record(
        "quarantine_skill_disposition",
        skill_name="x", skill_path="/p", disposition="once",
    )
    assert event["event_type"] == "quarantine_skill_disposition"
    assert event["disposition"] == "once"


# ── ledger scan ───────────────────────────────────────────────────────


def test_scan_finds_logged_execution(monkeypatch, tmp_path: Path) -> None:
    from grove import flywheel_cli
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    _log_execution(tmp_path, "my-skill")
    assert flywheel_cli._has_successful_quarantine_execution("my-skill") is True
    assert flywheel_cli._has_successful_quarantine_execution("other") is False


def test_scan_empty_when_no_ledger_dir(monkeypatch, tmp_path: Path) -> None:
    from grove import flywheel_cli
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert flywheel_cli._has_successful_quarantine_execution("my-skill") is False


# ── strict cli_approve gate ───────────────────────────────────────────


@pytest.fixture
def patched(monkeypatch, tmp_path: Path):
    """Mock the promotion side-effects + isolate ledger/home."""
    import hermes_constants
    promote_mock = MagicMock()
    save_rule_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", promote_mock)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", save_rule_mock)
    monkeypatch.setattr(
        "agent.prompt_builder.clear_skills_system_prompt_cache", MagicMock(),
    )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return promote_mock, save_rule_mock, tmp_path


def _approve_strict(qf: Path, proposal: RoutingProposal):
    from grove.flywheel_cli import cli_approve
    short = proposal.proposal_id.split(":")[-1][:12]
    return cli_approve(short, strict=True, queue_path=qf)


def test_strict_refuses_without_logged_execution(monkeypatch, patched) -> None:
    promote_mock, save_rule_mock, home = patched
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "y")  # would confirm
    qf = home / "proposals.jsonl"
    proposal = _skill_proposal()
    append(proposal, path=qf)

    rc = _approve_strict(qf, proposal)

    assert rc == 1
    promote_mock.assert_not_called()
    # Proposal stays in the queue — nothing was applied.
    assert len(read_all(path=qf)) == 1


def test_strict_proceeds_with_execution_and_confirm(monkeypatch, patched) -> None:
    promote_mock, save_rule_mock, home = patched
    _log_execution(home, "my-skill")
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "y")
    qf = home / "proposals.jsonl"
    proposal = _skill_proposal()
    append(proposal, path=qf)

    rc = _approve_strict(qf, proposal)

    assert rc == 0
    promote_mock.assert_called_once_with("my-skill")
    save_rule_mock.assert_called_once()
    assert read_all(path=qf) == []  # removed after approve


def test_strict_aborts_on_decline(monkeypatch, patched) -> None:
    promote_mock, save_rule_mock, home = patched
    _log_execution(home, "my-skill")
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "n")
    qf = home / "proposals.jsonl"
    proposal = _skill_proposal()
    append(proposal, path=qf)

    rc = _approve_strict(qf, proposal)

    assert rc == 1
    promote_mock.assert_not_called()
    assert len(read_all(path=qf)) == 1


def test_non_strict_approve_skips_gate(monkeypatch, patched) -> None:
    """Normal `flywheel approve` (no --strict) promotes without the gate."""
    from grove.flywheel_cli import cli_approve
    promote_mock, save_rule_mock, home = patched
    # No logged execution, no confirmation input — must still proceed.
    qf = home / "proposals.jsonl"
    proposal = _skill_proposal()
    append(proposal, path=qf)

    short = proposal.proposal_id.split(":")[-1][:12]
    rc = cli_approve(short, strict=False, queue_path=qf)

    assert rc == 0
    promote_mock.assert_called_once_with("my-skill")
