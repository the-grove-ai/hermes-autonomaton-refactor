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
SHIPPED_RULE = "  goal_request:\n    enabled: true\n    match:\n      request: goal\n    target_tier: T1\n"


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
