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
RULE = """routing_rules:
  goal_request:
    enabled: true
    match: {request: goal%s}
    target_tier: T1
"""


def _router(tmp_path, rule=RULE % "", strip=False):
    op = tmp_path / "routing.operational.yaml"
    text = (REPO / "config" / "routing.operational.yaml").read_text(encoding="utf-8")
    assert text.count("\nrouting_rules:\n") == 1
    if not strip:
        text = text.replace("\nrouting_rules:\n", "\n" + rule, 1)
    op.write_text(text, encoding="utf-8")
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
    named = _router(tmp_path, RULE % ", goals: [message-triage]")
    assert named.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="message-triage").tier == "T1"
    assert named.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="another").reason == "premium"
    # No such rule declared (the shipped file): a goal's request is routed as before.
    shipped = _router(tmp_path / "s" if (tmp_path / "s").mkdir() is None else tmp_path, strip=True)
    assert shipped.route(**READ_AS_ANALYSIS, request=REQUEST_GOAL, goal="g").reason == "premium"


def test_an_unknown_request_kind_is_refused_at_load(tmp_path):
    with pytest.raises(ValueError, match="match.request has unknown value"):
        _router(tmp_path, RULE.replace("request: goal%s", "request: gaol"))


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
