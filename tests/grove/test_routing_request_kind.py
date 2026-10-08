"""A routing rule can match on what the turn IS, not only on how a classifier
reads it.

2026-10-07, live: the same declared sentence ("code the next item") was read
as a memory operation by one classifier and as analysis by two others, so the
same work ran at T1 or T2 depending on which classifier was bound, and a
month's cost moved by a factor of three. The Dispatcher already knows, with no
model, that the turn is a goal's own declared request. ``match: {request:
goal}`` lets a rule route on that fact. It is a shape, not a special case: the
rule sits in ``routing_rules`` with the others, first match wins, and the
operator chooses the tier, the goals and the order.
"""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove import decision_work as dw
from grove.router import REQUEST_GOAL, CognitiveRouter

REPO = Path(__file__).resolve().parents[2]
SHIPPED_RULE = ("  goal_request:\n    enabled: true\n    match:\n      request: goal\n"
                "    target_tier: T1\n"
                "    classify: false        # decided by what the turn is: the classifier is not called\n")


def _router(tmp_path, change=lambda text: text):
    """A router on a copy of the shipped routing file, optionally changed."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    text = (REPO / "config" / "routing.operational.yaml").read_text(encoding="utf-8")
    assert text.count(SHIPPED_RULE) == 1                    # the shipped rule, as declared
    op = tmp_path / "routing.operational.yaml"
    op.write_text(change(text), encoding="utf-8")
    authority = REPO / "config" / "routing.authority.yaml"
    if authority.exists():
        shutil.copy(authority, tmp_path / "routing.authority.yaml")
    return CognitiveRouter(op)


READ_AS_ANALYSIS = dict(intent="analysis", confidence=0.9, complexity_signal="simple")


def test_a_goals_own_request_is_routed_by_the_rule_whatever_the_classifier_reads(tmp_path):
    router = _router(tmp_path)
    # The classifier's read alone sends "analysis" to the premium tier, as before.
    plain = router.route(**READ_AS_ANALYSIS)
    assert (plain.tier, plain.reason) == ("T2", "premium")
    # The same read, on a turn that IS a goal's own request: the rule decides.
    mine = router.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="message-triage")
    assert (mine.tier, mine.reason) == ("T1", "goal_request")
    # Whatever the classifier makes of the sentence, or if it failed outright.
    for read in (dict(intent="code_generation", confidence=0.6, complexity_signal="moderate"),
                 dict(intent=None, confidence=None, complexity_signal=None)):
        assert router.route(**read, request=REQUEST_GOAL, goal="g").reason == "goal_request"
    # A re-issue one tier up still names its tier: the ladder is not undone.
    up = router.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="g", operator_tier="T2")
    assert (up.tier, up.reason) == ("T2", "operator_override")
    # Another message in the same goal's session is not the goal's request.
    other = router.route(**READ_AS_ANALYSIS, request=None, goal="message-triage")
    assert (other.tier, other.reason) == ("T2", "premium")


def test_the_rule_can_name_goals_and_absent_it_nothing_changes(tmp_path):
    named = _router(tmp_path / "named", lambda t: t.replace(
        "      request: goal\n", "      request: goal\n      goals: [message-triage]\n"))
    assert named.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="message-triage").tier == "T1"
    assert named.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="another").reason == "premium"
    # With the rule taken out (or switched off), a goal's request is routed by
    # the classifier's read, as it was before.
    without = _router(tmp_path / "without", lambda t: t.replace(SHIPPED_RULE, ""))
    assert without.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="g").reason == "premium"
    off = _router(tmp_path / "off", lambda t: t.replace(
        SHIPPED_RULE, SHIPPED_RULE.replace("enabled: true", "enabled: false")))
    assert off.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="g").reason == "premium"


def test_an_unknown_request_kind_is_refused_at_load(tmp_path):
    with pytest.raises(ValueError, match="match.request has unknown value"):
        _router(tmp_path, lambda t: t.replace("      request: goal\n", "      request: gaol\n"))


def test_the_dispatcher_says_what_the_turn_is_with_no_model(monkeypatch):
    from grove.dispatcher import Dispatcher
    from tests.grove.test_work_session import GOAL, SESSION, _goal
    import pathlib, tempfile

    cfg = dw.load_config(_goal(pathlib.Path(tempfile.mkdtemp()), SESSION))
    monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: cfg)

    def what(message, goal=GOAL):
        return Dispatcher._turn_request(
            SimpleNamespace(_current_turn_isolation=goal, _current_turn_id="s#1"), message)

    assert what("tag the next message") == (REQUEST_GOAL, GOAL)      # the goal's declared request
    assert what("why that tag?") == (None, GOAL)                     # the goal's session, another message
    assert what("tag the next message", goal=None) == (None, None)   # no goal session
    # Threaded to the router, beside the classifier's read; a pinned tier still wins there.
    import inspect
    src = inspect.getsource(Dispatcher)
    assert "request=_request, goal=_goal," in src
    assert "explicit_tier=self._take_reissue_tier(agent)," in src


# ── no classifier call where a declared rule decides without it ────────
# 2026-10-07, live: the classifier ran first on every model turn and the turn
# waited for it (a median 11.5 s on one binding), though the goal_request rule
# had already made its label irrelevant to the tier.


def test_a_rule_that_needs_nothing_from_the_classifier_routes_without_calling_it(
        tmp_path, monkeypatch):
    from grove import classify, providers

    router = _router(tmp_path)
    rule = router.rule_without_classifier(request=REQUEST_GOAL, goal="message-triage")
    assert rule is not None and (rule.name, rule.classify) == ("goal_request", False)
    assert router.rule_without_classifier(request=None, goal="message-triage") is None
    assert router.rule_without_classifier(request=None, goal=None) is None

    calls = []
    monkeypatch.setattr(providers, "_ensure_router", lambda: router)
    monkeypatch.setattr(classify, "classify_for_routing",
                        lambda message: calls.append(message) or None)
    decision = providers.route_for_agent(
        message="tag the next message", request=REQUEST_GOAL, goal="message-triage")
    assert (decision.tier, decision.reason) == ("T1", "goal_request") and calls == []
    # The record says how the turn was routed and where that is declared.
    assert providers.current_route_note() == {
        "deterministic": True, "rule": "goal_request", "classifier": "not called",
        "declared_in": "routing.operational.yaml › routing_rules › goal_request"}
    # Any other turn: the classifier is asked, and there is no such note.
    providers.route_for_agent(message="why that tag?", request=None, goal="message-triage")
    assert calls == ["why that tag?"] and providers.current_route_note() is None
    # The trace says it in one line.
    from grove.api import fragments
    assert fragments._trace_turn_notes({"routed_by": {
        "rule": "goal_request",
        "declared_in": "routing.operational.yaml › routing_rules › goal_request"}}) == [
        "Deterministic routing: rule goal_request (routing.operational.yaml › "
        "routing_rules › goal_request). No classifier call."]


def test_the_order_of_rules_decides_whether_the_classifier_can_be_skipped(tmp_path):
    # Moved below a rule that needs the classifier, the same rule can no longer
    # decide first: that earlier rule might match, so the classifier is asked.
    def moved(text):
        text = text.replace(SHIPPED_RULE, "")
        return text.rstrip("\n") + "\n" if False else text.replace(
            "\n  premium:\n", "\n" + SHIPPED_RULE + "  premium:\n", 1)
    later = _router(tmp_path / "later", moved)
    assert later.rule_without_classifier(request=REQUEST_GOAL, goal="g") is None
    # With classify left at its default, the rule still routes but asks first.
    asks = _router(tmp_path / "asks", lambda t: t.replace(
        "    classify: false        # decided by what the turn is: the classifier is not called\n", ""))
    assert asks.rule_without_classifier(request=REQUEST_GOAL, goal="g") is None
    assert asks.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="g").reason == "goal_request"
    # classify: false on a rule the classifier is needed for is refused at load.
    with pytest.raises(ValueError, match="classify: false needs a match on request or goals"):
        _router(tmp_path / "bad", lambda t: t.replace(
            "      request: goal\n", "      request: goal\n      intents: [analysis]\n"))


def test_the_classifier_has_a_time_budget_and_the_turn_goes_on_without_it(
        tmp_path, monkeypatch):
    import threading
    import time

    from grove import classify, providers

    router = _router(tmp_path)
    assert router.classifier_budget() == 5.0                      # as shipped
    release = threading.Event()

    def stalled(message):
        release.wait(timeout=20)
        return None

    monkeypatch.setattr(classify, "classify_for_routing", stalled)
    started = time.time()
    assert providers._classify_within(0.4, "hello") is None
    assert 0.4 <= time.time() - started < 5
    assert classify.last_classification_failure() == (
        "classifier_over_budget", "no answer in 0.4 s")
    release.set()
    # Inside the budget, the answer and any failure reason pass through unchanged.
    monkeypatch.setattr(classify, "classify_for_routing", lambda message: "the result")
    assert providers._classify_within(5, "hello") == "the result"
    assert providers._classify_within(None, "hello") == "the result"   # no budget: as before
    for bad in ("fast", -1, True):
        with pytest.raises(ValueError, match="telemetry.budget_seconds"):
            _router(tmp_path / f"b{bad}", lambda t, bad=bad: t.replace(
                "  budget_seconds: 5\n", f"  budget_seconds: {bad}\n"))
