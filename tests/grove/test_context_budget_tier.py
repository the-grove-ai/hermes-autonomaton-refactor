"""Tier-aware tool filter — Sprint 73 declarative-jit-budget-v1 (Phase 2).

Covers the (intent x tier) matrix, the D4 mcp_* surgery, R1 intersection
semantics, D8 strip-detection, the crash-proof unparseable-MCP path, and the
load-bearing 'no-tier == legacy, byte-for-byte' equivalence (post-condition 3:
zero Sprint 29 regressions). The existing Sprint 29 suite
(tests/grove/test_context_budget.py) is the regression gate and is run
alongside this file.
"""

from __future__ import annotations

import logging

import pytest

from grove.context_budget import (
    ToolResolution,
    _mcp_server_of,
    filter_tools_by_name,
    resolve_tool_set,
    resolve_tools_for_tier,
)
from grove.tier_budget import TierBudget, ToolBudget

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


def _tb(allow, context=()):
    return TierBudget(
        context=tuple(context),
        tools=ToolBudget(allow_groups=tuple(allow)),
    )


# GRV-009 E4 C4 — exclude_mcp retired; MCP exposure is governed by the registry
# (kind=mcp records), not the tier budget. These low-level budgets exercise the
# native group cap + the registry-driven ``mcp_allow`` flip in _partition_tools.
T3 = _tb(["*"])  # apex — no group cap
T2 = _tb(["core", "code_generation", "debugging", "analysis"])
T1 = _tb(["core"])  # floor — core only


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


ALL_TOOLS = _mk(
    "clarify", "memory", "terminal", "read_file", "skill_view",
    "write_file", "patch", "execute_code", "search_files",
    "session_search", "web_search",
    "delegate_task", "browser_navigate",
    "mcp_notion_API_post_page", "mcp_notion_API_post_search",
    "mcp_other_do_thing",
)


# ── backward-compat: no-tier == legacy, byte-for-byte ──────────────────────


def test_no_tier_filter_equals_legacy_with_name_set():
    allowed = resolve_tool_set("code_generation", "moderate", TAXONOMY)
    legacy = filter_tools_by_name(ALL_TOOLS, allowed)
    same = filter_tools_by_name(ALL_TOOLS, allowed, tier_budget=None)
    assert _names(legacy) == _names(same)
    # legacy keeps every MCP (the old unconditional passthrough)
    assert "mcp_notion_API_post_page" in _names(legacy)
    assert "mcp_other_do_thing" in _names(legacy)


def test_no_tier_filter_allowed_none_returns_list_verbatim():
    # The legacy fast-path returns the SAME object, incl. any non-dict entries.
    weird = ALL_TOOLS + ["not-a-dict"]
    assert filter_tools_by_name(weird, None) is weird


def test_resolve_tool_set_is_registry_driven():
    # GRV-009 E5 C-RESOLVE — resolve_tool_set derives the intent-only (tier-
    # unaware) surface from the capability registry, not the passed taxonomy
    # materialization. The intent selects proactive-intent records + core; a
    # moderate turn excludes complexity records; fallback records never appear.
    got = resolve_tool_set("code_generation", "moderate", TAXONOMY)
    assert {"write_file", "patch", "search_files", "execute_code", "skill_manage"} <= got
    assert {"clarify", "terminal", "read_file"} <= got            # core (always)
    assert "spotify_search" not in got                            # fallback — never proactive
    assert "browser_navigate" not in got                          # complexity — not on a moderate turn
    assert resolve_tool_set("unknown", "simple", TAXONOMY) is None


# ── MCP disclose-on-match (mcp_allow) — the _partition_tools flip ──────────
#
# mcp_allow=None  -> no mcp records / legacy: every MCP admitted (flip OFF).
# mcp_allow=<set> -> flip ON: an MCP server is admitted only when its server is
#                    in the set (eligible-on-tier AND trigger-matched, computed
#                    by run_agent._compute_mcp_allow). GRV-009 E4 C4: the
#                    exclude_mcp ceiling is retired — the set is the sole gate.


def test_mcp_allow_none_is_legacy_byte_for_byte():
    # No mcp_allow argument == today's behavior: all MCP pass on an all-MCP tier.
    legacy = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", TAXONOMY, T3)
    flip_off = resolve_tools_for_tier(
        ALL_TOOLS, "research", "moderate", TAXONOMY, T3, mcp_allow=None
    )
    assert _names(list(legacy.tools)) == _names(list(flip_off.tools))
    assert "mcp_notion_API_post_page" in _names(list(legacy.tools))
    assert "mcp_other_do_thing" in _names(list(legacy.tools))


def test_mcp_allow_discloses_only_matched_servers():
    res = resolve_tools_for_tier(
        ALL_TOOLS, "research", "moderate", TAXONOMY, T3, mcp_allow={"notion"}
    )
    n = _names(list(res.tools))
    assert "mcp_notion_API_post_page" in n     # notion matched -> disclosed
    assert "mcp_notion_API_post_search" in n
    assert "mcp_other_do_thing" not in n        # unmatched -> withheld


def test_mcp_allow_empty_withholds_all_mcp():
    res = resolve_tools_for_tier(
        ALL_TOOLS, "research", "moderate", TAXONOMY, T3, mcp_allow=set()
    )
    assert not any(x.startswith("mcp_") for x in _names(list(res.tools)))
    # native tools are untouched by the MCP flip (core rides every turn)
    assert "terminal" in _names(list(res.tools))


# GRV-009 E4 C4 — test_exclude_mcp_is_hard_ceiling_over_match retired: the
# exclude_mcp ceiling is gone. The tier ceiling now lives in tier_rule.eligible
# and is folded into the mcp_allow set by run_agent._compute_mcp_allow (covered
# by tests/grove/test_mcp_gating_parity.py).


def test_mcp_allow_via_filter_tools_by_name():
    # The public name-filter surface also honors mcp_allow.
    allowed = resolve_tool_set("analysis", "moderate", TAXONOMY)
    kept = filter_tools_by_name(ALL_TOOLS, allowed, tier_budget=T3, mcp_allow={"notion"})
    n = _names(kept)
    assert "mcp_notion_API_post_page" in n
    assert "mcp_other_do_thing" not in n
    assert resolve_tool_set(None, None, TAXONOMY) is None


# ── T3 wildcard == legacy Sprint 29 (the 'T3 unchanged' DoD) ───────────────


def test_t3_wildcard_equals_legacy_sprint29():
    legacy = filter_tools_by_name(
        ALL_TOOLS, resolve_tool_set("code_generation", "moderate", TAXONOMY)
    )
    res = resolve_tools_for_tier(
        ALL_TOOLS, "code_generation", "moderate", TAXONOMY, T3
    )
    assert _names(res.tools) == _names(legacy)
    assert res.stripped_groups == frozenset()
    assert res.excluded_mcp == frozenset()
    assert res.fallback is False


# ── R1 intersection (native group cap) ─────────────────────────────────────
# GRV-009 E4 C4 — test_t2_caps_intent_and_excludes_notion_keeps_other_mcp
# retired: the exclude_mcp ceiling that withheld notion on T2 is gone. T2 MCP
# exposure (notion withheld; T2 not in tier_rule.eligible) is now proven in
# tests/grove/test_mcp_gating_parity.py. The native group cap is exercised by
# the remaining tests in this file.


# ── D8 strip-detection: intent group the tier forbids ──────────────────────


def test_strip_detection_intent_group_not_in_allow():
    # retrieval is not in T2.allow_groups → its group is stripped (D8 signal).
    res = resolve_tools_for_tier(ALL_TOOLS, "retrieval", "simple", TAXONOMY, T2)
    assert res.stripped_groups == frozenset({"retrieval"})
    # retrieval-only tools are not materialized; only core survives.
    assert "web_search" not in res.allowed_names
    assert "session_search" not in res.allowed_names
    assert "clarify" in res.allowed_names


def test_complex_turn_strips_exploratory_on_t2():
    res = resolve_tools_for_tier(
        ALL_TOOLS, "code_generation", "complex", TAXONOMY, T2
    )
    assert res.stripped_groups == frozenset({"exploratory"})
    assert "delegate_task" not in res.allowed_names
    assert "delegate_task" not in _names(res.tools)


def test_t1_caps_to_core_strips_domain():
    # GRV-009 E4 C4 — the MCP assertions moved to test_mcp_gating_parity.py (T1
    # MCP-free is now registry-enforced, not the exclude_mcp ["*"] ceiling).
    res = resolve_tools_for_tier(
        ALL_TOOLS, "code_generation", "moderate", TAXONOMY, T1
    )
    got = _names(res.tools)
    assert res.stripped_groups == frozenset({"code_generation"})
    assert "write_file" not in got                          # domain stripped
    assert "clarify" in got                                 # core survives


# ── crash-proof unparseable MCP: admitted + recorded + logged ──────────────


def test_unparseable_mcp_admitted_recorded_and_logged(caplog):
    tools = ALL_TOOLS + _mk("mcp_")  # 'mcp_' with no server segment
    with caplog.at_level(logging.WARNING, logger="grove.context_budget"):
        res = resolve_tools_for_tier(tools, "code_generation", "moderate", TAXONOMY, T2)
    assert "mcp_" in res.unparseable_mcp                    # surfaced in provenance
    assert "mcp_" in _names(res.tools)                      # admitted, not silently dropped
    assert any("unparseable" in r.message.lower() for r in caplog.records)


def test_mcp_server_of_is_crash_proof():
    assert _mcp_server_of("mcp_notion_API_post_page") == "notion"
    assert _mcp_server_of("mcp__notion__search") == "notion"
    assert _mcp_server_of("mcp_notion") == "notion"
    assert _mcp_server_of("mcp_") is None
    assert _mcp_server_of("notion_search") is None
    assert _mcp_server_of("") is None
    assert _mcp_server_of(None) is None        # non-str — never raises
    assert _mcp_server_of(12345) is None       # non-str — never raises


# ── fallback under a tier budget (budget still honored) ────────────────────


def test_unknown_intent_fallback_capped_on_t2():
    res = resolve_tools_for_tier(ALL_TOOLS, None, None, TAXONOMY, T2)
    assert res.fallback is True
    assert res.stripped_groups == frozenset()
    assert "write_file" in res.allowed_names                # allow_groups materialized
    assert "delegate_task" not in res.allowed_names         # exploratory not allowed
    # GRV-009 E4 C4 — MCP gating moved to the registry (mcp_allow); at this
    # low-level call with mcp_allow=None nothing gates MCP. See test_mcp_gating_parity.


def test_unknown_intent_fallback_full_on_t3():
    res = resolve_tools_for_tier(ALL_TOOLS, "unknown", "simple", TAXONOMY, T3)
    assert res.fallback is True
    got = _names(res.tools)
    assert "delegate_task" in got                           # full non-MCP registry
    assert "mcp_notion_API_post_page" in got                # all MCP (exclude [])


# ── result object ──────────────────────────────────────────────────────────


def test_tool_resolution_is_frozen():
    res = resolve_tools_for_tier(ALL_TOOLS, "analysis", "moderate", TAXONOMY, T2)
    assert isinstance(res, ToolResolution)
    with pytest.raises(Exception):
        res.fallback = True  # type: ignore[misc]
