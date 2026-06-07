"""Sprint 67 (kaizen-governance-parity-v1) — Dispatcher integration test.

Verifies the ``always`` disposition APPLIES the zone promotion
immediately instead of queuing a proposal:

* operator chooses "Always allow this" on a live Andon prompt
* dispatcher mutates ``_session_allow_cache`` (this turn's relief)
* dispatcher calls ``grove.zone_rules.save_zone_rule`` directly — the
  same apply step ``autonomaton flywheel approve`` performs
* NO ZonePromotionProposal is written to the queue

Operator-initiated "always" is self-approving: the tap (Telegram
``kz:always``) or keystroke (CLI ``[a]``) IS the approval, so there is
no second flywheel-approve gate. This inverts the Sprint 32 behavior
pinned by the prior ``test_dispatcher_always_queues_proposal``. System-
initiated promotions (Ratchet / observed patterns) still queue via a
different code path, untouched by this change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.eval.proposal_queue import read_all
from grove.intents import ToolIntent
from grove.zones import ZoneResult


def _halt(tool: str = "terminal", command: str = "python3 /x/.grove/skills/cal/run.py") -> AndonHalt:
    intents = [ToolIntent(
        tool_name=tool,
        arguments={"command": command},
        call_id="c1",
    )]
    zr = [ZoneResult(zone="yellow", matched_rule="r", source="default")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


@pytest.fixture
def dispatcher(monkeypatch, tmp_path: Path) -> Dispatcher:
    """A Dispatcher whose pending-andon marker is no-op'd, whose proposal
    queue is redirected to tmp_path, and whose ``save_zone_rule`` is
    captured rather than writing the operator's real zones.schema.yaml."""
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "always")
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_test#1"

    # Redirect the proposal queue's default path to tmp_path so a stray
    # write would be observable here and the operator's real
    # ~/.grove/proposals.jsonl is never touched.
    queue_file = tmp_path / "proposals.jsonl"
    import grove.eval.proposal_queue as _pq
    monkeypatch.setattr(_pq, "default_queue_path", lambda: queue_file)
    d._queue_file = queue_file  # carry for the test to read

    # Capture save_zone_rule calls instead of mutating the real schema.
    # _apply_zone_promotion imports it from grove.zone_rules at call time.
    calls: list[dict] = []
    import grove.zone_rules as _zr
    monkeypatch.setattr(_zr, "save_zone_rule", lambda **kw: calls.append(kw))
    d._save_calls = calls  # type: ignore[attr-defined]
    return d


# ── Sprint 67 integration ────────────────────────────────────────────


class TestAlwaysAppliesPromotion:
    def test_always_disposition_applies_zone_rule_immediately(
        self, dispatcher: Dispatcher,
    ):
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(), halt=_halt(),
        )
        assert result == "always"
        # The promotion was applied — save_zone_rule called once with the
        # canonical pattern build_zone_promotion_proposal generates.
        assert len(dispatcher._save_calls) == 1
        call = dispatcher._save_calls[0]
        assert call["tool_id"] == "terminal"
        assert call["pattern"] == r".*\.grove/skills/cal/.*"
        assert call["zone"] == "green"
        assert "Operator approved" in call["reason"]

    def test_always_does_not_queue_a_proposal(
        self, dispatcher: Dispatcher,
    ):
        """The whole point of Sprint 67: apply, do not queue. The
        proposal queue stays empty."""
        dispatcher._handle_andon_halt(agent=MagicMock(), halt=_halt())
        loaded = read_all(path=dispatcher._queue_file)
        assert loaded == []

    def test_always_apply_failure_does_not_block_disposition(
        self, dispatcher: Dispatcher, monkeypatch,
    ):
        """If the schema write throws, the dispatcher MUST still return
        ``always`` — the ``_session_allow_cache`` mutation is this turn's
        operational relief; persistence is observability."""
        import grove.zone_rules as _zr

        def _explode(**kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(_zr, "save_zone_rule", _explode)
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(), halt=_halt(),
        )
        assert result == "always"
        # Cache populated so the session-level relief still landed.
        assert len(dispatcher._session_allow_cache) == 1
