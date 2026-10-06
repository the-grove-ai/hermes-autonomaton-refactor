"""Keg proposals on the T0 cache: grammar, backtest, proposal, signing,
feedback and versioning.

Every fixture here is a MESSAGE-TAGGING keg on purpose. The loop is generic —
a new kind of work is a new Dock goal and a new skill, not new code — so no
test outside a domain's own adapter may lean on that domain's fields.
"""

from __future__ import annotations

import inspect
import json

import pytest

import grove.pattern_cache as pc
from grove import flywheel_cli as fc
from grove import keg
from grove.eval import pattern_compiler as compiler
from grove.eval.pattern_compiler import (
    DISPOSITION_DROPPED_BACKTEST_CONFLICT,
    DISPOSITION_PROPOSED,
    DISPOSITION_SKIPPED_KNOWN,
    backtest_keg,
    propose_keg,
)
from grove.eval.proposal_queue import PROPOSAL_TYPE_PATTERN_PROMOTION, read_all
from grove.kaizen import rendering
from grove.kaizen_ledger import KaizenLedger
from grove.pattern_cache import (
    PatternCacheStore,
    STATUS_ACTIVE,
    STATUS_HALTED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    STATUS_SUSPENDED,
)

INPUTS = {
    "channel": {"data_type": "string", "required": True},
    "subject": {"data_type": "string", "required": False},
    "urgent": {"data_type": "boolean", "required": False},
}
OUTPUTS = {"tag": {"data_type": "string"}}
RULES_V1 = [
    {"if": "channel == 'billing'", "then": {"tag": "finance"}},
    {"if": "channel IN ['outage', 'incident']", "then": {"tag": "ops"}},
]
# The refinement: an urgent billing message is escalated, checked first.
RULES_V2 = [
    {"if": "channel == 'billing' AND urgent == true", "then": {"tag": "escalate"}},
] + RULES_V1
REQUEST = "tag the next message"


def _case(ref, channel, served, confirmed=None, urgent=False):
    return {
        "ref": ref, "label": f"{channel} message",
        "inputs": {"channel": channel, "urgent": urgent},
        "served": {"tag": served},
        "confirmed": None if confirmed is None else {"tag": confirmed},
    }


HISTORY = [
    _case("m1", "billing", "finance", "finance"),
    _case("m2", "outage", "ops", "ops"),
    _case("m3", "social", "other", "other"),
    _case("m4", "Billing", "finance", "finance"),
    _case("m5", "incident", "ops", "ops"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)

    class Env:
        store = PatternCacheStore(db)
        queue = tmp_path / "proposals.jsonl"
        ledger_dir = tmp_path / "ledger"

        def ledger(self):
            return KaizenLedger("kaizen-test", ledger_dir=self.ledger_dir)

        def events(self):
            out = []
            for f in sorted(self.ledger_dir.glob("*.jsonl")):
                out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            return sorted(out, key=lambda e: e.get("timestamp") or e.get("ts") or "")

        def propose(self, rules=RULES_V1, history=HISTORY, evidence=("t1", "t2", "t3"),
                    flag=keg.FLAG_TIER_DOWN_PATTERN, feedback=None):
            return propose_keg(
                self.store, name="Message tagging", request=REQUEST,
                intent_class="analysis", tool_name="tag_message",
                tool_args={"verb": "apply_keg"}, inputs=INPUTS, outputs=OUTPUTS,
                conditions=rules, scope_text="Tags messages by channel.",
                reserve="Channels outside the table; anything ambiguous.",
                dock_goal="message-triage", scope="reserved",
                authority_level="green", flag=flag,
                flag_detail="three confirmed tags matched", evidence_turn_ids=evidence,
                history=history, feedback=feedback, queue_path=self.queue,
                ledger=self.ledger(),
            )

        def short(self, result):
            return result.proposal_id.split(":")[-1][:12]

    return Env()


# ── condition grammar ─────────────────────────────────────────────────


@pytest.mark.parametrize("expr, values, expected", [
    ("channel == 'billing'", {"channel": "billing"}, True),
    ("channel == 'billing'", {"channel": "  BILLING "}, True),
    ("channel == 'billing'", {"channel": "outage"}, False),
    ("channel IN ['a', 'b']", {"channel": "b"}, True),
    ("channel NOT IN ['a', 'b']", {"channel": "c"}, True),
    ("channel NOT IN ['a', 'b']", {"channel": "a"}, False),
    ("urgent == true", {"urgent": True}, True),
    ("urgent == true", {"urgent": False}, False),
    ("subject CONTAINS 'refund'", {"subject": "Re: REFUND  request"}, True),
    ("subject CONTAINS 'refund'", {"subject": "renewal"}, False),
    ("subject CONTAINS 'refund'", {}, False),
    ("channel == 'billing' AND subject CONTAINS 'refund'",
     {"channel": "billing", "subject": "refund please"}, True),
    # AND binds tighter than OR.
    ("channel == 'a' OR channel == 'b' AND urgent == true",
     {"channel": "a", "urgent": False}, True),
    ("channel == 'a' OR channel == 'b' AND urgent == true",
     {"channel": "b", "urgent": False}, False),
    # An input with no value never matches.
    ("channel == 'a'", {}, False),
    ("channel NOT IN ['a']", {"channel": None}, False),
])
def test_condition_grammar(expr, values, expected):
    spec = {"inputs": INPUTS, "conditions": [{"if": expr, "then": {"tag": "x"}}]}
    assert (keg.evaluate(spec, values) is not None) is expected


@pytest.mark.parametrize("expr", [
    "", "channel", "channel = 'a'", "channel == 'a' AND", "unknown == 'a'",
    "channel == 'a') OR", "channel IN 'a'", "channel == 'a' urgent == true",
    "__import__('os') == 1", "subject CONTAINS ''", "subject CONTAINS 3",
    "urgent CONTAINS 'x'", "subject CONTAINS ['a']",
])
def test_unreadable_condition_fails_loud(expr):
    with pytest.raises(ValueError):
        keg.parse_condition(expr, INPUTS)


def test_first_matching_condition_wins():
    spec = {"inputs": INPUTS, "conditions": RULES_V2}
    assert keg.evaluate(spec, {"channel": "billing", "urgent": True}) == {"tag": "escalate"}
    assert keg.evaluate(spec, {"channel": "billing", "urgent": False}) == {"tag": "finance"}
    assert keg.evaluate(spec, {"channel": "social"}) is None


def _spec(**over):
    spec = {
        "protocol": "GRV-004", "protocolVersion": "1.1", "name": "Message tagging",
        "version": 1, "scope": "reserved", "authority_level": "green",
        "dock_goal": "message-triage", "reserve": "anything else",
        "trigger": {"request": REQUEST}, "inputs": INPUTS, "outputs": OUTPUTS,
        "conditions": RULES_V1,
    }
    spec.update(over)
    return spec


@pytest.mark.parametrize("over", [
    {"scope": "private"}, {"authority_level": "blue"}, {"reserve": ""},
    {"dock_goal": ""}, {"trigger": {}}, {"version": 0}, {"conditions": []},
    {"protocol": "other"}, {"inputs": {}},
    {"conditions": [{"if": "channel == 'a'", "then": {"undeclared": 1}}]},
])
def test_incomplete_keg_spec_is_refused(over):
    keg.validate_spec(_spec())
    with pytest.raises(ValueError):
        keg.validate_spec(_spec(**over))


def test_lifecycle_maps_every_status():
    assert keg.lifecycle(STATUS_SUSPENDED)["status"] == "draft"
    assert keg.lifecycle(STATUS_ACTIVE) == {
        "status": "stable", "state": "serving", "serves": True, "halted": False,
    }
    halted = keg.lifecycle(STATUS_HALTED)
    assert halted["status"] == "stable" and halted["halted"] and not halted["serves"]
    assert keg.lifecycle(STATUS_SUPERSEDED)["status"] == "stable"
    assert keg.lifecycle(pc.STATUS_DEMOTED)["status"] == "deprecated"
    # Only revocation deprecates.
    deprecated = [s for s in (
        STATUS_SUSPENDED, STATUS_REJECTED, STATUS_ACTIVE, STATUS_HALTED,
        STATUS_SUPERSEDED, pc.STATUS_DEMOTED,
    ) if keg.lifecycle(s)["status"] == "deprecated"]
    assert deprecated == [pc.STATUS_DEMOTED]


# ── backtest ──────────────────────────────────────────────────────────


def test_backtest_counts_and_orders_changed_cases_first():
    history = HISTORY + [_case("m6", "billing", "finance", "escalate", urgent=True)]
    bt = backtest_keg({"inputs": INPUTS, "conditions": RULES_V2}, history)
    # Three outcomes, never merged: m3 ("social") is not covered, so it is
    # NOT one of the unchanged.
    assert (bt["replayed"], bt["unchanged"], bt["would_change"], bt["not_covered"]) == (6, 4, 1, 1)
    assert [c["result"] for c in bt["cases"]][:2] == ["would_change", "not_covered"]
    changed = bt["cases"][0]
    assert changed["ref"] == "m6" and changed["agrees_with_confirmed"] is True
    detail = rendering.KegBacktestDetail.from_dict(bt)
    assert detail.headline == (
        "Replayed on history: 4 unchanged · 1 would change · 1 not covered "
        "(social message)")
    # An envelope written when not-covered was folded into unchanged still
    # reads correctly: the counts come from the cases.
    old = dict(bt, unchanged=5)
    assert rendering.KegBacktestDetail.from_dict(old).unchanged == 4


def test_malformed_backtest_detail_fails_loud():
    with pytest.raises(ValueError):
        rendering.KegBacktestDetail.from_dict({"kind": "keg_backtest"})
    with pytest.raises(ValueError):
        rendering.KegBacktestDetail.from_dict({"cases": []})


# ── proposal ──────────────────────────────────────────────────────────


def test_proposed_keg_is_a_draft_that_never_serves(env):
    result = env.propose()
    assert result.status == DISPOSITION_PROPOSED and result.version == 1
    assert result.pattern_id.startswith("keg:message-tagging:v1:")
    entry = env.store.get(result.pattern_id)
    assert entry.status == STATUS_SUSPENDED
    assert env.store.get_active_for_message(REQUEST) is None

    [proposal] = read_all(path=env.queue)
    assert proposal.type == PROPOSAL_TYPE_PATTERN_PROMOTION
    assert proposal.proposer == "kaizen"
    assert proposal.requires_portal_review is True
    k = proposal.payload["keg"]
    assert (k["scope"], k["authority_level"], k["dock_goal"]) == (
        "reserved", "green", "message-triage")
    assert k["trigger"]["request"] == REQUEST
    assert rendering.decode_detail(proposal).headline == (
        "Replayed on history: 4 unchanged · 0 would change · 1 not covered "
        "(social message)")

    # What the operator signs is what T0 runs: the keg rides inside the
    # invocation the bind-and-verify signature covers.
    from grove.effect_signature import canonical_effect_signature
    inv = json.loads(entry.compiled_invocation)
    assert keg.keg_of(entry) == inv["args"]["keg"]
    assert inv["approved_signature"] == canonical_effect_signature(inv["tool"], inv["args"])

    [event] = [e for e in env.events() if e["event_type"] == "kaizen_proposal"]
    assert event["loop_step"] == keg.LOOP_KAIZEN_PROPOSAL
    assert (event["keg"], event["version"], event["flag"]) == (
        "Message tagging", 1, keg.FLAG_TIER_DOWN_PATTERN)


def test_ordinary_pattern_promotion_stays_chat_reviewable(env):
    from grove.eval.proposal_queue import RoutingProposal
    plain = RoutingProposal(
        proposal_id="sha256:x", type=PROPOSAL_TYPE_PATTERN_PROMOTION,
        payload={"pattern_id": "sha256:y"}, evidence=("t",), eval_hash="h",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert plain.requires_portal_review is False


def test_identical_keg_is_never_proposed_twice(env):
    first = env.propose()
    again = env.propose()
    assert again.status == DISPOSITION_SKIPPED_KNOWN
    assert again.pattern_id == first.pattern_id
    assert len(read_all(path=env.queue)) == 1


def test_keg_that_contradicts_a_confirmed_case_is_not_proposed(env):
    history = HISTORY + [_case("m9", "billing", "finance", "legal")]
    result = env.propose(history=history)
    assert result.status == DISPOSITION_DROPPED_BACKTEST_CONFLICT
    assert "m9" in result.detail
    assert read_all(path=env.queue) == []
    assert env.store.get(result.pattern_id) is None


def test_scanner_does_not_recompile_a_request_a_keg_answers(env, tmp_path):
    from tests.grove.test_pattern_compiler import _CFG, _seed, _store
    env.propose()
    intents = _store(tmp_path)
    _seed(intents, REQUEST, "analysis", 6,
          tool_inv=json.dumps({"tool": "tag_message", "args": {}}))
    result = compiler.propose_pattern_promotions(
        intents, env.store, queue_path=env.queue, config=_CFG)
    assert [d.status for d in result.dispositions] == [DISPOSITION_SKIPPED_KNOWN]


# ── signing, versioning ───────────────────────────────────────────────


def test_signing_makes_the_keg_standard_work(env):
    result = env.propose()
    assert fc.cli_approve(env.short(result), queue_path=env.queue,
                          ledger_dir=env.ledger_dir) == 0
    entry = env.store.get(result.pattern_id)
    assert entry.status == STATUS_ACTIVE
    assert env.store.get_active_for_message(REQUEST).pattern_id == result.pattern_id
    signed = keg.keg_record(entry)["signed"]
    assert signed["by"] == "operator" and signed["proposal_id"] == result.proposal_id

    steps = [e.get("loop_step") for e in env.events() if e.get("loop_step")]
    assert steps == [
        keg.LOOP_KAIZEN_PROPOSAL, keg.LOOP_SIGNED, keg.LOOP_NEW_STANDARD_WORK,
    ]
    work = [e for e in env.events() if e["event_type"] == "new_standard_work"][0]
    assert (work["keg"], work["version"], work["signed_by"]) == ("Message tagging", 1, "operator")


def test_v2_replaces_a_halted_v1_and_resumes_serving(env):
    v1 = env.propose()
    fc.cli_approve(env.short(v1), queue_path=env.queue, ledger_dir=env.ledger_dir)
    # A miss: the line stops. v1 halts — still signed, not serving.
    env.store.set_status(v1.pattern_id, STATUS_HALTED)
    assert env.store.get_active_for_message(REQUEST) is None

    history = HISTORY + [_case("m6", "billing", "finance", "escalate", urgent=True)]
    v2 = env.propose(rules=RULES_V2, history=history,
                     evidence=("t1", "t2", "t3", "t6"), flag=keg.FLAG_ANOMALY)
    assert v2.status == DISPOSITION_PROPOSED and v2.version == 2
    assert v2.backtest["would_change"] == 1
    [proposal] = read_all(path=env.queue)
    assert proposal.payload["keg"]["supersedes"] == v1.pattern_id
    # The draft does not serve and v1's halt stands until the operator rules.
    assert env.store.get(v1.pattern_id).status == STATUS_HALTED

    fc.cli_approve(env.short(v2), queue_path=env.queue, ledger_dir=env.ledger_dir)
    assert env.store.get(v1.pattern_id).status == STATUS_SUPERSEDED
    assert env.store.get_active_for_message(REQUEST).pattern_id == v2.pattern_id
    work = [e for e in env.events() if e["event_type"] == "new_standard_work"][-1]
    assert work["version"] == 2 and work["replaces"] == [v1.pattern_id]


# ── feedback ──────────────────────────────────────────────────────────


def test_rejection_is_feedback_that_kaizen_revises_with(env):
    draft = env.propose()
    assert fc.cli_reject(env.short(draft), reason="incident should be its own tag",
                         queue_path=env.queue, ledger_dir=env.ledger_dir) == 0
    assert env.store.get(draft.pattern_id).status == STATUS_REJECTED
    assert fc.keg_feedback_history("Message tagging", store=env.store) == [
        "incident should be its own tag"]
    disposition = [e for e in env.events() if e["event_type"] == "kaizen_disposition"][0]
    assert disposition["loop_step"] == keg.LOOP_FEEDBACK
    assert disposition["reason"] == "incident should be its own tag"

    # The identical keg is not proposed again...
    assert env.propose().status == DISPOSITION_SKIPPED_KNOWN
    # ...but a revision on the same evidence is, carrying the feedback. A draft
    # that was sent back does not consume a version number.
    revised_rules = [
        {"if": "channel == 'billing'", "then": {"tag": "finance"}},
        {"if": "channel == 'outage'", "then": {"tag": "ops"}},
    ]
    revised = env.propose(
        rules=revised_rules,
        feedback=fc.keg_feedback_history("Message tagging", store=env.store),
    )
    assert revised.status == DISPOSITION_PROPOSED and revised.version == 1
    [proposal] = read_all(path=env.queue)
    assert proposal.payload["keg"]["feedback"] == ["incident should be its own tag"]


def test_chat_tool_cannot_sign_a_keg(env):
    from tools.flywheel_review_tool import approve_proposal
    result = env.propose()
    out = json.loads(approve_proposal(env.short(result), queue_path=env.queue))
    assert out["success"] is False and "Operator Portal" in out["error"]
    assert env.store.get(result.pattern_id).status == STATUS_SUSPENDED
    assert len(read_all(path=env.queue)) == 1


# ── the grant is scope-defining ───────────────────────────────────────


def _portal_proposal(env):
    """A keg proposal in the DEFAULT queue (the portal reads no other)."""
    result = propose_keg(
        env.store, name="Message tagging", request=REQUEST,
        intent_class="analysis", tool_name="tag_message", tool_args={},
        inputs=INPUTS, outputs=OUTPUTS, conditions=RULES_V1,
        scope_text="Tags messages by channel.", reserve="Everything else.",
        dock_goal="message-triage", scope="reserved", authority_level="green",
        flag=keg.FLAG_TIER_DOWN_PATTERN, flag_detail="", evidence_turn_ids=("t1",),
        history=HISTORY, ledger=env.ledger(),
    )
    [proposal] = read_all()
    return result, proposal


async def test_portal_signature_carries_the_demo_stamp(env, monkeypatch):
    import grove.api.actions as actions
    from grove.kaizen_ledger import default_ledger_dir

    monkeypatch.setattr(actions, "_demo_tokenless_approve", lambda: True)
    result, proposal = _portal_proposal(env)
    response = await actions._apply_routing(
        proposal, "approve", proposal.proposal_id, "abc123", None)
    assert response.status == 200
    assert env.store.get(result.pattern_id).status == STATUS_ACTIVE
    events = []
    for f in default_ledger_dir().glob("*.jsonl"):
        events += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    [signed] = [e for e in events if e.get("loop_step") == keg.LOOP_SIGNED]
    assert signed["approval_surface"] == "portal_demo_tokenless"
    assert signed["applied_result"]["approval_surface"] == "portal_demo_tokenless"


async def test_portal_refuses_a_keg_signature_without_a_token_outside_demo_mode(
    env, monkeypatch,
):
    import grove.api.actions as actions

    monkeypatch.setattr(actions, "_demo_tokenless_approve", lambda: False)
    result, proposal = _portal_proposal(env)
    response = await actions._apply_routing(
        proposal, "approve", proposal.proposal_id, "abc123", None)
    assert response.status == 422
    assert env.store.get(result.pattern_id).status == STATUS_SUSPENDED
    # Still queued, unsigned. (The refusal also files its own failure notice.)
    assert proposal.proposal_id in [p.proposal_id for p in read_all()]


def test_only_keg_carrying_promotions_are_scope_defining():
    import grove.api.actions as actions
    from grove.eval.proposal_queue import RoutingProposal

    def _p(payload):
        return RoutingProposal(
            proposal_id="sha256:x", type=PROPOSAL_TYPE_PATTERN_PROMOTION,
            payload=payload, evidence=("t",), eval_hash="h",
            created_at="2026-01-01T00:00:00+00:00",
        )
    assert actions._is_scope_defining_proposal(_p({"keg": {"name": "k"}})) is True
    assert actions._is_scope_defining_proposal(_p({"pattern_id": "p"})) is False


# ── portal card ───────────────────────────────────────────────────────


def _card(proposal_dict):
    from grove.api import fragments
    view = fragments._RenderView(proposal_dict)
    return fragments._keg_card_html(proposal_dict, view, proposal_dict["proposal_id"], "abc123")


def test_keg_card_reads_in_review_order(env):
    # 2026-10-06: the card was rebuilt in review order — why, what changes,
    # the replay (cases to review first), what it covers, the rules, then the
    # technical record and the action bar.
    v1 = env.propose()
    fc.cli_approve(env.short(v1), queue_path=env.queue, ledger_dir=env.ledger_dir)
    history = HISTORY + [_case("m6", "billing", "finance", "escalate", urgent=True)]
    env.propose(rules=RULES_V2, history=history, evidence=("t1", "t6"),
                flag=keg.FLAG_ANOMALY, feedback=["be stricter"])
    html = _card(read_all(path=env.queue)[0].to_dict())
    order = [html.index(s) for s in (
        "KAIZEN PROPOSAL · STANDARD WORK · MESSAGE TAGGING",
        "DRAFT · AWAITING YOUR SIGNATURE",
        "Keg v2: answer items where channel is billing and urgent is true directly, escalate.",
        "Everything else stays the same.",
        "Revised after your feedback: “be stricter”.",
        "1 · JIDOKA FLAGGED", "2 · ANDON STOPPED THE LINE", "Keg v1 halted.",
        "3 · KAIZEN DRAFTED A FIX", "1 added rule, replayed on all 6 items so far.",
        "4 · YOU DECIDE", "Sign it and v2 replaces v1, or send it back with feedback.",
        "What changes from v1",
        "If channel is billing and urgent is true, answer escalate.",
        "→ tag escalate",                         # the formal rule, beneath the sentence
        "Nothing removed. The other 2 rules from v1 are unchanged.",
        "Replayed on history: 6 items", "REVIEW THESE FIRST",
        "Matches your revision", "Not covered by design",
        "Show the 4 unchanged items",
        "What v2 covers", "ANSWERS DIRECTLY, NO MODEL", "ALWAYS SENDS TO THE MODEL",
        "Your 2 confirmed decisions are the evidence",
        "The rules, in order", '<span class="sc-new">New</span>',
        "Technical details", "replaces keg:message-tagging:v1:",
        "GRV-004 keg · scope reserved · authority green",
        "Sign v2</button>", 'name="reason"', "Send back with feedback",
        "Signing takes effect immediately and replaces v1. Demo mode",
    )]
    assert order == sorted(order)
    # Three outcomes, each counted on its own, as a bar and as numbers.
    assert html.count("sc-seg-same") == 2 and html.count("sc-seg-change") == 2
    for count, word in ((4, "unchanged"), (1, "would change"), (1, "not covered")):
        assert f"<strong>{count}</strong>&nbsp;{word}" in html
    assert html.count('<span class="sc-new">') == 1          # only the added rule
    assert "sc-back-btn" in html and "btn-reject" not in html   # sending back is not an error


def test_handing_a_case_back_is_consistent_with_a_correction_not_a_match(env):
    v1 = env.propose()
    fc.cli_approve(env.short(v1), queue_path=env.queue, ledger_dir=env.ledger_dir)
    history = HISTORY + [_case("m6", "billing", "finance", "escalate", urgent=True)]
    history[-1]["served_by_keg"] = True
    defer = [{"if": "channel == 'billing' AND urgent == true", "defer": True}] + RULES_V1
    env.propose(rules=defer, history=history, evidence=("t1", "t6"), flag=keg.FLAG_ANOMALY)
    html = _card(read_all(path=env.queue)[0].to_dict())
    assert "Keg v2: send items where channel is billing and urgent is true back to the model." in html
    assert "If channel is billing and urgent is true, send it to the model." in html
    assert "<strong>sends it to the model</strong>" in html
    assert "Consistent with your revision" in html and "Matches your revision" not in html
    assert "Handed back by rule: channel is billing and urgent is true." in html


def test_a_first_version_says_it_is_the_first(env):
    env.propose()
    html = _card(read_all(path=env.queue)[0].to_dict())
    for text in ("Keg v1: answer 2 kinds of item with no model.",
                 "Everything else still goes to the model.",
                 "What v1 does", "This is the first version, so every rule is new.",
                 "Sign it and v1 starts serving", "· first version",
                 "Signing takes effect immediately. Demo mode"):
        assert text in html, text
    assert "sc-new" not in html and "replaces v" not in html


def test_keg_card_with_unreadable_backtest_cannot_be_signed(env):
    env.propose()
    data = read_all(path=env.queue)[0].to_dict()
    data["detail"] = {"kind": "keg_backtest"}
    html = _card(data)
    assert "Replay unreadable" in html and "cannot be signed" in html
    assert "sc-sign-btn" not in html and "Sign v1</button>" not in html
    assert "Send back with feedback" in html


def test_a_signed_or_returned_proposal_says_what_happened():
    from grove.api import fragments
    signed = fragments.keg_resolved_html("a1", {"version": 2, "supersedes_version": 1}, True)
    assert "SIGNED · V2 SERVING" in signed and "in place of v1" in signed
    back = fragments.keg_resolved_html("a1", {"version": 2}, False, reason="narrower")
    assert "SENT BACK TO KAIZEN" in back and "“narrower”" in back


def test_rules_read_in_plain_words_from_the_grammar():
    inputs = {"channel": {"data_type": "string"}, "subject": {"data_type": "string"},
              "urgent": {"data_type": "boolean"}}
    said = {
        "channel == 'billing' AND subject CONTAINS 'refund'":
            "channel is billing and subject mentions “refund”",
        "subject CONTAINS 'a' OR subject CONTAINS 'b' OR subject CONTAINS 'c'":
            "subject mentions “a”, “b” or “c”",
        "channel IN ['x', 'y']": "channel is one of x or y",
        "channel NOT IN ['x']": "channel is not one of x",
        "channel == 'x' OR urgent == true": "channel is x or urgent is true",
    }
    for expr, plain in said.items():
        assert keg.describe_condition(expr, inputs) == plain
    with pytest.raises(ValueError):
        keg.describe_condition("nonsense ==", inputs)
    diff = keg.diff_rules(
        [{"if": "a", "then": {"t": 1}}, {"if": "b", "then": {"t": 2}}, {"if": "c", "defer": True}],
        [{"if": "n", "defer": True}, {"if": "a", "then": {"t": 1}}, {"if": "b", "then": {"t": 9}}])
    assert [len(diff[k]) for k in ("added", "removed", "changed", "unchanged")] == [1, 1, 1, 1]
    assert len(keg.diff_rules(None, [{"if": "a", "then": {}}])["added"]) == 1


# ── generality ────────────────────────────────────────────────────────


def test_the_loop_carries_no_domain_vocabulary():
    # A new kind of work is a new Dock goal and a new skill, not new code. The
    # loop's own code must name no domain's fields.
    from grove.api import fragments
    sources = [
        inspect.getsource(keg),
        inspect.getsource(compiler.backtest_keg),
        inspect.getsource(compiler.propose_keg),
        inspect.getsource(fc._sign_keg),
        inspect.getsource(fc._keg_feedback),
        inspect.getsource(fragments._keg_card_html),
        inspect.getsource(rendering.KegBacktestDetail),
    ]
    for word in ("vendor", "invoice", "gl_code", "ledger code", "coding"):
        for src in sources:
            assert word not in src.lower(), word
