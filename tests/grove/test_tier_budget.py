"""Unit tests for grove.tier_budget — declarative-jit-budget-v1.

Loader + fail-loud validator (D7). The budget governs gateable CONTEXT blocks
and the optional prefill ceiling only; per-tier TOOL exposure is retired from the
budget (web-surface-admission-fix, Option B — tier_rule.eligible on each
Capability record is the sole tool gate). A leftover ``tools:`` block in a config
is silently ignored. Every malformed context/structure path must raise ValueError
at load — the tests below pin each branch. All tests are hermetic: an explicit
config_path (tmp file), no ~/.grove read.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from grove.tier_budget import (
    GATEABLE_CONTEXT_BLOCKS,
    TierBudget,
    load_tier_budgets,
)

# Provider-backed T1/T2/T3 + a non-inference T0 (handler) that must be exempt.
BASE_TIERS = {
    "T0": {"handler": "pattern_cache"},
    "T1": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "T2": {"provider": "gemma-mac", "model": "gemma-4-12b"},
    "T3": {"provider": "anthropic", "model": "claude-opus-4-6"},
}

# K6 (A-goalrec-tests ruling) — representative gateable block swapped
# goal_record -> skills_index after goal_record left GATEABLE_CONTEXT_BLOCKS.
VALID_BUDGETS = {
    "T1": {"context": []},
    "T2": {"context": ["skills_index"]},
    "T3": {"context": ["claude_contract", "skills_index"]},
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
    return load_tier_budgets(_write_config(tmp_path, **kw))


# ── happy path ───────────────────────────────────────────────────────────


def test_valid_budget_loads_all_inference_tiers(tmp_path):
    budgets = _load(tmp_path)
    assert set(budgets) == {"T1", "T2", "T3"}  # T0 (handler) exempt, absent
    assert all(isinstance(b, TierBudget) for b in budgets.values())


def test_valid_budget_values_are_typed_and_ordered(tmp_path):
    budgets = _load(tmp_path)
    t2 = budgets["T2"]
    assert t2.context == ("skills_index",)
    assert t2.prefill_ceiling_tokens is None
    # frozen dataclasses are immutable
    with pytest.raises(Exception):
        t2.context = ("x",)  # type: ignore[misc]


def test_leftover_tools_block_is_silently_ignored(tmp_path):
    # A stale tools.allow_groups key in an old/sovereign config must not crash —
    # the budget no longer reads it (Option B); only context is governed.
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"] = {"context": ["skills_index"], "tools": {"allow_groups": ["core"]}}
    loaded = _load(tmp_path, budgets=budgets)
    assert loaded["T2"].context == ("skills_index",)


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
    budgets["T0"] = {"context": []}
    with pytest.raises(ValueError, match=r"T0.*not a provider-backed tier"):
        _load(tmp_path, budgets=budgets)


def test_budget_for_unknown_tier_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T9"] = {"context": []}
    with pytest.raises(ValueError, match=r"T9.*not a provider-backed tier"):
        _load(tmp_path, budgets=budgets)


# ── D5: unknown / malformed context block ──────────────────────────────────


def test_unknown_context_block_raises(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["context"] = ["skills_index", "kitchen_sink"]
    with pytest.raises(ValueError, match=r"unknown block 'kitchen_sink'"):
        _load(tmp_path, budgets=budgets)


def test_context_must_be_a_list(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"]["context"] = "skills_index"
    with pytest.raises(ValueError, match="context must be a list"):
        _load(tmp_path, budgets=budgets)


def test_context_required(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    del budgets["T2"]["context"]
    with pytest.raises(ValueError, match="context is required"):
        _load(tmp_path, budgets=budgets)


def test_gateable_blocks_constant_is_the_d5_set():
    # K6 (A-pin ruling) — cellar_context joins the D5 set (SPEC post-condition 1).
    # skill-adoption-v1 C2 — skill_payload joins as the fourth gateable block.
    assert GATEABLE_CONTEXT_BLOCKS == frozenset(
        {"claude_contract", "skills_index", "cellar_context", "skill_payload"}
    )


# ── structural: routing / tier_budgets shape ───────────────────────────────


def test_tier_entry_must_be_a_mapping(tmp_path):
    budgets = copy.deepcopy(VALID_BUDGETS)
    budgets["T2"] = ["not", "a", "mapping"]
    with pytest.raises(ValueError, match=r"tier_budgets\['T2'\] must be a mapping"):
        _load(tmp_path, budgets=budgets)


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
        load_tier_budgets(path)
