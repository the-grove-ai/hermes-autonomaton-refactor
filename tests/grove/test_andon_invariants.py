"""The andon handler's invariants — THE DEPLOY GATE.

``scripts/deploy.sh`` runs this file on its own and refuses to deploy when
any test here fails. Each test name states the invariant it guards, and each
assertion message names the andon id involved, so a refusal reads as "which
rule, on which event".

  Invariant 1 — every andon event closes with exactly one Kaizen answer.
  Invariant 2 — nothing accepted in chat can write a scope-defining surface.
  Invariant 3 — no detector imports Kaizen.
  Recursion   — Kaizen's own failure goes back through the same handler.

The five cases are the events that used to end with a bare refusal. Fixtures
are a MESSAGE-TAGGING goal: the handler is generic and so is its gate.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import grove.grants as grants_mod
import grove.pattern_cache as pc
from grove import andon, keg
from grove import decision_work as dw
from grove import flywheel_cli as fc
from grove import turn_provenance
from grove.decision_work import DecisionRefused, DecisionWork
from grove.eval.proposal_queue import read_all
from grove.kaizen import answers, standard_work
from grove.kaizen_ledger import default_ledger_dir, verify_ledger_chain
from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE, STATUS_HALTED

GOAL = "message-triage"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)
    # A per-test grant store IN the test's own directory. The default store
    # lives under the real home directory, which the suite does not redirect:
    # until 2026-10-06 these tests wrote their grants into the operator's own
    # ~/.grove/grants.yaml.
    monkeypatch.setattr(grants_mod, "_store", grants_mod.GrantStore(tmp_path / "grants.yaml"))
    (tmp_path / "queue").mkdir()
    (tmp_path / "channels.csv").write_text(
        "Channel,Default Tag\nbilling,finance\noutage,ops\npress,comms\n", encoding="utf-8")
    (tmp_path / "tags.csv").write_text(
        "Tag\nfinance\nops\ncomms\nescalate\nother\n", encoding="utf-8")
    goal = SimpleNamespace(
        id=GOAL, root=tmp_path, keywords=("message",), resolved_sources=lambda: [],
        extra={"decision_work": {
            "tool": "tag_message", "queue": "queue", "isolation": "sources_only",
            "on_unclean": "open_clean_session",
            "inputs": {"channel": {"data_type": "string", "required": True},
                       "subject": {"data_type": "string", "required": False}},
            "outputs": {"tag": {"data_type": "string"}},
            "reference_table": {"path": "channels.csv", "key_column": "Channel",
                                "value_column": "Default Tag", "key_input": "channel",
                                "value_output": "tag"},
            "output_domains": [{"output": "tag", "path": "tags.csv", "column": "Tag"}],
            "evidence": {"threshold": 3},
            "keg": {"name": "Message tagging", "request": "tag the next message",
                    "revision_tiers": ["T1", "T2", "T3"]},
        }},
    )
    cfg = dw.load_config(goal)
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)
    token = turn_provenance.set_current(None)

    class Env:
        store = PatternCacheStore(db)
        work = DecisionWork(cfg)
        n = 0

        def add(self, channel, subject=""):
            self.n += 1
            (tmp_path / "queue" / f"m{self.n:02d}.txt").write_text(
                json.dumps({"channel": channel, "subject": subject}))

        def prov(self, **over):
            base = {"session_id": "sess", "turn_id": f"sess#{self.n}",
                    "turn_uid": f"u{self.n}", "tier": "T1", "model": "m",
                    "request": "tag the next message",
                    "cellar_hits": 0, "sections": [], "tools_yielded": [],
                    "isolation_goal": GOAL}
            base.update(over)
            turn_provenance.set_current(base)
            return base

        def inputs(self):
            return json.loads(self.work.next_item().read_text())

        def code(self, tag, decision="confirm", corrected=None, keg_served=False):
            item = self.work.next_item()
            if keg_served:
                [entry] = [e for e in self.store.all() if e.status == STATUS_ACTIVE]
                spec = keg.keg_of(entry)
                self.work.apply_keg(
                    spec, item_id=item.stem, inputs=self.inputs(),
                    keg_ref={"name": spec["name"], "version": spec["version"],
                             "pattern_id": entry.pattern_id},
                    provenance=self.prov(tier="T0"))
            else:
                self.work.record(item_id=item.stem, inputs=self.inputs(),
                                 output={"tag": tag}, reasoning="", provenance=self.prov())
            self.work.decide(
                decision=decision,
                corrected_output={"tag": corrected} if corrected else None,
                provenance=self.prov())
            return self.work.last_observations

        def earn_v1(self):
            for channel, tag in (("billing", "finance"), ("outage", "ops"), ("press", "comms")):
                self.add(channel)
                self.code(tag)
            [proposal] = [p for p in read_all() if (p.payload or {}).get("keg")]
            assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0

        def events(self):
            out = []
            for f in sorted(default_ledger_dir().glob("*.jsonl")):
                out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            return out

    yield Env()
    turn_provenance.reset(token)


def _drafts(*conditions):
    drafts = list(conditions)

    def call(prompt, *, system=None, tool=None, tier=None, max_tokens=0):
        return {"condition": drafts.pop(0) if drafts else "", "rationale": "r"}
    return call


def _with_drafts(monkeypatch, *conditions):
    real = standard_work.draft_condition
    call = _drafts(*conditions)
    monkeypatch.setattr(
        standard_work, "draft_condition",
        lambda *a, **kw: real(*a, **{**kw, "call": call}))


def assert_every_andon_closed_exactly_once(events):
    """Invariant 1, as a count over the ledger."""
    raised = [e["andon_id"] for e in events if e["event_type"] == "andon_event"]
    closed = [a for e in events if e["event_type"] == "kaizen_answer" for a in e["closes"]]
    assert raised, "no andon event was raised — the scenario exercised nothing"
    for andon_id in raised:
        assert closed.count(andon_id) == 1, (
            f"andon {andon_id} has {closed.count(andon_id)} closing answers, expected 1")
    assert sorted(closed) == sorted(raised), "a closing answer names an unknown andon"
    for e in events:
        if e["event_type"] == "kaizen_answer":
            assert e["kind"] in andon.ANSWER_KINDS, f"andon {e['andon_id']}: bad kind"
            assert e["artifact"], f"andon {e['andon_id']} closed with no artifact"
            assert len(e["source_chain"]) >= 2, f"andon {e['andon_id']}: no source chain"
    return raised


# ── the five cases that used to dead-end ──────────────────────────────


def test_case_1_unclean_session_gets_a_proposed_session_rule_then_a_clean_session(env):
    env.add("billing")
    # The rule is unsigned: the refusal carries a proposal to sign it.
    with pytest.raises(DecisionRefused) as refused:
        env.work.check_turn(env.prov(isolation_goal=None))
    answer = refused.value.answer
    assert (answer["kind"], answer["channel"]) == (andon.KIND_STANDARD_WORK, andon.CHANNEL_PORTAL)
    [proposal] = read_all()
    assert proposal.type == "session_rule" and proposal.payload["rule"]["on_unclean"] == (
        "open_clean_session")
    # Signed in the portal path: a revocable standing grant on exactly that rule.
    assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0
    grant = dw.session_rule_grant(env.work.config)
    assert grant is not None and grant.scope == GOAL
    # Now the same event is answered by the remedy the standing rule authorizes
    # — and the operator is never told to type /new.
    with pytest.raises(DecisionRefused) as again:
        env.work.check_turn(env.prov(isolation_goal=None))
    remedy = again.value.answer
    assert (remedy["kind"], remedy["write_class"], remedy["channel"]) == (
        andon.KIND_REMEDY, "session_reset", andon.CHANNEL_CHAT)
    assert remedy["detail"]["standing_grant"] == grant.id
    # Live 2026-10-06: the action worked, but the operator was still told to
    # type /new. No message on this path may tell them to.
    for refusal in (refused.value, again.value):
        assert "/new" not in str(refusal), "the operator was told to start a session by hand"
        assert "/new" not in (refusal.answer or {}).get("summary", "")
    # Authorized by the signed rule, so it is carried out without a further
    # accept: the re-issue is armed for the gateway and recorded on the bus.
    from grove import reissue
    armed = reissue.take("sess")
    assert armed["clean_session"] is True and armed["request"] == "tag the next message"
    assert armed["authorized"] == grant.id and reissue.take("sess") is None   # once only
    [applied] = [e for e in env.events() if e["event_type"] == "remedy_applied"]
    assert (applied["write_class"], applied["standing_grant"], applied["channel"]) == (
        "session_reset", grant.id, "standing_rule")
    # Revoked, the rule is out of force at once.
    assert grants_mod.get_grant_store().revoke_grant(grant.id) is True
    assert dw.session_rule_grant(env.work.config) is None
    assert_every_andon_closed_exactly_once(env.events())


def test_a_request_reissued_into_a_clean_session_is_never_reissued_again(env):
    # Live, 2026-10-06: "keep going" was re-issued into a clean session, where
    # it still did not open the work — so it was re-issued into another, and
    # another, every few seconds. One hop, then stop and say so.
    from grove import reissue

    env.add("billing")
    # Sign the session rule, as in case 1.
    env.work.abnormal("session_not_isolated", "x", env.prov(isolation_goal=None))
    [proposal] = read_all()
    assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0
    # First time: the signed rule opens a clean session and re-issues.
    first = env.work.abnormal(
        "session_not_isolated", "not clean", env.prov(isolation_goal=None, request="keep going"))
    assert first.answer["write_class"] == "session_reset"
    armed = reissue.take("sess")
    assert (armed["clean_session"], armed["request"]) == (True, "keep going")
    # The re-issued request still does not open the work: no second hop.
    again = env.work.abnormal(
        "session_not_isolated", "not clean",
        env.prov(isolation_goal=None, request="keep going", reissued_clean=True))
    assert again.answer["kind"] == andon.KIND_WATCH
    assert again.answer["summary"].startswith(
        "That request does not start this work, so it was not run again.")
    assert reissue.take("sess") is None                       # nothing armed: the chain ends
    assert_every_andon_closed_exactly_once(env.events())


def test_case_2_invalid_output_fails_upward_automatically_one_tier_at_a_time(env):
    # 2026-10-06: was a chat-accepted remedy. Moving one turn one tier up
    # grants no authority, so the ladder rule carries it out with no accept.
    from grove import reissue

    env.add("billing")
    bad = dict(item_id="m01", inputs=env.inputs(), output={"tag": "made-up"}, reasoning="")
    with pytest.raises(DecisionRefused) as refused:
        env.work.record(**bad, provenance=env.prov())
    answer = refused.value.answer
    assert (answer["kind"], answer["write_class"]) == (andon.KIND_REMEDY, "tier_escalation")
    assert answer["detail"]["authorized"] == andon.AUTHORIZED_LADDER_RULE
    assert answer["summary"] == answers.ESCALATING_MESSAGE      # the one line the operator sees
    assert read_all() == []                                     # nothing waits for an accept
    [applied] = [e for e in env.events() if e["event_type"] == "remedy_applied"]
    assert (applied["channel"], applied["summary"], applied["andon_id"]) == (
        "ladder_rule", "escalated T1 → T2 (ladder rule)", refused.value.andon_id)
    # The same turn may not carry on: the request now belongs to the next tier.
    with pytest.raises(DecisionRefused) as again:
        env.work.record(**{**bad, "output": {"tag": "finance"}}, provenance=env.prov())
    assert again.value.reason == "attempt_stopped" and env.work.pending() is None
    armed = reissue.take("sess")
    assert (armed["tier"], armed["clean_session"], armed["request"]) == (
        "T2", False, "tag the next message")
    assert [(a["tier"], a["reason"]) for a in armed["attempts"]] == [
        ("T1", "output_not_in_domain")]
    # One attempt per tier: T2 fails the same way and goes to T3, trace intact.
    with pytest.raises(DecisionRefused):
        env.work.record(**bad, provenance=env.prov(
            tier="T2", turn_uid="u-t2", attempts=armed["attempts"]))
    armed = reissue.take("sess")
    assert armed["tier"] == "T3" and [a["tier"] for a in armed["attempts"]] == ["T1", "T2"]
    # The top of the ladder: stop. A watch, nothing armed, no further retry.
    with pytest.raises(DecisionRefused) as top:
        env.work.record(**bad, provenance=env.prov(
            tier="T3", turn_uid="u-t3", attempts=armed["attempts"]))
    assert top.value.answer["kind"] == andon.KIND_WATCH
    assert top.value.answer["summary"].startswith(answers.NOT_COMPLETED_MESSAGE)
    assert reissue.take("sess") is None and reissue.stopped("sess", "u-t3")
    with pytest.raises(DecisionRefused) as after:
        env.work.record(**{**bad, "output": {"tag": "finance"}},
                        provenance=env.prov(tier="T3", turn_uid="u-t3"))
    assert after.value.reason == "attempt_stopped"
    assert len([e for e in env.events() if e["event_type"] == "remedy_applied"]) == 2
    assert_every_andon_closed_exactly_once(env.events())


def test_case_6_a_reply_that_claims_the_work_without_the_tool_is_refused(env):
    from grove import reissue

    env.add("billing")
    asked = env.prov()                                  # "tag the next message", no tool call
    refused = env.work.unanswered("m01 is tagged finance.", asked)
    assert refused is not None and refused.reason == "reply_without_tool"
    assert refused.answer["detail"]["authorized"] == andon.AUTHORIZED_LADDER_RULE
    assert reissue.take("sess")["tier"] == "T2"
    # In order: the tool was called; or the operator asked something else.
    assert env.work.unanswered("x", env.prov(tools_yielded=["tag_message"])) is None
    assert env.work.unanswered("x", env.prov(request="why that tag?")) is None
    assert env.work.unanswered("x", env.prov(isolation_goal=None)) is None
    assert_every_andon_closed_exactly_once(env.events())


def test_the_dispatcher_withholds_the_reply_and_says_one_line(env):
    from grove import reissue
    from grove.dispatcher import Dispatcher

    env.add("billing")

    def stand_in(**prov):
        return SimpleNamespace(
            _current_turn_isolation=GOAL, _current_turn_t0_pattern=None,
            _current_turn_id="sess#1", _current_turn_withheld=None,
            turn_provenance=lambda agent: env.prov(**prov))

    # Asked for the work, answered without the tool: withheld, retried one tier up.
    d = stand_in()
    assert Dispatcher.review_final_reply(d, None, "m01 is tagged finance.") == (
        answers.ESCALATING_MESSAGE)
    assert d._current_turn_withheld["kind"] == "reply_without_tool"
    assert "T2" in d._current_turn_withheld["summary"]
    assert reissue.take("sess")["tier"] == "T2"
    # At the top tier: withheld, and the operator is told plainly.
    d = stand_in(tier="T3", turn_uid="u-top")
    assert Dispatcher.review_final_reply(d, None, "m01 is tagged finance.") == (
        answers.NOT_COMPLETED_MESSAGE)
    assert reissue.take("sess") is None
    # A turn that did the work, and a session with no goal, are left alone.
    d = stand_in(tools_yielded=["tag_message"], turn_uid="u-ok")
    assert Dispatcher.review_final_reply(d, None, "Tagged.") is None
    assert d._current_turn_withheld is None
    d = stand_in(turn_uid="u-open")
    d._current_turn_isolation = None
    assert Dispatcher.review_final_reply(d, None, "Hello.") is None
    assert_every_andon_closed_exactly_once(env.events())


def test_the_ladder_rule_authorizes_a_tier_step_and_nothing_else(env, monkeypatch):
    env.add("billing")
    smuggled = andon.Answer(
        kind=andon.KIND_REMEDY, summary="x", write_class="set_aside_item",
        detail={"authorized": andon.AUTHORIZED_LADDER_RULE,
                "reissue": {"session_id": "sess", "tier": "T2"}})
    with pytest.raises(andon.ScopeViolation, match="ladder rule"):
        andon.channel_for(smuggled)
    monkeypatch.setitem(answers._ANSWERS, "turn_check", lambda a, c: smuggled)
    refused = env.work.abnormal("output_not_in_domain", "x", env.prov())
    from grove import reissue
    assert reissue.take("sess") is None                  # nothing was carried out
    assert refused.answer["kind"] in andon.ANSWER_KINDS  # closed through Kaizen's failure path
    assert not [e for e in env.events() if e["event_type"] == "remedy_applied"]
    assert_every_andon_closed_exactly_once(env.events())


def test_case_3_unreadable_item_is_set_aside_on_accept(env):
    env.add("billing")
    env.add("outage")
    refused = env.work.abnormal(
        "item_unreadable", "The next item could not be read.",
        {**env.prov(), "item_id": "m01"})
    assert refused.answer["write_class"] == "set_aside_item"
    [remedy] = read_all()
    assert fc.cli_approve(remedy.proposal_id.split(":")[-1][:12]) == 0   # the chat accept
    assert env.work.next_item().stem == "m02"                # the queue moved on
    aside = env.work.set_aside_items()["m01"]
    assert aside["andon_id"] == refused.andon_id             # nothing dropped, all on record
    applied = [e for e in env.events() if e["event_type"] == "remedy_applied"]
    assert applied and applied[0]["write_class"] == "set_aside_item"
    assert_every_andon_closed_exactly_once(env.events())


def test_case_4_correction_of_a_model_answer_is_watched_then_promoted(env):
    env.earn_v1()
    # A channel the keg does not cover, answered by the interpreter and
    # corrected the same way each time.
    watches = []
    for n in range(1, 4):
        env.add("social", f"post {n}")
        [event] = env.code("other", decision="correct", corrected="comms")
        watches.append(event)
        proposals = [p for p in read_all() if (p.payload or {}).get("keg")]
        if n < 3:
            # Single corrections change nothing, and are remembered.
            assert event["answer"]["kind"] == andon.KIND_WATCH, event["andon_id"]
            assert f"seen {n} of 3" in event["answer"]["summary"]
            assert proposals == []
    # At the goal's own evidence threshold the watch promotes to a proposal:
    # in the portal, with a backtest. Nothing changed silently in between.
    final = watches[-1]["answer"]
    assert final["kind"] == andon.KIND_STANDARD_WORK, watches[-1]["andon_id"]
    assert final["detail"]["promoted_from"] == watches[0]["answer"]["artifact"]
    [proposal] = [p for p in read_all() if (p.payload or {}).get("keg")]
    k = proposal.payload["keg"]
    assert k["version"] == 2 and k["conditions"][0] == {
        "if": "channel == 'social'", "then": {"tag": "comms"}}
    assert_every_andon_closed_exactly_once(env.events())


def test_case_5_kaizen_draft_failure_fails_upward_then_asks_the_operator(env, monkeypatch):
    env.earn_v1()
    asked = []
    real = standard_work.draft_condition

    def call(prompt, *, system=None, tool=None, tier=None, max_tokens=0):
        asked.append(tier)
        return {"condition": "not a condition", "rationale": "r"}

    monkeypatch.setattr(
        standard_work, "draft_condition",
        lambda *a, **kw: real(*a, **{**kw, "call": call}))
    env.add("billing", "refund")
    [event] = env.code(None, decision="correct", corrected="escalate", keg_served=True)
    assert asked == ["T1", "T2", "T3"]                       # failed upward, to the top
    assert event["halted"] and env.store.get(event["halted"][0]).status == STATUS_HALTED
    assert event["answer"]["kind"] == andon.KIND_STANDARD_WORK
    [request] = [p for p in read_all() if p.type == "kaizen_request"]
    assert event["answer"]["artifact"] == request.proposal_id
    assert_every_andon_closed_exactly_once(env.events())


# ── invariant 1 across the whole loop ─────────────────────────────────


def test_invariant_1_every_andon_event_closes_with_exactly_one_answer(env, monkeypatch):
    _with_drafts(monkeypatch, "channel == 'billing' AND subject CONTAINS 'refund'")
    env.earn_v1()                                             # tier-down → proposal
    env.add("billing", "refund")
    env.code(None, decision="correct", corrected="escalate", keg_served=True)   # miss → v2
    env.add("outage")
    env.code("finance", decision="correct", corrected="other")                  # watch
    with pytest.raises(DecisionRefused):
        env.work.check_turn(env.prov(isolation_goal=None))                      # session rule
    raised = assert_every_andon_closed_exactly_once(env.events())
    assert len(raised) == 4
    kinds = {e["kind"] for e in env.events() if e["event_type"] == "kaizen_answer"}
    assert kinds == {andon.KIND_STANDARD_WORK, andon.KIND_WATCH}


def test_invariant_1_the_ledger_that_holds_the_closes_is_hash_chained(env):
    env.earn_v1()
    for path in default_ledger_dir().glob("*.jsonl"):
        lines = path.read_text().splitlines()
        report = verify_ledger_chain(lines)
        assert report["problems"] == [] and report["unchained"] == 0, path.name
        if len(lines) > 2:
            tampered = json.loads(lines[1])
            tampered["summary"] = "edited after the fact"
            broken = verify_ledger_chain([lines[0], json.dumps(tampered)] + lines[2:])
            assert broken["problems"], f"an edit to {path.name} went undetected"
            assert verify_ledger_chain([lines[0]] + lines[2:])["problems"], (
                f"a removal from {path.name} went undetected")


# ── invariant 2: the chat-accept path ─────────────────────────────────


def test_invariant_2_no_chat_accepted_answer_touches_a_scope_defining_surface(env, tmp_path):
    from grove.andon import Answer, ScopeViolation
    from hermes_constants import get_hermes_home

    dock = str(Path(get_hermes_home()) / "dock" / "dock.yaml")
    in_scope = Answer(kind=andon.KIND_REMEDY, summary="s", write_class="set_aside_item")
    assert andon.channel_for(in_scope) == andon.CHANNEL_CHAT
    andon.assert_chat_acceptable(in_scope)
    for bad in (
        Answer(kind=andon.KIND_REMEDY, summary="s", write_class="set_aside_item",
               write_targets=[dock]),                    # reaches a scope-defining file
        Answer(kind=andon.KIND_REMEDY, summary="s", write_class="edit_the_dock"),  # undeclared
        Answer(kind=andon.KIND_STANDARD_WORK, summary="s"),
        Answer(kind=andon.KIND_WATCH, summary="s"),
    ):
        with pytest.raises(ScopeViolation):
            andon.assert_chat_acceptable(bad)
    with pytest.raises(ScopeViolation):
        andon.channel_for(Answer(kind=andon.KIND_REMEDY, summary="s",
                                 write_class="set_aside_item", write_targets=[dock]))
    # Standard-work answers only ever travel on the portal channel.
    assert andon.channel_for(Answer(kind=andon.KIND_STANDARD_WORK, summary="s")) == (
        andon.CHANNEL_PORTAL)


def test_invariant_2_applying_a_remedy_rechecks_its_surface(env):
    from grove.andon import ScopeViolation
    from grove.eval.proposal_queue import RoutingProposal
    from hermes_constants import get_hermes_home

    smuggled = RoutingProposal(
        proposal_id="sha256:x", type="remedy", evidence=("t",), eval_hash="h",
        created_at="2026-01-01T00:00:00+00:00",
        payload={"write_class": "set_aside_item", "action": {"goal": GOAL, "item_id": "m01"},
                 "write_targets": [str(Path(get_hermes_home()) / "routing.authority.yaml")]},
    )
    with pytest.raises(ScopeViolation):
        fc._apply_remedy(smuggled)


def test_invariant_2_standard_work_cannot_be_approved_from_chat(env):
    from grove.api import actions
    from tools.flywheel_review_tool import approve_proposal

    env.add("billing")
    with pytest.raises(DecisionRefused):
        env.work.check_turn(env.prov(isolation_goal=None))
    [rule] = read_all()
    out = json.loads(approve_proposal(rule.proposal_id.split(":")[-1][:12]))
    assert out["success"] is False and "Operator Portal" in out["error"]
    assert dw.session_rule_grant(env.work.config) is None     # nothing was signed
    assert actions._is_scope_defining_proposal(rule) is True  # portal gates it as such


# ── invariant 3: detectors cannot reach Kaizen ────────────────────────


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"


def test_invariant_3_no_detector_imports_kaizen():
    detectors = sorted((REPO / "grove" / "detectors").glob("*.py"))
    assert len(detectors) >= 2, "the detectors package is missing"
    # The decision engine raises the turn-check andon; it is held to the same rule.
    for path in detectors + [REPO / "grove" / "decision_work.py"]:
        for name in _imports(path):
            assert not (name == "grove.kaizen" or name.startswith("grove.kaizen.")), (
                f"{path.relative_to(REPO)} imports {name}: a detector raises an "
                f"andon and returns; it never calls Kaizen")


def test_invariant_3_kaizen_is_reached_only_through_the_handler():
    # The one place the handler hands an event to Kaizen.
    handler = (REPO / "grove" / "andon.py").read_text(encoding="utf-8")
    assert handler.count("answers.answer(andon, context)") == 1
    for path in sorted((REPO / "grove" / "detectors").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if path.name != "__init__.py":
            assert "raise_andon(" in src, f"{path.name} never raises an andon"


# ── recursion: the handler is invariant over Kaizen's own failures ────


def test_recursion_kaizens_failure_goes_through_the_same_handler(env, monkeypatch):
    calls = []
    real_raise = andon.raise_andon

    def spy(kind, **kw):
        calls.append(kw["detector"])
        return real_raise(kind, **kw)

    monkeypatch.setattr(andon, "raise_andon", spy)
    real_answer = answers.answer

    def failing(event, context=None):
        if event["detector"] == "probe":
            raise andon.KaizenCouldNotAnswer("nothing valid", {"reason": "probe_failed"})
        return real_answer(event, context)

    monkeypatch.setattr(answers, "answer", failing)
    event = andon.raise_andon(
        keg.FLAG_ANOMALY, detector="probe", goal=GOAL, summary="a detector fired",
        evidence=[{"turn_uid": "u1"}])
    # The failure was raised by the SAME function, as the next andon event.
    assert calls == ["probe", "kaizen_failure"], calls
    assert event["escalated_to"] and event["answer"]["kind"] in andon.ANSWER_KINDS
    cords = [e for e in env.events() if e["event_type"] == "andon_event"]
    assert cords[1]["originating"] == [event["andon_id"]]
    assert_every_andon_closed_exactly_once(env.events())


def test_recursion_always_terminates_in_one_of_the_three_kinds(env, monkeypatch):
    def always_fails(event, context=None):
        raise andon.KaizenCouldNotAnswer("still nothing", {"reason": "always"})

    monkeypatch.setattr(answers, "answer", always_fails)
    event = andon.raise_andon(
        keg.FLAG_ANOMALY, detector="probe", goal=GOAL, summary="a detector fired")
    assert event["answer"]["kind"] == andon.KIND_WATCH       # built without a model
    raised = assert_every_andon_closed_exactly_once(env.events())
    assert len(raised) == andon._MAX_DEPTH + 1


def test_recursion_a_scope_violating_remedy_is_itself_an_andon_event(env, monkeypatch):
    from hermes_constants import get_hermes_home

    def overreaching(event, context=None):
        if event["detector"] == "probe":
            return andon.Answer(
                kind=andon.KIND_REMEDY, summary="edit the dock", write_class="set_aside_item",
                write_targets=[str(Path(get_hermes_home()) / "dock" / "dock.yaml")])
        return answers.watch_unresolved(event, context)

    monkeypatch.setattr(answers, "answer", overreaching)
    event = andon.raise_andon(
        keg.FLAG_ANOMALY, detector="probe", goal=GOAL, summary="a detector fired")
    # Never offered on the chat channel: refused, and answered as a failure.
    assert event["escalated_to"] and event["answer"]["kind"] == andon.KIND_WATCH
    closes = [e for e in env.events() if e["event_type"] == "kaizen_answer"]
    assert all(c["channel"] != andon.CHANNEL_CHAT for c in closes)
    assert_every_andon_closed_exactly_once(env.events())


# ── carrying out a re-issue ───────────────────────────────────────────


def test_reissue_is_consumed_once_and_pins_one_tier_once():
    from grove import reissue

    assert [reissue.next_tier(t) for t in ("T1", "T2", "T3", "T0", None)] == [
        "T2", "T3", None, None, None]
    reissue.arm({"clean_session": True, "request": "do it"}, session_id="chat-1")
    assert reissue.take("chat-1")["request"] == "do it" and reissue.take("chat-1") is None
    reissue.arm_tier("chat-1", "T2")
    assert reissue.take_tier("chat-1") == "T2" and reissue.take_tier("chat-1") is None
    with pytest.raises(ValueError):
        reissue.arm({"clean_session": True})          # no session to belong to


async def test_gateway_carries_out_an_armed_reissue_after_the_turn():
    from gateway.run import GatewayRunner
    from grove import reissue

    calls = []

    class _Store:
        session_id = "chat-old"

        def get_or_create_session(self, source):
            return SimpleNamespace(session_id=self.session_id)

    class _Gateway:
        session_store = _Store()
        adapters = {"telegram": object()}

        async def _handle_reset_command(self, event):
            calls.append("reset")
            self.session_store.session_id = "chat-new"

        def _enqueue_fifo(self, key, event, adapter):
            calls.append(("enqueue", key, event.text))

    gateway = _Gateway()
    source = SimpleNamespace(platform="telegram")
    event = SimpleNamespace(text="approve", source=source)
    # Nothing armed: nothing happens.
    await GatewayRunner._post_turn_reissue(gateway, event, source, "key")
    assert calls == []
    reissue.arm({"clean_session": True, "tier": "T2", "request": "tag the next message"},
                session_id="chat-old")
    await GatewayRunner._post_turn_reissue(gateway, event, source, "key")
    # Reset first (it clears the chat's queue), then the ORIGINAL request —
    # not the operator's "approve" — goes back on the queue, pinned one tier up
    # in the new session.
    assert calls == ["reset", ("enqueue", "key", "tag the next message")]
    assert reissue.take_tier("chat-new") == "T2"


# ── feedback never dead-ends (live 2026-10-06) ────────────────────────
#
# The operator sent a v2 revision back with feedback. The redraft lost the
# corrected case, every tier failed, and nothing was proposed: the keg stayed
# halted with no answer on the table. Feedback now goes through the handler.


def _halted_with_v2(env, monkeypatch, *drafts):
    env.earn_v1()
    _with_drafts(monkeypatch, *drafts)
    env.add("billing", "Refund course fee")
    [event] = env.code(None, decision="correct", corrected="escalate", keg_served=True)
    [v2] = [p for p in read_all() if (p.payload or {}).get("keg")]
    return event, v2


def test_feedback_on_a_revision_is_redrafted_with_the_miss_and_the_feedback(env, monkeypatch):
    first = "channel == 'billing' AND subject CONTAINS 'course'"
    wider = ("channel == 'billing' AND subject CONTAINS 'course' OR "
             "channel == 'billing' AND subject CONTAINS 'training'")
    event, v2 = _halted_with_v2(env, monkeypatch, first, wider)
    # The proposal carries the corrected case, so a redraft cannot lose it.
    assert v2.payload["keg"]["miss"]["corrected"] == {"tag": "escalate"}
    assert fc.cli_reject(v2.proposal_id.split(":")[-1][:12],
                         reason="also training") == 0
    [redraft] = [p for p in read_all() if (p.payload or {}).get("keg")]
    k = redraft.payload["keg"]
    assert k["version"] == 2 and k["feedback"] == ["also training"]
    assert k["conditions"][0] == {"if": wider, "defer": True}
    assert k["miss"]["item_id"] == v2.payload["keg"]["miss"]["item_id"]
    assert env.store.get(event["halted"][0]).status == STATUS_HALTED   # still stopped
    assert_every_andon_closed_exactly_once(env.events())


def test_feedback_that_no_tier_can_satisfy_asks_the_operator_never_silence(env, monkeypatch):
    event, v2 = _halted_with_v2(
        env, monkeypatch, "channel == 'billing' AND subject CONTAINS 'course'")
    # Every later draft is empty: all three tiers fail on the redraft.
    assert fc.cli_reject(v2.proposal_id.split(":")[-1][:12], reason="also training") == 0
    pending = read_all()
    assert [p.type for p in pending] == ["kaizen_request"], (
        "feedback left the operator with nothing on the table")
    assert pending[0].payload["miss"]["corrected"] == {"tag": "escalate"}
    assert len(pending[0].payload["attempts"]) == 3
    assert_every_andon_closed_exactly_once(env.events())


def test_drafting_falls_back_to_text_where_a_tier_refuses_a_forced_tool():
    calls = []

    def call(prompt, *, system=None, tool=None, tier=None, max_tokens=0):
        calls.append((tier, tool is not None, max_tokens))
        if tool is not None:
            raise RuntimeError("Error code: 400 - tool_choice: type \"tool\" not allowed")
        return "Here is the condition:\n`channel == 'billing'`"

    assert standard_work._ask(call, "prompt", "T3") == "channel == 'billing'"
    assert calls == [("T3", True, 2000), ("T3", False, 2000)]   # room to think, both times

    def broken(prompt, **kw):
        raise ConnectionError("network down")

    with pytest.raises(ConnectionError):      # any other failure is that tier's failure
        standard_work._ask(broken, "prompt", "T2")


def test_t0_record_names_what_answered_not_a_model():
    import inspect
    from grove.dispatcher import Dispatcher
    src = inspect.getsource(Dispatcher._write_intent_record)
    assert 'if tier_override == "T0":' in src and 'model_used = "pattern_cache"' in src


# ── a rule earned from the operator's own confirmations ───────────────
# For a key the reference table does not list: enough model-decided items,
# all confirmed with the same answer, none revised. Same proposal, backtest
# and portal signature as any other change to standard work.


def _declare_confirmed_key(env, monkeypatch, threshold=3):
    from dataclasses import replace

    cfg = replace(env.work.config, evidence=replace(
        env.work.config.evidence, confirmed_key_threshold=threshold))
    env.work.config = cfg
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)


def _keg_proposals():
    return [p for p in read_all() if (p.payload or {}).get("keg")]


def test_confirmed_decisions_for_an_unlisted_key_become_a_proposed_rule(env, monkeypatch):
    _declare_confirmed_key(env, monkeypatch)
    env.earn_v1()
    answers = []
    for n in range(1, 4):
        env.add("social", f"post {n}")
        events = env.code("comms")
        answers.append(events)
        if n < 3:
            assert events == [] and _keg_proposals() == []      # below the count: nothing
    [event] = answers[-1]
    assert event["detector"] == "confirmed_key" and event["flag"] == keg.FLAG_TIER_DOWN_PATTERN
    assert len(event["provenance"]) == 3                        # the confirmations are the evidence
    assert event["answer"]["kind"] == andon.KIND_STANDARD_WORK
    assert event["answer"]["channel"] == andon.CHANNEL_PORTAL   # signed in the portal, never in chat
    [proposal] = _keg_proposals()
    k = proposal.payload["keg"]
    assert (k["version"], k["flag"]) == (2, keg.FLAG_TIER_DOWN_PATTERN)
    assert k["conditions"][0] == {"if": "channel == 'social'", "then": {"tag": "comms"}}
    assert len(proposal.evidence) == 3
    detail = event["answer"]["detail"]
    assert (detail["would_change"], detail["rules_added"]) == (0, 1)
    assert "You confirmed 'social' the same way 3 times" in event["answer"]["summary"]
    # Nothing serves until it is signed; signed, the keg answers that key.
    [serving] = [e for e in env.store.all() if e.status == STATUS_ACTIVE]
    assert keg.evaluate(keg.keg_of(serving), {"channel": "social"}) is None
    assert fc.cli_approve(proposal.proposal_id.split(":")[-1][:12]) == 0
    [serving] = [e for e in env.store.all() if e.status == STATUS_ACTIVE]
    assert keg.evaluate(keg.keg_of(serving), {"channel": "social"}) == {"tag": "comms"}
    assert keg.evaluate(keg.keg_of(serving), {"channel": "billing"}) == {"tag": "finance"}
    assert_every_andon_closed_exactly_once(env.events())


def test_what_never_counts_as_a_confirmation(env, monkeypatch):
    _declare_confirmed_key(env, monkeypatch)
    env.earn_v1()
    # Keg-decided items never count, however many are confirmed.
    for n in range(4):
        env.add("billing", f"invoice {n}")
        env.code("finance", keg_served=True)
    assert env.work.confirmed_key_evidence("billing")["confirmations"] == 0
    # A key the reference table lists is not this rule's to earn.
    assert env.work.confirmed_key_evidence("press")["met"] is False
    # Two different confirmed answers: no pattern.
    for tag in ("comms", "other", "comms", "comms"):
        env.add("forum", "thread")
        env.code(tag)
    found = env.work.confirmed_key_evidence("forum")
    assert (found["met"], found["confirmations"]) == (False, 4)
    # One revision of any item with the key: no pattern.
    for n in range(3):
        env.add("social", f"post {n}")
        env.code("comms")
    assert len(_keg_proposals()) == 1
    env.work.rule_on(f"m{env.n - 1:02d}", decision="correct",
                     corrected_output={"tag": "escalate"}, provenance=env.prov())
    assert env.work.confirmed_key_evidence("social")["met"] is False


def test_the_rule_is_declared_or_it_does_not_exist(env):
    env.earn_v1()
    for n in range(5):
        env.add("social", f"post {n}")
        assert env.code("comms") == []
    assert _keg_proposals() == []
    assert env.work.confirmed_key_evidence("social")["threshold"] is None
    bad = SimpleNamespace(
        id=GOAL, root=env.work.config.queue.parent, keywords=(), resolved_sources=lambda: [],
        extra={"decision_work": {
            "tool": "t", "queue": "queue", "isolation": "sources_only",
            "inputs": {"channel": {"data_type": "string", "required": True}},
            "outputs": {"tag": {"data_type": "string"}},
            "reference_table": {"path": "channels.csv", "key_column": "Channel",
                                "value_column": "Default Tag", "key_input": "channel",
                                "value_output": "tag"},
            "evidence": {"threshold": 3, "confirmed_key": {"threshold": 0}}}})
    with pytest.raises(ValueError, match="confirmed_key needs a threshold of 1 or more"):
        dw.load_config(bad)


def test_two_keys_earned_before_signing_ride_one_card(env, monkeypatch):
    _declare_confirmed_key(env, monkeypatch)
    env.earn_v1()
    for n in range(3):
        env.add("social", f"post {n}")
        env.code("comms")
    [first] = _keg_proposals()
    # A fourth confirmation of the same key while its draft waits: watched, not re-proposed.
    env.add("social", "post 4")
    [again] = env.code("comms")
    assert again["answer"]["kind"] == andon.KIND_WATCH and _keg_proposals() == [first]
    for n in range(3):
        env.add("forum", f"thread {n}")
        events = env.code("other")
    [event] = events
    [card] = _keg_proposals()
    assert card.proposal_id != first.proposal_id                 # the first draft was withdrawn
    k = card.payload["keg"]
    assert k["version"] == 2
    assert k["conditions"][:2] == [
        {"if": "channel == 'forum'", "then": {"tag": "other"}},
        {"if": "channel == 'social'", "then": {"tag": "comms"}}]
    assert event["answer"]["detail"]["rules_added"] == 2
    assert event["answer"]["detail"]["replaced"] == [first.proposal_id]
    assert "one card, 2 rules" in event["answer"]["summary"]
    withdrawn = [e for e in env.events() if e.get("disposition") == "withdrawn"]
    assert [e["proposal_id"] for e in withdrawn] == [first.proposal_id]
    # One signature brings both rules into force.
    assert fc.cli_approve(card.proposal_id.split(":")[-1][:12]) == 0
    [serving] = [e for e in env.store.all() if e.status == STATUS_ACTIVE]
    spec = keg.keg_of(serving)
    assert keg.evaluate(spec, {"channel": "social"}) == {"tag": "comms"}
    assert keg.evaluate(spec, {"channel": "forum"}) == {"tag": "other"}
    assert_every_andon_closed_exactly_once(env.events())
