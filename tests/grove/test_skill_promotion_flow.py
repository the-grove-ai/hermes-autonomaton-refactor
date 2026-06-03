"""Sprint 53.2 Phase 3 — quarantine → promoted skill promotion flow.

Covers the Dispatcher's ``_promote_quarantined_skill`` (normal vs strict)
and the Flywheel CLI ``skill_promotion`` approve dispatch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_SKILL_PROMOTION,
    RoutingProposal,
    append,
    compute_proposal_id,
    read_all,
)
from grove.intents import PostExecutionKaizenYield


def _dispatcher(monkeypatch) -> Dispatcher:
    d = Dispatcher()
    d._current_turn_id = "s_test#1"
    return d


def _payload(name="my-skill") -> PostExecutionKaizenYield:
    return PostExecutionKaizenYield(
        skill_name=name,
        skill_path=f"/Users/op/.grove/skills/.andon/{name}",
        exit_status="success",
        execution_turn_id="s_test#1",
        suggested_action="promote",
    )


@pytest.fixture
def queue_file(monkeypatch, tmp_path: Path) -> Path:
    qf = tmp_path / "proposals.jsonl"
    import grove.eval.proposal_queue as _pq
    monkeypatch.setattr(_pq, "default_queue_path", lambda: qf)
    return qf


# ── _skill_promotion_is_strict reads config ───────────────────────────


def test_strict_flag_reads_config(monkeypatch) -> None:
    d = _dispatcher(monkeypatch)
    d._base_runtime_ctx = RuntimeContext(
        env={}, config={"skills": {"skill_promotion": "strict"}},
    )
    assert d._skill_promotion_is_strict() is True
    d._base_runtime_ctx = RuntimeContext(
        env={}, config={"skills": {"skill_promotion": "normal"}},
    )
    assert d._skill_promotion_is_strict() is False
    d._base_runtime_ctx = RuntimeContext(env={}, config={})
    assert d._skill_promotion_is_strict() is False  # default normal


# ── normal mode: immediate move + green rule + cache drop ─────────────


def test_promote_normal_mode_moves_and_greenlights(monkeypatch, queue_file: Path) -> None:
    promote_mock = MagicMock()
    save_rule_mock = MagicMock()
    clear_cache_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", promote_mock)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", save_rule_mock)
    monkeypatch.setattr(
        "agent.prompt_builder.clear_skills_system_prompt_cache", clear_cache_mock,
    )

    d = _dispatcher(monkeypatch)
    monkeypatch.setattr(d, "_skill_promotion_is_strict", lambda: False)
    d._promote_quarantined_skill(_payload(), ledger=MagicMock())

    promote_mock.assert_called_once_with("my-skill")
    save_rule_mock.assert_called_once()
    assert save_rule_mock.call_args.kwargs["zone"] == "green"
    # re.escape escapes the hyphen, so match the escaped form.
    import re as _re
    assert _re.escape("my-skill") in save_rule_mock.call_args.kwargs["pattern"]
    clear_cache_mock.assert_called_once_with(clear_snapshot=True)
    # Normal mode does NOT queue — promotion is immediate.
    assert read_all(path=queue_file) == []


def test_promote_normal_mode_missing_skill_is_non_fatal(monkeypatch, queue_file: Path) -> None:
    def _raise(_name):
        raise FileNotFoundError("no such proposal")

    save_rule_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", _raise)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", save_rule_mock)

    d = _dispatcher(monkeypatch)
    monkeypatch.setattr(d, "_skill_promotion_is_strict", lambda: False)
    # Must not raise; must not write a zone rule for a skill that never moved.
    d._promote_quarantined_skill(_payload(), ledger=MagicMock())
    save_rule_mock.assert_not_called()


# ── strict mode: queue only, no move ──────────────────────────────────


def test_promote_strict_mode_queues_only(monkeypatch, queue_file: Path) -> None:
    promote_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", promote_mock)

    d = _dispatcher(monkeypatch)
    monkeypatch.setattr(d, "_skill_promotion_is_strict", lambda: True)
    d._promote_quarantined_skill(_payload(), ledger=MagicMock())

    promote_mock.assert_not_called()
    proposals = read_all(path=queue_file)
    assert len(proposals) == 1
    assert proposals[0].type == PROPOSAL_TYPE_SKILL_PROMOTION
    assert proposals[0].payload["skill_name"] == "my-skill"


# ── Flywheel CLI approve dispatch ─────────────────────────────────────


def _make_skill_proposal(name="my-skill") -> RoutingProposal:
    payload = {
        "skill_name": name,
        "skill_path": f"/Users/op/.grove/skills/.andon/{name}",
        "execution_turn_id": "s_test#1",
        "suggested_action": "promote",
    }
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_SKILL_PROMOTION, payload=payload, evidence=("s_test#1",),
        ),
        type=PROPOSAL_TYPE_SKILL_PROMOTION,
        payload=payload,
        evidence=("s_test#1",),
        eval_hash="sha256:deadbeef",
        created_at="2026-06-03T00:00:00+00:00",
    )


def test_cli_approve_skill_promotion(monkeypatch, tmp_path: Path) -> None:
    from grove.flywheel_cli import cli_approve

    promote_mock = MagicMock()
    save_rule_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", promote_mock)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", save_rule_mock)
    monkeypatch.setattr(
        "agent.prompt_builder.clear_skills_system_prompt_cache", MagicMock(),
    )

    qf = tmp_path / "proposals.jsonl"
    proposal = _make_skill_proposal()
    append(proposal, path=qf)

    short = proposal.proposal_id.split(":")[-1][:12]
    rc = cli_approve(short, queue_path=qf)

    assert rc == 0
    promote_mock.assert_called_once_with("my-skill")
    save_rule_mock.assert_called_once()
    assert save_rule_mock.call_args.kwargs["zone"] == "green"
    # Approved proposal is removed from the queue.
    assert read_all(path=qf) == []


def test_format_summary_and_diff_render_skill_promotion() -> None:
    from grove.flywheel_cli import _format_summary, _proposal_to_diff

    proposal = _make_skill_proposal("calendar-helper")
    summary = _format_summary(proposal)
    assert "skill_promotion" in summary
    assert "calendar-helper" in summary

    diff = _proposal_to_diff(proposal)
    assert "skill_promotion" in diff
    assert diff["skill_promotion"]["skill_name"] == "calendar-helper"
    assert diff["skill_promotion"]["zone_rule"]["zone"] == "green"
