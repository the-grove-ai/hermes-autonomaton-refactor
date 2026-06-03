"""Sprint 53.2 Phase 5 — skill quarantine pipeline integration (T16–T19).

These wire the REAL component seams end-to-end over a tmp grove home —
ZoneClassifier (against the shipped repo schema), the Dispatcher's halt
detection + post-execution emission, grove.sovereignty.promote/reject
(real .andon → active filesystem moves), the proposal queue, and the
Kaizen ledger. No live LLM.

SPEC amendment: the contract nominated live-CLI PTY tests in
tests/integration/test_live_cli.py. The lifecycle ("the LLM authors a
skill, then runs it across turns with two interleaved prompts") is too
non-deterministic to assert reliably as a live test, and the live
harness's orphan-PID conftest fights in-process Dispatcher construction.
So T16–T19 are implemented as deterministic component-integration tests
in tests/grove/ — exercising the same seams, running in the default
suite. See the HANDOFF.

Skill names are uuid-suffixed and every artifact is isolated under a tmp
grove home, so nothing touches the operator's real ~/.grove.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.eval.proposal_queue import PROPOSAL_TYPE_SKILL_PROMOTION, read_all
from grove.intents import ToolIntent
from grove.kaizen_ledger import KaizenLedger
from grove.zones import ZoneClassifier, ZoneResult

REPO_SCHEMA = Path(__file__).resolve().parents[2] / "config" / "zones.schema.yaml"


@pytest.fixture
def tmp_home(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    home = tmp_path / "grove"
    (home / "skills").mkdir(parents=True)

    import grove.skills as gskills
    import hermes_constants
    import grove.eval.proposal_queue as pq

    monkeypatch.setattr(gskills, "get_hermes_home", lambda: home)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pq, "default_queue_path", lambda: home / "proposals.jsonl")

    # Zone-rule writes + cache drops + telemetry are unit-tested elsewhere;
    # mock them here so the lifecycle focuses on the move / queue / ledger
    # seams and never touches the operator's real zones or telemetry.
    save_rule = MagicMock()
    clear_cache = MagicMock()
    monkeypatch.setattr("grove.zone_rules.save_zone_rule", save_rule)
    monkeypatch.setattr(
        "agent.prompt_builder.clear_skills_system_prompt_cache", clear_cache,
    )
    monkeypatch.setattr("grove.sovereignty.log_sovereignty_decision", MagicMock())

    return SimpleNamespace(
        path=home, save_rule=save_rule, clear_cache=clear_cache,
    )


def _make_quarantined_skill(home: Path, name: str) -> Path:
    sd = home / "skills" / ".andon" / name
    (sd / "scripts").mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A quarantine lifecycle test skill.\n"
        f"---\n\n# {name}\n\nRun the script.\n",
        encoding="utf-8",
    )
    (sd / "scripts" / "run.py").write_text("print('ran')\n", encoding="utf-8")
    return sd


def _andon_halt(command: str) -> AndonHalt:
    intents = [ToolIntent(tool_name="terminal", arguments={"command": command}, call_id="c1")]
    zr = [ZoneResult(
        zone="yellow", matched_rule=r".*\.grove/skills/\.andon/.*", source="default",
    )]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


def _dispatcher(monkeypatch, *, sovereign="once", post=None, strict=False) -> Dispatcher:
    d = Dispatcher(
        sovereign_prompt_handler=lambda h: sovereign,
        post_execution_prompt_handler=(lambda p: post) if post else None,
    )
    d._write_pending_andon = lambda a, h: None  # type: ignore[method-assign]
    d._clear_pending_andon = lambda a, m: None  # type: ignore[method-assign]
    d._current_turn_id = "sess#1"
    monkeypatch.setattr(d, "_skill_promotion_is_strict", lambda: strict)
    return d


def _classify(cmd: str) -> str:
    cls = ZoneClassifier(REPO_SCHEMA)
    return cls.classify_command_string(
        cmd, "command.execute.python3", tool_id="terminal",
    ).zone


def _flag(name: str, andon: Path, turn="sess#1", cache_key="ck") -> dict:
    return {
        "skill_name": name,
        "skill_path": str(andon),
        "execution_turn_id": turn,
        "cache_key": cache_key,
    }


# ── T16: full lifecycle ───────────────────────────────────────────────


def test_T16_full_lifecycle(monkeypatch, tmp_home) -> None:
    home = tmp_home.path
    name = f"test-skill-532-{uuid.uuid4().hex[:8]}"
    andon = _make_quarantined_skill(home, name)
    assert (andon / "SKILL.md").exists()

    andon_cmd = f"python3 /op/.grove/skills/.andon/{name}/scripts/run.py"
    promoted_cmd = f"python3 /op/.grove/skills/{name}/scripts/run.py"

    # Execute the quarantined skill → yellow (the four-choice Kaizen fires).
    assert _classify(andon_cmd) == "yellow"

    ledger = KaizenLedger("sess", ledger_dir=home / ".kaizen_ledger")
    d = _dispatcher(monkeypatch, sovereign="once", post="promote")

    # Allow once → flag set + additive ledger event.
    disp = d._handle_andon_halt(agent=MagicMock(), halt=_andon_halt(andon_cmd), ledger=ledger)
    assert disp == "once"
    assert d._quarantine_skill_executed_this_turn["skill_name"] == name

    # Post-execution prompt → Promote → real move out of quarantine.
    d._emit_post_execution_kaizen(d._quarantine_skill_executed_this_turn, ledger=ledger)
    assert not andon.exists()
    assert (home / "skills" / name / "SKILL.md").exists()

    # Promotion wrote a green zone rule and dropped the skills cache.
    tmp_home.save_rule.assert_called_once()
    assert tmp_home.save_rule.call_args.kwargs["zone"] == "green"
    tmp_home.clear_cache.assert_called_once_with(clear_snapshot=True)

    # Ledger captured the disposition and the promotion.
    types = {e["event_type"] for e in ledger.events()}
    assert "quarantine_skill_disposition" in types
    assert "skill_promoted" in types

    # Re-execute the now-promoted path → green (no halt).
    assert _classify(promoted_cmd) == "green"


# ── T17: strict mode blocks auto-promotion ────────────────────────────


def test_T17_strict_blocks_auto_promotion(monkeypatch, tmp_home) -> None:
    home = tmp_home.path
    name = f"test-skill-532-{uuid.uuid4().hex[:8]}"
    andon = _make_quarantined_skill(home, name)

    ledger = KaizenLedger("sess", ledger_dir=home / ".kaizen_ledger")
    d = _dispatcher(monkeypatch, post="promote", strict=True)
    d._emit_post_execution_kaizen(_flag(name, andon), ledger=ledger)

    # Strict mode does NOT move the skill — it queues a pending proposal.
    assert andon.exists()
    assert not (home / "skills" / name).exists()

    proposals = read_all(path=home / "proposals.jsonl")
    assert len(proposals) == 1
    assert proposals[0].type == PROPOSAL_TYPE_SKILL_PROMOTION
    assert proposals[0].payload["skill_name"] == name


# ── T18: "Never" denies and caches ────────────────────────────────────


def test_T18_never_denies_and_purges(monkeypatch, tmp_home) -> None:
    home = tmp_home.path
    name = f"test-skill-532-{uuid.uuid4().hex[:8]}"
    andon = _make_quarantined_skill(home, name)

    ledger = KaizenLedger("sess", ledger_dir=home / ".kaizen_ledger")
    d = _dispatcher(monkeypatch, post="never_purge")
    d._emit_post_execution_kaizen(_flag(name, andon, cache_key="deny-me"), ledger=ledger)

    # Purged from quarantine; the command's cache key is denied for the session.
    assert not andon.exists()
    assert "deny-me" in d._session_deny_cache
    types = {e["event_type"] for e in ledger.events()}
    assert "skill_promotion_denied" in types


def test_T18b_never_without_purge_only_denies(monkeypatch, tmp_home) -> None:
    home = tmp_home.path
    name = f"test-skill-532-{uuid.uuid4().hex[:8]}"
    andon = _make_quarantined_skill(home, name)

    ledger = KaizenLedger("sess", ledger_dir=home / ".kaizen_ledger")
    d = _dispatcher(monkeypatch, post="never")
    d._emit_post_execution_kaizen(_flag(name, andon, cache_key="deny-me"), ledger=ledger)

    # Plain "never": deny cache only, quarantine dir left in place.
    assert andon.exists()
    assert "deny-me" in d._session_deny_cache


# ── T19: feedback loop (not yet → modify → promote) ───────────────────


def test_T19_feedback_loop(monkeypatch, tmp_home) -> None:
    home = tmp_home.path
    name = f"test-skill-532-{uuid.uuid4().hex[:8]}"
    andon = _make_quarantined_skill(home, name)
    ledger = KaizenLedger("sess", ledger_dir=home / ".kaizen_ledger")

    # First run: operator picks "Not yet" — the skill stays quarantined.
    d1 = _dispatcher(monkeypatch, post="not_yet")
    d1._emit_post_execution_kaizen(_flag(name, andon), ledger=ledger)
    assert andon.exists()
    assert not (home / "skills" / name).exists()

    # Operator iterates on the quarantined skill in place.
    (andon / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Improved on the feedback loop.\n---\n\n"
        f"# {name}\n\nBetter now.\n",
        encoding="utf-8",
    )

    # Next run: operator promotes — the modified skill moves to active.
    d2 = _dispatcher(monkeypatch, post="promote")
    d2._current_turn_id = "sess#2"
    d2._emit_post_execution_kaizen(
        _flag(name, andon, turn="sess#2"), ledger=ledger,
    )
    assert not andon.exists()
    promoted = home / "skills" / name / "SKILL.md"
    assert promoted.exists()
    assert "Improved on the feedback loop." in promoted.read_text(encoding="utf-8")
