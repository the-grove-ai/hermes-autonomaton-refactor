"""Tool-filter consolidation + D8 strip provenance — Sprint 73 (Phase 4b).

Covers: the permissive budget reproduces legacy Sprint 29 byte-for-byte (the
consolidation onto one resolution surface), and _maybe_apply_tool_filter's
tier-aware behavior + the stripped_groups provenance the generator's D8
escalation fires on. The live strip→escalate→ledger flow is Phase 6; the
escalation-event enrichment is exercised by the dispatcher escalation suite.
"""

from __future__ import annotations

import pytest

from grove.context_budget import (
    filter_tools_by_name,
    resolve_tool_set,
    resolve_tools_for_tier,
)
from grove.tier_budget import PERMISSIVE_TIER_BUDGET, TierBudget, ToolBudget

TAXONOMY = {
    "version": 1,
    "core": ["clarify", "memory", "terminal", "read_file", "skill_view"],
    "domain_chunks": {
        "code_generation": ["write_file", "patch", "search_files", "terminal", "execute_code"],
        "debugging": ["search_files", "terminal", "process"],
        "analysis": ["search_files", "session_search", "web_search"],
        "retrieval": ["session_search", "web_search"],
    },
    "exploratory": ["delegate_task", "browser_navigate"],
}

T2 = TierBudget(
    context=("goal_record",),
    tools=ToolBudget(
        allow_groups=("core", "code_generation", "debugging", "analysis"),
        exclude_mcp=("notion",),
    ),
)


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


ALL_TOOLS = _mk(
    "clarify", "memory", "terminal", "read_file", "skill_view",
    "write_file", "patch", "execute_code", "search_files",
    "session_search", "web_search", "delegate_task",
    "mcp_notion_API_post_page", "mcp_other_do_thing",
)


# ── the consolidation: permissive == legacy Sprint 29 ──────────────────────


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
def test_permissive_budget_reproduces_legacy(intent, complexity):
    legacy = filter_tools_by_name(
        ALL_TOOLS, resolve_tool_set(intent, complexity, TAXONOMY)
    )
    res = resolve_tools_for_tier(
        ALL_TOOLS, intent, complexity, TAXONOMY, PERMISSIVE_TIER_BUDGET
    )
    assert _names(res.tools) == _names(legacy)
    assert res.stripped_groups == frozenset()    # permissive never strips
    assert res.excluded_mcp == frozenset()        # permissive excludes no MCP


def test_permissive_budget_is_wildcard():
    assert PERMISSIVE_TIER_BUDGET.tools.allow_groups == ("*",)
    assert PERMISSIVE_TIER_BUDGET.tools.exclude_mcp == ()
    assert PERMISSIVE_TIER_BUDGET.context == ()


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
        agent, "_apply_disclosure", lambda res: called.append(1) or ["sentinel"]
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
    # tools; MCPs now disclose on manifest match instead of passing through.
    # This code_generation turn (no notion keyword) matches no MCP -> withheld.
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
    assert not any(n.startswith("mcp_") for n in got)   # no MCP matched -> withheld
    assert agent._last_tool_selection["fallback"] is False
    assert agent._last_tool_selection["stripped_groups"] == []   # permissive
    assert agent._tool_resolution is not None


def test_no_budget_unknown_intent_full_registry(monkeypatch):
    _setup(monkeypatch, "unknown", "simple")
    agent = _bare_agent(ALL_TOOLS)
    agent._maybe_apply_tool_filter()
    assert agent._tools_for_turn is None             # full-registry signal
    assert agent._tools_for_api is ALL_TOOLS
    assert agent._last_tool_selection["fallback"] is True


def test_budgeted_caps_and_excludes_mcp(monkeypatch):
    _setup(monkeypatch, "code_generation", "moderate")
    agent = _bare_agent(ALL_TOOLS, tier_budget=T2)
    agent._maybe_apply_tool_filter()
    got = _names(agent._tools_for_api)
    assert "write_file" in got                       # code_generation allowed
    assert "mcp_notion_API_post_page" not in got      # notion: tier exclude ceiling
    # Sprint 74: 'other' has no manifest unit and nothing matched this turn —
    # disclose-on-match withholds it (distinct from the tier-exclude ceiling).
    assert "mcp_other_do_thing" not in got
    sel = agent._last_tool_selection
    # Provenance still distinguishes the TIER exclusion (notion) from match-
    # withholding (other): only tier-excluded servers land in excluded_mcp.
    assert sel["excluded_mcp"] == ["notion"]
    assert sel["stripped_groups"] == []               # code_generation in allow
    assert sel["tier"] == "T2"


def test_budgeted_strip_surfaces_for_d8(monkeypatch):
    # retrieval is not in T2.allow_groups → its group is stripped; the
    # generator reads _tool_resolution.stripped_groups to fire the escalation.
    _setup(monkeypatch, "retrieval", "simple")
    agent = _bare_agent(ALL_TOOLS, tier_budget=T2)
    agent._maybe_apply_tool_filter()
    assert agent._tool_resolution.stripped_groups == frozenset({"retrieval"})
    assert agent._last_tool_selection["stripped_groups"] == ["retrieval"]
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
