"""The work session: the operator decides, the system presents the next item.

MESSAGE-TAGGING fixtures — the session is generic over any goal's decision
work. Invariants pinned here:

  * one item per presentation, one presentation per operator decision;
  * decisions are recorded by the system from the action, never from model text;
  * a card that is out of date never decides the item now waiting;
  * the kill switch: with ``work_session`` off, nothing here acts at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import grove.grants as grants_mod
import grove.pattern_cache as pc
from grove import decision_work as dw
from grove import reissue, turn_provenance
from grove.decision_work import DecisionRefused, DecisionWork
from grove.dispatcher import Dispatcher

GOAL = "message-triage"
SESSION = {
    "enabled": True,
    "start": ["let's tag some messages", "back to messages"],
    "confirm": ["confirm", "yes", "ok", "looks good"],
    "revise": ["revise"],
    "buttons": {"confirm": "Confirm", "revise": "Revise"},
    "card": "{item} {n} of {total}: {channel} from {sender}\nProposed: {value}\n{why}",
}


class _Grants:
    def __init__(self):
        self.signed = {}

    def sign(self, cfg):
        self.signed[(cfg.goal_id, dw.SESSION_RULE_PREFIX + dw.session_rule_digest(cfg))] = (
            SimpleNamespace(id="grant-ws", revoked=False))

    def get_grant(self, scope, write_class):
        return self.signed.get((scope, write_class))


def _goal(tmp_path, session=SESSION):
    (tmp_path / "queue").mkdir(exist_ok=True)
    (tmp_path / "channels.csv").write_text(
        "Channel,Default Tag\nbilling,finance\noutage,ops\n", encoding="utf-8")
    (tmp_path / "tags.csv").write_text(
        "Tag,Meaning\nfinance,Money in or out\nops,Something is down\nother,Everything else\n",
        encoding="utf-8")
    block = {
        "tool": "tag_message", "queue": "queue", "isolation": "sources_only",
        "on_unclean": "open_clean_session",
        "item_name": {"one": "message", "many": "messages"},
        "inputs": {"channel": {"data_type": "string", "required": True}},
        "outputs": {"tag": {"data_type": "string"}},
        "reference_table": {"path": "channels.csv", "key_column": "Channel",
                            "value_column": "Default Tag", "key_input": "channel",
                            "value_output": "tag"},
        "output_domains": [{"output": "tag", "path": "tags.csv", "column": "Tag",
                            "name_column": "Meaning"}],
        "evidence": {"threshold": 3},
        "keg": {"name": "Message tagging", "request": "tag the next message",
                "requests": ["next"]},
    }
    if session is not None:
        block["work_session"] = dict(session)
    return SimpleNamespace(id=GOAL, root=tmp_path, keywords=("message",),
                           resolved_sources=lambda: [], extra={"decision_work": block})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pc.db")
    grants = _Grants()
    monkeypatch.setattr(grants_mod, "get_grant_store", lambda: grants)
    token = turn_provenance.set_current(None)

    class Env:
        turn = 0

        def work(self, session=SESSION, signed=True):
            cfg = dw.load_config(_goal(tmp_path, session))
            if signed:
                grants.sign(cfg)
            monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
            return DecisionWork(cfg)     # the default log: the one the Dispatcher reads too

        def add(self, *channels):
            for channel in channels:
                n = len(list((tmp_path / "queue").iterdir())) + 1
                (tmp_path / "queue" / f"m{n:02d}.txt").write_text(channel)

        def prov(self, **over):
            self.turn += 1
            base = {"session_id": "sess", "turn_id": f"sess#{self.turn}",
                    "turn_uid": f"u{self.turn}", "tier": "T1", "model": "m",
                    "request": "tag the next message", "cellar_hits": 0, "sections": [],
                    "tools_yielded": ["tag_message"], "isolation_goal": GOAL}
            base.update(over)
            return base

        def propose(self, work, tag="finance", reasoning="billing goes to finance"):
            item = work.next_item()
            return work.record(item_id=item.stem, inputs={"channel": item.read_text()},
                               output={"tag": tag}, reasoning=reasoning,
                               provenance=self.prov())

    yield Env()
    turn_provenance.reset(token)


# ── what a message unambiguously is ───────────────────────────────────


def test_only_an_unambiguous_message_is_acted_on_without_a_model(env):
    work = env.work()
    env.add("billing", "outage")
    assert work.session_action("ok") is None                 # nothing is pending yet
    env.propose(work)
    assert work.session_action("OK!") == {"action": "confirm", "item_id": "m01"}
    assert work.session_action("Looks good.")["action"] == "confirm"
    assert work.session_action("revise")["action"] == "revise_prompt"
    assert work.session_action("ops") == {
        "action": "revise", "item_id": "m01", "output": {"tag": "ops"}}
    assert work.session_action("next")["action"] == "present"
    # A reason, a question, a phrase inside a sentence, a value not in the
    # table: none of these is unambiguous. They go to the model.
    for said in ("ok but why finance?", "it's really an outage", "why finance?",
                 "urgent", "yes and move on to something else"):
        assert work.session_action(said) is None, said


def test_the_card_says_what_who_and_why(env):
    work = env.work()
    env.add("billing", "outage")
    record = env.propose(work)
    card = work.card(record, {"sender": "pat@example.com"})
    assert card == ("Message 1 of 2: billing from pat@example.com\n"
                    "Proposed: finance Money in or out\n"
                    "Model (T1): billing goes to finance")
    # A field the template names and nobody supplies is empty, never a crash.
    assert "billing from \n" in work.card(record)


def test_a_keg_decided_item_names_the_version_and_the_rule_that_fired(env, tmp_path):
    from grove.pattern_cache import CompiledPattern, PatternCacheStore, STATUS_ACTIVE

    work = env.work()
    env.add("billing")
    spec = {"name": "Message tagging", "version": 2,
            "inputs": {"channel": {"data_type": "string"}},
            "outputs": {"tag": {"data_type": "string"}},
            "conditions": [{"if": "channel == 'billing'", "then": {"tag": "finance"}}]}
    PatternCacheStore().upsert(CompiledPattern(
        pattern_id="keg:mt:v2", t0_key="keg:mt:v2", intent_class="conversation",
        cacheable_type="executable", cached_response=None,
        compiled_invocation=json.dumps({"tool": "tag_message", "args": {"keg": spec}}),
        evidence_hash="e", status=STATUS_ACTIVE, created_at="2026-01-01T00:00:00+00:00"))
    record = work.apply_keg(
        spec, item_id="m01", inputs={"channel": "billing"},
        keg_ref={"name": "Message tagging", "version": 2, "pattern_id": "keg:mt:v2"},
        provenance=env.prov(tier="T0"))
    assert work.why(record) == "Keg v2, no model call: billing → finance Money in or out"


# ── decisions are recorded by the system; one presentation per decision ──


def test_confirm_records_the_decision_and_arms_exactly_one_next_item(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    out = work.session_step({"action": "confirm", "item_id": "m01"}, env.prov(tier="T0"))
    assert (out["decided"], out["next_armed"]) == (True, True)
    assert out["reply"].splitlines()[0] == "Confirmed: finance Money in or out."
    assert work.pending() is None
    armed = reissue.take("sess")
    assert armed["request"] == "tag the next message" and armed["authorized"] == "grant-ws"
    assert armed["tier"] is None and armed["clean_session"] is False
    assert reissue.take("sess") is None                       # one, never more
    # Showing an item, or asking what it should be, arms nothing.
    env.propose(work, tag="ops")
    for action in ("present", "revise_prompt"):
        out = work.session_step({"action": action}, env.prov(tier="T0"))
        assert out["decided"] is False and reissue.take("sess") is None
    assert work.session_step({"action": "revise_prompt"}, env.prov())["reply"] == (
        "What should it be?")


def test_a_revision_is_the_same_correction_jidoka_watches(env):
    work = env.work()
    env.add("billing")
    env.propose(work)
    out = work.session_step(
        {"action": "revise", "item_id": "m01", "output": {"tag": "ops"}}, env.prov(tier="T0"))
    assert out["reply"].splitlines()[0] == (
        "Revised: finance Money in or out → ops Something is down.")
    [decided] = [r for r in work.log.run_records() if r["kind"] == "decided"]
    assert (decided["decision"], decided["output"]) == ("correct", {"tag": "ops"})
    # A value outside the declared table is refused, exactly as for a model.
    env.add("outage")
    env.propose(work, tag="ops")
    with pytest.raises(DecisionRefused) as bad:
        work.session_step({"action": "revise", "output": {"tag": "made-up"}},
                          env.prov(tier="T0"))
    assert bad.value.reason == "output_not_in_domain" and work.pending() is not None


def test_an_out_of_date_card_never_decides_the_item_now_waiting(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "confirm", "item_id": "m01"}, env.prov(tier="T0"))
    env.propose(work, tag="ops")                                   # m02 is now pending
    for action in ({"action": "confirm", "item_id": "m01"},
                   {"action": "revise", "item_id": "m01", "output": {"tag": "other"}}):
        with pytest.raises(DecisionRefused) as stale:
            work.session_step(action, env.prov(tier="T0"))
        assert (stale.value.reason, str(stale.value)) == ("stale_card", "That card is out of date.")
    assert work.pending()["item_id"] == "m02"                      # untouched


def test_the_queue_ends_with_a_summary_and_a_link(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    env.propose(work, tag="finance")
    work.session_step({"action": "revise", "output": {"tag": "ops"}}, env.prov(tier="T0"))
    assert work.session_action("tag the next message") == {"action": "summary"}
    reply = work.session_step({"action": "summary"}, env.prov(tier="T0"))["reply"]
    assert reply.startswith(
        "Queue complete: 2 messages decided — 0 by the keg, 2 by a model, 1 revised.")
    assert "/portal#fragments/audit/" in reply


# ── one matcher, with a line drawn between routing and writing ────────


def test_routing_matches_by_overlap_and_writing_only_by_exact_phrase(env):
    work = env.work()
    env.add("billing")
    # Routing: wording that overlaps a declared start phrase opens the work.
    assert dw.asks_for_work("ok let's tag some messages now", work.config) is True
    assert dw.asks_for_work("what's for lunch", work.config) is False
    # Writing: only the whole declared phrase, or an exact value. A near miss
    # is never a decision, however close it scores.
    env.propose(work)
    for near in ("sounds right", "ok then", "yes please confirm that one", "looks good to me"):
        assert work.session_action(near) is None, near
        assert work.last_match["fired"] is False and work.last_match["action"] is None
    assert work.pending() is not None
    assert work.session_action("looks good")["action"] == "confirm"
    assert (work.last_match["match"], work.last_match["fired"]) == ("exact", True)


def test_every_scored_message_is_traced_fired_or_not(env):
    work = env.work()
    env.add("billing")
    env.propose(work)
    work.session_action("Looks good enough")
    assert work.last_match == {
        "message": "looks good enough", "best_phrase": "looks good",
        "phrase_kind": "confirm", "score": work.last_match["score"],
        "fired": False, "action": None, "match": None}
    assert 0.5 < work.last_match["score"] < 1.0
    # Filler words score a full overlap, and it still records nothing: the
    # phrase was not the whole message, so the model interprets it.
    assert work.session_action("looks good to me") is None
    assert (work.last_match["score"], work.last_match["fired"]) == (1.0, False)
    work.session_action("next")
    assert (work.last_match["action"], work.last_match["match"]) == ("present", "overlap")
    d, agent, written, said = _dispatcher(env, work)
    d._current_turn_phrase_match = None
    assert d._session_intercept(agent, "sounds right", None) is None
    assert d._current_turn_phrase_match["fired"] is False     # kept for the turn's record
    assert d._current_turn_phrase_match["message"] == "sounds right"


# ── the signed rule, and the kill switch ──────────────────────────────


def test_the_work_session_is_part_of_the_signed_session_rule(env):
    on = env.work()
    rule = dw.session_rule(on.config)
    assert rule["work_session"]["confirm"] == ["confirm", "yes", "ok", "looks good"]
    assert rule["work_session"]["after_a_decision"] == "present_the_next_item"
    # Unsigned: nothing is armed. The operator decides; the system does not act.
    unsigned = env.work(signed=False, session={**SESSION, "confirm": ["affirmative"]})
    env.add("billing")
    env.propose(unsigned)
    out = unsigned.session_step({"action": "confirm"}, env.prov(tier="T0"))
    assert out["decided"] is True and out["next_armed"] is False
    assert reissue.take("sess") is None


def test_switched_off_the_rhythm_is_exactly_as_before(env, tmp_path):
    off_cfg = dw.load_config(_goal(tmp_path, {**SESSION, "enabled": False}))
    none_cfg = dw.load_config(_goal(tmp_path, None))
    # Off, or no block at all: the same session rule the operator signed.
    assert dw.session_rule(off_cfg) == dw.session_rule(none_cfg)
    assert "work_session" not in dw.session_rule(off_cfg)
    assert dw.session_rule_digest(off_cfg) == dw.session_rule_digest(none_cfg)
    work = env.work(session={**SESSION, "enabled": False})
    env.add("billing", "outage")
    env.propose(work)
    assert work.session_action("ok") is None                  # nothing acts without a model
    assert dw.asks_for_work("let's tag some messages", work.config) is False
    assert dw.opens_work("let's tag some messages", _goal(tmp_path), work.config) is False
    work.decide(decision="confirm", provenance=env.prov())    # the operator's typed confirm
    assert work.next_armed is False and reissue.take("sess") is None
    with pytest.raises(DecisionRefused) as off:
        work.session_step({"action": "present"}, env.prov(tier="T0"))
    assert off.value.reason == "work_session_off"
    # On, the start phrases open and ask for the work.
    on = env.work()
    assert dw.asks_for_work("Let's tag some messages!", on.config) is True
    assert dw.opens_work("back to messages", _goal(tmp_path), on.config) is True


def test_a_declaration_that_cannot_be_read_is_refused(tmp_path):
    for bad in ({"enabled": "yes"}, {"enabled": True, "confirm": "ok"},
                {"enabled": True}, {"enabled": True, "confirm": ["ok"], "buttons": {"go": "Go"}}):
        with pytest.raises(ValueError, match="work_session"):
            dw.load_config(_goal(tmp_path, bad))


# ── the Dispatcher carries the step out with no model ─────────────────


class _Agent:
    """Hosts the goal's tool. The adapter is three lines: item fields, then
    the generic step — which is all a real adapter adds."""

    def __init__(self, work):
        self.work, self.calls = work, []

    def _invoke_tool(self, name, args, turn_id):
        self.calls.append(args)
        prov = turn_provenance.current() or {}
        assert prov.get("session_step"), "only the Dispatcher runs a session step"
        try:
            out = self.work.session_step(args["action"], prov, {"sender": "pat@example.com"})
        except DecisionRefused as exc:
            return json.dumps({"t0_refused": True, "reason": exc.reason,
                               "message": str(exc), "andon_id": exc.andon_id})
        return json.dumps({"session": True, **out})


def _dispatcher(env, work):
    written, said = [], []
    d = SimpleNamespace(
        _current_turn_isolation=GOAL, _current_turn_id="sess#9", _current_turn_uid="u9",
        _current_turn_session_step=None, _current_turn_t0_pattern=None,
        _current_turn_withheld=None, session_id="sess",
        _approval_gate=SimpleNamespace(activate=lambda: None, mint=lambda sig: None,
                                       flush=lambda: None),
        _finalize_previous_turn_pending=lambda turn_id: None,
        _write_intent_record=lambda agent, **kw: written.append(kw),
        _persist_t0_turn=lambda user, reply: said.append((user, reply)),
        _t0_result_dict=lambda agent, text: {"final_response": text, "model": "pattern_cache",
                                             "tier": "T0", "api_calls": 0},
    )
    for name in ("_run_session_step", "_session_intercept", "_session_result_dict",
                 "_session_card_for"):
        setattr(d, name, getattr(Dispatcher, name).__get__(d))

    def _invoke(agent):
        def run(name, args, turn_id):
            mode = d._current_turn_session_step
            token = turn_provenance.set_current(env.prov(
                tier="T0" if mode == "t0" else "T1", session_step=mode,
                turn_uid=d._current_turn_uid))
            try:
                return _Agent._invoke_tool(agent, name, args, turn_id)
            finally:
                turn_provenance.reset(token)
        return run

    agent = _Agent(work)
    agent._invoke_tool = _invoke(agent)
    return d, agent, written, said


def test_the_dispatcher_confirms_with_no_model_and_the_next_item_is_its_own_turn(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    d, agent, written, said = _dispatcher(env, work)
    result = d._session_intercept(agent, "ok", "sess#8")
    assert result["final_response"].startswith("Confirmed: finance Money in or out.")
    assert (result["tier"], result["model"], result["api_calls"]) == ("T0", "session_rule", 0)
    assert written == [{"outcome": "pending", "final_response_chars": len(result["final_response"]),
                        "intent_class_override": "conversation", "tier_override": "T0"}]
    assert said == [("ok", result["final_response"])]
    # What the operator was told is kept for the turn's execution summary —
    # and NOT as response_content, which the T0 pattern compiler mines.
    assert d._current_turn_session_reply == result["final_response"]
    assert "response_content" not in written[0]
    # The decision is on record, made by the system from the action.
    [decided] = [r for r in work.log.run_records() if r["kind"] == "decided"]
    assert decided["decision"] == "confirm" and decided["turn_uid"] == "u9"
    # The next item is NOT presented in this turn: it is armed, as a request
    # the gateway re-issues and the Dispatcher routes from scratch.
    assert work.pending() is None and len(agent.calls) == 1
    assert reissue.take("sess")["request"] == "tag the next message"


def test_an_ambiguous_message_falls_through_to_the_model(env):
    work = env.work()
    env.add("billing")
    env.propose(work)
    d, agent, written, said = _dispatcher(env, work)
    for message in ("why finance?", "that looks like an outage to me", "what's the weather"):
        assert d._session_intercept(agent, message, None) is None
    assert agent.calls == [] and written == [] and work.pending() is not None


def test_a_stale_confirm_is_answered_plainly_and_recorded_as_no_decision(env):
    work = env.work()
    env.add("billing")
    d, agent, written, said = _dispatcher(env, work)
    # "next" with the queue done and nothing pending: the summary, no model.
    env.propose(work)
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    reissue.take("sess")
    result = d._session_intercept(agent, "next", None)
    assert result["final_response"].startswith("Queue complete: 1 message decided")
    assert reissue.take("sess") is None                       # the end arms nothing


def test_after_a_model_turn_the_card_replaces_the_prose(env):
    work = env.work()
    env.add("billing", "outage")
    d, agent, written, said = _dispatcher(env, work)
    # This turn proposed m01: the reply is the card.
    record = work.record(item_id="m01", inputs={"channel": "billing"}, output={"tag": "finance"},
                         reasoning="billing goes to finance",
                         provenance=env.prov(turn_uid="u9"))
    card = d._session_card_for(agent, work, "u9")
    assert card == work.card(record, {"sender": "pat@example.com"})
    assert d._current_turn_session_step is None               # the review step is over
    # A later turn that proposed nothing (a question about the item) keeps its own reply.
    assert d._session_card_for(agent, work, "u10") is None
    # Switched off: never.
    off = env.work(session={**SESSION, "enabled": False})
    assert d._session_card_for(agent, off, "u9") is None


def test_with_the_switch_off_the_dispatcher_does_nothing(env):
    work = env.work(session={**SESSION, "enabled": False})
    env.add("billing")
    env.propose(work)
    d, agent, written, said = _dispatcher(env, work)
    assert d._session_intercept(agent, "ok", None) is None
    assert agent.calls == [] and written == [] and reissue.take("sess") is None


# ── batch: the keg acts under its own authority; the rest come to the operator ──


def _serve_keg(version=2, authority="green"):
    from grove.pattern_cache import CompiledPattern, PatternCacheStore, STATUS_ACTIVE

    spec = {"name": "Message tagging", "version": version, "dock_goal": GOAL,
            "authority_level": authority,
            "inputs": {"channel": {"data_type": "string"}},
            "outputs": {"tag": {"data_type": "string"}},
            "conditions": [{"if": "channel == 'billing'", "then": {"tag": "finance"}},
                           {"if": "channel == 'outage'", "then": {"tag": "ops"}}]}
    PatternCacheStore().upsert(CompiledPattern(
        pattern_id=f"keg:mt:v{version}", t0_key=f"keg:mt:v{version}",
        intent_class="conversation", cacheable_type="executable", cached_response=None,
        compiled_invocation=json.dumps({"tool": "tag_message", "args": {"keg": spec}}),
        evidence_hash="e", status=STATUS_ACTIVE, created_at="2026-01-01T00:00:00+00:00",
        promotion_evidence=json.dumps({"keg": {"dock_goal": GOAL}})))
    return spec


BATCH = {**SESSION, "batch": ["work the backlog"], "batch_label": "Month 2",
         "before_label": "Month 1", "done_word": "tagged"}
_read = lambda path: {"channel": path.read_text()}      # the adapter's reader


def test_the_keg_pass_decides_what_it_covers_and_leaves_the_rest(env):
    work = env.work(session=BATCH)
    _serve_keg()
    env.add("billing", "legal", "outage", "billing", "press")
    assert work.session_action("ok, work the backlog") == {"action": "batch"}   # routing: overlap
    out = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert out["reply"] == ("Tagged 3 of 5 · 3 by the keg v2 · 0 model calls.\n"
                            "2 need a model. Bringing them to you one at a time.")
    assert out["decided"] is False and out["next_armed"] is True
    assert reissue.take("sess")["request"] == "tag the next message"   # one presentation
    assert reissue.take("sess") is None
    records = work.log.run_records()
    accepted = [r for r in records if r["kind"] == "decided"]
    assert [r["item_id"] for r in accepted] == ["m01", "m03", "m04"]
    assert {(r["decision"], r["by"]) for r in accepted} == {("accepted", "keg_authority")}
    proposed = [r for r in records if r["kind"] == "proposed"]
    assert {r["tier"] for r in proposed} == {"T0"} and len({r["batch"] for r in proposed}) == 1
    # The uncovered items are untouched and come up in queue order, nothing pending.
    assert work.pending() is None and work.next_item().stem == "m02"
    # An exception decided by a model carries the same batch.
    exception = env.propose(work, tag="other", reasoning="no rule for legal")
    assert exception["batch"] == proposed[0]["batch"]


def test_accepted_is_never_the_operators_confirmation(env):
    work = env.work(session=BATCH)
    _serve_keg()
    env.add("billing", "outage", "billing", "outage", "billing")
    work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    # Five accepted items that match the reference table: zero evidence.
    assert work.tally() == {"decided": 5, "by_keg": 5, "confirmed": 0,
                            "accepted": 5, "revised": 0}
    assert work.evidence()["confirmations"] == 0 and work.evidence()["met"] is False
    # And not ground truth for a backtest: the operator never ruled on them.
    assert {c["confirmed"] for c in work.history() if c["served_by_keg"]} == {None}
    assert work.summary() == "5 tagged: 5 by the keg, not reviewed; 0 reviewed by you; 0 revised."


def test_ruling_on_an_accepted_item_confirms_it_or_is_a_miss(env):
    from grove.pattern_cache import PatternCacheStore, STATUS_HALTED

    work = env.work(session=BATCH)
    _serve_keg()
    env.add("billing", "outage", "billing")
    work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    reissue.take("sess")
    confirmed = work.rule_on("m01", decision="confirm", provenance=env.prov())
    assert (confirmed["decision"], confirmed["after"]) == ("confirm", "accepted")
    assert work.tally()["confirmed"] == 1 and work.tally()["accepted"] == 2
    # A revision of a keg-coded item is a miss: flagged, and the keg halts.
    work.rule_on("m02", decision="correct", corrected_output={"tag": "other"},
                 provenance=env.prov())
    assert work.tally()["revised"] == 1
    [event] = work.last_observations
    assert event["detector"] == "correction" and event["halted"] == ["keg:mt:v2"]
    assert PatternCacheStore().get("keg:mt:v2").status == STATUS_HALTED
    # Confirming what is already confirmed changes nothing, and says so.
    with pytest.raises(DecisionRefused) as again:
        work.rule_on("m01", decision="confirm", provenance=env.prov())
    assert again.value.reason == "already_ruled"


def test_no_green_keg_means_nothing_is_decided_in_bulk(env):
    work = env.work(session=BATCH)
    env.add("billing", "outage")
    out = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert out["reply"].startswith("No signed keg is serving")
    assert work.log.run_records() == [] and out["next_armed"] is True
    reissue.take("sess")
    _serve_keg(authority="yellow")
    out = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert "not signed to act without review" in out["reply"]
    assert [r for r in work.log.run_records() if r["kind"] == "decided"] == []
    assert work.current_batch() is None


def test_batch_respects_the_kill_switch_and_the_signed_rule(env):
    off = env.work(session={**BATCH, "enabled": False})
    _serve_keg()
    env.add("billing")
    assert off.session_action("work the backlog") is None
    with pytest.raises(DecisionRefused) as refused:
        off.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert refused.value.reason == "work_session_off" and off.log.run_records() == []
    # The batch phrases are part of the rule the operator signs.
    on = env.work(session=BATCH)
    assert dw.session_rule(on.config)["work_session"]["batch"] == ["work the backlog"]
    assert "batch" not in dw.session_rule(env.work().config)["work_session"]


# ── buttons: a press is a message that names its item ─────────────────


def test_a_card_offers_buttons_that_carry_the_item_id(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "present"}, env.prov(tier="T0"))
    assert reissue.take_actions("sess") == {
        "goal": GOAL, "item_id": "m01",
        "buttons": [["confirm", "Confirm"], ["revise", "Revise"]]}
    assert reissue.take_actions("sess") is None               # offered once
    # No buttons declared, or the switch off: nothing is offered.
    plain = env.work(session={k: v for k, v in SESSION.items() if k != "buttons"})
    plain.session_step({"action": "present"}, env.prov(tier="T0"))
    assert reissue.take_actions("sess") is None


def test_a_press_decides_only_the_item_its_card_was_for(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    press = dw.button_message("confirm", "m01")
    assert work.session_action(press) == {"action": "confirm", "item_id": "m01", "button": True}
    assert work.last_match["match"] == "button"
    work.session_step(work.session_action(press), env.prov(tier="T0"))
    env.propose(work, tag="ops")                              # m02 is now the one waiting
    # The same press again — the old card's button — is refused, and m02 is untouched.
    for old in (dw.button_message("confirm", "m01"), dw.button_message("revise", "m01")):
        with pytest.raises(DecisionRefused) as stale:
            work.session_step(work.session_action(old), env.prov(tier="T0"))
        assert str(stale.value) == "That card is out of date."
    assert work.pending()["item_id"] == "m02"
    # With nothing pending at all, an old press is still just out of date.
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    with pytest.raises(DecisionRefused) as stale:
        work.session_step(work.session_action(dw.button_message("confirm", "m02")),
                          env.prov(tier="T0"))
    assert stale.value.reason == "stale_card"
    # Revise on the live card asks what it should be; it decides nothing.
    env.add("billing")
    env.propose(work)
    out = work.session_step(work.session_action(dw.button_message("revise", "m03")),
                            env.prov(tier="T0"))
    assert out["reply"] == "What should it be?" and out["decided"] is False


def test_the_dispatcher_answers_a_stale_press_plainly(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    reissue.take("sess")
    env.propose(work, tag="ops")
    d, agent, written, said = _dispatcher(env, work)
    result = d._session_intercept(agent, dw.button_message("confirm", "m01"), None)
    assert result["final_response"] == "That card is out of date."
    assert written[0]["outcome"] == "pending" and "failure_kind" not in written[0]
    assert work.pending()["item_id"] == "m02" and reissue.take("sess") is None


# ── a model turn must end in something on record ──────────────────────
# Live, run 5 (2026-10-06): the model fetched the item, then told the
# operator "My code: …" without recording it. Nothing was pending, so there
# was no card, no buttons, and "confirm" had nothing to confirm.


def test_presenting_an_answer_that_was_never_recorded_fails_upward(env):
    work = env.work()
    env.add("billing", "outage")
    called = env.prov()                       # the tool WAS called, but nothing recorded
    refused = work.unanswered("My tag: finance. Confirm or revise?", called)
    assert refused is not None and refused.reason == "reply_without_record"
    assert reissue.take("sess")["tier"] == "T2"               # the ladder, automatically
    # In order: the item was proposed on record this turn.
    env.add()
    record = env.propose(work)
    assert work.unanswered("x", {**env.prov(), "turn_uid": record["turn_uid"]}) is None


def test_a_declared_question_is_not_a_decision_and_not_a_fault(env):
    work = env.work()
    env.add("billing")
    asking = env.prov()
    work.ask(asking)
    assert work.unanswered("Is this billing or legal?", asking) is None
    assert work.pending() is None and reissue.take("sess") is None
    # The declaration belongs to that turn only.
    later = env.prov()
    assert work.unanswered("My tag: finance.", later).reason == "reply_without_record"


def test_an_empty_queue_is_a_fine_way_to_end_and_off_is_as_before(env):
    work = env.work()
    assert work.unanswered("Nothing left.", env.prov()) is None      # nothing queued
    off = env.work(session={**SESSION, "enabled": False})
    env.add("billing")
    assert off.unanswered("My tag: finance.", env.prov()) is None    # the old rule only


def test_a_model_turn_that_recorded_the_decision_replies_from_the_record(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    deciding = env.prov()
    work.decide(decision="correct", corrected_output={"tag": "ops"}, provenance=deciding)
    assert work.decided_reply(deciding["turn_uid"]).splitlines()[0] == (
        "Revised: finance Money in or out → ops Something is down.")
    assert work.decided_reply("some-other-turn") is None
    d, agent, written, said = _dispatcher(env, work)
    assert d._session_card_for(agent, work, deciding["turn_uid"]).startswith("Revised: finance")


# ── pause, resume, the declared question, and what the model is told ──


def test_a_pause_phrase_is_routing_and_decides_nothing(env):
    work = env.work(session={**SESSION, "pause": ["pause", "i'll come back later"]})
    env.add("billing", "outage")
    env.propose(work)
    assert work.session_action("I'll come back later") == {"action": "pause"}
    assert work.pause_notice() == (
        "Paused at message 1 of 2. Say “let's tag some messages” to pick it back up.")
    d, agent, written, said = _dispatcher(env, work)
    ended = []
    d._end_isolation = lambda a: ended.append(True)
    result = d._session_intercept(agent, "pause", None)
    assert result["final_response"].startswith("Paused at message 1 of 2.")
    assert ended == [True] and agent.calls == []              # no tool, no model
    assert work.pending()["item_id"] == "m01"                 # the item still waits


def test_the_model_can_pause_for_a_change_of_subject(env):
    work = env.work()
    env.add("billing")
    env.propose(work)
    asked = env.prov(request="what's on my calendar?")
    work.pause(asked)
    armed = reissue.take("sess")
    assert (armed["request"], armed["leave_goal"], armed["advance"]) == (
        "what's on my calendar?", True, False)
    assert work.pending() is not None                         # nothing decided


def test_absorbing_is_only_for_what_repeats_the_loop(env):
    work = env.work()
    for said in ("next", "ok", "Tag the next message", dw.button_message("confirm", "m01")):
        assert work.absorbs(said) is True, said
    for said in ("what's the weather", "that one was wrong", "ops"):
        assert work.absorbs(said) is False, said
    assert env.work(session={**SESSION, "enabled": False}).absorbs("next") is False


def test_with_the_session_on_the_model_is_never_told_to_wait_for_the_operator(env):
    work = env.work()
    env.add("billing", "outage")
    env.propose(work)
    deciding = env.prov()
    work.decide(decision="confirm", provenance=deciding)
    with pytest.raises(DecisionRefused) as stop:
        work.check_one_step_per_turn(deciding)
    assert "the system presents the next item itself" in str(stop.value)
    assert "asks for it" not in str(stop.value).replace("to ask for it", "")
    off = env.work(session={**SESSION, "enabled": False})
    with pytest.raises(DecisionRefused) as stop:
        off.check_one_step_per_turn(deciding)
    assert "starts when the operator asks for it" in str(stop.value)   # as before


# ── setup and teardown: reset, and a backlog that arrives later ───────


def _with_backlog(env, tmp_path):
    work = env.work(session=BATCH)
    backlog = tmp_path / "backlog"
    backlog.mkdir()
    for n, channel in ((21, "billing"), (22, "legal")):
        (backlog / f"m{n}.txt").write_text(channel)
    cfg = work.config.__class__(**{**work.config.__dict__, "backlog": backlog})
    return DecisionWork(cfg), cfg


def test_the_backlog_is_released_into_the_queue_and_taken_back_out(env, tmp_path):
    work, cfg = _with_backlog(env, tmp_path)
    env.add("billing", "outage")
    assert dw.backlog_state(cfg) == {"declared": True, "items": 2, "released": 0}
    assert dw.release_backlog(cfg) == 2 and len(work.queue_items()) == 4
    assert dw.release_backlog(cfg) == 0                        # already there
    assert dw.backlog_state(cfg)["released"] == 2
    # Taking it back removes ONLY the backlog's own files.
    assert dw.withhold_backlog(cfg) == 2
    assert [p.name for p in work.queue_items()] == ["m01.txt", "m02.txt"]
    assert sorted(p.name for p in cfg.backlog.iterdir()) == ["m21.txt", "m22.txt"]
    with pytest.raises(ValueError, match="no backlog"):
        dw.release_backlog(env.work().config)


def test_reset_starts_over_without_deleting_anything(env, tmp_path):
    from grove.kaizen_ledger import default_ledger_dir
    from grove.pattern_cache import PatternCacheStore, STATUS_DEMOTED

    work, cfg = _with_backlog(env, tmp_path)
    _serve_keg()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    dw.release_backlog(cfg)
    before = len(work.log.records())
    plan = dw.reset_work(cfg, "again", apply=False)
    assert plan["applied"] is False and plan["backlog_removed"] == 2
    assert len(work.log.records()) == before                   # a dry run writes nothing
    assert PatternCacheStore().get("keg:mt:v2").status != STATUS_DEMOTED
    done = dw.reset_work(cfg, "again", surface="portal_demo_tokenless")
    assert (done["label"], done["first_item"], done["backlog_removed"]) == ("again", "m01", 2)
    assert done["kegs_revoked"] == [("keg:mt:v2", "active")]
    assert PatternCacheStore().get("keg:mt:v2").status == STATUS_DEMOTED
    # Every earlier record is still in the log; the new run just starts empty.
    assert len(work.log.records()) == before + 1 and work.tally()["decided"] == 0
    assert len(work.queue_items()) == 2
    events = [json.loads(l) for f in default_ledger_dir().glob("operator-*.jsonl")
              for l in f.read_text().splitlines()]
    [event] = [e for e in events if e.get("action") == "work_reset"]
    assert (event["event_type"], event["approval_surface"], event["run_opened"]) == (
        "operator_applied", "portal_demo_tokenless", done["run"])
    assert event["kegs_revoked"] == ["keg:mt:v2"] and event["record_hash"]


def test_the_portals_demo_controls_exist_only_in_demo_mode(env, tmp_path, monkeypatch):
    import asyncio
    import grove.api.actions as actions
    import grove.dock as dock_mod
    from grove.api import fragments

    work, cfg = _with_backlog(env, tmp_path)
    env.add("billing")
    goal = SimpleNamespace(id=GOAL)
    monkeypatch.setattr(dock_mod, "load_dock", lambda: SimpleNamespace(goals=(goal,)))
    monkeypatch.setattr(dw, "load_config", lambda g: cfg)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
    monkeypatch.setattr(actions, "_demo_tokenless_approve", lambda: True)
    panel = fragments._demo_panel_html()
    assert "Demo controls" in panel and "Reset to the start" in panel
    assert "Release the backlog (2 messages)" in panel
    assert f'/portal/actions/demo/{GOAL}/reset' in panel
    # Outside demo mode: no panel, and the action itself refuses and writes nothing.
    monkeypatch.setattr(actions, "_demo_tokenless_approve", lambda: False)
    assert fragments._demo_panel_html() == ""
    monkeypatch.setattr(fragments, "audit_page_html", lambda scale, note="": f"<div>{note}</div>")
    refused = []

    async def loud(html, **kw):
        refused.append(kw)
        return SimpleNamespace(status=kw["status"])

    monkeypatch.setattr(actions, "_loud_action_failure", loud)
    request = SimpleNamespace(match_info={"goal_id": GOAL})
    response = asyncio.run(actions._demo_action(request, "reset"))
    assert response.status == 403 and refused[0]["failure_class"] == "demo_controls_off"
    assert work.log.records() == []                            # nothing was opened


def test_after_the_backlog_arrives_the_next_request_runs_the_keg_pass_first(env, tmp_path):
    # Operator, 2026-10-06: once the backlog is in, the keg's answers go
    # through first, all at once; then the operator deals with the exceptions.
    work, cfg = _with_backlog(env, tmp_path)
    _serve_keg()
    assert work.session_action("keep going") is None          # nothing queued: not a request yet
    assert dw.release_backlog(cfg) == 2
    # Any request for the work now runs the pass — not only the batch phrase.
    assert work.session_action("next") == {"action": "batch"}
    out = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert out["reply"].startswith("Tagged 1 of 2 · 1 by the keg v2 · 0 model calls.")
    reissue.take("sess")
    # Once only: the next request brings the exception, not a second pass.
    assert work.session_action("next") is None
    assert reissue.goal_note(GOAL) is None
    # A reset owes no pass.
    dw.release_backlog(cfg) if dw.withhold_backlog(cfg) else None
    dw.reset_work(cfg, "again")
    assert reissue.goal_note(GOAL) is None


def test_the_operator_can_always_revise_a_call_already_made(env):
    # Live, run 8 (2026-10-06): the operator confirmed an item by button, then
    # asked to revise it, and was told a ruled item could not be reopened.
    from grove.pattern_cache import PatternCacheStore, STATUS_HALTED

    work = env.work()
    spec = _serve_keg()
    env.add("billing", "outage")
    work.apply_keg(spec, item_id="m01", inputs={"channel": "billing"},
                   keg_ref={"name": "Message tagging", "version": 2, "pattern_id": "keg:mt:v2"},
                   provenance=env.prov(tier="T0"))
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))      # confirmed by button
    reissue.take("sess")
    env.propose(work, tag="ops")                                       # m02 is now waiting
    later = env.prov()
    revised = work.rule_on("m01", decision="correct", corrected_output={"tag": "other"},
                           provenance=later)
    assert (revised["decision"], revised["after"], revised["output"]) == (
        "correct", "confirm", {"tag": "other"})
    # The earlier confirmation is still on record; the revision is what stands.
    kinds = [r["decision"] for r in work.log.run_records()
             if r["kind"] == "decided" and r["item_id"] == "m01"]
    assert kinds == ["confirm", "correct"] and work.tally()["revised"] == 1
    # A keg produced that answer, so this is a miss: flagged, and the keg halts.
    [event] = work.last_observations
    assert event["detector"] == "correction" and event["halted"] == ["keg:mt:v2"]
    assert PatternCacheStore().get("keg:mt:v2").status == STATUS_HALTED
    # The item that was waiting still waits, and nothing was armed.
    assert work.pending()["item_id"] == "m02" and reissue.take("sess") is None
    # The reply comes from the record.
    assert work.decided_reply(later["turn_uid"]).splitlines()[0] == (
        "Revised: finance Money in or out → other Everything else.")
    # And it can be revised again; revising to what it already is says so.
    work.rule_on("m01", decision="correct", corrected_output={"tag": "ops"}, provenance=env.prov())
    with pytest.raises(DecisionRefused) as same:
        work.rule_on("m01", decision="correct", corrected_output={"tag": "ops"},
                     provenance=env.prov())
    assert same.value.reason == "correction_matches"


def test_however_the_operator_asks_the_keg_goes_first_on_a_new_backlog(env, tmp_path):
    # Live, run 8 (2026-10-06): the operator said "let's dig into the backlog".
    # No declared phrase matched, so a model coded the first invoice — one the
    # keg covers — before the keg pass ran.
    work, cfg = _with_backlog(env, tmp_path)
    _serve_keg()
    dw.release_backlog(cfg)
    assert work.session_action("let's dig into the pile") is None      # the model's turn
    asked = env.prov(request="let's dig into the pile")
    assert work.backlog_first(asked) is True                           # ...but the keg goes first
    armed = reissue.take("sess")
    assert (armed["request"], armed["advance"]) == ("tag the next message", True)
    assert work.log.run_records() == []                                # the model was handed nothing
    # The re-issued request is the keg pass.
    assert work.session_action("tag the next message") == {"action": "batch"}
    work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    reissue.take("sess")
    assert work.backlog_first(env.prov()) is False                     # once
    # No released backlog, an item waiting, or the switch off: the model proceeds as usual.
    plain = env.work()
    assert plain.backlog_first(env.prov()) is False


# ── the goal page: a goal and its standard work ───────────────────────


def test_the_goal_page_shows_the_goals_standard_work_from_its_own_records(env, monkeypatch):
    from grove.api import fragments
    import grove.dock.attachment_store as attachments

    monkeypatch.setattr(attachments, "attachments_for_goal", lambda goal_id: [])
    work = env.work(session={**BATCH, "pause": ["pause"]})
    _serve_keg()
    env.add("billing", "outage")
    env.propose(work)
    work.session_step({"action": "confirm"}, env.prov(tier="T0"))
    goal = SimpleNamespace(
        id=GOAL, name="Tag every message", vector="operational", status="accelerating",
        definition_of_done="Every message has the tag I would give it.",
        keywords=("message",), root=work.config.queue.parent,
        extra={}, resolved_sources=lambda: [])
    monkeypatch.setattr(dw, "load_config", lambda g: work.config)
    html = fragments.render_goal_detail(None, goal)
    order = [html.index(s) for s in (
        '<div id="goal-detail">', "all goals", "GOAL · ACTIVE",
        "Tag every message", "THIS RUN", "1 / 2", "STANDARD WORK", "SESSION RULE",
        "Standard work, version by version",
        "How this work runs", "SIGNED · IN FORCE",
        "These phrases are acted on with no model; everything else goes to one.",
        "ACTED ON WITH NO MODEL",
        "Confirms", "Lets the keg decide a backlog at once",
        # The version history sits directly under the tiles; the buttons follow
        # the first pair of panels.
        "Open the scorecard",
        "What it works from", "Reference table", "channels.csv",
    )]
    assert order == sorted(order)
    # 2026-10-07: nothing on the page that reads as unfinished or internal:
    # no empty attachments section, no keyword tags, no Dock status words;
    # the session rule's detail (its match threshold) is in the hover.
    assert "Attached artifacts" not in html and "ACCELERATING" not in html
    assert '<span class="tag">message</span>' not in html
    assert 'title="Sessions for message-triage' in html and "overlap" in html
    parked = fragments.render_goal_detail(None, SimpleNamespace(**{**goal.__dict__,
                                                                  "status": "parked"}))
    assert "GOAL · PARKED" in parked
    for phrase in ("looks good", "work the backlog", "let&#x27;s tag some messages"):
        assert phrase in html, phrase
    # 2026-10-07: every link on the page is live. A link inside an element
    # that makes its own request is dead in the browser (the click is
    # cancelled), which is how the three buttons stopped working.
    from html.parser import HTMLParser

    class Links(HTMLParser):
        open, dead, seen = [], [], 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and attrs.get("href"):
                self.seen += 1
                if any(self.open):
                    self.dead.append(attrs["href"])
            if tag not in ("br", "hr", "img", "input"):
                self.open.append("hx-get" in attrs or "hx-post" in attrs)

        def handle_endtag(self, tag):
            if tag not in ("br", "hr", "img", "input"):
                self.open.pop()

    links = Links()
    links.feed(html)
    assert links.seen >= 4 and links.dead == []
    assert 'href="/portal#fragments/proposals/pending?type=signature">To sign<' in html
    # Unsigned: the page says the rule is not in force, in the event color.
    grants_mod.get_grant_store().signed.clear()
    unsigned = fragments.render_goal_detail(None, goal)
    assert "NOT SIGNED · NOT IN FORCE" in unsigned and "not signed" in unsigned
    # A goal with no decision work keeps its plain page.
    monkeypatch.setattr(dw, "load_config", lambda g: None)
    plain = fragments.render_goal_detail(None, goal)
    assert "Tag every message" in plain and "Attached artifacts" not in plain
    assert "STANDARD WORK" not in plain and "sc-tiles" not in plain


# ── a work session's knowledge stays in its records ───────────────────


def _row(n, said, *, work, sid="sess"):
    return {"session_id": sid, "turn_id": f"{sid}#{n}", "user_message_stem": said,
            "stages": {"telemetry": {"goal_session": GOAL} if work else {}}}


def _transcript(*turns):
    out = []
    for said, reply in turns:
        out += [{"role": "user", "content": said}, {"role": "assistant", "content": reply}]
    return out


def test_an_isolated_goal_keeps_its_sessions_out_of_memory_by_default(env, tmp_path):
    on = env.work()
    assert on.config.session_memory == dw.SESSION_MEMORY_RECORDS_ONLY      # follows isolation
    declared = dw.load_config(_goal(tmp_path, {**SESSION}))
    assert declared.session_memory == "records_only"
    goal = _goal(tmp_path)
    goal.extra["decision_work"]["session_memory"] = "compact"
    assert dw.load_config(goal).session_memory == "compact"                # the operator's call
    goal.extra["decision_work"]["session_memory"] = "sometimes"
    with pytest.raises(ValueError, match="session_memory"):
        dw.load_config(goal)


def test_work_turns_are_taken_out_and_only_the_conversation_is_left(env):
    work = env.work()
    env.add("billing")
    env.propose(work)                              # this session recorded a decision
    assert dw.goals_worked_in("sess") == [GOAL] and dw.goals_worked_in("other") == []
    transcript = _transcript(
        ("let's tag some messages", "Message 1 of 1: billing … Confirm or revise?"),
        ("ok", "Confirmed: finance."),
        ("pause", "Paused at message 1 of 1."),
        ("what did we decide about the offsite?", "You settled on March."),
        ("thanks, book it", "Booked."))
    rows = [_row(1, "let's tag some messages", work=True), _row(2, "ok", work=True),
            _row(3, "pause", work=True),
            _row(4, "what did we decide about the offsite?", work=False),
            _row(5, "thanks, book it", work=False)]
    kept, report = dw.conversation_only("sess", transcript, rows)
    assert [m["content"] for m in kept if m["role"] == "user"] == [
        "what did we decide about the offsite?", "thanks, book it"]
    assert (report["goals"], report["turns"], report["work_turns"], report["kept_turns"]) == (
        [GOAL], 5, 3, 2)
    assert not any("finance" in str(m["content"]) for m in kept)       # no decision leaks
    # A session that did no goal's work is untouched.
    same, untouched = dw.conversation_only("other", transcript, rows)
    assert same == transcript and untouched["goals"] == []


def test_when_in_doubt_the_whole_session_stays_out(env):
    work = env.work()
    env.add("billing")
    env.propose(work)
    transcript = _transcript(("let's tag some messages", "…"), ("ok", "Confirmed: finance."))
    # Records from before turns were marked: nothing can be told apart.
    old = [{"session_id": "sess", "turn_id": "sess#1", "user_message_stem": "let's tag some messages",
            "stages": {"telemetry": {}}},
           {"session_id": "sess", "turn_id": "sess#2", "user_message_stem": "ok",
            "stages": {"telemetry": {}}}]
    kept, report = dw.conversation_only("sess", transcript, old)
    assert kept == [] and "predate" in report["reason"]
    # A transcript that does not line up with its turn records.
    rows = [_row(1, "let's tag some messages", work=True), _row(2, "something else", work=False)]
    kept, report = dw.conversation_only("sess", transcript, rows)
    assert kept == [] and "could not be matched" in report["reason"]
    # The same exchange saved twice (transcripts before 2026-10-06) still lines up.
    doubled = _transcript(("let's tag some messages", "…"), ("ok", "Confirmed."), ("ok", "Confirmed."),
                          ("unrelated question", "Answer."))
    rows = [_row(1, "let's tag some messages", work=True), _row(2, "ok", work=True),
            _row(3, "unrelated question", work=False)]
    kept, report = dw.conversation_only("sess", doubled, rows)
    assert [m["content"] for m in kept if m["role"] == "user"] == ["unrelated question"]
    assert report["work_turns"] == 3


def test_a_goal_that_chooses_to_be_summarized_is(env, tmp_path, monkeypatch):
    goal = _goal(tmp_path)
    goal.extra["decision_work"]["session_memory"] = "compact"
    cfg = dw.load_config(goal)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
    work = DecisionWork(cfg)
    env.add("billing")
    env.propose(work)
    transcript = _transcript(("let's tag some messages", "…"))
    kept, report = dw.conversation_only("sess", transcript, [_row(1, "let's tag some messages", work=True)])
    assert kept == transcript and report["goals"] == []


def test_the_operators_own_words_are_kept_with_the_ruling(env):
    work = env.work()
    env.add("billing", "outage")
    # An answer to the model's question travels with the item it produced.
    answered = env.prov(request="it's a refund dispute, so legal")
    record = work.record(item_id="m01", inputs={"channel": "billing"}, output={"tag": "other"},
                         reasoning="operator says legal", provenance=answered)
    assert record["operator_said"] == "it's a refund dispute, so legal"
    # A revision explained in the operator's words keeps the explanation.
    why = env.prov(request="no, refund disputes go to finance")
    ruled = work.decide(decision="correct", corrected_output={"tag": "finance"}, provenance=why)
    assert ruled["operator_said"] == "no, refund disputes go to finance"
    # ...and Kaizen is handed it, as the best evidence of what differs.
    [case] = work.history()
    assert case["operator_said"] == "no, refund disputes go to finance"
    # A button, an exact phrase, or a bare request for the work carries no reasoning.
    reissue.take("sess")
    env.propose(work, tag="ops")
    quiet = work.log.run_records()[-1]
    assert "operator_said" not in quiet                        # "tag the next message"
    out = work.session_step({"action": "confirm"}, env.prov(tier="T0", request="ok"))
    assert "operator_said" not in [r for r in work.log.run_records() if r["kind"] == "decided"][-1]


def test_a_no_model_turn_is_saved_to_the_transcript_once():
    # Live, 2026-10-06: every keg serve and every T0 confirm was in the session
    # transcript twice — written by the Dispatcher, then again by the gateway.
    # The model read "confirm" twice and spent a call acting on the second.
    import inspect
    from gateway import run as gw

    result = Dispatcher._t0_result_dict(SimpleNamespace(), SimpleNamespace(), "Confirmed.")
    assert result["transcript_persisted"] is True and result["messages"] == []
    source = inspect.getsource(gw)
    fallback = source[source.index("# If no new messages found (edge case)"):]
    fallback = fallback[:fallback.index("else:")]
    assert 'agent_result.get("transcript_persisted")' in fallback
    assert fallback.count("skip_db=_already") == 2            # the user line and the reply


def test_compaction_and_memory_mining_both_honor_the_split():
    import inspect
    from grove import dispatcher as disp

    mined = inspect.getsource(disp.Dispatcher._session_is_goal_isolated)
    assert "goals_worked_in" in mined                         # the lasting record, not the latch
    compacted = inspect.getsource(disp.Dispatcher._extract_memory_from_dormant_sessions)
    split = compacted.index("conversation_only(")
    assert split < compacted.index("filter_transcript_for_extraction(transcript)")
    assert split < compacted.index("compact_session(")        # work is out before any summary


# ── live, run 11 (2026-10-06): a question left open, and a pin that wandered ──


def test_asking_for_the_work_while_a_question_is_open_repeats_the_question(env):
    work = env.work()
    env.add("billing", "outage")
    asking = env.prov()
    work.ask(asking, "Is this a refund or a new charge?")
    # "next" has nothing to present: the model is waiting on the operator.
    action = work.session_action("next", session_id="sess")
    assert action == {"action": "ask_again", "question": "Is this a refund or a new charge?"}
    out = work.session_step(action, env.prov(tier="T0"))
    assert out["reply"] == "Still waiting on your answer:\nIs this a refund or a new charge?"
    assert out["decided"] is False and work.log.run_records() == []
    assert work.last_match["fired"] is True and work.last_match["action"] == "ask_again"
    # The operator's answer is not a request for the work: it goes to the model.
    assert work.session_action("a refund", session_id="sess") is None
    # Once the answer is on record the question is closed, and "next" is the work again.
    env.propose(work)
    assert reissue.open_question("sess") is None
    assert work.session_action("next", session_id="sess")["action"] == "present"
    # With no question open, asking for the work is routed as it always was.
    work.decide(decision="confirm", provenance=env.prov())
    reissue.take("sess")
    assert work.session_action("next", session_id="sess") is None


def test_a_tier_pin_is_only_for_the_request_it_was_armed_for(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    reissue.arm_tier("sess", "T3", attempts=[{"tier": "T2"}], andon_id="a1", request="next")
    # Another message arrives first: the pin is dropped, never kept for later.
    assert reissue.take_pin("sess", "furniture, sorry") is None
    assert reissue.take_pin("sess", "next") is None
    reissue.arm_tier("sess", "T3", attempts=[{"tier": "T2"}], andon_id="a1", request="next")
    assert reissue.take_pin("sess", " next ")["tier"] == "T3"
    assert reissue.take_pin("sess", "next") is None                 # exactly once
    # A pin armed with no request (an older caller) still pins the next turn.
    reissue.arm_tier("sess", "T2")
    assert reissue.take_pin("sess", "anything")["tier"] == "T2"


def test_the_dispatcher_takes_the_pin_at_the_start_of_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    d = SimpleNamespace(session_id="sess", _current_turn_escalation=None)
    for name in ("_take_pin", "_take_reissue_tier"):
        setattr(d, name, getattr(Dispatcher, name).__get__(d))
    reissue.arm_tier("sess", "T3", attempts=[{"tier": "T2"}], andon_id="a1", request="next")
    # A turn that is not the armed request consumes the pin and does not use it.
    d._current_turn_pin = d._take_pin(None, "furniture, sorry")
    assert d._take_reissue_tier(None) is None and d._current_turn_escalation is None
    d._current_turn_pin = d._take_pin(None, "code the next invoice")
    assert d._take_reissue_tier(None) is None                       # nothing left behind
    reissue.arm_tier("sess", "T3", attempts=[{"tier": "T2"}], andon_id="a1", request="next")
    d._current_turn_pin = d._take_pin(None, "next")
    assert d._take_reissue_tier(None) == "T3"
    assert d._current_turn_escalation == {"attempts": [{"tier": "T2"}], "andon_id": "a1"}


# ── a backlog in stages: one release, one batch, one period each ──────


def _staged(env, tmp_path):
    work = env.work(session=BATCH)
    stages = []
    for label, items in (("Month 2", ((21, "billing"), (22, "legal"))),
                         ("Month 3", ((31, "outage"), (32, "billing"), (33, "press")))):
        folder = tmp_path / label.replace(" ", "").lower()
        folder.mkdir()
        for n, channel in items:
            (folder / f"m{n}.txt").write_text(channel)
        stages.append((folder, label))
    cfg = work.config.__class__(**{**work.config.__dict__, "backlog": stages[0][0],
                                   "backlog_stages": tuple(stages)})
    return DecisionWork(cfg), cfg


def test_a_staged_backlog_is_released_one_stage_at_a_time(env, tmp_path):
    work, cfg = _staged(env, tmp_path)
    state = dw.backlog_state(cfg)
    assert (state["items"], state["released"]) == (5, 0)
    assert [(s["label"], s["items"], s["released"]) for s in state["stages"]] == [
        ("Month 2", 2, 0), ("Month 3", 3, 0)]
    assert dw.next_backlog_stage(cfg)["label"] == "Month 2"
    assert dw.release_backlog(cfg) == 2                          # the first stage only
    assert sorted(p.stem for p in work.queue_items()) == ["m21", "m22"]
    assert dw.next_backlog_stage(cfg)["label"] == "Month 3"
    assert dw.release_backlog(cfg) == 3 and len(work.queue_items()) == 5
    assert dw.next_backlog_stage(cfg) is None and dw.release_backlog(cfg) == 0
    assert dw.withhold_backlog(cfg) == 5 and work.queue_items() == []   # every stage, back out


def test_one_batch_per_stage_however_many_keg_passes_it_takes(env, tmp_path):
    work, cfg = _staged(env, tmp_path)
    _serve_keg()
    dw.release_backlog(cfg)                                      # Month 2 arrives
    first = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    month2 = first["batch"]["batch"]
    assert first["batch"]["coded"] == 1 and month2
    # The exception goes to a model and is confirmed; then a second keg pass
    # inside the same month does not begin a new batch.
    reissue.take("sess")
    env.propose(work, tag="other")
    work.decide(decision="confirm", provenance=env.prov())
    reissue.take("sess")
    (cfg.queue / "m23.txt").write_text("billing")                # a late arrival, same month
    again = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert again["batch"]["batch"] == month2
    # A newly released stage begins another.
    dw.release_backlog(cfg)                                      # Month 3 arrives
    third = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    month3 = third["batch"]["batch"]
    assert month3 and month3 != month2 and third["batch"]["coded"] == 2
    batches = [r["batch"] for r in work.log.run_records() if r["kind"] == "proposed"]
    assert batches == [month2, month2, month2, month3, month3]


def test_backlog_stages_are_declared_and_a_bare_folder_is_one_stage(tmp_path):
    def cfg(backlog):
        goal = _goal(tmp_path, BATCH)
        goal.extra["decision_work"]["backlog"] = backlog
        return dw.load_config(goal)

    assert cfg("later").backlog_stages == ((tmp_path / "later", "Month 2"),)
    staged = cfg([{"folder": "m2", "label": "Month 2 · production"},
                  {"folder": "m3", "label": "Month 3 · production"}])
    assert [label for _f, label in staged.backlog_stages] == [
        "Month 2 · production", "Month 3 · production"]
    assert staged.backlog == tmp_path / "m2"
    for bad, why in (([{"folder": "m2"}], "needs a folder and a label"),
                     ([{"folder": "a", "label": "X"}, {"folder": "b", "label": "X"}],
                      "labels must be different"),
                     (7, "a folder or a list of stages")):
        with pytest.raises(ValueError, match=why):
            cfg(bad)


# ── a proposal is its own card, and the session waits for the ruling ──
# Live, run 12 (2026-10-07): "Kaizen proposed keg v4" arrived as a line inside
# a confirmation reply and was missed; the rule went unsigned for the rest of
# the run.


def _earn_a_proposal(env):
    """Three confirmed decisions matching the reference table: Kaizen proposes v1."""
    work = env.work(session={**SESSION, "pause": ["pause"]})
    env.add("billing", "outage", "billing", "outage", "billing")
    out = None
    for _ in range(3):
        env.propose(work, tag={"billing": "finance", "outage": "ops"}[work.next_item().read_text()])
        out = work.session_step({"action": "confirm"}, env.prov(tier="T0"))
        if _ < 2:
            reissue.take("sess")
    return work, out


def test_a_proposal_arrives_as_its_own_card_and_the_session_holds(env):
    from grove.eval.proposal_queue import read_all

    work, out = _earn_a_proposal(env)
    [proposal] = [p for p in read_all() if (p.payload or {}).get("keg")]
    # Not a line inside the confirmation reply.
    assert "Kaizen proposed" not in out["reply"] and out["reply"].startswith("Confirmed:")
    [card] = reissue.take_cards("sess")
    assert card["proposal_id"] == proposal.proposal_id
    assert card["text"].startswith("Kaizen proposed keg v1. It needs your signature.")
    assert "The work pauses here until you sign it, send it back, or tap Later." in card["text"]
    link, later = card["buttons"]
    assert link["label"] == "Review and sign"
    short = proposal.proposal_id.split(":")[-1][:12]
    assert link["url"].endswith(f"#fragments/proposals/pending?type=signature&at=proposal-{short}")
    assert later == ["Later", f"proposal later #{short}"]
    # The next item is NOT brought: the session is held after the item in hand.
    assert out["next_armed"] is False and reissue.take("sess") is None
    assert reissue.held("sess")["proposal_id"] == proposal.proposal_id
    # Asking for the work says what it is waiting on, with no model.
    asked = work.session_action("next", session_id="sess")
    assert asked["action"] == "hold_signature"
    reply = work.session_step(asked, env.prov(tier="T0"))["reply"]
    assert reply.startswith("Paused for your signature on keg v1.") and "Review and sign: " in reply
    assert reissue.take("sess") is None and work.pending() is None


def test_later_carries_on_and_leaves_the_proposal_waiting(env):
    from grove.eval.proposal_queue import read_all

    work, _ = _earn_a_proposal(env)
    [proposal] = [p for p in read_all() if (p.payload or {}).get("keg")]
    press = dw.proposal_message(proposal.proposal_id)
    action = work.session_action(press, session_id="sess")
    assert action == {"action": "proposal_later", "button": True,
                      "proposal": proposal.proposal_id.split(":")[-1][:12]}
    out = work.session_step(action, env.prov(tier="T0"))
    assert out["reply"] == "OK, later. Keg v1 stays under To sign. Carrying on."
    assert out["next_armed"] is True and reissue.take("sess")["request"] == "tag the next message"
    assert reissue.held("sess") is None
    assert [p.proposal_id for p in read_all() if (p.payload or {}).get("keg")] == [
        proposal.proposal_id]                                  # still waiting to be signed
    # The card is spent: a second tap is answered plainly.
    with pytest.raises(DecisionRefused) as stale:
        work.session_step(action, env.prov(tier="T0"))
    assert stale.value.reason == "stale_card"
    # A press is never a change of subject while the next item is on its way.
    assert work.waits(press) is True and work.waits("what's the weather") is False


def test_signing_or_sending_back_lifts_the_hold_and_wakes_the_chat(env, monkeypatch):
    from grove import flywheel_cli as fc
    from grove.eval.proposal_queue import read_all

    work, _ = _earn_a_proposal(env)
    [proposal] = [p for p in read_all() if (p.payload or {}).get("keg")]
    woken = []
    monkeypatch.setattr(reissue, "_waker", lambda session_id, info: woken.append(
        (session_id, info["goal"], info["proposal_id"])))
    assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0
    assert woken == [("sess", GOAL, proposal.proposal_id)] and reissue.held("sess") is None
    # With no chat to wake (another surface, a restart), the hold still lifts
    # on the operator's next request.
    monkeypatch.setattr(reissue, "_waker", None)
    reissue.hold("sess", {"goal": GOAL, "proposal_id": "sha256:gone", "what": "keg v9"})
    assert work.session_action("next", session_id="sess") is None      # routed as usual
    assert reissue.held("sess") is None


def test_a_batch_already_running_is_not_interrupted(env):
    work = env.work(session=BATCH)
    _serve_keg()
    env.add("billing", "outage", "billing")
    out = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    # The keg pass decides everything it covers in one turn; no card, no hold.
    assert out["batch"]["coded"] == 3 and reissue.take_cards("sess") == []
    assert reissue.held("sess") is None


def test_releasing_a_backlog_stage_goes_on_the_ledger(env, tmp_path):
    from grove.kaizen_ledger import default_ledger_dir

    work, cfg = _staged(env, tmp_path)
    assert dw.release_backlog(cfg) == 2
    events = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        events += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    [released] = [e for e in events if e.get("action") == "backlog_released"]
    assert (released["event_type"], released["stage"], released["items"], released["goal"]) == (
        "operator_applied", "Month 2", 2, GOAL)


# ── a backlog worked in item order ────────────────────────────────────
# 2026-10-07: keg first put every model item at the end of the batch, back to
# back. Declared ``batch: {order: item_order}``, the queue is worked in its
# own order: the keg decides each run of items it covers, a model takes each
# item between, and every item of a stage carries that stage's batch.


def _in_order(env, tmp_path, hold=True):
    work = env.work(session=BATCH)
    stages = []
    for label, items in (("Month 2", ((21, "billing"), (22, "legal"), (23, "press"),
                                      (24, "outage"), (25, "billing"))),
                         ("Month 3", ((31, "social"), (32, "billing")))):
        folder = tmp_path / label.replace(" ", "").lower()
        folder.mkdir()
        for n, channel in items:
            (folder / f"m{n}.txt").write_text(channel)
        stages.append((folder, label))
    cfg = work.config.__class__(**{
        **work.config.__dict__, "backlog": stages[0][0], "backlog_stages": tuple(stages),
        "batch_order": dw.BATCH_ITEM_ORDER, "hold_on_proposal": hold})
    return DecisionWork(cfg), cfg


def test_a_backlog_in_item_order_interleaves_the_keg_and_the_model(env, tmp_path):
    work, cfg = _in_order(env, tmp_path)
    _serve_keg()                                   # covers billing and outage
    ask = "tag the next message"

    def by_model(tag="other"):
        item = work.next_item()
        work.record(item_id=item.stem, inputs={"channel": item.read_text()},
                    output={"tag": tag}, reasoning="r", provenance=env.prov())
        work.decide(decision="confirm", provenance=env.prov())
        reissue.take("sess")
        return item.stem

    dw.release_backlog(cfg)                        # Month 2 arrives
    assert work.session_action(ask) == {"action": "batch"}
    first = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    # The keg decided the run it covers and stopped at the first item it does not.
    # 2026-10-07: the first pass of a version says it is in force; the line on
    # what comes next names the item and why it goes to a model.
    assert first["reply"] == (
        "Signed: v2 is in force.\n"
        "Tagged 1 by the keg v2 · 0 model calls · 4 to go.\n"
        "Next: legal, not covered by any rule, so it goes to a model. "
        "Then 1 more for a model before the keg's next run.")
    assert first["batch"]["for_model"] == ["m22", "m23"] and first["next_armed"] is True
    reissue.take("sess")
    # Those two go straight to a model, in order: the request is routed as usual.
    assert work.session_action(ask) is None and by_model() == "m22"
    assert work.session_action(ask) is None and by_model() == "m23"
    # Then the keg again, to the end of the stage.
    assert work.session_action(ask) == {"action": "batch"}
    rest = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert rest["batch"]["coded"] == 2 and rest["batch"]["left"] == 0
    assert rest["reply"].startswith("Tagged 2 of 2 · 2 by the keg v2 · 0 model calls.")
    assert work.session_action(ask) == {"action": "summary"}       # month 2 is done
    order = [(r["item_id"], bool(r.get("keg"))) for r in work.log.run_records()
             if r["kind"] == "proposed"]
    assert order == [("m21", True), ("m22", False), ("m23", False), ("m24", True),
                     ("m25", True)]                                # the queue's own order
    month2 = {r["batch"] for r in work.log.run_records() if r["kind"] == "proposed"}
    assert len(month2) == 1 and None not in month2                 # one batch, model items too

    # Month 3 opens on an item the keg does not cover: a model first, and the
    # stage still gets a batch of its own.
    dw.release_backlog(cfg)
    assert work.session_action(ask) == {"action": "batch"}
    opened = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert opened["reply"] == "Next: social, not covered by any rule, so it goes to a model."
    assert opened["batch"]["coded"] == 0
    reissue.take("sess")
    assert work.session_action(ask) is None and by_model() == "m31"
    assert work.session_action(ask) == {"action": "batch"}
    work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    batches = [r["batch"] for r in work.log.run_records()
               if r["kind"] == "proposed" and r["item_id"] in ("m31", "m32")]
    assert len(set(batches)) == 1 and batches[0] not in month2


def test_a_new_keg_version_looks_again_at_what_the_last_one_left(env, tmp_path):
    work, cfg = _in_order(env, tmp_path)
    _serve_keg()
    dw.release_backlog(cfg)
    work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    reissue.take("sess")
    assert work._for_model() == ["m22", "m23"]
    assert work.session_action("tag the next message") is None     # v2 does not answer m22
    # The operator signs a version: what v2 left is the new version's to look at.
    from grove.pattern_cache import PatternCacheStore, STATUS_SUPERSEDED
    for entry in PatternCacheStore().all():
        PatternCacheStore().set_status(entry.pattern_id, STATUS_SUPERSEDED)
    _serve_keg(version=3)
    assert work._for_model() == []
    assert work.session_action("tag the next message") == {"action": "batch"}
    # Straight after a signature, with nothing for the new version to decide
    # yet: the reply still moves the story on.
    after = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert after["reply"] == (
        "Signed: v3 is in force.\n"
        "Next: legal, not covered by any rule, so it goes to a model. "
        "Then 1 more for a model before the keg's next run.")
    # A rule that sends the item to a model says so.
    spec = {"inputs": {"channel": {"data_type": "string"}},
            "conditions": [{"if": "channel == 'legal'", "defer": True}]}
    assert work._next_for_model_line(spec, _read, 1) == (
        "Next: legal, a rule sends it to a model.")


def test_the_batch_block_is_declared_and_the_hold_can_be_switched_off(env, tmp_path):
    def cfg(**block):
        goal = _goal(tmp_path, BATCH)
        if block:
            goal.extra["decision_work"]["batch"] = block
        return dw.load_config(goal)

    plain = cfg()
    assert (plain.batch_order, plain.hold_on_proposal) == (dw.BATCH_KEG_FIRST, True)
    declared = cfg(order="item_order", hold_on_proposal=False)
    assert (declared.batch_order, declared.hold_on_proposal) == (dw.BATCH_ITEM_ORDER, False)
    for bad in ({"order": "random"}, {"hold_on_proposal": "yes"}, {"speed": 1}):
        with pytest.raises(ValueError, match="batch"):
            cfg(**bad)
    # Not part of the signed session rule.
    assert dw.session_rule_digest(plain) == dw.session_rule_digest(declared)

    # The hold itself: with it on the session is held; switched off, the card
    # is still offered and the work carries on.
    for hold in (True, False):
        work = DecisionWork(plain.__class__(**{**plain.__dict__, "hold_on_proposal": hold}))
        work.last_observations = [{"answer": {"kind": "standard_work", "artifact": "sha256:abc",
                                              "detail": {"version": 3}}}]
        reissue.release_hold("sess")
        assert work._hold_for_signature(env.prov()) == 1
        [card] = reissue.take_cards("sess")
        assert ("pauses here" in card["text"]) is hold
        assert bool(reissue.held("sess")) is hold
    reissue.release_hold("sess")


# ── a flagged check: a keg hands back what resembles a case it was corrected on ──


def _resembling(env, tmp_path, monkeypatch, resemble):
    goal = _goal(tmp_path)
    block = goal.extra["decision_work"]
    block["inputs"]["subject"] = {"data_type": "string"}
    if resemble is not None:
        block["evidence"]["resembles_corrected"] = resemble
    cfg = dw.load_config(goal)
    grants_mod.get_grant_store().sign(cfg)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
    work = DecisionWork(cfg)
    spec = _serve_keg()
    spec["inputs"]["subject"] = {"data_type": "string"}
    # The keg decides a message on its channel; the operator revises it.
    env.add("billing")
    work.apply_keg(
        spec, item_id="m01",
        inputs={"channel": "billing", "subject": "Admin certification course, 2 seats"},
        keg_ref={"name": "Message tagging", "version": 2, "pattern_id": "keg:mt:v2"},
        provenance=env.prov(tier="T0"))
    work.decide(decision="correct", corrected_output={"tag": "other"}, provenance=env.prov())
    return work, spec


ALIKE = {"channel": "billing", "subject": "Certification course for the admin team, 5 seats"}
PLAIN = {"channel": "billing", "subject": "Monthly subscription, 30 seats"}


def test_switched_on_a_keg_hands_back_what_resembles_a_corrected_item(env, tmp_path, monkeypatch):
    work, spec = _resembling(env, tmp_path, monkeypatch,
                             {"enabled": True, "threshold": 0.5, "on": ["subject"]})
    assert work.resembles_corrected(ALIKE) == {"item_id": "m01", "share": 0.57}
    assert work.keg_answer(spec, ALIKE) is None                  # goes to a model
    # An ordinary item with the same key is still the keg's to decide.
    assert work.resembles_corrected(PLAIN) is None
    assert work.keg_answer(spec, PLAIN) == {"tag": "finance"}
    # The same words under another key: no correction there, nothing to resemble.
    assert work.keg_answer(spec, {**ALIKE, "channel": "outage"}) == {"tag": "ops"}


def test_the_resemblance_check_is_off_unless_the_goal_switches_it_on(env, tmp_path, monkeypatch):
    work, spec = _resembling(env, tmp_path, monkeypatch, None)
    assert work.resembles_corrected(ALIKE) is None
    assert work.keg_answer(spec, ALIKE) == {"tag": "finance"}


def test_the_resemblance_check_declared_and_switched_off_changes_nothing(
        env, tmp_path, monkeypatch):
    work, spec = _resembling(env, tmp_path, monkeypatch,
                             {"enabled": False, "threshold": 0.5, "on": ["subject"]})
    assert work.config.evidence.resemble_threshold is None
    assert work.keg_answer(spec, ALIKE) == {"tag": "finance"}
    # It is not part of the signed session rule: switching it needs no new signature.
    on = _goal(tmp_path)
    on.extra["decision_work"]["inputs"]["subject"] = {"data_type": "string"}
    on.extra["decision_work"]["evidence"]["resembles_corrected"] = {
        "enabled": True, "threshold": 0.5, "on": ["subject"]}
    assert dw.session_rule_digest(dw.load_config(on)) == dw.session_rule_digest(work.config)


@pytest.mark.parametrize("bad", [
    "yes", {"enabled": True}, {"enabled": True, "threshold": 0, "on": ["subject"]},
    {"enabled": True, "threshold": 1.5, "on": ["subject"]},
    {"enabled": True, "threshold": 0.5}, {"enabled": True, "threshold": 0.5, "on": ["nope"]},
    {"enabled": "on", "threshold": 0.5, "on": ["subject"]},
])
def test_a_resemblance_declaration_that_cannot_be_read_is_refused(tmp_path, bad):
    goal = _goal(tmp_path)
    goal.extra["decision_work"]["inputs"]["subject"] = {"data_type": "string"}
    goal.extra["decision_work"]["evidence"]["resembles_corrected"] = bad
    with pytest.raises(ValueError, match="resembles_corrected"):
        dw.load_config(goal)


@pytest.mark.parametrize("authority", [None, "yellow"])
def test_with_no_keg_to_take_a_run_every_backlog_item_reaches_a_model(env, tmp_path, authority):
    # Live 2026-10-08, a run in which no keg ever formed: after the release,
    # every request for the next item was answered "nothing is decided in
    # bulk" and no item was ever reached.
    work, cfg = _in_order(env, tmp_path)
    if authority:
        _serve_keg(authority=authority)
    ask = "tag the next message"
    dw.release_backlog(cfg)
    assert work.session_action(ask) == {"action": "batch"}        # the release: said once
    said = work.session_step({"action": "batch", "inputs_for": _read}, env.prov(tier="T0"))
    assert "nothing is decided in bulk" in said["reply"]
    reissue.take("sess")
    for expected in ("m21", "m22", "m23", "m24", "m25"):
        assert work.session_action(ask) is None                   # a model's turn
        item = work.next_item()
        assert item.stem == expected
        work.record(item_id=item.stem, inputs={"channel": item.read_text()},
                    output={"tag": "other"}, reasoning="r", provenance=env.prov())
        work.decide(decision="confirm", provenance=env.prov())
        reissue.take("sess")
    assert work.session_action(ask) == {"action": "summary"}


def test_a_goal_may_name_one_of_its_own_runs_as_the_benchmark(tmp_path):
    goal = _goal(tmp_path)
    assert dw.load_config(goal).benchmark is None
    goal.extra["decision_work"]["benchmark"] = {"run": 37, "label": "Inference only"}
    cfg = dw.load_config(goal)
    assert cfg.benchmark == (37, "Inference only")
    # A comparison is not part of how a session behaves: no new signature.
    plain = _goal(tmp_path)
    assert dw.session_rule_digest(cfg) == dw.session_rule_digest(dw.load_config(plain))
    for bad in ("37", {"label": "x"}, {"run": 0}, {"run": True}, {"run": "37"}):
        goal.extra["decision_work"]["benchmark"] = bad
        with pytest.raises(ValueError, match="benchmark"):
            dw.load_config(goal)


def test_a_goal_may_say_what_it_calls_its_expert(tmp_path):
    goal = _goal(tmp_path)
    assert dw.load_config(goal).operator_called == "operator"
    goal.extra["decision_work"]["operator_called"] = "controller"
    cfg = dw.load_config(goal)
    assert cfg.operator_called == "controller"
    assert dw.session_rule_digest(cfg) == dw.session_rule_digest(dw.load_config(_goal(tmp_path)))
    for bad in ("", 7, ["controller"]):
        goal.extra["decision_work"]["operator_called"] = bad
        with pytest.raises(ValueError, match="operator_called"):
            dw.load_config(goal)


def test_the_queue_is_worked_in_the_order_its_items_are_numbered(env, tmp_path):
    # Live 2026-10-08: later items were numbered with three digits ("021") and
    # earlier ones with two ("20"). Spelled order put "021" before "02_", so the
    # 64th item's card read "49 of 100".
    work = env.work()
    for name in ("01_a", "02_b", "20_c", "021_d", "064_e", "100_f", "notes"):
        (tmp_path / "queue" / f"{name}.txt").write_text("billing")
    assert [p.stem for p in work.queue_items()] == [
        "01_a", "02_b", "20_c", "021_d", "064_e", "100_f", "notes"]
    assert work.progress("064_e") == (5, 7) and work.next_item().stem == "01_a"
