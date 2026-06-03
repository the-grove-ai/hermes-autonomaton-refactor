"""Sprint 53.2 Phase 2 — post-execution Kaizen promotion prompt.

Covers:
* detection — ``_handle_andon_halt`` flags an "allow once" execution of a
  quarantined (.andon) skill and records the additive
  ``quarantine_skill_disposition`` ledger event (not Sprint 32's
  ``andon_disposition``);
* emission dispatch — Promote / Not yet / Never / non-TTY auto-log;
* the TTY three-choice renderer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.eval.proposal_queue import PROPOSAL_TYPE_SKILL_PROMOTION, read_all
from grove.intents import PostExecutionKaizenYield, ToolIntent
from grove.zones import ZoneResult

_ANDON_RULE = r".*\.grove/skills/\.andon/.*"
_ANDON_CMD = "python3 /Users/op/.grove/skills/.andon/my-skill/scripts/run.py"


def _halt(*, tool="terminal", command=_ANDON_CMD, matched_rule=_ANDON_RULE) -> AndonHalt:
    intents = [ToolIntent(tool_name=tool, arguments={"command": command}, call_id="c1")]
    zr = [ZoneResult(zone="yellow", matched_rule=matched_rule, source="default")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


@pytest.fixture
def queue_file(monkeypatch, tmp_path: Path) -> Path:
    qf = tmp_path / "proposals.jsonl"
    import grove.eval.proposal_queue as _pq
    monkeypatch.setattr(_pq, "default_queue_path", lambda: qf)
    return qf


def _dispatcher(monkeypatch, **kwargs) -> Dispatcher:
    d = Dispatcher(**kwargs)
    d._write_pending_andon = lambda agent, halt: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda agent, marker: None  # type: ignore[method-assign]
    d._current_turn_id = "s_test#1"
    return d


# ── intents type ──────────────────────────────────────────────────────


def test_post_execution_yield_is_frozen_dataclass() -> None:
    y = PostExecutionKaizenYield(
        skill_name="x", skill_path="/p", exit_status="success",
        execution_turn_id="t#1", suggested_action="promote",
    )
    assert y.skill_name == "x"
    with pytest.raises(Exception):
        y.skill_name = "y"  # type: ignore[misc]


# ── detection: _handle_andon_halt → per-turn flag ─────────────────────


def test_once_on_andon_sets_flag_and_ledger_event(monkeypatch) -> None:
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "once")
    ledger = MagicMock()
    result = d._handle_andon_halt(agent=MagicMock(), halt=_halt(), ledger=ledger)

    assert result == "once"
    flag = d._quarantine_skill_executed_this_turn
    assert flag is not None
    assert flag["skill_name"] == "my-skill"
    assert flag["skill_path"].endswith(".grove/skills/.andon/my-skill")
    assert flag["execution_turn_id"] == "s_test#1"

    # Additive ledger event (GATE-A decision 2) — NOT andon_disposition.
    event_names = [c.args[0] for c in ledger.record.call_args_list]
    assert "quarantine_skill_disposition" in event_names
    qsd = next(
        c for c in ledger.record.call_args_list
        if c.args[0] == "quarantine_skill_disposition"
    )
    assert qsd.kwargs["disposition"] == "once"
    assert qsd.kwargs["skill_name"] == "my-skill"


def test_non_andon_once_does_not_flag(monkeypatch) -> None:
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "once")
    halt = _halt(matched_rule="some.other.rule",
                 command="python3 /x/.grove/skills/promoted/run.py")
    d._handle_andon_halt(agent=MagicMock(), halt=halt, ledger=MagicMock())
    assert d._quarantine_skill_executed_this_turn is None


def test_session_disposition_does_not_flag(monkeypatch) -> None:
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "session")
    d._handle_andon_halt(agent=MagicMock(), halt=_halt(), ledger=MagicMock())
    assert d._quarantine_skill_executed_this_turn is None


def test_non_terminal_tool_does_not_flag(monkeypatch) -> None:
    d = _dispatcher(monkeypatch, sovereign_prompt_handler=lambda h: "once")
    halt = _halt(tool="skill_view")
    d._handle_andon_halt(agent=MagicMock(), halt=halt, ledger=MagicMock())
    assert d._quarantine_skill_executed_this_turn is None


# ── emission dispatch: _emit_post_execution_kaizen ────────────────────


def _flag(cache_key="ck") -> dict:
    return {
        "skill_name": "my-skill",
        "skill_path": "/Users/op/.grove/skills/.andon/my-skill",
        "execution_turn_id": "s_test#1",
        "cache_key": cache_key,
    }


def test_non_tty_auto_logs_pending_promotion(monkeypatch, queue_file: Path) -> None:
    d = _dispatcher(monkeypatch)  # no post_execution handler → headless
    d._emit_post_execution_kaizen(_flag(), ledger=MagicMock())

    proposals = read_all(path=queue_file)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.type == PROPOSAL_TYPE_SKILL_PROMOTION
    assert p.payload["skill_name"] == "my-skill"
    assert p.eval_hash.startswith("sha256:")


def test_promote_choice_invokes_promotion(monkeypatch, queue_file: Path) -> None:
    """In normal mode, Promote routes to the immediate promotion flow
    (Phase 3) — it calls grove.sovereignty.promote, it does not queue."""
    promote_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.promote", promote_mock)
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", MagicMock())
    monkeypatch.setattr(
        "agent.prompt_builder.clear_skills_system_prompt_cache", MagicMock(),
    )
    d = _dispatcher(monkeypatch, post_execution_prompt_handler=lambda p: "promote")
    monkeypatch.setattr(d, "_skill_promotion_is_strict", lambda: False)
    d._emit_post_execution_kaizen(_flag(), ledger=MagicMock())
    promote_mock.assert_called_once_with("my-skill")
    assert read_all(path=queue_file) == []


def test_not_yet_is_noop(monkeypatch, queue_file: Path) -> None:
    d = _dispatcher(monkeypatch, post_execution_prompt_handler=lambda p: "not_yet")
    d._emit_post_execution_kaizen(_flag(), ledger=MagicMock())
    assert read_all(path=queue_file) == []
    assert "ck" not in d._session_deny_cache


def test_never_adds_to_deny_cache(monkeypatch, queue_file: Path) -> None:
    d = _dispatcher(monkeypatch, post_execution_prompt_handler=lambda p: "never")
    d._emit_post_execution_kaizen(_flag(cache_key="deny-me"), ledger=MagicMock())
    assert "deny-me" in d._session_deny_cache
    assert read_all(path=queue_file) == []


def test_never_purge_calls_reject(monkeypatch, queue_file: Path) -> None:
    reject_mock = MagicMock()
    monkeypatch.setattr("grove.sovereignty.reject", reject_mock)
    d = _dispatcher(monkeypatch, post_execution_prompt_handler=lambda p: "never_purge")
    d._emit_post_execution_kaizen(_flag(), ledger=MagicMock())
    reject_mock.assert_called_once()
    assert reject_mock.call_args.args[0] == "my-skill"


def test_handler_error_degrades_to_autolog(monkeypatch, queue_file: Path) -> None:
    def _boom(payload):
        raise RuntimeError("handler blew up")

    d = _dispatcher(monkeypatch, post_execution_prompt_handler=_boom)
    d._emit_post_execution_kaizen(_flag(), ledger=MagicMock())
    proposals = read_all(path=queue_file)
    assert len(proposals) == 1
    assert proposals[0].type == PROPOSAL_TYPE_SKILL_PROMOTION


# ── TTY renderer ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inputs,expected",
    [
        (["1"], "promote"),
        (["2"], "not_yet"),
        (["3", "y"], "never_purge"),
        (["3", "n"], "never"),
        (["promote"], "promote"),
    ],
)
def test_tty_prompt_choice_parsing(monkeypatch, inputs, expected) -> None:
    import builtins
    from grove.sovereign_prompt_handlers import tty_post_execution_prompt

    it = iter(inputs)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: next(it))
    payload = PostExecutionKaizenYield(
        skill_name="my-skill", skill_path="/p", exit_status="success",
        execution_turn_id="t#1", suggested_action="promote",
    )
    assert tty_post_execution_prompt(payload, out=None) == expected


def test_tty_prompt_eof_defaults_not_yet(monkeypatch) -> None:
    import builtins
    from grove.sovereign_prompt_handlers import tty_post_execution_prompt

    def _eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)
    payload = PostExecutionKaizenYield(
        skill_name="s", skill_path="/p", exit_status="success",
        execution_turn_id="t#1", suggested_action="promote",
    )
    assert tty_post_execution_prompt(payload) == "not_yet"
