"""retrieval-ambient-class-v1 P5 — clarify crash fix + disclosure telemetry.

* ``awaiting_operator`` joins VALID_OUTCOMES (the GATE-A live defect: every
  gateway store-and-resume deferral crashed the intent WRITE — a silent
  record drop inside the best-effort catch). ONE state: the clarify-vs-
  governance distinction rides PendingOperatorRequest.kind + tools_yielded.
* Per-turn disclosure verdicts, compact: baseline/core by count+hash; pull/
  hidden enumerated grouped by the deciding census gate. ~700B/turn typical.
* first_clarification: session-scoped (the narrowest honest thread identity
  on the gateway path).
"""

import json
from types import SimpleNamespace

import pytest

from grove.context_budget import (
    hidden_verdict_reasons,
    reset_caps_index_cache,
    resolve_tools_for_tier,
)
from grove.disclosure import reset_disclosure_split_cache
from grove.intent_store import VALID_OUTCOMES, IntentRecord, IntentStore


@pytest.fixture(autouse=True)
def _fresh():
    reset_caps_index_cache()
    reset_disclosure_split_cache()
    yield
    reset_caps_index_cache()
    reset_disclosure_split_cache()


def _record(**over):
    base = dict(
        timestamp="2026-07-21T00:00:00+00:00", session_id="s1", turn_id="t1",
        user_message_stem="stem", pattern_hash="h", intent_class="retrieval",
        register_class="task", complexity_signal="simple", confidence=0.9,
        outcome="pending",
    )
    base.update(over)
    return IntentRecord(**base)


# ── the clarify crash (before/after) ────────────────────────────────────────


def test_awaiting_operator_is_a_valid_outcome(tmp_path):
    store = IntentStore(tmp_path / "feed.jsonl")
    persisted = store.append(_record(outcome="awaiting_operator"))
    assert persisted["outcome"] == "awaiting_operator"


def test_unknown_outcome_still_fails_loud(tmp_path):
    # The closed set survives the widening — fail-loud discipline intact.
    store = IntentStore(tmp_path / "feed.jsonl")
    with pytest.raises(ValueError, match="unknown outcome"):
        store.append(_record(outcome="awaiting_godot"))


def test_one_deferral_state_not_two():
    # The asked-a-clarification fact rides tools_yielded + first_clarification;
    # the pending KIND lives on PendingOperatorRequest. A second outcome would
    # split one terminal fact (deferred awaiting operator input).
    assert "awaiting_operator" in VALID_OUTCOMES
    assert not any("clarif" in o for o in VALID_OUTCOMES)


# ── hidden-verdict attribution (deciding census gates) ──────────────────────


def _cap_doc(rid, tools, disclosure="proactive", state="approved",
             intents=None, always=False):
    return {
        "id": rid, "kind": "verb",
        "trigger": {"intents": intents or [], "keywords": [],
                    "dock_affinity": [], "always": always,
                    "disclosure": disclosure},
        "bindings": {"tools": tools, "credentials": None, "toolset_key": None},
        "tier_rule": {"eligible": [1, 2, 3], "preferred": 1,
                      "promotion_criteria": {},
                      "validation": {"strategy": "shadow_compare",
                                     "confidence_threshold": 0.95,
                                     "shadow_window": 20}},
        "zone": "green",
        "telemetry": {"feed": "intent_feed", "track": ["invocation"]},
        "context": {"disclosure": "eager", "payload": "p",
                    "dock_composition": "none"},
        "lifecycle": {"state": state, "provenance": "operator_authored",
                      "created_at": "2026-07-21T00:00:00+00:00",
                      "last_used": None, "use_count": 0,
                      "flywheel_eligible": True},
        "lineage": {"source_patterns": [], "parent_id": None,
                    "decision_log": []},
        "failure": {"fallback": "halt_and_surface", "diagnostic_context": [],
                    "circuit_breaker": {"threshold": 3, "window_seconds": 300}},
    }


def test_hidden_reasons_name_the_deciding_gate(monkeypatch):
    from grove.capability import Capability
    import grove.capability_registry as capreg

    caps = {
        d["id"]: Capability.from_dict(d) for d in (
            _cap_doc("dead", ["dead_tool"], state="suspended", always=True),
            _cap_doc("explore", ["explore_tool"], disclosure="complexity",
                     always=True),
            _cap_doc("gated", ["gated_tool"], intents=["code_generation"]),
        )
    }
    monkeypatch.setattr(capreg, "load_capabilities", lambda *a, **k: caps)
    reasons = hidden_verdict_reasons(
        {"dead_tool", "explore_tool", "gated_tool", "ghost_tool",
         "mcp_foo_bar"},
        "conversation", "simple",
    )
    assert reasons["lifecycle-null"] == ["dead_tool"]
    assert reasons["complexity-gate"] == ["explore_tool"]
    assert reasons["trigger-miss"] == ["gated_tool"]
    assert reasons["recordless"] == ["ghost_tool"]
    assert reasons["mcp-allow-miss"] == ["mcp_foo_bar"]


def test_complexity_record_not_hidden_reason_on_complex_turn(monkeypatch):
    from grove.capability import Capability
    import grove.capability_registry as capreg

    caps = {"explore": Capability.from_dict(
        _cap_doc("explore", ["explore_tool"], disclosure="complexity",
                 always=True))}
    monkeypatch.setattr(capreg, "load_capabilities", lambda *a, **k: caps)
    # On a complex turn the complexity gate ADMITS — if the tool is hidden
    # anyway (e.g. filtered upstream), the reason falls to trigger-miss, not
    # a false complexity-gate claim.
    reasons = hidden_verdict_reasons({"explore_tool"}, "retrieval", "complex")
    assert "complexity-gate" not in reasons


# ── verdict encoding: round-trip + volume ceiling ───────────────────────────


def _stash_for(intent, cx, mode="eager-t1"):
    import run_agent
    from tools.registry import ToolRegistry, register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg)
    all_defs = reg.get_definitions(set(reg.get_all_tool_names()), quiet=True)
    res = resolve_tools_for_tier(all_defs, intent, cx)
    agent = SimpleNamespace(tools=all_defs, _last_tool_selection={})
    run_agent.AIAgent._stash_disclosure_verdicts(
        agent, res, list(res.tools), intent, cx, mode=mode,
    )
    return agent._last_tool_selection["disclosure_verdicts"]


def test_verdict_encoding_round_trips_and_is_compact():
    v = _stash_for("conversation", "simple")
    assert v["mode"] == "eager-t1"
    # baseline/core by reference only — never enumerated.
    assert set(v["eager"]["baseline"]) == {"n", "sha12"}
    assert set(v["eager"]["core"]) == {"n", "sha12"}
    assert v["eager"]["baseline"]["n"] > 0
    # hidden groups name census gates.
    assert set(v["hidden"]) <= {
        "complexity-gate", "trigger-miss", "lifecycle-null",
        "mcp-allow-miss", "recordless", "unattributed",
    }
    # round-trip through the store encoding.
    assert json.loads(json.dumps(v)) == v
    # VOLUME DISCIPLINE: typical turn under the stated ~1KB ceiling.
    assert len(json.dumps(v, separators=(",", ":"))) < 1024


def test_verdict_rides_the_intent_record(tmp_path):
    v = _stash_for("retrieval", "simple")
    store = IntentStore(tmp_path / "feed.jsonl")
    persisted = store.append(_record(disclosure_verdicts=v))
    assert persisted["disclosure_verdicts"]["eager"]["baseline"]["n"] == \
        v["eager"]["baseline"]["n"]


# ── first-clarification (session-scoped) ────────────────────────────────────


def _mark(disp, session, *, deferral=False):
    from grove.dispatcher import Dispatcher

    return Dispatcher._mark_first_clarification(
        disp, session, clarify_deferral=deferral,
    )


def test_first_clarification_session_scoped():
    disp = SimpleNamespace(_current_turn_tools_yielded=("clarify",))
    assert _mark(disp, "s1") is True          # first in s1
    assert _mark(disp, "s1") is False         # second in s1
    assert _mark(disp, "s2") is True          # first in s2


def test_no_clarify_no_flag():
    disp = SimpleNamespace(_current_turn_tools_yielded=("web_search",))
    assert _mark(disp, "s1") is False


def test_gateway_deferral_counts_as_clarify_use():
    # The gateway path defers before the clarify yield is recorded — the
    # pending kind carries the fact instead.
    disp = SimpleNamespace(_current_turn_tools_yielded=())
    assert _mark(disp, "s9", deferral=True) is True
    assert _mark(disp, "s9", deferral=True) is False
