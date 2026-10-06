"""The improvement loop end to end on a goal's decision work:
standard work → Jidoka → andon → Kaizen → signed → new standard work.

MESSAGE-TAGGING fixtures throughout: the loop is generic, so its tests may not
lean on any one domain.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

import grove.pattern_cache as pc
from grove import decision_work as dw
from grove import flywheel_cli as fc
from grove import jidoka, keg
from grove.decision_work import DecisionWork
from grove.eval.proposal_queue import read_all
from grove.kaizen import standard_work
from grove.kaizen_ledger import default_ledger_dir
from grove.pattern_cache import (
    PatternCacheStore, STATUS_ACTIVE, STATUS_DEMOTED, STATUS_HALTED,
    STATUS_SUPERSEDED, STATUS_SUSPENDED,
)

GOAL = "message-triage"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)
    (tmp_path / "queue").mkdir()
    (tmp_path / "channels.csv").write_text(
        "Channel,Default Tag\nbilling,finance\noutage,ops\n"
        "legal,contracts / compliance\npress,comms\n", encoding="utf-8")
    (tmp_path / "tags.csv").write_text(
        "Tag\nfinance\nops\ncontracts\ncompliance\ncomms\nescalate\nother\n", encoding="utf-8")
    goal = SimpleNamespace(
        id=GOAL, root=tmp_path, keywords=("message",), resolved_sources=lambda: [],
        extra={"decision_work": {
            "tool": "tag_message", "queue": "queue", "isolation": "sources_only",
            "inputs": {"channel": {"data_type": "string", "required": True},
                       "subject": {"data_type": "string", "required": False}},
            "outputs": {"tag": {"data_type": "string"}},
            "reference_table": {"path": "channels.csv", "key_column": "Channel",
                                "value_column": "Default Tag", "key_input": "channel",
                                "value_output": "tag"},
            "output_domains": [{"output": "tag", "path": "tags.csv", "column": "Tag"}],
            "evidence": {"threshold": 3},
            "keg": {"name": "Message tagging", "request": "tag the next message"},
        }},
    )
    cfg = dw.load_config(goal)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)

    class Env:
        store = PatternCacheStore(db)
        work = DecisionWork(cfg)      # the goal's default log, under the test home
        n = 0

        def add(self, channel, subject=""):
            self.n += 1
            (tmp_path / "queue" / f"m{self.n:02d}.txt").write_text(
                json.dumps({"channel": channel, "subject": subject}))

        def prov(self, **over):
            base = {"session_id": "s", "turn_id": f"s#{self.n}", "turn_uid": f"u{self.n}",
                    "tier": "T1", "model": "m", "cellar_hits": 0, "sections": [],
                    "tools_yielded": [], "isolation_goal": GOAL}
            base.update(over)
            return base

        def inputs(self):
            return json.loads(self.work.next_item().read_text())

        def code(self, tag, decision="confirm", corrected=None):
            item = self.work.next_item()
            self.work.record(item_id=item.stem, inputs=self.inputs(), output={"tag": tag},
                             reasoning="", provenance=self.prov())
            return self.decide(decision, corrected)

        def decide(self, decision="confirm", corrected=None):
            self.work.decide(
                decision=decision,
                corrected_output={"tag": corrected} if corrected else None,
                provenance=self.prov())
            return self.work.last_observations

        def serve(self):
            """What the T0 path does: apply the active keg to the next item."""
            [entry] = [e for e in self.store.all() if e.status == STATUS_ACTIVE]
            spec = keg.keg_of(entry)
            item = self.work.next_item()
            return self.work.apply_keg(
                spec, item_id=item.stem, inputs=self.inputs(),
                keg_ref={"name": spec["name"], "version": spec["version"],
                         "pattern_id": entry.pattern_id},
                provenance=self.prov(tier="T0", model="pattern_cache"))

        def sign(self):
            [proposal] = read_all()
            assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0
            return proposal

        def events(self):
            out = []
            for f in default_ledger_dir().glob("*.jsonl"):
                out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            return sorted(out, key=lambda e: e.get("timestamp") or e.get("ts") or "")

        def steps(self):
            return [e["loop_step"] for e in self.events() if e.get("loop_step")]

    return Env()


def _earn_v1(env):
    for channel, tag in (("billing", "finance"), ("legal", "contracts"),
                         ("outage", "ops"), ("billing", "finance")):
        env.add(channel)
        observations = env.code(tag)
    return observations


def _call(*conditions):
    """A stand-in for the tier call: returns the given drafts in order and
    records which tier each was asked of."""
    drafts, asked = list(conditions), []

    def call(prompt, *, system=None, tool=None, tier=None, max_tokens=0):
        asked.append(tier)
        return {"condition": drafts.pop(0), "rationale": "r"}

    call.asked = asked
    return call


# ── defer ─────────────────────────────────────────────────────────────


def test_defer_rule_hands_the_case_back_and_wins_by_order():
    spec = {
        "inputs": {"channel": {"data_type": "string"}, "subject": {"data_type": "string"}},
        "conditions": [
            {"if": "channel == 'billing' AND subject CONTAINS 'refund'", "defer": True},
            {"if": "channel == 'billing'", "then": {"tag": "finance"}},
        ],
    }
    assert keg.evaluate(spec, {"channel": "billing", "subject": "Refund please"}) is None
    assert keg.defers(spec, {"channel": "billing", "subject": "Refund please"}) is True
    assert keg.evaluate(spec, {"channel": "billing", "subject": "invoice"}) == {"tag": "finance"}
    assert keg.defers(spec, {"channel": "press", "subject": "x"}) is False


def test_a_rule_cannot_both_defer_and_answer():
    from tests.grove.test_keg import _spec
    keg.validate_spec(_spec(conditions=[{"if": "channel == 'a'", "defer": True}]))
    for bad in ({"if": "channel == 'a'", "defer": True, "then": {"tag": "x"}},
                {"if": "channel == 'a'"}):
        with pytest.raises(ValueError):
            keg.validate_spec(_spec(conditions=[bad]))


# ── tier-down: Jidoka flags, Kaizen drafts from the reference table ───


def test_reference_agreement_raises_one_flag_and_one_proposal(env):
    observations = _earn_v1(env)
    [andon] = observations
    assert (andon["flag"], andon["detector"], andon["goal"], andon["stops_line"]) == (
        keg.FLAG_TIER_DOWN_PATTERN, jidoka.DETECTOR_REFERENCE_AGREEMENT, GOAL, False)
    assert [e["item_id"] for e in andon["provenance"]] == ["m01", "m03", "m04"]
    assert andon["kaizen"]["status"] == "proposed" and andon["kaizen"]["version"] == 1

    # The trace reads as the loop's separate, linked steps.
    assert env.steps() == [keg.LOOP_JIDOKA_FLAG, keg.LOOP_ANDON_EVENT, keg.LOOP_KAIZEN_PROPOSAL]
    flag, cord, proposal = [e for e in env.events() if e.get("loop_step")]
    assert cord["flag_id"] == flag["flag_id"] and proposal["andon_id"] == cord["andon_id"]

    [queued] = read_all()
    k = queued.payload["keg"]
    assert k["andon_id"] == andon["andon_id"] and k["dock_goal"] == GOAL
    # The procedure, not the items seen: every single-value key is covered,
    # including one never decided ("press"); the multi-value key is not.
    assert [c["if"] for c in k["conditions"]] == [
        "channel == 'billing'", "channel == 'outage'", "channel == 'press'"]
    assert env.store.get(queued.payload["pattern_id"]).status == STATUS_SUSPENDED

    # Already answered: more confirmations raise nothing further.
    env.add("outage")
    assert env.code("ops") == []
    assert len(read_all()) == 1


def test_signed_keg_decides_without_a_model_and_declines_what_it_does_not_cover(env):
    _earn_v1(env)
    env.sign()
    env.add("press")                       # never decided before
    record = env.serve()
    assert record["output"] == {"tag": "comms"} and record["tier"] == "T0"
    assert record["keg"]["name"] == "Message tagging" and record["keg"]["version"] == 1
    assert env.decide() == []
    env.add("legal")                       # multi-value key: not covered
    assert env.serve() is None
    env.code("contracts")                  # the interpreter takes it
    env.add("social")                      # not in the table at all
    assert env.serve() is None


# ── anomaly: the line stops, Kaizen drafts the narrowest fix ──────────


def _miss(env, call):
    _earn_v1(env)
    env.sign()
    env.add("billing", "Refund for order 7")
    env.serve()
    real = standard_work.draft_condition
    standard_work.draft_condition = (
        lambda *a, **kw: real(*a, **{**kw, "call": call}))
    try:
        return env.decide("correct", "escalate")
    finally:
        standard_work.draft_condition = real


def test_correction_of_a_keg_answer_halts_it_and_proposes_a_deferring_v2(env):
    good = "channel == 'billing' AND subject CONTAINS 'refund'"
    call = _call(good)
    [andon] = _miss(env, call)
    assert (andon["flag"], andon["stops_line"]) == (keg.FLAG_ANOMALY, True)
    assert andon["details"]["served"] == {"tag": "finance"}
    assert andon["details"]["corrected"] == {"tag": "escalate"}

    # Halted — not a draft again. Its grant stands; it just does not serve.
    [v1_id] = andon["halted"]
    assert env.store.get(v1_id).status == STATUS_HALTED
    assert keg.lifecycle(STATUS_HALTED)["status"] == "stable"
    assert env.store.get_active_for_message("tag the next message") is None

    assert andon["kaizen"]["status"] == "proposed" and andon["kaizen"]["version"] == 2
    assert call.asked == ["T1"]            # the cheapest tier was enough
    [queued] = read_all()
    k = queued.payload["keg"]
    assert k["supersedes"] == v1_id and k["andon_id"] == andon["andon_id"]
    # Narrowest change: one new rule, first, that hands the cases back.
    assert k["conditions"][0] == {"if": good, "defer": True}
    assert len(k["conditions"]) == 4
    assert (queued.detail["would_change"], queued.detail["cases"][0]["ref"]) == (1, "m05")
    case = queued.detail["cases"][0]
    assert case["keg"] is None and case["deferred"] and case["agrees_with_confirmed"] is True

    env.sign()
    assert env.store.get(v1_id).status == STATUS_SUPERSEDED
    env.add("billing", "refund again")
    assert env.serve() is None             # deferred to the interpreter
    env.code("escalate")
    env.add("billing", "March statement")
    assert env.serve()["output"] == {"tag": "finance"}   # the rest still serves
    # Both passes of the loop, in order: earn v1, then the miss and v2.
    once = [keg.LOOP_JIDOKA_FLAG, keg.LOOP_ANDON_EVENT, keg.LOOP_KAIZEN_PROPOSAL,
            keg.LOOP_SIGNED, keg.LOOP_NEW_STANDARD_WORK]
    assert env.steps() == once + once
    work = [e for e in env.events() if e["event_type"] == "new_standard_work"]
    assert [w["version"] for w in work] == [1, 2] and work[1]["replaces"] == [v1_id]


def test_a_refused_draft_is_retried_one_tier_up(env):
    call = _call("channel == 'billing'",          # also true for confirmed cases
                 "channel == 'billing' AND subject CONTAINS 'refund'")
    [andon] = _miss(env, call)
    assert call.asked == ["T1", "T2"]
    attempts = andon["kaizen"]["attempts"]
    assert "confirmed" in attempts[0]["refused"] and attempts[1]["refused"] is None
    assert andon["kaizen"]["status"] == "proposed"


@pytest.mark.parametrize("drafts", [
    ("not a condition", "channel = 'billing'"),
    ("subject CONTAINS 'zzz'", "channel == 'outage'"),   # false for the missed case
])
def test_when_no_tier_can_draft_kaizen_says_so_and_proposes_nothing(env, drafts):
    [andon] = _miss(env, _call(*drafts))
    assert andon["kaizen"]["status"] == "draft_failed"
    assert read_all() == []
    assert env.store.get(andon["halted"][0]).status == STATUS_HALTED   # line stays stopped
    flags = [e for e in env.events() if e["event_type"] == "jidoka_flag"]
    assert flags[-1]["detector"] == "kaizen_draft"


def test_correction_of_a_model_answer_is_flagged_but_stops_nothing(env):
    env.add("billing")
    [andon] = env.code("ops", decision="correct", corrected="finance")
    assert andon["flag"] == keg.FLAG_ANOMALY and andon["halted"] == []
    assert andon["kaizen"] is None and read_all() == []


def test_watcher_fault_never_unrecords_the_operators_decision(env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(jidoka, "observe_decision", boom)
    env.add("billing")
    env.code("finance")
    assert [r["kind"] for r in env.work.log.run_records()] == ["proposed", "decided"]
    assert "ledger down" in env.work.last_observation_error


# ── feedback goes back to Kaizen ──────────────────────────────────────


def test_rejection_feedback_is_answered_with_a_revised_draft(env, monkeypatch):
    _earn_v1(env)
    [first] = read_all()
    call = _call("channel == 'press'")
    monkeypatch.setattr(
        standard_work, "draft_condition",
        lambda *a, _real=standard_work.draft_condition, **kw: _real(*a, **{**kw, "call": call}))
    assert fc.cli_reject(first.proposal_id.split(":")[-1][:12],
                         reason="leave press to a person") == 0
    [revised] = read_all()
    k = revised.payload["keg"]
    assert k["version"] == 1 and k["feedback"] == ["leave press to a person"]
    assert k["conditions"][0] == {"if": "channel == 'press'", "defer": True}
    assert k["andon_id"] == first.payload["keg"]["andon_id"]   # same event answered
    assert revised.proposal_id != first.proposal_id


# ── versions belong to a run ──────────────────────────────────────────


def test_a_new_run_starts_again_at_v1(env):
    _earn_v1(env)
    env.sign()
    [v1] = [e for e in env.store.all() if e.status == STATUS_ACTIVE]
    env.store.set_status(v1.pattern_id, STATUS_DEMOTED)       # what the reset does
    env.work.log.start_run("reset")
    env.n = 0
    [andon] = _earn_v1(env)
    assert andon["kaizen"]["status"] == "proposed" and andon["kaizen"]["version"] == 1


# ── the older scanner is a Jidoka detector too ────────────────────────


def test_repetition_scanner_flags_through_the_same_door(tmp_path, env):
    from grove.eval.pattern_compiler import propose_pattern_promotions
    from tests.grove.test_pattern_compiler import _CFG, _seed, _store

    intents = _store(tmp_path)
    _seed(intents, "what is our mission statement", "factual_lookup", 6, response="X.")
    queue = tmp_path / "proposals.jsonl"
    propose_pattern_promotions(intents, env.store, queue_path=queue, config=_CFG)
    assert env.steps() == [keg.LOOP_JIDOKA_FLAG, keg.LOOP_ANDON_EVENT, keg.LOOP_KAIZEN_PROPOSAL]
    flag, cord, proposal = [e for e in env.events() if e.get("loop_step")]
    assert (flag["detector"], flag["flag"], flag["goal"]) == (
        jidoka.DETECTOR_REPETITION, keg.FLAG_TIER_DOWN_PATTERN, None)
    assert cord["stops_line"] is False and proposal["andon_id"] == cord["andon_id"]
    assert proposal["proposal_id"] == read_all(path=queue)[0].proposal_id


# ── T0 contract and the joined trace ──────────────────────────────────


def test_t0_decline_contract():
    from grove.dispatcher import _t0_declined
    assert _t0_declined('{"t0_declined": true, "reason": "x"}') is True
    for served in ("Coded 6110.", '{"t0_declined": false}', '{"success": true}', "", None, "{oops"):
        assert _t0_declined(served) is False


def test_tool_sees_a_t0_serve_as_t0():
    from grove.dispatcher import Dispatcher
    d = Dispatcher.__new__(Dispatcher)
    d.session_id, d._current_turn_id, d._current_turn_uid = "s", "s#1", "u"
    d._current_turn_routing_decision = SimpleNamespace(tier="T1")
    d._current_turn_tools_yielded, d._current_turn_isolation = [], None
    agent = SimpleNamespace(model="some-model", _cellar_retrieval_hits=0, _composed_prompt=None)
    assert d.turn_provenance(agent)["tier"] == "T1"
    d._current_turn_t0_pattern = "keg:x:v1:abc"
    prov = d.turn_provenance(agent)
    assert (prov["tier"], prov["model"], prov["t0_pattern"]) == ("T0", "pattern_cache", "keg:x:v1:abc")


def test_correction_path_halts_and_leaves_kegs_to_the_decision_feed():
    from grove.dispatcher import Dispatcher
    src = inspect.getsource(Dispatcher)
    start = src.index("log ``pattern_drift_detected``")
    body = src[start:src.index("def _queue_pattern_demotion_proposal", start)]
    assert "STATUS_HALTED" in body and "STATUS_SUSPENDED" not in body
    assert body.index("keg_of(pattern) is not None") < body.index("set_status(pattern_id, STATUS_HALTED)")


def test_turn_trace_joins_the_feed_by_turn_uid(tmp_path):
    from grove.trace import turn_trace
    (tmp_path / "intent_records.jsonl").write_text(
        json.dumps({"turn_uid": "u1", "outcome": "pending"}) + "\n"
        + json.dumps({"turn_uid": "u2", "outcome": "success"}) + "\n"
        + json.dumps({"turn_uid": "u1", "outcome": "success"}) + "\n")
    (tmp_path / ".capability_feed").mkdir()
    (tmp_path / ".capability_feed" / "feed.jsonl").write_text(
        json.dumps({"turn_uid": "u1", "tool_name": "tag_message"}) + "\n"
        + json.dumps({"turn_uid": "u2", "tool_name": "other"}) + "\n")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "g.jsonl").write_text(
        json.dumps({"turn_uid": "u1", "kind": "proposed", "item_id": "m01"}) + "\n")
    trace = turn_trace("u1", home=tmp_path)
    assert trace["intent"]["outcome"] == "success"
    assert [r["tool_name"] for r in trace["tool_rows"]] == ["tag_message"]
    assert [r["item_id"] for r in trace["decisions"]] == ["m01"]


def test_the_loop_carries_no_domain_vocabulary():
    from grove import trace
    for module in (jidoka, standard_work, trace):
        src = inspect.getsource(module).lower()
        for word in ("vendor", "invoice", "gl_code", "gl code", "chart of accounts"):
            assert word not in src, (module.__name__, word)
