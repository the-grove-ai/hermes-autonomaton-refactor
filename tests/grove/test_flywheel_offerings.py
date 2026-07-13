"""kaizen-offerings (Cut B) — voice composer + current-session push gate.

Proofs:
  * one composer chokepoint: _format_summary + cli_show route their human clause
    through compose_offering; the push offer uses it too.
  * push/pull split: is_push=False is the bare body (inventory), is_push=True is
    the conversational interrupt.
  * current-session gate: a past-session proposal does NOT push; a current-
    session one does; one-at-a-time, highest type-priority; ephemeral guard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_SKILL_SYNTHESIS,
    RoutingProposal,
    append,
    compute_proposal_id,
)


def _proposal(ptype, payload, *, created_at, evidence=("t1",)):
    return RoutingProposal(
        proposal_id=compute_proposal_id(type=ptype, payload=payload, evidence=evidence),
        type=ptype, payload=payload, evidence=evidence,
        eval_hash="", created_at=created_at,
        source_patterns=("cluster:x",) if ptype == PROPOSAL_TYPE_ROUTING_ADJUSTMENT else (),
    )


def _routing(created_at):
    return _proposal(
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        {"rule": "upward", "add_intents": ["date_arithmetic"]},
        created_at=created_at,
    )


def _synth(created_at, name="prep-brief"):
    return _proposal(
        PROPOSAL_TYPE_SKILL_SYNTHESIS,
        {"skill_name": name, "skill_md": "x", "goal": "draft a brief"},
        created_at=created_at,
    )


_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


# ── composer: push/pull split ────────────────────────────────────────


def test_pull_form_is_bare_body() -> None:
    p = _routing(_NOW.isoformat())
    pull = flywheel_cli.compose_offering(p, is_push=False)
    # Bare = exactly the per-type summary body, no interrupt wrapper.
    assert pull == flywheel_cli._summary_routing_adjustment(p)
    assert "Shop floor note" not in pull
    assert "flywheel approve" not in pull


def test_push_form_is_interrupt() -> None:
    p = _routing(_NOW.isoformat())
    push = flywheel_cli.compose_offering(p, is_push=True)
    assert push.startswith(flywheel_cli._OFFERING_PUSH_PREFIX)
    # kaizen-voice-conformance — conversational register, no CLI syntax.
    assert "flywheel approve" not in push
    assert "`" not in push
    assert "Reply 'approve'" in push
    # Same factual core rides inside the interrupt.
    assert flywheel_cli._summary_routing_adjustment(p) in push


# ── composer is the one chokepoint ───────────────────────────────────


def test_format_summary_routes_through_composer() -> None:
    p = _routing(_NOW.isoformat())
    line = flywheel_cli._format_summary(p)
    # The human clause in the structured index IS the composer's pull form.
    assert flywheel_cli.compose_offering(p, is_push=False) in line
    # Structured framing preserved (id + type + evidence + timestamp).
    assert p.type in line and "evidence:" in line and p.created_at in line


def test_cli_show_lead_is_composer_pull_form(tmp_path: Path, capsys) -> None:
    queue = tmp_path / "proposals.jsonl"
    p = _routing(_NOW.isoformat())
    append(p, path=queue)
    flywheel_cli.cli_show(p.proposal_id, queue_path=queue)
    out = capsys.readouterr().out
    assert flywheel_cli.compose_offering(p, is_push=False) in out
    # The retired generic _LEAD string is gone.
    assert "Here's a routing change I'd recommend" not in out


# ── current-session push gate ────────────────────────────────────────


def _agent(session_start):
    # portal-link-reliability-v1 (P1): the push path now resolves the portal
    # base URL from the agent's resident config snapshot via _config_load_or().
    # The stub supplies it ({} → resolver falls back to the loopback default,
    # so a Review link is appended but the substring assertions below are
    # unaffected). Without it, AttributeError is swallowed by the push path's
    # broad except and the offer silently vanishes.
    # test-baseline-hygiene R-T2 (kaizen-push-cadence-v1 a89575e16): the cooldown
    # guard reads self._PUSH_COOLDOWN_TURNS; mirror the AIAgent class value (3) so
    # the once-per-cooldown check runs instead of raising AttributeError.
    return SimpleNamespace(
        session_start=session_start,
        _config_load_or=lambda: {},
        _PUSH_COOLDOWN_TURNS=3,
    )


def test_past_session_proposal_does_not_push(tmp_path: Path) -> None:
    from run_agent import AIAgent
    from grove.eval.proposal_queue import default_queue_path

    # Proposal created an hour BEFORE the session began → pull-only.
    p = _routing((_NOW - timedelta(hours=1)).isoformat())
    append(p, path=default_queue_path())
    agent = _agent(_NOW.replace(tzinfo=None))  # session_start naive-local (==UTC in tests)
    out = AIAgent._append_pending_offer(agent, "Done.")
    assert out == "Done."  # not pushed


def test_current_session_proposal_pushes(tmp_path: Path) -> None:
    from run_agent import AIAgent
    from grove.eval.proposal_queue import default_queue_path

    p = _routing((_NOW + timedelta(minutes=5)).isoformat())  # after session start
    append(p, path=default_queue_path())
    agent = _agent(_NOW.replace(tzinfo=None))
    out = AIAgent._append_pending_offer(agent, "Done.")
    assert out.startswith("Done.")
    assert flywheel_cli._OFFERING_PUSH_PREFIX in out
    assert "date_arithmetic" in out


def test_one_at_a_time_highest_priority(tmp_path: Path) -> None:
    """Highest-priority proposal surfaces first; the push cadence guard then
    holds further same-turn pushes (kaizen-push-cadence-v1 a89575e16)."""
    from run_agent import AIAgent
    from grove.eval.proposal_queue import default_queue_path, read_all

    # Both current-session; skill_synthesis (priority 0) outranks routing (1).
    append(_routing((_NOW + timedelta(minutes=1)).isoformat()), path=default_queue_path())
    append(_synth((_NOW + timedelta(minutes=2)).isoformat()), path=default_queue_path())
    assert len(read_all()) == 2
    agent = _agent(_NOW.replace(tzinfo=None))
    first = AIAgent._append_pending_offer(agent, "Done.")
    # Highest priority surfaced = skill_synthesis (its summary mentions "skill").
    assert "skill" in first.lower()
    # test-baseline-hygiene R-T3 (kaizen-push-cadence-v1 a89575e16): the push
    # cadence guard suppresses any further push within _PUSH_COOLDOWN_TURNS, so
    # a same-turn second call is held (returns the bare response) rather than
    # surfacing the next-priority proposal. Only the highest-priority proposal
    # (skill_synthesis) surfaces this cooldown window.
    second = AIAgent._append_pending_offer(agent, "Done.")
    assert second == "Done."
    # Third same-turn call likewise suppressed by the cooldown.
    assert AIAgent._append_pending_offer(agent, "Done.") == "Done."
