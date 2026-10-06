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
from grove import andon, keg
from grove.detectors import decision_feed
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


LOOP_ONCE = [keg.LOOP_JIDOKA_FLAG, keg.LOOP_ANDON_EVENT, keg.LOOP_KAIZEN_PROPOSAL,
             andon.LOOP_KAIZEN_ANSWER, keg.LOOP_SIGNED, keg.LOOP_NEW_STANDARD_WORK]


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
    [event] = observations
    assert (event["flag"], event["detector"], event["goal"], event["stops_line"]) == (
        keg.FLAG_TIER_DOWN_PATTERN, "reference_agreement", GOAL, False)
    assert [e["item_id"] for e in event["provenance"]] == ["m01", "m03", "m04"]
    answer = event["answer"]
    assert (answer["kind"], answer["channel"], answer["surface_class"]) == (
        andon.KIND_STANDARD_WORK, andon.CHANNEL_PORTAL, andon.SURFACE_SCOPE_DEFINING)
    assert answer["detail"]["version"] == 1

    # The trace reads as the loop's separate, linked steps, and the close
    # commits to the flag and the event by their ledger hashes.
    assert env.steps() == LOOP_ONCE[:4]
    flag, cord, proposal, close = [e for e in env.events() if e.get("loop_step")]
    assert cord["flag_id"] == flag["flag_id"] and proposal["andon_id"] == cord["andon_id"]
    assert close["closes"] == [cord["andon_id"]] and close["artifact"] == proposal["proposal_id"]
    assert close["source_chain"][:2] == [flag["record_hash"], cord["record_hash"]]

    [queued] = read_all()
    k = queued.payload["keg"]
    assert k["andon_id"] == event["andon_id"] and k["dock_goal"] == GOAL
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
    [event] = _miss(env, call)
    assert (event["flag"], event["stops_line"]) == (keg.FLAG_ANOMALY, True)
    assert event["details"]["served"] == {"tag": "finance"}
    assert event["details"]["corrected"] == {"tag": "escalate"}
    andon_event, kaizen = event, event["answer"]["detail"]

    # Halted — not a draft again. Its grant stands; it just does not serve.
    [v1_id] = andon_event["halted"]
    assert env.store.get(v1_id).status == STATUS_HALTED
    assert keg.lifecycle(STATUS_HALTED)["status"] == "stable"
    assert env.store.get_active_for_message("tag the next message") is None

    assert event["answer"]["kind"] == andon.KIND_STANDARD_WORK and kaizen["version"] == 2
    assert call.asked == ["T1"]            # the cheapest tier was enough
    [queued] = read_all()
    k = queued.payload["keg"]
    assert k["supersedes"] == v1_id and k["andon_id"] == andon_event["andon_id"]
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
    assert env.steps() == LOOP_ONCE + LOOP_ONCE
    work = [e for e in env.events() if e["event_type"] == "new_standard_work"]
    assert [w["version"] for w in work] == [1, 2] and work[1]["replaces"] == [v1_id]


def test_a_refused_draft_is_retried_one_tier_up(env):
    call = _call("channel == 'billing'",          # also true for confirmed cases
                 "channel == 'billing' AND subject CONTAINS 'refund'")
    [event] = _miss(env, call)
    assert call.asked == ["T1", "T2"]
    attempts = event["answer"]["detail"]["attempts"]
    assert "confirmed" in attempts[0]["refused"] and attempts[1]["refused"] is None
    assert event["answer"]["kind"] == andon.KIND_STANDARD_WORK


@pytest.mark.parametrize("drafts", [
    ("not a condition", "channel = 'billing'"),
    ("subject CONTAINS 'zzz'", "channel == 'outage'"),   # false for the missed case
])
def test_when_no_tier_can_draft_the_failure_is_the_next_andon_event(env, drafts):
    # Kaizen's own failure goes back through the SAME handler and is answered
    # like any other event: here, by asking the operator for the condition.
    [event] = _miss(env, _call(*drafts))
    assert env.store.get(event["halted"][0]).status == STATUS_HALTED   # line stays stopped
    assert event["escalated_to"]
    flags = [e for e in env.events() if e["event_type"] == "jidoka_flag"]
    assert [f["detector_id"] for f in flags][-2:] == ["correction", "kaizen_failure"]
    cords = [e for e in env.events() if e["event_type"] == "andon_event"]
    assert cords[-1]["originating"] == [event["andon_id"]]
    assert cords[-1]["details"]["originating_andon_id"] == event["andon_id"]
    # One close covers both events — nothing is left open.
    [close] = [e for e in env.events() if e["event_type"] == "kaizen_answer"][-1:]
    assert set(close["closes"]) == {event["andon_id"], event["escalated_to"]}
    assert close["kind"] == andon.KIND_STANDARD_WORK and close["channel"] == andon.CHANNEL_PORTAL
    [request] = read_all()
    assert request.type == "kaizen_request" and close["artifact"] == request.proposal_id
    assert len(request.payload["attempts"]) == 2
    assert request.payload["miss"]["corrected"] == {"tag": "escalate"}


def test_operator_written_condition_comes_back_as_a_keg_proposal(env):
    [event] = _miss(env, _call("nope", "still nope"))
    [request] = read_all()
    assert fc.cli_reject(
        request.proposal_id.split(":")[-1][:12],
        reason="channel == 'billing' AND subject CONTAINS 'refund'") == 0
    [proposal] = read_all()
    k = proposal.payload["keg"]
    assert proposal.type == "pattern_promotion" and k["version"] == 2
    assert k["conditions"][0] == {
        "if": "channel == 'billing' AND subject CONTAINS 'refund'", "defer": True}
    # It is still only a proposal: the keg stays halted until it is signed.
    assert env.store.get(event["halted"][0]).status == STATUS_HALTED
    env.sign()
    assert env.store.get_active_for_message("tag the next message") is not None


def test_operator_condition_that_fails_its_checks_is_an_andon_event(env):
    _miss(env, _call("nope", "still nope"))
    [request] = read_all()
    before = len([e for e in env.events() if e["event_type"] == "andon_event"])
    fc.cli_reject(request.proposal_id.split(":")[-1][:12], reason="channel == 'billing'")
    cords = [e for e in env.events() if e["event_type"] == "andon_event"]
    assert len(cords) == before + 1 and cords[-1]["detector"] == "kaizen_failure"
    assert cords[-1]["details"]["reason"] == "condition_refused"


def test_correction_of_a_model_answer_is_watched_not_ignored(env):
    env.add("billing")
    [event] = env.code("ops", decision="correct", corrected="other")
    assert event["flag"] == keg.FLAG_ANOMALY and event["halted"] == []
    answer = event["answer"]
    assert (answer["kind"], answer["channel"], answer["surface_class"]) == (
        andon.KIND_WATCH, andon.CHANNEL_AUTOMATIC, andon.SURFACE_IN_SCOPE)
    assert "seen 1 of 3" in answer["summary"]      # the goal's evidence threshold
    entry = env.store.get(answer["artifact"])
    assert entry.status == pc.STATUS_WATCHING
    assert keg.lifecycle(entry.status) == {
        "status": "draft", "state": "watching", "serves": False, "halted": False}
    record = json.loads(entry.promotion_evidence)["watch"]
    assert record["signature"] == {"key": "billing", "corrected": {"tag": "other"}}
    assert record["trigger"] == {"seen": 3} and record["andon_ids"] == [event["andon_id"]]
    assert read_all() == []                          # inert: nothing to sign
    assert env.store.get_active_for_message(answer["artifact"]) is None

    # A different correction is a different watch; the same one counts up.
    env.add("outage")
    [other] = env.code("finance", decision="correct", corrected="other")
    assert other["answer"]["artifact"] != answer["artifact"]


def test_watcher_fault_never_unrecords_the_operators_decision(env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(decision_feed, "observe", boom)
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
    # Feedback is its own event through the handler; it points back at the
    # event the rejected draft answered.
    cords = {e["andon_id"]: e for e in env.events() if e["event_type"] == "andon_event"}
    feedback_event = cords[k["andon_id"]]
    assert feedback_event["detector"] == "operator_feedback"
    assert feedback_event["details"]["originating_andon_id"] == first.payload["keg"]["andon_id"]
    assert revised.proposal_id != first.proposal_id


# ── versions belong to a run ──────────────────────────────────────────


def test_a_new_run_starts_again_at_v1(env):
    _earn_v1(env)
    env.sign()
    [v1] = [e for e in env.store.all() if e.status == STATUS_ACTIVE]
    env.store.set_status(v1.pattern_id, STATUS_DEMOTED)       # what the reset does
    env.work.log.start_run("reset")
    env.n = 0
    [event] = _earn_v1(env)
    assert event["answer"]["kind"] == andon.KIND_STANDARD_WORK
    assert event["answer"]["detail"]["version"] == 1


# ── the older scanner is a Jidoka detector too ────────────────────────


def test_repetition_scanner_flags_through_the_same_door(tmp_path, env):
    from grove.eval.pattern_compiler import propose_pattern_promotions
    from tests.grove.test_pattern_compiler import _CFG, _seed, _store

    intents = _store(tmp_path)
    _seed(intents, "what is our mission statement", "factual_lookup", 6, response="X.")
    queue = tmp_path / "proposals.jsonl"
    propose_pattern_promotions(intents, env.store, queue_path=queue, config=_CFG)
    assert env.steps() == [keg.LOOP_JIDOKA_FLAG, keg.LOOP_ANDON_EVENT, andon.LOOP_KAIZEN_ANSWER]
    flag, cord, close = [e for e in env.events() if e.get("loop_step")]
    assert (flag["detector_id"], flag["flag"], flag["goal"]) == (
        "repetition", keg.FLAG_TIER_DOWN_PATTERN, None)
    assert cord["stops_line"] is False and close["closes"] == [cord["andon_id"]]
    assert close["kind"] == andon.KIND_STANDARD_WORK
    assert close["artifact"] == read_all(path=queue)[0].proposal_id
    # A rescan of the same waiting pattern raises nothing new.
    propose_pattern_promotions(intents, env.store, queue_path=queue, config=_CFG)
    assert len([e for e in env.events() if e["event_type"] == "andon_event"]) == 1


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
    from grove.kaizen import answers, session_rule
    for module in (andon, answers, session_rule, standard_work, trace, decision_feed):
        src = inspect.getsource(module).lower()
        for word in ("vendor", "invoice", "gl_code", "gl code", "chart of accounts"):
            assert word not in src, (module.__name__, word)


# ── regressions from the first live T0 serve (2026-10-06) ─────────────


def test_t0_serve_reports_no_prompt_sections_even_if_the_agent_holds_some():
    # Live: the agent held a prompt composed when it was built — recall
    # sections included — and the keg's own tool refused the T0 serve as a
    # contaminated turn. A T0 serve sends no prompt to any model.
    from grove.dispatcher import Dispatcher
    d = Dispatcher.__new__(Dispatcher)
    d.session_id, d._current_turn_id, d._current_turn_uid = "s", "s#1", "u"
    d._current_turn_routing_decision = None
    d._current_turn_tools_yielded, d._current_turn_isolation = [], GOAL
    stale = SimpleNamespace(sections={"cellar_knowledge": "x", "accumulated_domain_memory": "y"})
    agent = SimpleNamespace(model="m", _cellar_retrieval_hits=4, _composed_prompt=stale)
    model_turn = d.turn_provenance(agent)
    assert model_turn["sections"] == ["accumulated_domain_memory", "cellar_knowledge"]
    assert model_turn["cellar_hits"] == 4
    d._current_turn_t0_pattern = "keg:x:v1:abc"
    t0 = d.turn_provenance(agent)
    assert t0["sections"] == [] and t0["cellar_hits"] == 0 and t0["tier"] == "T0"


def _keg(env, **over):
    from grove.eval.pattern_compiler import propose_keg
    args = dict(
        name="Message tagging", request="Tag the next message",
        requests=["Next message", "Next"], match_threshold=0.8, intent_class="analysis",
        tool_name="tag_message", tool_args={}, inputs=env.work.config.inputs,
        outputs=env.work.config.outputs,
        conditions=[{"if": "channel == 'billing'", "then": {"tag": "finance"}}],
        scope_text="s", reserve="r", dock_goal=GOAL, scope="reserved",
        authority_level="green", flag=keg.FLAG_TIER_DOWN_PATTERN, flag_detail="",
        evidence_turn_ids=("t1",), history=[],
    )
    args.update(over)
    return propose_keg(env.store, **args)


def test_keg_answers_requests_that_overlap_its_examples(env):
    result = _keg(env)
    spec = keg.keg_of(env.store.get(result.pattern_id))
    assert keg.trigger_requests(spec) == ["Tag the next message", "Next message", "Next"]
    assert spec["trigger"]["match_threshold"] == 0.8
    find = env.store.get_active_for_message
    assert find("next") is None                       # a draft never serves
    env.store.set_status(result.pattern_id, STATUS_ACTIVE)
    for said in ("Tag the next message.", "next message", "NEXT!", "the next one please",
                 "ok next", "tag the next messages"):
        assert find(said).pattern_id == result.pattern_id, said
    # Extra content words count against the match: an instruction, a question
    # or a different subject is not the keg's request.
    for said in ("tag the next message as urgent", "why did you tag the last message",
                 "what is next on my calendar", "confirm", "tag"):
        assert find(said) is None, said
    env.store.set_status(result.pattern_id, STATUS_HALTED)
    assert find("next") is None                       # a halted keg does not serve


def test_goal_keg_serves_only_in_its_goals_own_sessions(env):
    # "Next" in a personal session is not the keg's request at all: the keg
    # is scoped to sessions isolated to its goal. A miss, not a stop.
    from grove.dispatcher import Dispatcher
    result = _keg(env, sessions="goal_isolated")
    env.store.set_status(result.pattern_id, STATUS_ACTIVE)
    entry = env.store.get_active_for_message("next")
    d = Dispatcher.__new__(Dispatcher)
    d._current_turn_isolation = None
    assert d._t0_pattern_in_scope(entry) is False
    d._current_turn_isolation = "some-other-goal"
    assert d._t0_pattern_in_scope(entry) is False
    d._current_turn_isolation = GOAL
    assert d._t0_pattern_in_scope(entry) is True
    # A keg with no session scope, and an ordinary pattern, serve anywhere.
    open_keg = _keg(env, sessions=None, evidence_turn_ids=("t2",))
    d._current_turn_isolation = None
    assert d._t0_pattern_in_scope(env.store.get(open_keg.pattern_id)) is True
    assert d._t0_pattern_in_scope(SimpleNamespace(compiled_invocation=None)) is True
    assert "if not self._t0_pattern_in_scope(pattern):" in inspect.getsource(
        Dispatcher._t0_intercept)


def test_keg_without_a_threshold_answers_exact_text_only(env):
    result = _keg(env, match_threshold=None)
    env.store.set_status(result.pattern_id, STATUS_ACTIVE)
    find = env.store.get_active_for_message
    assert find("Tag the next message!").pattern_id == result.pattern_id
    assert find("next") is None and find("the next one please") is None


def test_overlap_scores_are_checkable_by_hand():
    from grove.intent_match import best_match, overlap, tokens
    assert tokens("Code the next invoices, please!") == {"code", "next", "invoice"}
    assert overlap("next one", "Next") == 1.0
    assert overlap("code the next invoice as 6800", "Code the next invoice") == 0.6
    assert overlap("please", "the") == 0.0            # no content words: no match
    score, example = best_match("ok next", ["Code the next invoice", "Next"])
    assert (score, example) == (1.0, "Next")
    # No verb bonus: sharing a verb does not lift a different request.
    assert overlap("code the next invoice as 6800", "code the next invoice") < 0.8


def test_a_session_is_opened_by_overlap_with_the_goals_examples(env):
    goal = SimpleNamespace(id=GOAL, keywords=("message",))
    cfg = env.work.config
    assert cfg.keg.match_threshold == 0.8
    for said in ("tag the next message", "Tag the next message please"):
        assert dw.opens_work(said, goal, cfg) is True
    # A session is opened by the goal's primary request only. A short
    # continuation example ("Next") answers inside a session; it never opens one.
    import dataclasses
    with_next = dataclasses.replace(
        cfg, keg=dataclasses.replace(cfg.keg, requests=("Next",)))
    assert dw.opens_work("next", goal, with_next) is False
    # The goal keyword alone no longer opens it — that was the brittle part.
    for said in ("did my message send?", "message the team about lunch"):
        assert dw.opens_work(said, goal, cfg) is False


def test_scanner_clusters_differently_worded_requests(tmp_path, env):
    from grove.eval.pattern_compiler import propose_pattern_promotions, scan_candidates
    from tests.grove.test_pattern_compiler import _CFG, _seed, _store

    intents = _store(tmp_path)
    # Same content words, different wording and order. (The seed helper keys
    # turn ids on a phrasing's first four characters, so each differs there.)
    for phrasing in ("what is our mission statement",
                     "our mission statements, what is it",
                     "so what is the mission statement"):
        _seed(intents, phrasing, "factual_lookup", 2, response="X.")
    _seed(intents, "refund policy, what is it", "factual_lookup", 2, response="Y.")
    _seed(intents, "mission — what is it", "factual_lookup", 2, response="X.")  # a word short
    cfg = {**_CFG, "match_threshold": 0.8}
    [cand] = scan_candidates(intents, cfg)
    assert cand.repetition_count == 6 and len(cand.phrasings) == 3
    # The threshold is the dial: looser admits the near miss, and the three
    # phrasings share identical content words so they cluster even at 1.0.
    assert scan_candidates(intents, {**_CFG, "match_threshold": 0.6})[0].repetition_count == 8
    assert scan_candidates(intents, {**_CFG, "match_threshold": 1.0})[0].repetition_count == 6

    queue = tmp_path / "proposals.jsonl"
    propose_pattern_promotions(intents, env.store, queue_path=queue, config=cfg)
    entry = env.store.get(cand.t0_key)
    assert json.loads(entry.promotion_evidence)["match"]["threshold"] == 0.8
    env.store.set_status(entry.pattern_id, STATUS_ACTIVE)
    find = env.store.get_active_for_message
    assert find("the mission statement, what is it?").pattern_id == entry.pattern_id
    assert find("what is our refund policy") is None
    assert find("what is our mission") is None        # a content word short


def test_identically_worded_pattern_serves_exact_text_only(tmp_path, env):
    from grove.eval.pattern_compiler import propose_pattern_promotions, scan_candidates
    from tests.grove.test_pattern_compiler import _CFG, _seed, _store

    intents = _store(tmp_path)
    _seed(intents, "what is our mission statement", "factual_lookup", 6, response="X.")
    [cand] = scan_candidates(intents, _CFG)
    propose_pattern_promotions(intents, env.store, queue_path=tmp_path / "q.jsonl", config=_CFG)
    entry = env.store.get(cand.t0_key)
    assert "match" not in json.loads(entry.promotion_evidence)
    env.store.set_status(entry.pattern_id, STATUS_ACTIVE)
    assert env.store.get_active_for_message("our mission statement please") is None


def test_no_side_channels_on_the_t0_path():
    # One bus. A keg that does not cover a request is standard work, noted on
    # the turn's own record; a refusal is an andon stop. Neither gets its own
    # event stream, and neither hands a stopped turn to a model.
    from grove import dispatcher
    src = inspect.getsource(dispatcher.Dispatcher._t0_intercept)
    assert 'event_type="t0_declined"' not in src
    assert "self._current_turn_t0_handback = pattern.pattern_id" in src
    assert "return self._t0_andon_stop(agent, pattern, user_message, stop)" in src
    stop = inspect.getsource(dispatcher.Dispatcher._t0_andon_stop)
    assert 'outcome="error"' in stop and 'failure_kind="andon_stop"' in stop
    assert '"t0_handback"' in inspect.getsource(dispatcher)
    assert dispatcher._t0_refused('{"t0_refused": true, "message": "m"}')["message"] == "m"
    for not_a_stop in ("Coded 6110.", '{"t0_declined": true}', "{oops", None):
        assert dispatcher._t0_refused(not_a_stop) is None


def test_goal_isolated_sessions_are_not_mined_for_memory():
    from grove.memory.lifecycle import run_memory_extraction

    class _Detector:
        seen = []

        def detect_and_stage(self, session_id, transcript, goals):
            self.seen.append(session_id)
            return 1

    class _Store:
        flushed = []

        def flush_access_events(self, session_id):
            self.flushed.append(session_id)
            return 0

    detector, store = _Detector(), _Store()
    staged = run_memory_extraction(
        detector=detector, store=store, session_ids=["chat", "coding", "chat2"],
        transcript_loader=lambda sid: [], dock_goals=[],
        skip_session=lambda sid: sid == "coding",
    )
    assert staged == 2 and detector.seen == ["chat", "chat2"]
    assert store.flushed == ["chat", "coding", "chat2"]


def test_unknown_isolation_is_treated_as_isolated():
    from grove.dispatcher import Dispatcher
    d = Dispatcher.__new__(Dispatcher)
    d._isolation_by_session = {}

    class _Meta:
        def __init__(self, data):
            self.data = data

        def get_meta(self, key):
            return self.data.get(key)

    d.session = _Meta({dw.isolation_meta_key("coding"): GOAL, dw.isolation_meta_key("chat"): ""})
    assert d._session_is_goal_isolated("coding") is True
    assert d._session_is_goal_isolated("chat") is False
    assert d._session_is_goal_isolated("never-seen") is False

    class _Broken:
        def get_meta(self, key):
            raise RuntimeError("db down")

    d.session = _Broken()
    assert d._session_is_goal_isolated("coding") is True      # when in doubt, do not mine


# ── verb bonus: missing words are forgivable, extra words are not ─────


def test_verb_bonus_applies_only_when_the_message_adds_no_words():
    from grove.intent_match import overlap, verb_of
    example = "Code the next invoice"
    assert verb_of(example) == "code" and verb_of("Please, the next one") == "next"
    # A shorter way to say the same thing: lifted over the 0.8 bar.
    assert overlap("code next", example) == pytest.approx(2 / 3)
    assert overlap("code next", example, verb_bonus=0.2) == pytest.approx(2 / 3 + 0.2)
    # Extra words — an instruction — get nothing, however alike the rest is.
    assert overlap("code the next invoice as 6800", example, verb_bonus=0.2) == 0.6
    # The verb alone is not the request.
    assert overlap("code", example, verb_bonus=0.2) < 0.8
    # No bonus without the example's verb, and none when it is switched off.
    assert overlap("next invoice", example, verb_bonus=0.2) == pytest.approx(2 / 3)
    assert overlap("code next", example, verb_bonus=0.0) == pytest.approx(2 / 3)
    assert overlap("Code the next invoice", example, verb_bonus=0.2) == 1.0   # capped


def test_keg_serves_a_shortened_request_only_when_it_declares_the_bonus(env):
    plain = _keg(env)
    env.store.set_status(plain.pattern_id, STATUS_ACTIVE)
    find = env.store.get_active_for_message
    assert find("tag next") is None
    env.store.set_status(plain.pattern_id, STATUS_HALTED)
    lifted = _keg(env, verb_bonus=0.2, evidence_turn_ids=("t2",))
    spec = keg.keg_of(env.store.get(lifted.pattern_id))
    assert spec["trigger"]["verb_bonus"] == 0.2          # signed with the keg
    env.store.set_status(lifted.pattern_id, STATUS_ACTIVE)
    assert find("tag next").pattern_id == lifted.pattern_id
    assert find("tag the next message as urgent") is None


def test_verb_bonus_plays_no_part_in_what_opens_a_session(env):
    import dataclasses
    goal = SimpleNamespace(id=GOAL, keywords=("message",))
    cfg = dataclasses.replace(
        env.work.config, keg=dataclasses.replace(env.work.config.keg, verb_bonus=0.2))
    assert dw.opens_work("tag next", goal, cfg) is False
    assert dw.session_rule_digest(cfg) == dw.session_rule_digest(env.work.config)


def test_backtest_names_what_the_keg_leaves_to_the_interpreter(env):
    # Live 2026-10-06: Kaizen reported "six replayed, zero would change" for a
    # keg that does not cover one of the six. Three outcomes, each counted.
    [event] = _earn_v1(env)          # billing, legal (two values), outage, billing
    detail = event["answer"]["detail"]
    assert (detail["replayed"], detail["unchanged"], detail["would_change"],
            detail["not_covered"]) == (4, 3, 0, 1)
    assert detail["not_covered_cases"] == ["legal: 2 values in channels.csv"]
    assert event["answer"]["summary"].endswith(
        "Replayed 4 on history: 3 unchanged, 0 would change, 1 not covered "
        "(legal: 2 values in channels.csv).")
    [proposal] = read_all()
    assert rendering_headline(proposal) == (
        "Replayed on history: 3 unchanged · 0 would change · 1 not covered "
        "(legal: 2 values in channels.csv)")
    # The not-covered case is listed with the changed ones, not folded away.
    assert [c["result"] for c in proposal.detail["cases"]][0] == "not_covered"


def rendering_headline(proposal):
    from grove.kaizen import rendering
    return rendering.decode_detail(proposal).headline
