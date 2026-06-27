"""Tool-filter consolidation + D8 strip provenance — web-surface-admission-fix.

Covers: the no-tier (cloud) path reproduces the tier-unaware Sprint 29 surface
byte-for-byte, and _maybe_apply_tool_filter's tier-aware behavior + the
stripped_capabilities provenance the generator's D8 escalation fires on (Option
B — the SOLE tier gate is tier_rule.eligible; allow_groups retired). These agent
tests run against the LIVE record corpus: web_search/session_search are eligible
at every tier; x_search is eligible at T3 only.
"""

from __future__ import annotations

import pytest

from grove.context_budget import (
    filter_tools_by_name,
    resolve_tool_set,
    resolve_tools_for_tier,
)
from grove.tier_budget import TierBudget

TAXONOMY = {"version": 1, "core": [], "domain_chunks": {}, "exploratory": []}

# A budget carrier with no tool key (tools retired) — its presence is what makes
# the turn "budgeted"; the routed tier (current_tier) does the gating.
T2 = TierBudget(context=("goal_record",))


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


def _stripped_ids(res):
    return {cid for (cid, _elig) in res.stripped_capabilities}


ALL_TOOLS = _mk(
    "clarify", "memory", "terminal", "read_file", "skill_view",
    "write_file", "patch", "execute_code", "search_files",
    "session_search", "web_search", "x_search", "delegate_task",
    "mcp_notion_API_post_page", "mcp_other_do_thing",
)


# ── the consolidation: no-tier (cloud) == tier-unaware Sprint 29 ────────────


@pytest.mark.parametrize(
    "intent,complexity",
    [
        ("code_generation", "moderate"),
        ("analysis", "complex"),
        ("retrieval", "simple"),
        ("unknown", "simple"),
        (None, None),
    ],
)
def test_no_tier_reproduces_tier_unaware(intent, complexity):
    legacy = filter_tools_by_name(
        ALL_TOOLS, resolve_tool_set(intent, complexity, TAXONOMY)
    )
    res = resolve_tools_for_tier(
        ALL_TOOLS, intent, complexity, TAXONOMY, None, current_tier=None
    )
    assert _names(res.tools) == _names(legacy)
    assert res.stripped_capabilities == frozenset()   # no tier -> nothing stripped
    assert res.excluded_mcp == frozenset()


# ── _maybe_apply_tool_filter consolidation (bare AIAgent) ──────────────────


def _bare_agent(tools, tier_budget=None):
    import run_agent
    a = object.__new__(run_agent.AIAgent)
    a.tools = tools
    a._tools_for_turn = None
    a._last_tool_selection = None
    a._tool_resolution = None
    if tier_budget is not None:
        a._tier_budget = tier_budget
    return a


def _setup(monkeypatch, intent, complexity, tier="T2"):
    from grove import providers as pmod
    from grove.classify import ClassificationResult
    cls = ClassificationResult(
        intent_class=intent,
        pattern_hash="h",
        confidence=0.9,
        register_class="technical",
        complexity_signal=complexity,
        goal_alignment="direct",
    )
    monkeypatch.setattr(pmod, "_last_classification", cls)
    monkeypatch.setattr(pmod, "_last_routed_tier", tier)
    monkeypatch.setattr("grove.context_budget.load_taxonomy", lambda *a, **k: TAXONOMY)


# ── Sprint 74 Phase 3: disclosure tier gate ────────────────────────────────


def test_disclosure_gate_engages_on_t2(monkeypatch):
    # T2 routes through the disclosure reduction (index + pull replaces eager).
    _setup(monkeypatch, "code_generation", "moderate", tier="T2")
    agent = _bare_agent(ALL_TOOLS)
    called = []
    monkeypatch.setattr(
        agent, "_apply_disclosure",
        lambda res, intent_class=None: called.append(1) or ["sentinel"]
    )
    agent._maybe_apply_tool_filter()
    assert called, "T2 turn must route through _apply_disclosure"


def test_disclosure_gate_t1_eager_core_no_pull(monkeypatch):
    # D3: T1 stays eager-core — never reduces, never offers the pull tools.
    _setup(monkeypatch, "code_generation", "moderate", tier="T1")
    agent = _bare_agent(ALL_TOOLS)
    monkeypatch.setattr(
        agent, "_apply_disclosure",
        lambda res: (_ for _ in ()).throw(AssertionError("T1 must not disclose")),
    )
    agent._maybe_apply_tool_filter()
    names = set(_names(agent._tools_for_api or []))
    assert "read_tool_schema" not in names
    assert "read_goal_context" not in names
    assert agent._disclosure_manifest is None


def test_no_budget_native_matches_legacy_mcp_disclose_on_match(monkeypatch):
    # Sprint 74: the no-budget path still matches Sprint 29 legacy for NATIVE
    # tools; MCPs disclose on registry trigger match instead of passing through.
    # tool-admission-simplification-v1 B2: notion_read carries trigger.always:true,
    # so the notion server discloses every turn (server-level gating) even with no
    # notion keyword; 'other' has no kind=mcp record and stays withheld.
    _setup(monkeypatch, "code_generation", "moderate")
    agent = _bare_agent(ALL_TOOLS)            # no _tier_budget
    agent._maybe_apply_tool_filter()
    got = _names(agent._tools_for_api)
    legacy_native = [
        n
        for n in _names(
            filter_tools_by_name(
                ALL_TOOLS, resolve_tool_set("code_generation", "moderate", TAXONOMY)
            )
        )
        if not n.startswith("mcp_")
    ]
    assert [n for n in got if not n.startswith("mcp_")] == legacy_native
    assert "mcp_notion_API_post_page" in got            # notion always-on -> disclosed
    assert "mcp_other_do_thing" not in got              # no record -> withheld
    assert agent._last_tool_selection["fallback"] is False
    assert agent._last_tool_selection["stripped_capabilities"] == []  # no tier strip
    assert agent._tool_resolution is not None


def test_no_budget_unknown_intent_full_registry(monkeypatch):
    _setup(monkeypatch, "unknown", "simple")
    agent = _bare_agent(ALL_TOOLS)
    agent._maybe_apply_tool_filter()
    assert agent._tools_for_turn is None             # full-registry signal
    assert agent._tools_for_api is ALL_TOOLS
    assert agent._last_tool_selection["fallback"] is True


def test_budgeted_serves_intent_and_excludes_mcp(monkeypatch):
    # code_generation @ T2: every code_generation cap is eligible at T2 (eligible
    # [2,3]) so the intent is fully served and nothing is stripped.
    _setup(monkeypatch, "code_generation", "moderate")
    agent = _bare_agent(ALL_TOOLS, tier_budget=T2)
    agent._maybe_apply_tool_filter()
    got = _names(agent._tools_for_api)
    assert "write_file" in got                       # code_generation eligible@T2
    # tool-admission-simplification-v1 B2: notion_read carries trigger.always:true
    # and the tier-eligible ceiling is neutered, so the notion server discloses at
    # T2 regardless of intent/keyword; 'other' has no record and stays withheld.
    assert "mcp_notion_API_post_page" in got            # notion always-on -> disclosed
    assert "mcp_other_do_thing" not in got              # no record -> withheld
    sel = agent._last_tool_selection
    assert sel["excluded_mcp"] == []
    assert sel["stripped_capabilities"] == []         # code caps all eligible@T2
    assert sel["tier"] == "T2"


def test_research_at_t2_strips_nothing_no_d8(monkeypatch):
    # research-tier-widen: after widening x_search [3]->[1,2,3], a research turn at
    # T2 strips NOTHING — stripped_capabilities is empty, so the generator's D8
    # block raises no EscalationRequest (the eager escalation is dissolved at the
    # root). web_search/session_search/x_search are all served at T2.
    _setup(monkeypatch, "research", "simple")
    agent = _bare_agent(ALL_TOOLS, tier_budget=T2)
    agent._maybe_apply_tool_filter()
    assert agent._tool_resolution.stripped_capabilities == frozenset()
    assert agent._last_tool_selection["stripped_capabilities"] == []
    assert "web_search" in agent._tool_resolution.allowed_names
    assert "session_search" in agent._tool_resolution.allowed_names
    assert "x_search" in agent._tool_resolution.allowed_names
    assert agent._last_tool_selection["tier"] == "T2"


def test_empty_tools_is_noop(monkeypatch):
    _setup(monkeypatch, "code_generation", "moderate")
    agent = _bare_agent([])
    agent._maybe_apply_tool_filter()
    assert agent._tool_resolution is None
    assert agent._tools_for_turn is None
    assert agent._last_tool_selection is None


# ── Sprint 75 Phase 2: T1 param-scopes terminal (eager, not pulled) ────────


def _terminal_def():
    from tools.terminal_tool import TERMINAL_SCHEMA
    return {"type": "function", "function": {
        "name": "terminal",
        "description": TERMINAL_SCHEMA["description"],
        "parameters": TERMINAL_SCHEMA["parameters"],
    }}


def test_t1_param_scopes_terminal_to_command_and_workdir(monkeypatch):
    _setup(monkeypatch, "code_generation", "moderate", tier="T1")
    agent = _bare_agent([_mk("clarify")[0], _terminal_def()])
    agent._maybe_apply_tool_filter()
    api = {t["function"]["name"]: t for t in (agent._tools_for_api or [])}
    assert "terminal" in api                         # eager, directly callable
    props = api["terminal"]["function"]["parameters"]["properties"]
    assert set(props) == {"command", "workdir"}       # async params dropped on T1
    assert "background=true" not in api["terminal"]["function"]["description"]


def test_t2_leaves_terminal_full(monkeypatch):
    # T2 keeps terminal full (no param-scope) — the async machinery is available.
    _setup(monkeypatch, "code_generation", "moderate", tier="T2")
    agent = _bare_agent([_mk("clarify")[0], _terminal_def()])
    agent._maybe_apply_tool_filter()
    api = {t["function"]["name"]: t for t in (agent._tools_for_api or [])}
    assert "terminal" in api
    assert "background" in api["terminal"]["function"]["parameters"]["properties"]
