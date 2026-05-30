"""Sprint 32 Phase 2 — Dispatcher integration test.

Verifies the ``always`` disposition end-to-end:

* operator chooses "Always allow this"
* dispatcher mutates ``_session_allow_cache``
* dispatcher calls ``_queue_zone_promotion_proposal``
* a ZonePromotionProposal lands in the supplied queue file

Per GATE-A A4 lock: non-TTY handlers (gateway, batch) never return
``always`` — they map to ``once``. Phase 2 honors that by relying on
the handler-vocabulary constraint; this test pins the TTY path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ZONE_PROMOTION,
    read_all,
)
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
    """A Dispatcher whose pending-andon marker is no-op'd and whose
    proposal queue is redirected to tmp_path."""
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "always")
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_test#1"

    # Redirect the proposal queue's default path to tmp_path so the
    # operator's real ~/.grove/proposals.jsonl is never touched.
    queue_file = tmp_path / "proposals.jsonl"
    import grove.eval.proposal_queue as _pq
    monkeypatch.setattr(_pq, "default_queue_path", lambda: queue_file)
    d._queue_file = queue_file  # carry for the test to read
    return d


# ── Phase 2 integration ──────────────────────────────────────────────


class TestAlwaysQueuesProposal:
    def test_always_disposition_appends_zone_promotion_to_queue(
        self, dispatcher: Dispatcher,
    ):
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(), halt=_halt(),
        )
        assert result == "always"
        # Cache mutation already verified by the Phase 1 tests; this
        # test focuses on the Phase 2 queue write.
        loaded = read_all(path=dispatcher._queue_file)
        assert len(loaded) == 1
        prop = loaded[0]
        assert prop.type == PROPOSAL_TYPE_ZONE_PROMOTION
        assert prop.payload["tool"] == "terminal"
        assert prop.payload["pattern"] == r".*\.grove/skills/cal/.*"
        assert prop.payload["zone"] == "green"
        assert "Operator approved" in prop.payload["reason"]
        assert prop.evidence == ("s_test#1",)

    def test_always_idempotent_on_duplicate_command(
        self, dispatcher: Dispatcher,
    ):
        """Two ``always`` dispositions for the SAME tool + command in
        the same session produce the same proposal_id; the queue
        idempotently absorbs the duplicate."""
        agent = MagicMock()
        dispatcher._handle_andon_halt(agent=agent, halt=_halt())
        # Re-trigger with the same command; the allow cache will hit
        # on the second call, so the handler is bypassed. To force
        # the second always-flow, clear the cache.
        dispatcher._session_allow_cache.clear()
        dispatcher._handle_andon_halt(agent=agent, halt=_halt())
        loaded = read_all(path=dispatcher._queue_file)
        # Only one proposal in queue — proposal_id is content-
        # addressable so the second append was a no-op.
        assert len(loaded) == 1

    def test_always_queue_failure_does_not_block_disposition(
        self, dispatcher: Dispatcher, monkeypatch,
    ):
        """If the queue write throws, the dispatcher MUST still
        return ``always`` (the cache mutation is the operational
        relief; the queue write is observability)."""
        def _explode(*a, **k):
            raise RuntimeError("disk full")
        # Patch the queue's append to raise so the catch handler
        # exercises its non-fatal path.
        monkeypatch.setattr(
            "grove.eval.proposal_queue.append", _explode,
        )
        result = dispatcher._handle_andon_halt(
            agent=MagicMock(), halt=_halt(),
        )
        assert result == "always"
        # Cache populated so the session-level relief still landed.
        assert len(dispatcher._session_allow_cache) == 1
