"""Unit tests for grove.tier_budget — Sprint 73 declarative-jit-budget-v1.

Phase 1: loader + fail-loud validator only (no enforcement, no wiring). Every
malformed-budget path must raise ValueError at load (D7) — the tests below
pin each branch. All tests are hermetic: an explicit config_path (tmp file)
and an injected taxonomy dict, so neither ~/.grove nor tool_groups.yaml is
touched.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from grove.tier_budget import (
    GATEABLE_CONTEXT_BLOCKS,
    TierBudget,
    ToolBudget,
    load_tier_budgets,
)

# A minimal taxonomy injected for the allow_groups cross-check (D2). Valid
# group names derived from it: core, exploratory, + the domain_chunks keys.
TAXONOMY = {
    "version": 1,
    "core": ["clarify", "memory", "terminal", "read_file"],
    "domain_chunks": {
        "code_generation": ["write_file", "patch"],
        "debugging": ["search_files"],
        "analysis": ["session_search"],
        "system_admin": ["cronjob"],
        "retrieval": ["web_search"],
    },
    "exploratory": ["delegate_task"],
}

# Provider-backed T1/T2/T3 + a non-inference T0 (handler) that must be exempt.
BASE_TIERS = {
    "T0": {"handler": "pattern_cache"},
    "T1": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "T2": {"provider": "gemma-mac", "model": "gemma-4-12b"},
    "T3": {"provider": "anthropic", "model": "claude-opus-4-6"},
}

VALID_BUDGETS = {
    "T1": {"context": [], "tools": {"allow_groups": ["core"]}},
    "T2": {
        "context": ["goal_record"],
        "tools": {
            "allow_groups": ["core", "code_generation", "debugging", "analysis", "system_admin"],
        },
    },
    "T3": {
        "context": ["claude_contract", "goal_record", "skills_index"],
        "tools": {"allow_groups": ["*"]},
    },
}

_SENTINEL = object()


def _write_config(tmp_path, *, tiers=_SENTINEL, budgets=_SENTINEL, routing=_SENTINEL):
    """Write a routing.config.yaml into tmp_path and return its path.

    Pass ``budgets=None`` to omit the tier_budgets block entirely; pass a dict
    to substitute one. Pass ``routing=None`` to omit the routing mapping.
    """
    if routing is _SENTINEL:
        routing = {
            "schema_version": 1,
            "default_tier": "T1",
            "tier_preferences": copy.deepcopy(
                BASE_TIERS if tiers is _SENTINEL else tiers
            ),
        }
    doc = {}
    if routing is not None:
        doc["routing"] = routing
    if budgets is _SENTINEL:
        doc["tier_budgets"] = copy.deepcopy(VALID_BUDGETS)
    elif budgets is not None:
        doc["tier_budgets"] = budgets
    path = tmp_path / "routing.config.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _load(tmp_path, **kw):
    return load_tier_budgets(_write_config(tmp_path, **kw), taxonomy=TAXONOMY)


# ── happy path ───────────────────────────────────────────────────────────


def test_valid_budget_loads_all_inference_tiers(tmp_path):
    budgets = _load(tmp_path)
    assert set(budgets) == {"T1", "T2", "T3"}  # T0 (handler) exempt, absent
    assert all(isinstance(b, TierBudget) for b in budgets.values())


def test_valid_budget_values_are_typed_and_ordered(tmp_path):
    budgets = _load(tmp_path)
    t2 = budgets["T2"]
    assert t2.context == ("goal_record",)
    assert isinstance(t2.tools, ToolBudget)
    assert t2.tools.allow_groups == (
        "core", "code_generation", "debugging", "analysis", "system_admin",
    )
    # frozen dataclasses are hashable / immutable
    with pytest.raises(Exception):
        t2.context = ("x",)  # type: ignore[misc]


def test_wildcards_accepted(tmp_path):
    budgets = _load(tmp_path)
    assert budgets["T3"].tools.allow_groups == ("*",)


def test_t0_handler_tier_requires_no_budget(tmp_path):
    # BASE_TIERS includes T0 with a handler; VALID_BUDGETS omits T0. Loads fine.
    budgets = _load(tmp_path)
    assert "T0" not in budgets


# ── D7: missing budget for a provider-backed tier ──────────────────────────


def test_missing_tier_budgets_block_raises(tmp_path):
    with pytest.raises(ValueError, match="no 'tier_budgets' block"):
        _load(tmp_path, budgets=None)


def test_inference_tier_without_budget_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    del budgets["T2"]
    with pytest.raises(ValueError, match=r"T2.*no tier_budgets entry"):
        _load(tmp_path, budgets=budgets)


def test_budget_for_handler_tier_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T0"] = {"context": [], "tools": {"allow_groups": ["core"]}}
    with pytest.raises(ValueError, match=r"T0.*not a provider-backed tier"):
        _load(tmp_path, budgets=budgets)


def test_budget_for_unknown_tier_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T9"] = {"context": [], "tools": {"allow_groups": ["core"]}}
    with pytest.raises(ValueError, match=r"T9.*not a provider-backed tier"):
        _load(tmp_path, budgets=budgets)


# ── D5: unknown / malformed context block ──────────────────────────────────


def test_unknown_context_block_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["context"] = ["goal_record", "kitchen_sink"]
    with pytest.raises(ValueError, match=r"unknown block 'kitchen_sink'"):
        _load(tmp_path, budgets=budgets)


def test_context_must_be_a_list(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["context"] = "goal_record"
    with pytest.raises(ValueError, match="context must be a list"):
        _load(tmp_path, budgets=budgets)


def test_context_required(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    del budgets["T2"]["context"]
    with pytest.raises(ValueError, match="context is required"):
        _load(tmp_path, budgets=budgets)


def test_gateable_blocks_constant_is_the_d5_set():
    assert GATEABLE_CONTEXT_BLOCKS == frozenset(
        {"claude_contract", "goal_record", "skills_index"}
    )


# ── D2: unknown / malformed tool group ─────────────────────────────────────


def test_unknown_allow_group_raises_the_file_ops_defect(tmp_path):
    # The exact defect caught at the gate: file_ops/terminal are not groups.
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["tools"]["allow_groups"] = ["core", "file_ops", "terminal"]
    with pytest.raises(ValueError, match=r"unknown group 'file_ops'"):
        _load(tmp_path, budgets=budgets)


def test_allow_groups_required(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    del budgets["T2"]["tools"]["allow_groups"]
    with pytest.raises(ValueError, match="tools.allow_groups is required"):
        _load(tmp_path, budgets=budgets)


# GRV-009 E4 C4 — test_exclude_mcp_required retired: exclude_mcp is no longer a
# tier_budget field (MCP exposure is governed by the kind=mcp Capability records).


def test_allow_groups_must_be_a_list(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["tools"]["allow_groups"] = "core"
    with pytest.raises(ValueError, match="tools.allow_groups must be a list"):
        _load(tmp_path, budgets=budgets)


def test_non_string_group_entry_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["tools"]["allow_groups"] = ["core", 7]
    with pytest.raises(ValueError, match="entries must be strings"):
        _load(tmp_path, budgets=budgets)


def test_tools_must_be_a_mapping(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["tools"] = ["core"]
    with pytest.raises(ValueError, match="tools must be"):
        _load(tmp_path, budgets=budgets)


def test_tier_entry_must_be_a_mapping(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"] = ["not", "a", "mapping"]
    with pytest.raises(ValueError, match=r"tier_budgets\['T2'\] must be a mapping"):
        _load(tmp_path, budgets=budgets)


# ── structural: routing / tier_budgets shape ───────────────────────────────


def test_tier_budgets_not_a_mapping_raises(tmp_path):
    with pytest.raises(ValueError, match="'tier_budgets' must be a mapping"):
        _load(tmp_path, budgets=["T1", "T2"])


def test_missing_routing_mapping_raises(tmp_path):
    with pytest.raises(ValueError, match="no 'routing' mapping"):
        _load(tmp_path, routing=None)


def test_empty_tier_preferences_raises(tmp_path):
    routing = {"schema_version": 1, "default_tier": "T1", "tier_preferences": {}}
    with pytest.raises(ValueError, match="tier_preferences' is missing or empty"):
        _load(tmp_path, routing=routing)


def test_config_not_a_mapping_raises(tmp_path):
    path = tmp_path / "routing.config.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is not a mapping"):
        load_tier_budgets(path, taxonomy=TAXONOMY)


# ── taxonomy cross-check uses the real taxonomy when not injected ──────────


def test_taxonomy_loaded_from_path_when_not_injected(tmp_path):
    # Write a tool_groups.yaml and let the loader read it (no taxonomy kwarg).
    tax_path = tmp_path / "tool_groups.yaml"
    tax_path.write_text(yaml.safe_dump(TAXONOMY), encoding="utf-8")
    cfg = _write_config(tmp_path)
    budgets = load_tier_budgets(cfg, taxonomy_path=tax_path)
    assert set(budgets) == {"T1", "T2", "T3"}
