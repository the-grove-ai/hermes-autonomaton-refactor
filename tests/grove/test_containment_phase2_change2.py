"""execute-code-meta-surface-containment-v1 Phase-2 Change 2 — promotability +
disposition + loud headless-cancel.

Covers:
  * a bucket-3 UNRESOLVED_WRITER is NEVER denied-by-policy — it store-pends on a
    reachable surface (deny-list exclusion);
  * on an UNREACHABLE surface it is dropped to headless Cancel AND files a
    ``headless_governance_block`` ledger event;
  * the Always affordance / resolve_always_store refuses a non-promotable
    classification (returns None → no standing store).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from grove.dispatcher import AndonResolutionHalt, Dispatcher
from grove.governance_halt import TerminalGovernanceHalt
from grove.intents import ToolIntent
from grove.sovereign_prompt_handlers import non_interactive_deny_handler
from tests.grove.test_kaizen_voice_red_fork_b1 import _bare_agent


@pytest.fixture(autouse=True)
def _redirect_grove_home(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _fresh_red_store(monkeypatch):
    import grove.red_pending_store as rps
    monkeypatch.setattr(rps, "_STORE", None)
    yield


@pytest.fixture(autouse=True)
def _capture_queue_writes(monkeypatch):
    from grove.eval import proposal_queue as pq
    monkeypatch.setattr(pq, "append", lambda p: None)
    yield


class _FakeGen:
    def send(self, obs: Any) -> Any:
        return obs


class _CapLedger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, **fields: Any) -> None:
        self.events.append((event_type, fields))


def _term(cmd: str) -> ToolIntent:
    return ToolIntent(tool_name="terminal", arguments={"command": cmd}, call_id="c1")


def _classify_to_halt(d: Dispatcher, intent: ToolIntent) -> AndonResolutionHalt:
    try:
        d._classify_intents_batch_and_halt_or_raise([intent])
    except AndonResolutionHalt as halt:
        return halt
    raise AssertionError("expected AndonResolutionHalt")


# ── deny-list exclusion: UNRESOLVED_WRITER store-pends, never denied ──────────

class TestUnresolvedWriterStorePends:
    def test_unresolved_writer_is_not_denied_by_policy(self):
        from grove.red_policy import is_denied_by_policy
        from grove.shell_effects import classify_shell_effect
        zr = classify_shell_effect("git reset --hard origin/main")
        assert "UNRESOLVED_WRITER" in zr.pattern_key
        assert is_denied_by_policy(zr.pattern_key) is False

    def test_reachable_unresolved_writer_store_pends(self):
        d = Dispatcher()  # reachable (default TTY handler)
        halt = _classify_to_halt(d, _term("git reset --hard origin/main"))
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt, ledger=_CapLedger())
        assert len(d._red_pending_store) == 1  # store-pending, not denied/cancelled

    def test_reachable_unresolved_writer_no_headless_block(self):
        d = Dispatcher()
        halt = _classify_to_halt(d, _term("git reset --hard origin/main"))
        cap = _CapLedger()
        d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt, ledger=cap)
        assert not any(e[0] == "headless_governance_block" for e in cap.events)


# ── loud headless-cancel: unreachable + UNRESOLVED_WRITER → event filed ───────

class TestHeadlessGovernanceBlock:
    def test_unreachable_unresolved_writer_files_block(self):
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        halt = _classify_to_halt(d, _term("git reset --hard origin/main"))
        cap = _CapLedger()
        with pytest.raises(TerminalGovernanceHalt):
            d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt, ledger=cap)
        blocks = [e for e in cap.events if e[0] == "headless_governance_block"]
        assert len(blocks) == 1
        assert "UNRESOLVED_WRITER" in blocks[0][1]["pattern_key"]
        assert d._red_pending_store == [] or len(d._red_pending_store) == 0

    def test_unreachable_priv_red_files_no_block(self):
        # A non-UNRESOLVED_WRITER RED (priv:*) on an unreachable surface does NOT
        # file headless_governance_block (the event is UNRESOLVED_WRITER-only).
        d = Dispatcher(sovereign_prompt_handler=non_interactive_deny_handler)
        halt = _classify_to_halt(d, _term("sudo apt install foo"))
        cap = _CapLedger()
        with pytest.raises(TerminalGovernanceHalt):
            d._resolve_red_halt(_bare_agent([]), _FakeGen(), halt, ledger=cap)
        assert not any(e[0] == "headless_governance_block" for e in cap.events)


# ── Always-gate: non-promotable → no standing store ──────────────────────────

class TestAlwaysGateOnPromotability:
    def _halt(self, is_promotable: bool):
        zr = SimpleNamespace(is_promotable=is_promotable, pattern_key="x")
        intent = _term("git reset --hard origin/main")
        return SimpleNamespace(
            intents=[intent], zone_results=[zr], triggering_index=0
        )

    def test_non_promotable_resolves_no_store(self):
        from grove.grant_recognition import resolve_always_store
        assert resolve_always_store(self._halt(is_promotable=False)) is None

    def test_promotable_yellow_generic_resolves_zone_rule(self):
        from grove.grant_recognition import resolve_always_store
        store = resolve_always_store(self._halt(is_promotable=True))
        assert store is not None and store[0] == "zone_rule"
