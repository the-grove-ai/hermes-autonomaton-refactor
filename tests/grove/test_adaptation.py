"""Adaptation: a goal's work learns a phrase the operator keeps using.

MESSAGE-TAGGING fixtures — nothing here knows a domain. Pinned:

  * an alias can only reach a verb the SIGNED session rule lets the
    vocabulary supply;
  * a typed "yes" never approves an alias, and never confirms an item by
    accident while a question is open;
  * adaptation off is today's behavior, byte for byte;
  * the count comes from the decision log, and a revision resets it;
  * the whole cycle — read by a model, asked, approved, served with no
    model, taken back — runs on the existing bus and the existing records.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import grove.grants as grants_mod
import grove.pattern_cache as pc
from grove import adaptation, andon
from grove import decision_work as dw
from grove import flywheel_cli as fc
from grove import reissue, turn_provenance
from grove.decision_work import DecisionRefused, DecisionWork
from grove.eval.proposal_queue import read_all
from grove.kaizen_ledger import default_ledger_dir, verify_ledger_chain

GOAL = "message-triage"
SESSION = {
    "enabled": True,
    "start": ["let's tag some messages"],
    "confirm": ["confirm", "yes", "ok", "looks good"],
    "revise": ["revise"],
    "pause": ["pause"],
    "buttons": {"confirm": "Confirm", "revise": "Revise"},
}
LEARNING = {
    "enabled": True,
    "patterns": [
        {"id": "phrase-for-confirm", "counts": "phrase_read_as", "verb": "confirm",
         "threshold": 3},
        {"id": "escalations", "counts": "turns_failed_upward", "threshold": 3,
         "propose": False},
    ],
}


class _Grants:
    def __init__(self):
        self.signed = {}

    def sign(self, cfg):
        self.signed[(cfg.goal_id, dw.SESSION_RULE_PREFIX + dw.session_rule_digest(cfg))] = (
            SimpleNamespace(id="grant-ws", revoked=False))

    def get_grant(self, scope, write_class):
        return self.signed.get((scope, write_class))


def _goal(tmp_path, learning=LEARNING, session=SESSION):
    (tmp_path / "queue").mkdir(exist_ok=True)
    (tmp_path / "channels.csv").write_text(
        "Channel,Default Tag\nbilling,finance\noutage,ops\n", encoding="utf-8")
    (tmp_path / "tags.csv").write_text(
        "Tag,Meaning\nfinance,Money in or out\nops,Something is down\nother,Everything else\n",
        encoding="utf-8")
    block = {
        "tool": "tag_message", "queue": "queue", "isolation": "sources_only",
        "on_unclean": "open_clean_session",
        "inputs": {"channel": {"data_type": "string", "required": True}},
        "outputs": {"tag": {"data_type": "string"}},
        "reference_table": {"path": "channels.csv", "key_column": "Channel",
                            "value_column": "Default Tag", "key_input": "channel",
                            "value_output": "tag"},
        "output_domains": [{"output": "tag", "path": "tags.csv", "column": "Tag",
                            "name_column": "Meaning"}],
        "evidence": {"threshold": 50},
        "keg": {"name": "Message tagging", "request": "tag the next message",
                "requests": ["next"]},
        "work_session": dict(session),
    }
    if learning is not None:
        block["adaptation"] = json.loads(json.dumps(learning))
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
        signed = True
        learning = LEARNING

        def revoke_signature(self):
            grants.signed.clear()
            self.signed = False

        def work(self):
            """The goal as the next turn would load it: Dock, then vocabulary."""
            cfg = dw.load_config(_goal(tmp_path, self.learning))
            if self.signed:
                grants.sign(cfg)
                cfg = dw.load_config(_goal(tmp_path, self.learning))
            monkeypatch.setattr(dw, "config_for_goal",
                                lambda goal_id, dock=None: self.work().config)
            return DecisionWork(cfg)

        def prov(self, **over):
            self.turn += 1
            base = {"session_id": "sess", "turn_id": f"sess#{self.turn}",
                    "turn_uid": f"u{self.turn}", "tier": "T1", "model": "m",
                    "request": "tag the next message", "cellar_hits": 0, "sections": [],
                    "tools_yielded": ["tag_message"], "isolation_goal": GOAL}
            base.update(over)
            return base

        def present(self, work, channel="billing"):
            n = len(list((tmp_path / "queue").iterdir())) + 1
            (tmp_path / "queue" / f"m{n:02d}.txt").write_text(channel)
            reissue.take("sess")
            return work.record(item_id=f"m{n:02d}", inputs={"channel": channel},
                               output={"tag": "finance"}, reasoning="billing is finance",
                               provenance=self.prov())

        def says(self, said):
            """One operator message, as the Dispatcher would route it. Returns
            (tier, reply or None, decided record or None)."""
            work = self.work()
            action = work.session_action(said)
            if action is not None:
                out = work.session_step(action, self.prov(
                    tier="T0", request=said, session_step="t0"))
                return "T0", out["reply"], work.last_match
            # Fell through to the model, which reads it and records the verb.
            decided = work.decide(decision="confirm", provenance=self.prov(request=said))
            return "T1", None, decided

    yield Env()
    turn_provenance.reset(token)


def _questions():
    return adaptation.pending(GOAL)


def _press(answer):
    [proposal] = _questions()
    return dw.alias_message(answer, proposal.proposal_id.split(":")[-1][:12])


# ── the three invariants ──────────────────────────────────────────────


def test_an_alias_cannot_reach_a_verb_the_signed_rule_does_not_name(env, tmp_path):
    work = env.work()
    rule = dw.session_rule(work.config)
    assert rule["work_session"]["vocabulary"] == {
        "verbs": ["confirm"], "match": "exact", "added_by": "the operator, in conversation"}
    # Applying an alias for any other verb is refused at the moment of writing.
    for verb in ("revise", "batch", "pause", "decide_everything"):
        with pytest.raises(andon.ScopeViolation, match="signed session rule"):
            adaptation.apply_alias({"goal": GOAL, "verb": verb, "phrase": "ship it"})
    assert adaptation.load(GOAL) == {}
    # A vocabulary file edited by hand to name another verb is refused loud.
    adaptation._write(GOAL, {"batch": ["ship it"]})
    with pytest.raises(ValueError, match="does not let it supply"):
        dw.load_config(_goal(tmp_path))
    # A pattern cannot declare an alias for a verb with nothing to count.
    bad = {"enabled": True, "patterns": [
        {"id": "x", "counts": "phrase_read_as", "verb": "batch", "threshold": 1}]}
    with pytest.raises(ValueError, match="an alias needs a verb"):
        dw.load_config(_goal(tmp_path, bad))


def test_a_learned_phrase_acts_only_while_the_rule_is_signed(env, tmp_path):
    adaptation._write(GOAL, {"confirm": ["ship it"]})
    env.signed = False
    unsigned = env.work()
    env.present(unsigned)
    assert unsigned.config.work_session.learned == ()
    assert unsigned.session_action("ship it") is None            # goes to the model
    env.signed = True
    signed = env.work()
    assert signed.session_action("ship it") == {"action": "confirm", "item_id": "m01"}
    # The signature does not depend on what has been learned.
    assert dw.session_rule_digest(signed.config) == dw.session_rule_digest(unsigned.config)
    assert "ship it" not in dw.session_rule(signed.config)["work_session"]["confirm"]
    # With the evidence in hand and the signature gone, applying is refused
    # for that reason and no other.
    assert env.says("ship it")[0] == "T0"
    for _ in range(3):
        env.present(env.work())
        env.says("send it")
    assert len(_questions()) == 1
    env.revoke_signature()
    assert env.work().session_action("ship it") is None        # and it stops acting
    with pytest.raises(andon.ScopeViolation, match="signed session rule"):
        adaptation.apply_alias({"goal": GOAL, "verb": "confirm", "phrase": "send it"})
    assert "send it" not in adaptation.load(GOAL).get("confirm", [])


def test_a_typed_yes_never_approves_an_alias_and_never_confirms_by_accident(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    [question] = _questions()
    env.present(env.work())                                      # an item is waiting
    before = len(env.work().log.run_records())
    for said in ("yes", "Yes!", "ok", "sure", "no", "not now"):
        tier, reply, match = env.says(said)
        assert tier == "T0" and "did nothing" in reply, said
        assert match["action"] == "hold"
    # Nothing was approved, nothing was decided, the question still waits.
    assert adaptation.load(GOAL) == {} and _questions() == [question]
    assert len(env.work().log.run_records()) == before
    assert env.work().pending()["item_id"] == "m04"
    # "confirm" is not an answer to a question: it applies to the item, as usual.
    tier, reply, _ = env.says("confirm")
    assert tier == "T0" and reply.startswith("Confirmed:")
    assert adaptation.load(GOAL) == {} and len(_questions()) == 1


def test_switched_off_is_todays_behavior_byte_for_byte(env, tmp_path):
    none = dw.load_config(_goal(tmp_path, None))
    off = dw.load_config(_goal(tmp_path, {**LEARNING, "enabled": False}))
    assert dw.session_rule(off) == dw.session_rule(none)
    assert json.dumps(dw.session_rule(off), sort_keys=True) == json.dumps(
        dw.session_rule(none), sort_keys=True)
    assert dw.session_rule_digest(off) == dw.session_rule_digest(none)
    assert "vocabulary" not in dw.session_rule(off)["work_session"]
    # A vocabulary file left behind is ignored, not honored.
    adaptation._write(GOAL, {"confirm": ["ship it"]})
    env.learning = {**LEARNING, "enabled": False}
    for _ in range(4):
        work = env.work()
        env.present(work)
        assert work.config.work_session.learned == ()
        assert work.session_action("ship it") is None
        assert work.session_action("alias yes #abc") is None
        assert work.session_action("forget ship it") is None
        tier, _, decided = env.says("ship it")
        assert tier == "T1" and work.last_observations == []     # nothing counted
    assert _questions() == [] and adaptation.status(env.work()) == []
    assert adaptation.load(GOAL) == {"confirm": ["ship it"]}
    with pytest.raises(DecisionRefused) as refused:
        env.work().session_step({"action": "alias_yes", "proposal": "x"}, env.prov(tier="T0"))
    assert refused.value.reason == "adaptation_off"


# ── the cycle ─────────────────────────────────────────────────────────


def test_the_whole_cycle_runs_on_the_existing_bus(env):
    trace = []

    def turn(said):
        work = env.work()
        if work.pending() is None:
            env.present(work)
        tier, reply, _ = env.says(said)
        trace.append((said, tier))
        return reply

    # Three times a model reads "ship it" as confirm: watched, then asked.
    turn("ship it")
    turn("Ship it!")
    assert _questions() == [] and reissue.take_cards("sess") == []
    [row] = [r for r in adaptation.status(env.work()) if r.get("phrase") == "ship it"]
    assert (row["count"], row["threshold"], row["state"]) == (2, 3, "counting")
    turn("ship it")
    [question] = _questions()
    action = question.payload["action"]
    assert question.payload["write_class"] == "vocabulary_alias"
    assert (action["verb"], action["phrase"]) == ("confirm", "ship it")
    assert len(action["turns"]) == 3 and all(action["turns"])      # the evidence
    # The question is its own card, with buttons that name the proposal.
    [card] = reissue.take_cards("sess")
    assert "“ship it”" in card["text"] and "3 times" in card["text"]
    assert [label for label, _ in card["buttons"]] == ["Yes", "Not now"]
    assert card["buttons"][0][1] == _press("yes")
    assert [r["state"] for r in adaptation.status(env.work())
            if r.get("phrase") == "ship it"] == ["proposed"]

    # Yes, by the button.
    reply = turn(_press("yes"))
    assert reply.startswith("Done. “ship it” now means confirm")
    assert adaptation.load(GOAL) == {"confirm": ["ship it"]} and _questions() == []
    assert [r["state"] for r in adaptation.status(env.work())
            if r.get("phrase") == "ship it"] == ["live"]

    # The fourth "ship it" confirms with no model.
    reply = turn("ship it")
    assert trace[-1] == ("ship it", "T0")
    assert reply.startswith("Confirmed: finance Money in or out.")   # read from the record

    # Taken back in conversation: the fifth goes to the model again.
    env.present(env.work())
    reply = turn("forget ship it")
    assert reply.startswith("Forgotten.") and adaptation.load(GOAL) == {}
    turn("ship it")
    assert [t for _, t in trace] == ["T1", "T1", "T1", "T0", "T0", "T0", "T1"]
    [row] = [r for r in adaptation.status(env.work()) if r.get("phrase") == "ship it"]
    assert (row["count"], row["state"]) == (1, "counting")       # counting again, from now
    # Jidoka, Kaizen and the panel all say the same number.
    last = env.work()
    last.log  # noqa: B018
    flags = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        flags += [json.loads(line) for line in path.read_text().splitlines()
                  if '"phrase_reading"' in line]
    newest = sorted((f for f in flags if f.get("flag_id") and f.get("summary")),
                    key=lambda f: f.get("ts") or f.get("timestamp") or "")[-1]
    assert "1 time(s)" in newest["summary"], newest["summary"]

    # Every step is on the chained Kaizen ledger, in existing event types.
    events = []
    for path in sorted(default_ledger_dir().glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert verify_ledger_chain(lines)["problems"] == [], path.name
        events += [json.loads(line) for line in lines if line.strip()]
    kinds = [e.get("event_type") or e.get("type") for e in events]
    for kind in ("jidoka_flag", "andon_event", "kaizen_answer", "kaizen_disposition",
                 "remedy_applied", "operator_applied"):
        assert kind in kinds, kind
    [applied] = [e for e in events if (e.get("event_type") or e.get("type")) == "remedy_applied"]
    assert applied["write_class"] == "vocabulary_alias"
    assert applied["alias"] == {"goal": GOAL, "verb": "confirm", "phrase": "ship it"}
    [forgot] = [e for e in events if e.get("action") == "alias_forgotten"]
    assert (forgot["verb"], forgot["phrase"], forgot["approval_surface"]) == (
        "confirm", "ship it", "chat")


def test_a_revision_resets_the_count(env):
    for _ in range(2):
        env.present(env.work())
        env.says("ship it")
    work = env.work()
    pattern = adaptation.alias_pattern(work.config, "confirm")
    assert len(adaptation.readings(work, pattern, "ship it")) == 2
    # The operator revises an item "ship it" decided: the model read it wrong once.
    work.rule_on("m02", decision="correct", corrected_output={"tag": "ops"},
                 provenance=env.prov(request="m02 was an outage, make it ops"))
    assert adaptation.readings(env.work(), pattern, "ship it") == []
    env.present(env.work())
    env.says("ship it")
    assert len(adaptation.readings(env.work(), pattern, "ship it")) == 1
    assert _questions() == []


def test_not_now_withdraws_the_question_and_counts_from_there(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    env.present(env.work())
    tier, reply, _ = env.says(_press("later"))
    assert tier == "T0" and reply.startswith("OK, not now.")
    assert _questions() == [] and adaptation.load(GOAL) == {}
    env.says("ship it")                                          # still read by the model
    [row] = [r for r in adaptation.status(env.work()) if r.get("phrase") == "ship it"]
    assert (row["count"], row["state"]) == (1, "counting")


def test_an_out_of_date_card_changes_nothing(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    press = _press("yes")
    # The ground moves: an item the question rested on is revised.
    env.work().rule_on("m01", decision="correct", corrected_output={"tag": "ops"},
                       provenance=env.prov(request="m01 should be ops"))
    env.present(env.work())
    with pytest.raises(DecisionRefused) as stale:
        env.says(press)
    assert stale.value.reason == "stale_card" and str(stale.value) == "This question expired."
    assert adaptation.load(GOAL) == {} and _questions() == []
    with pytest.raises(DecisionRefused) as gone:
        env.says(press)                                           # pressed again
    assert gone.value.reason == "stale_card"


def test_what_may_never_be_an_alias(env):
    cfg = env.work().config
    for phrase in ("ok", "yes", "no", "not that one", "hmm maybe", "ops", "pause",
                   "next", "tag the next message", "forget it",
                   "this one is fine and so is the last"):
        assert adaptation.refusal(cfg, "confirm", phrase) is not None, phrase
    assert adaptation.refusal(cfg, "confirm", "Ship it!") is None
    # A phrase that may not be an alias is never counted.
    env.present(env.work())
    work = env.work()
    work.decide(decision="confirm", provenance=env.prov(request="no objection here"))
    assert work.last_observations == []


def test_the_alias_is_an_in_scope_remedy_and_the_wall_agrees(env):
    answer = andon.Answer(kind=andon.KIND_REMEDY, summary="s",
                          write_class="vocabulary_alias",
                          write_targets=[str(adaptation.path(GOAL))])
    assert andon.classify_surface(answer) == andon.SURFACE_IN_SCOPE
    assert andon.channel_for(answer) == andon.CHANNEL_CHAT
    # Pointed at the Dock instead, it is a change to authority: refused in chat.
    from hermes_constants import get_hermes_home
    dock = andon.Answer(kind=andon.KIND_REMEDY, summary="s", write_class="vocabulary_alias",
                        write_targets=[str(get_hermes_home() / "dock" / "dock.yaml")])
    with pytest.raises(andon.ScopeViolation):
        andon.channel_for(dock)


def test_a_reset_starts_the_vocabulary_over_and_records_it(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    env.present(env.work())
    env.says(_press("yes"))
    for _ in range(3):
        env.says("lgtm") if env.work().pending() else None
        env.present(env.work())
    out = dw.reset_work(env.work().config, "again", surface="portal")
    assert out["vocabulary"]["aliases_forgotten"] == [["confirm", "ship it"]]
    assert adaptation.load(GOAL) == {} and _questions() == []


def test_patterns_are_declared_and_a_bad_declaration_is_refused(env, tmp_path):
    rows = adaptation.status(env.work())
    assert [(r["pattern"], r["count"], r["threshold"], r["state"]) for r in rows] == [
        ("phrase-for-confirm", 0, 3, "counting"), ("escalations", 0, 3, "counting")]
    assert rows[1]["proposes"] is False
    for bad, why in (
        ({"enabled": "yes"}, "true or false"),
        ({"enabled": True, "patterns": [{"id": "a", "counts": "vibes", "threshold": 2}]},
         "it can count"),
        ({"enabled": True, "patterns": [
            {"id": "a", "counts": "phrase_read_as", "verb": "confirm", "threshold": 0}]},
         "threshold of 1 or more"),
        ({"enabled": True, "patterns": [
            {"id": "a", "counts": "turns_failed_upward", "threshold": 2, "propose": True}]},
         "not yet proposed"),
        ({"enabled": True, "patterns": [
            {"id": "a", "counts": "phrase_read_as", "verb": "confirm", "threshold": 2,
             "becomes": "routing_keg"}]}, "can only become"),
    ):
        with pytest.raises(ValueError, match=why):
            dw.load_config(_goal(tmp_path, bad))
    with pytest.raises(ValueError, match="needs an enabled work_session"):
        dw.load_config(_goal(tmp_path, LEARNING, {**SESSION, "enabled": False}))


# ── a press never goes to a model ─────────────────────────────────────


def test_a_press_outside_the_session_is_answered_against_its_proposal(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    short = _press("yes").split("#")[1]
    # The session is paused; the press still names its proposal.
    reply = adaptation.answer_press("yes", short, env.prov(tier="T0"))
    assert reply.startswith("Done. “ship it” now means confirm")
    assert adaptation.load(GOAL) == {"confirm": ["ship it"]}
    # Pressed again, or a card nobody knows: one line, nothing changes.
    assert adaptation.answer_press("yes", short, env.prov(tier="T0")) == "This question expired."
    assert adaptation.answer_press("later", "nope", env.prov(tier="T0")) == "This question expired."


def test_a_press_with_adaptation_switched_off_expires_in_one_line(env):
    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    short = _press("yes").split("#")[1]
    env.learning = {**LEARNING, "enabled": False}
    env.work()
    assert adaptation.answer_press("yes", short, env.prov(tier="T0")) == "This question expired."
    assert adaptation.load(GOAL) == {}


def test_the_dispatcher_never_hands_a_press_to_a_model(env):
    from grove.dispatcher import Dispatcher

    for _ in range(3):
        env.present(env.work())
        env.says("ship it")
    press = _press("yes")
    written, said = [], []
    d = SimpleNamespace(
        _current_turn_id="other#3", _current_turn_session_step=None, session_id="other",
        turn_provenance=lambda agent: env.prov(session_id="other", tier="T0"),
        _finalize_previous_turn_pending=lambda turn_id: None,
        _write_intent_record=lambda agent, **kw: written.append(kw),
        _persist_t0_turn=lambda user, reply: said.append((user, reply)),
        _t0_result_dict=lambda agent, text: {"final_response": text, "tier": "T0",
                                             "api_calls": 0},
    )
    for name in ("_press_intercept", "_session_result_dict"):
        setattr(d, name, getattr(Dispatcher, name).__get__(d))
    assert d._press_intercept(None, "what's the weather", None) is None
    # An item card pressed outside its session: refused in one line.
    item = d._press_intercept(None, "confirm #m03", None)
    assert item["final_response"] == "Resume the session to answer."
    assert (item["tier"], item["model"], item["api_calls"]) == ("T0", "session_rule", 0)
    assert adaptation.load(GOAL) == {}
    # A question card: carried out against the proposal it names.
    done = d._press_intercept(None, press, None)
    assert done["final_response"].startswith("Done. “ship it” now means confirm")
    assert (done["tier"], done["api_calls"]) == ("T0", 0)
    assert d._current_turn_phrase_match["action"] == "alias_press"
    assert [w["tier_override"] for w in written] == ["T0", "T0"]
    gone = d._press_intercept(None, press, None)
    assert gone["final_response"] == "This question expired."
