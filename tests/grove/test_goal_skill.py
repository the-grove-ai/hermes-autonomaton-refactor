"""An isolated goal turn loads the goal's skill from its keg, the turn record
says so, and a model turn without it cannot record a decision
(compiler-shapes-v1, Task 3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import grove.grants as grants_mod
import grove.pattern_cache as pc
from grove import decision_work as dw
from grove import turn_provenance
from grove.decision_work import DecisionRefused, DecisionWork
from grove.dispatcher import Dispatcher
from grove.prompt.composer import _PROVIDER_GATEABLE_BLOCK, build_default_composer

from tests.grove.test_work_session import GOAL, SESSION, _Grants, _goal

SKILL = {
    "slug": "message-triage", "body": "# Triage\n\nRead the message.",
    "keg_id": "keg:message-triage-skill:v1:abcabcabcabc", "version": 1,
    "content_hash": "sha256:" + "ab" * 32, "commit": "c" * 40,
}
LOADED = {"keg_id": SKILL["keg_id"], "version": "1", "content_hash": SKILL["content_hash"]}


# ── the composer section ───────────────────────────────────────────────


def test_goal_skill_section_composes_the_skill_body_for_an_isolated_turn():
    out = build_default_composer().compose(
        valid_tool_names=set(), goal_skills=[SKILL],
        tier_context_blocks=frozenset({"cellar_context"}),      # T1's allow-list
    )
    assert "Read the message." in out.sections["goal_skill"]
    assert SKILL["keg_id"] in out.sections["goal_skill"]
    assert "goal_skill" not in _PROVIDER_GATEABLE_BLOCK


def test_goal_skill_is_absent_when_no_goal_is_isolated():
    out = build_default_composer().compose(valid_tool_names=set(), goal_skills=[])
    assert "goal_skill" not in out.sections
    out = build_default_composer().compose(valid_tool_names=set())
    assert "goal_skill" not in out.sections


# ── provenance and the turn record ─────────────────────────────────────


def _dispatcher_standin(**over):
    d = SimpleNamespace(
        _current_turn_routing_decision=None, _current_turn_t0_pattern=None,
        _current_turn_session_step=None, _current_turn_id="s#1", _current_turn_uid="u1",
        _current_turn_user_message="next", _current_turn_tools_yielded=[],
        _current_turn_isolation=GOAL, session_id="s",
    )
    for k, v in over.items():
        setattr(d, k, v)
    d.turn_provenance = Dispatcher.turn_provenance.__get__(d)
    return d


def test_provenance_names_the_skill_on_a_model_turn_and_none_at_t0():
    agent = SimpleNamespace(
        _composed_prompt=SimpleNamespace(sections={"goal_skill": "x", "identity": "y"}),
        _goal_skills=[SKILL], model="m", session_id="s",
    )
    assert _dispatcher_standin().turn_provenance(agent)["skills_loaded"] == [LOADED]
    # composed without the section (a recompose that dropped it): nothing was loaded
    agent.__dict__["_composed_prompt"] = SimpleNamespace(sections={"identity": "y"})
    assert _dispatcher_standin().turn_provenance(agent)["skills_loaded"] == []
    agent.__dict__["_composed_prompt"] = SimpleNamespace(sections={"goal_skill": "x"})
    assert _dispatcher_standin(_current_turn_session_step="t0").turn_provenance(agent)["skills_loaded"] == []
    assert _dispatcher_standin(_current_turn_t0_pattern="keg:x:v1:a").turn_provenance(agent)["skills_loaded"] == []


def test_the_turn_record_names_the_skill_it_loaded(monkeypatch, tmp_path):
    from grove.intent_store import IntentStore
    from tests.grove.test_dispatcher_intent_records import (
        _bare_agent_with_exec, _patch_classifier_green, _set_current_classification,
        _synthetic_generator,
    )

    _patch_classifier_green(monkeypatch)
    _set_current_classification(monkeypatch, intent_class="analysis")
    monkeypatch.setattr(dw, "isolating_goal_for", lambda message: GOAL)
    monkeypatch.setattr("grove.keg_store.skill_kegs_for_goal", lambda goal, **kw: [SKILL])
    agent = _bare_agent_with_exec([])
    agent._run_turn_generator = lambda **kw: _synthetic_generator(
        None, {"final_response": "done"}, final_text="done")
    store = IntentStore(store_path=tmp_path / "records.jsonl")
    d = Dispatcher(intent_store=store)
    # The bare agent never composes; stand in for the composed prompt.
    agent._composed_prompt = SimpleNamespace(sections={"goal_skill": "x"})
    d.dispatch_turn(agent, user_message="tag the next message")
    [rec] = list(store.records())
    assert rec.skills_loaded == (LOADED,)
    assert agent._goal_skills == [SKILL]


def test_a_goal_skills_change_forces_a_recompose():
    d = Dispatcher.__new__(Dispatcher)
    d._last_applied_tier_context_blocks = "B"
    d._last_applied_isolated_sections = None
    d._last_loaded_primary_slug = None
    d._current_turn_classification = None
    d.agent = SimpleNamespace(_tier_context_blocks="B", _isolated_sections=None, _goal_skills=[SKILL])
    calls = []
    d.recompose_system_prompt = lambda **kw: calls.append("recompose")
    d._maybe_recompose_for_tier(d.agent)
    assert calls == ["recompose"]
    d._maybe_recompose_for_tier(d.agent)
    assert calls == ["recompose"]            # same skills: no second recompose


# ── Jidoka: a model turn without the skill cannot decide ──────────────


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pc.db")
    grants = _Grants()
    monkeypatch.setattr(grants_mod, "get_grant_store", lambda: grants)
    token = turn_provenance.set_current(None)
    goal = _goal(tmp_path, SESSION)
    goal.unlocked_skills = ("message-triage",)
    cfg = dw.load_config(goal)
    grants.sign(cfg)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
    (tmp_path / "queue" / "m01.txt").write_text("billing")
    yield DecisionWork(cfg)
    turn_provenance.reset(token)


def _prov(**over):
    base = {"session_id": "sess", "turn_id": "sess#1", "turn_uid": "u1", "tier": "T1",
            "model": "m", "request": "tag the next message", "cellar_hits": 0,
            "sections": [], "tools_yielded": ["tag_message"], "isolation_goal": GOAL,
            "skills_loaded": [LOADED]}
    base.update(over)
    return base


def test_a_model_turn_without_the_skill_cannot_record_a_decision(work):
    item = work.next_item()
    with pytest.raises(DecisionRefused) as refused:
        work.record(item_id=item.stem, inputs={"channel": "billing"}, output={"tag": "finance"},
                    reasoning="billing", provenance=_prov(skills_loaded=[]))
    assert refused.value.reason == "no_skill_loaded"
    assert "without the goal's skill (message-triage)" in str(refused.value)
    rec = work.record(item_id=item.stem, inputs={"channel": "billing"}, output={"tag": "finance"},
                      reasoning="billing", provenance=_prov())
    assert rec["output"] == {"tag": "finance"}


def test_a_t0_turn_needs_no_skill(work):
    work.check_turn(_prov(tier="T0", model="session_rule", session_step="t0", skills_loaded=[]))
    work.check_turn(_prov(tier="T0", model="pattern_cache", t0_pattern="keg:x:v1:a", skills_loaded=[]))


def test_a_goal_that_unlocks_no_skill_is_not_held_to_one(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pc.db")
    grants = _Grants()
    monkeypatch.setattr(grants_mod, "get_grant_store", lambda: grants)
    cfg = dw.load_config(_goal(tmp_path, SESSION))
    grants.sign(cfg)
    assert cfg.unlocked_skills == ()
    DecisionWork(cfg).check_turn(_prov(skills_loaded=[]))
