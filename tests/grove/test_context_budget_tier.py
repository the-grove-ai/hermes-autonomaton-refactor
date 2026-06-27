"""Tier-aware tool filter — web-surface-admission-fix (Option B).

Covers the (intent x tier) admission matrix under the SOLE tier gate
(``tier_rule.eligible``), the D4 mcp_* surgery, the capability-level D8
strip-detection, the crash-proof unparseable-MCP path, and the load-bearing
'no-tier == legacy, byte-for-byte' equivalence (post-condition 3: zero Sprint 29
regressions).

allow_groups is retired: the native surface is the capability registry gated by
``current_tier in tier_rule.eligible``. The strip/admission tests install a
synthetic ``_caps_index`` so the (intent, tier) matrix is hermetic and does not
drift with the live record corpus; the MCP-surgery tests reuse it so the native
floor (core) is deterministic.
"""

from __future__ import annotations

import logging

import pytest

import grove.context_budget as cb
from grove.context_budget import (
    ToolResolution,
    _mcp_server_of,
    filter_tools_by_name,
    resolve_tool_set,
    resolve_tools_for_tier,
)

# A back-compat taxonomy dict is still accepted (and ignored) by the resolver.
TAXONOMY = {"version": 1, "core": [], "domain_chunks": {}, "exploratory": []}


# Synthetic registry projection: (cap_id, disclosure, always, intents, eligible,
# native_tools). Mirrors the real record shape — core always-on at every tier;
# the web verb eligible at all tiers (the victim); x_search eligible at T3 only
# (the relocation trap); a code verb at T2/T3; an exploratory cap at T3 only.
SYNTH_CAPS = [
    ("core", "proactive", True, frozenset(), frozenset({1, 2, 3}),
        ("clarify", "memory", "terminal", "read_file", "skill_view")),
    ("web", "proactive", False,
        frozenset({"analysis", "research", "retrieval", "factual_lookup"}),
        frozenset({1, 2, 3}), ("web_search", "session_search")),
    ("xsearch", "proactive", False, frozenset({"research"}), frozenset({3}),
        ("x_search",)),
    ("code", "proactive", False, frozenset({"code_generation"}),
        frozenset({2, 3}), ("write_file", "patch", "execute_code", "search_files")),
    ("explore", "complexity", False, frozenset(), frozenset({3}),
        ("delegate_task", "browser_navigate")),
]


@pytest.fixture
def synth_caps(monkeypatch):
    monkeypatch.setattr(cb, "_caps_index", lambda: SYNTH_CAPS)
    # The co-location guard reads CO_LOCATED_TOOLS; the synthetic surface carries
    # no such pairs, so disable it to keep the matrix focused on tier gating.
    monkeypatch.setattr(cb, "_validate_co_location", lambda *a, **k: None)
    return SYNTH_CAPS


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


ALL_TOOLS = _mk(
    "clarify", "memory", "terminal", "read_file", "skill_view",
    "write_file", "patch", "execute_code", "search_files",
    "session_search", "web_search", "x_search",
    "delegate_task", "browser_navigate",
    "mcp_notion_API_post_page", "mcp_notion_API_post_search",
    "mcp_other_do_thing",
)


def _stripped_ids(res):
    return {cid for (cid, _elig) in res.stripped_capabilities}


# ── backward-compat: no-tier == legacy, byte-for-byte ──────────────────────


def test_no_tier_filter_allowed_none_returns_list_verbatim():
    weird = ALL_TOOLS + ["not-a-dict"]
    assert filter_tools_by_name(weird, None) is weird


def test_resolve_tool_set_is_registry_driven():
    # GRV-009 E5 C-RESOLVE — resolve_tool_set derives the intent-only (tier-
    # unaware) surface from the LIVE capability registry. Unchanged by Option B.
    got = resolve_tool_set("code_generation", "moderate")
    assert {"write_file", "patch", "search_files", "execute_code", "skill_manage"} <= got
    assert {"clarify", "terminal", "read_file"} <= got            # core (always)
    assert "spotify_search" not in got                            # fallback — never proactive
    assert "browser_navigate" not in got                          # complexity — not on a moderate turn
    assert resolve_tool_set("unknown", "simple") is None


# ── MCP disclose-on-match (mcp_allow) — the _partition_tools flip ──────────


def test_mcp_allow_none_is_legacy_byte_for_byte(synth_caps):
    legacy = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", current_tier=3)
    flip_off = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", mcp_allow=None, current_tier=3)
    assert _names(list(legacy.tools)) == _names(list(flip_off.tools))
    assert "mcp_notion_API_post_page" in _names(list(legacy.tools))
    assert "mcp_other_do_thing" in _names(list(legacy.tools))


def test_mcp_allow_discloses_only_matched_servers(synth_caps):
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", mcp_allow={"notion"}, current_tier=3)
    n = _names(list(res.tools))
    assert "mcp_notion_API_post_page" in n     # notion matched -> disclosed
    assert "mcp_notion_API_post_search" in n
    assert "mcp_other_do_thing" not in n        # unmatched -> withheld


def test_mcp_allow_empty_withholds_all_mcp(synth_caps):
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", mcp_allow=set(), current_tier=3)
    assert not any(x.startswith("mcp_") for x in _names(list(res.tools)))
    assert "terminal" in _names(list(res.tools))   # core rides every turn (native)


def test_mcp_allow_via_filter_tools_by_name():
    allowed = resolve_tool_set("analysis", "moderate")
    kept = filter_tools_by_name(ALL_TOOLS, allowed, mcp_allow={"notion"})
    n = _names(kept)
    assert "mcp_notion_API_post_page" in n
    assert "mcp_other_do_thing" not in n
    assert resolve_tool_set(None, None) is None


# ── the SOLE tier gate: tier_rule.eligible (Option B) ──────────────────────


def test_t3_admits_all_eligible_nothing_stripped(synth_caps):
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", current_tier=3)
    got = _names(res.tools)
    assert {"web_search", "session_search", "x_search"} <= set(got)  # all eligible@3
    assert res.stripped_capabilities == frozenset()
    assert res.excluded_mcp == frozenset()
    assert res.fallback is False


def test_victim_offered_at_eligible_tier_on_triggering_intent(synth_caps):
    # web_search (eligible [1,2,3]) is OFFERED at T2 on a research turn — the
    # orphan the dual-gate produced is closed.
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", current_tier=2)
    assert "web_search" in res.allowed_names
    assert "session_search" in res.allowed_names


def test_x_search_offered_at_t2_not_stripped(synth_caps):
    # neuter-tier-eligible-gate: x_search (record documents eligible [3]) is now
    # OFFERED at T2 on a research turn — tier no longer strips. The record's
    # eligible is documentation, not enforcement; nothing is tier-stripped.
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", current_tier=2)
    assert "x_search" in res.allowed_names
    assert "x_search" in _names(res.tools)
    assert res.stripped_capabilities == frozenset()


def test_complexity_cap_admitted_regardless_of_tier(synth_caps):
    # neuter-tier-eligible-gate: the exploratory cap (record documents eligible
    # [3]) selected on a complex turn is now ADMITTED at T2 — tier no longer
    # strips. Disclosure-mode gating (complexity) is unchanged; tier gating gone.
    res = resolve_tools_for_tier(ALL_TOOLS, "code_generation", "complex", current_tier=2)
    assert "delegate_task" in res.allowed_names
    assert res.stripped_capabilities == frozenset()


def test_t1_admits_code_cap_and_core(synth_caps):
    # neuter-tier-eligible-gate: the code verb (record documents eligible [2,3])
    # is now ADMITTED at T1 alongside core — tier no longer strips.
    res = resolve_tools_for_tier(ALL_TOOLS, "code_generation", "moderate", current_tier=1)
    got = _names(res.tools)
    assert "write_file" in got
    assert "clarify" in got
    assert res.stripped_capabilities == frozenset()


def test_none_tier_admits_all_intent_matched(synth_caps):
    # Cloud / no tier routed: the eligibility gate is bypassed (mirrors the seam).
    res = resolve_tools_for_tier(ALL_TOOLS, "research", "moderate", current_tier=None)
    assert {"web_search", "session_search", "x_search"} <= set(res.allowed_names)
    assert res.stripped_capabilities == frozenset()


def test_empty_eligible_still_offered_gate_neutered(synth_caps, monkeypatch):
    # neuter-tier-eligible-gate: a record with empty tier_rule.eligible is no
    # longer special — the tier gate is retired, so an intent-matched cap is
    # offered regardless of its (now documentary) eligible set. Nothing stripped.
    caps = SYNTH_CAPS + [
        ("orphan", "proactive", False, frozenset({"research"}), frozenset(),
            ("orphan_tool",)),
    ]
    monkeypatch.setattr(cb, "_caps_index", lambda: caps)
    res = resolve_tools_for_tier(ALL_TOOLS + _mk("orphan_tool"), "research",
                                 "moderate", current_tier=3)
    assert "orphan_tool" in res.allowed_names
    assert res.stripped_capabilities == frozenset()


# ── crash-proof unparseable MCP: admitted + recorded + logged ──────────────


def test_unparseable_mcp_admitted_recorded_and_logged(synth_caps, caplog):
    tools = ALL_TOOLS + _mk("mcp_")  # 'mcp_' with no server segment
    with caplog.at_level(logging.WARNING, logger="grove.context_budget"):
        res = resolve_tools_for_tier(tools, "code_generation", "moderate", current_tier=2)
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


# ── fallback under a tier (budget still honored; unknown strips nothing) ────


def test_unknown_intent_fallback_uncapped_on_t2(synth_caps):
    # neuter-tier-eligible-gate: the unknown maximal fallback at T2 is no longer
    # tier-capped — x_search (record documents eligible [3]) is admitted at T2
    # too. Unknown still strips nothing.
    res = resolve_tools_for_tier(ALL_TOOLS, None, None,
                                 current_tier=2)
    assert res.fallback is True
    assert res.stripped_capabilities == frozenset()         # unknown strips nothing
    assert "write_file" in res.allowed_names
    assert "x_search" in res.allowed_names                  # tier gate retired


def test_unknown_intent_fallback_full_on_t3(synth_caps):
    res = resolve_tools_for_tier(ALL_TOOLS, "unknown", "simple",
                                 current_tier=3)
    assert res.fallback is True
    got = _names(res.tools)
    assert "delegate_task" in got                           # full non-MCP registry@T3
    assert "x_search" in got


# ── result object ──────────────────────────────────────────────────────────


def test_tool_resolution_is_frozen(synth_caps):
    res = resolve_tools_for_tier(ALL_TOOLS, "analysis", "moderate", current_tier=2)
    assert isinstance(res, ToolResolution)
    with pytest.raises(Exception):
        res.fallback = True  # type: ignore[misc]
