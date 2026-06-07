"""Tests for grove.context_budget — Sprint 29 context-budget-optimization-v1.

Covers the tool-group taxonomy loader (schema validation, caching,
runtime/repo path resolution), the per-turn ``resolve_tool_set``
logic (core + reads always, domain chunks per intent, exploratory
gating, write-intent gating, unknown→None fallback), and the
``filter_tools_by_name`` pass-through / name-match behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
import yaml

from grove import context_budget as _cb_mod
from grove.context_budget import (
    filter_tools_by_name,
    load_taxonomy,
    reset_taxonomy_cache,
    resolve_tool_set,
)


# ── Test helpers ──────────────────────────────────────────────────────────


def _minimal_taxonomy() -> dict:
    return {
        "version": 1,
        "core": ["clarify", "read_file", "memory"],
        "domain_chunks": {
            "code_generation": ["write_file", "patch"],
            "planning": ["search_files", "web_search"],
            "conversation": [],
        },
        "exploratory": ["delegate_task", "browser_navigate"],
    }


def _write_taxonomy(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "tool_groups.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": ""}}


# ── load_taxonomy — repo template ─────────────────────────────────────────


class TestLoadTaxonomyRepoTemplate:
    """The repo's config/tool_groups.yaml is the shipped baseline. The
    loader must accept it as-is and pass every structural check."""

    def test_repo_template_loads_clean(self):
        # The repo template is the shipped baseline — must always load.
        repo_yaml = (
            Path(__file__).resolve().parents[2]
            / "config" / "tool_groups.yaml"
        )
        taxonomy = load_taxonomy(path=repo_yaml)
        assert taxonomy["version"] == 1
        assert "clarify" in taxonomy["core"]
        assert "code_generation" in taxonomy["domain_chunks"]
        # Sprint 69 retired the mcp_notion taxonomy block — MCP tools now
        # pass the per-turn filter generically, so the key is gone.
        assert "mcp_notion" not in taxonomy


# ── load_taxonomy — schema validation ─────────────────────────────────────


class TestSchemaValidation:
    def test_missing_top_level_key_raises(self, tmp_path: Path):
        bad = _minimal_taxonomy()
        del bad["exploratory"]
        p = _write_taxonomy(tmp_path, bad)
        with pytest.raises(ValueError, match="missing required keys"):
            load_taxonomy(path=p)

    def test_unsupported_version_raises(self, tmp_path: Path):
        bad = _minimal_taxonomy()
        bad["version"] = 99
        p = _write_taxonomy(tmp_path, bad)
        with pytest.raises(ValueError, match="unsupported schema_version"):
            load_taxonomy(path=p)

    def test_core_must_be_list(self, tmp_path: Path):
        bad = _minimal_taxonomy()
        bad["core"] = "not a list"
        p = _write_taxonomy(tmp_path, bad)
        with pytest.raises(ValueError, match="core must be a list"):
            load_taxonomy(path=p)

    def test_domain_chunk_must_be_list(self, tmp_path: Path):
        bad = _minimal_taxonomy()
        bad["domain_chunks"]["analysis"] = "not a list"
        p = _write_taxonomy(tmp_path, bad)
        with pytest.raises(ValueError, match="domain_chunks"):
            load_taxonomy(path=p)

    def test_non_mapping_root_raises(self, tmp_path: Path):
        p = tmp_path / "tool_groups.yaml"
        p.write_text("- this\n- is\n- a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="is not a mapping"):
            load_taxonomy(path=p)


# ── load_taxonomy — caching ───────────────────────────────────────────────


class TestTaxonomyCache:
    def test_cache_returns_same_instance_on_default_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        # Redirect _resolve_taxonomy_path at a tmp YAML; first call
        # caches, second call returns the same dict.
        p = _write_taxonomy(tmp_path, _minimal_taxonomy())
        monkeypatch.setattr(_cb_mod, "_resolve_taxonomy_path", lambda: p)
        first = load_taxonomy()
        second = load_taxonomy()
        assert first is second

    def test_explicit_path_bypasses_cache(self, tmp_path: Path):
        # Tests pass explicit paths; they should always re-read and
        # never poison the module cache.
        (tmp_path / "a").mkdir(parents=True, exist_ok=True)
        (tmp_path / "b").mkdir(parents=True, exist_ok=True)
        a = _write_taxonomy(tmp_path / "a", _minimal_taxonomy())
        bad = _minimal_taxonomy()
        bad["core"] = ["only_one"]
        b = _write_taxonomy(tmp_path / "b", bad)
        load_a = load_taxonomy(path=a)
        load_b = load_taxonomy(path=b)
        assert load_a is not load_b
        assert load_a["core"] != load_b["core"]

    def test_reset_taxonomy_cache_forces_reload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        p = _write_taxonomy(tmp_path, _minimal_taxonomy())
        monkeypatch.setattr(_cb_mod, "_resolve_taxonomy_path", lambda: p)
        first = load_taxonomy()
        reset_taxonomy_cache()
        second = load_taxonomy()
        # Same content but different dict identity — reload happened.
        assert first is not second
        assert first == second


# ── resolve_tool_set ─────────────────────────────────────────────────────


class TestResolveToolSet:
    def test_unknown_intent_returns_none_loud_fallback(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="grove.context_budget"):
            result = resolve_tool_set("unknown", "simple", _minimal_taxonomy())
        assert result is None
        assert "maximal fallback" in caplog.text

    def test_none_intent_returns_none(self):
        assert resolve_tool_set(None, "simple", _minimal_taxonomy()) is None

    def test_core_always_loaded(self):
        result = resolve_tool_set(
            "conversation", "simple", _minimal_taxonomy(),
        )
        assert {"clarify", "read_file", "memory"}.issubset(result)

    def test_mcp_tools_not_in_budget_set(self):
        # Sprint 69 — resolve_tool_set no longer enumerates MCP tools.
        # They reach the agent via the generic mcp_* passthrough in
        # filter_tools_by_name, not the per-turn budget set. So the
        # resolved set contains no mcp_ names.
        result = resolve_tool_set(
            "conversation", "simple", _minimal_taxonomy(),
        )
        assert not any(name.startswith("mcp_") for name in result)

    def test_domain_chunk_added_for_intent(self):
        result = resolve_tool_set(
            "code_generation", "simple", _minimal_taxonomy(),
        )
        assert "write_file" in result
        assert "patch" in result

    def test_unknown_intent_class_skips_domain_chunk_silently(self):
        # An intent_class not in domain_chunks gets core + reads only
        # (the empty-chunk path). This is fail-safe: a new intent_class
        # the taxonomy hasn't been updated for still gets a usable set.
        # NOTE: this is NOT the same as intent_class="unknown" which
        # signals maximal fallback. This is an intent_class the Sprint 12
        # classifier produced that isn't in the taxonomy yet.
        tax = _minimal_taxonomy()
        result = resolve_tool_set("brand_new_intent", "simple", tax)
        assert result is not None
        assert "clarify" in result
        # No exploratory and no writes.
        assert "delegate_task" not in result
        assert "mcp_notion_API_patch_page" not in result

    def test_complex_adds_exploratory(self):
        result = resolve_tool_set(
            "code_generation", "complex", _minimal_taxonomy(),
        )
        assert "delegate_task" in result
        assert "browser_navigate" in result

    def test_novel_adds_exploratory(self):
        result = resolve_tool_set(
            "code_generation", "novel", _minimal_taxonomy(),
        )
        assert "delegate_task" in result

    def test_simple_does_not_add_exploratory(self):
        result = resolve_tool_set(
            "code_generation", "simple", _minimal_taxonomy(),
        )
        assert "delegate_task" not in result

    def test_moderate_does_not_add_exploratory(self):
        result = resolve_tool_set(
            "code_generation", "moderate", _minimal_taxonomy(),
        )
        assert "delegate_task" not in result

    # Sprint 69 removed write-intent gating of MCP tools — the
    # mcp_notion taxonomy block and its reads/writes/write_intents are
    # gone (see test_mcp_tools_not_in_budget_set). MCP write tools are
    # governed at execution time by the zone classifier, not hidden by
    # the per-turn budget.


# ── Co-location guard ────────────────────────────────────────────────────


class TestCoLocationGuard:
    """Discovery tools and their execution vehicles MUST appear in the
    same resolved set. Loading discovery without execution is the
    silent-degradation antipattern that froze the Agent in a
    skill_view → clarify loop."""

    def _taxonomy_with(self, core: list, domain: dict | None = None) -> dict:
        tax = _minimal_taxonomy()
        tax["core"] = list(core)
        if domain is not None:
            tax["domain_chunks"] = dict(domain)
        return tax

    def test_skill_view_without_terminal_raises(self) -> None:
        """The exact bug reproduction: skill_view in core, terminal
        absent from both core and the domain chunk → RuntimeError."""
        tax = self._taxonomy_with(
            core=["clarify", "skill_view", "read_file"],
            domain={"factual_retrieval": ["web_search"]},
        )
        with pytest.raises(RuntimeError) as exc_info:
            resolve_tool_set("factual_retrieval", "simple", tax)
        message = str(exc_info.value)
        assert "skill_view" in message
        assert "terminal" in message
        assert "factual_retrieval" in message
        assert "co-location invariant" in message

    def test_happy_path_both_present_in_core(self) -> None:
        """skill_view AND terminal in core → no error for any intent."""
        tax = self._taxonomy_with(
            core=["clarify", "skill_view", "terminal", "read_file"],
            domain={"conversation": [], "factual_retrieval": ["web_search"]},
        )
        for intent in ("conversation", "factual_retrieval", "code_generation"):
            tax["domain_chunks"].setdefault(intent, [])
            result = resolve_tool_set(intent, "simple", tax)
            assert "skill_view" in result
            assert "terminal" in result

    def test_happy_path_terminal_in_domain_chunk(self) -> None:
        """skill_view in core + terminal in domain chunk → no error."""
        tax = self._taxonomy_with(
            core=["clarify", "skill_view"],
            domain={"system_admin": ["terminal", "process"]},
        )
        result = resolve_tool_set("system_admin", "simple", tax)
        assert "skill_view" in result
        assert "terminal" in result

    def test_guard_skipped_on_maximal_fallback(self) -> None:
        """Unknown-intent maximal fallback returns None — every tool
        is loaded, so the invariant holds trivially and the guard
        MUST NOT run on the None path."""
        tax = self._taxonomy_with(
            core=["clarify", "skill_view"],  # missing terminal — would fail guard
            domain={"factual_retrieval": ["web_search"]},
        )
        result = resolve_tool_set("unknown", "simple", tax)
        assert result is None  # maximal fallback signal

    def test_message_points_at_fix_path(self) -> None:
        """The Andon message MUST tell the operator exactly where to
        edit — tool_groups.yaml, with the specific chunks named."""
        tax = self._taxonomy_with(
            core=["clarify", "skill_view"],
            domain={"factual_retrieval": ["web_search"]},
        )
        with pytest.raises(RuntimeError) as exc_info:
            resolve_tool_set("factual_retrieval", "simple", tax)
        message = str(exc_info.value)
        assert "tool_groups.yaml" in message
        assert "core" in message

    def test_repo_template_satisfies_guard_for_every_intent(self) -> None:
        """The shipped ``config/tool_groups.yaml`` MUST satisfy the
        co-location invariant for every intent_class × complexity
        combination — the architectural commitment ships, not just
        the validation logic."""
        from grove.context_budget import load_taxonomy
        reset_taxonomy_cache()
        tax = load_taxonomy()
        intents = [
            "code_generation", "debugging", "analysis", "planning",
            "factual_retrieval", "creative_writing", "system_admin",
            "conversation",
        ]
        for intent in intents:
            for complexity in ("simple", "moderate", "complex", "novel"):
                result = resolve_tool_set(intent, complexity, tax)
                assert result is not None
                if "skill_view" in result:
                    assert "terminal" in result, (
                        f"co-location broken: {intent}/{complexity} "
                        f"loads skill_view without terminal"
                    )


# ── filter_tools_by_name ──────────────────────────────────────────────────


class TestFilterToolsByName:
    @pytest.fixture
    def tools(self) -> List[dict]:
        return [
            _tool("clarify"),
            _tool("write_file"),
            _tool("delegate_task"),
            _tool("mcp_notion_notion_search"),
        ]

    def test_none_allowed_returns_input_unchanged(self, tools):
        # The maximal-fallback signal: pass-through, full registry.
        out = filter_tools_by_name(tools, allowed=None)
        assert out is tools

    def test_set_filters_by_name(self, tools):
        # Non-mcp tools filter by name; the mcp_ tool passes through
        # unconditionally (Sprint 69 generic MCP passthrough).
        out = filter_tools_by_name(
            tools, allowed={"clarify", "delegate_task"},
        )
        names = [t["function"]["name"] for t in out]
        assert names == ["clarify", "delegate_task", "mcp_notion_notion_search"]
        assert "write_file" not in names

    def test_mcp_tools_always_pass_through(self, tools):
        # Even an empty allow-set keeps mcp_ tools — they are governed at
        # execution time by the zone classifier, not by tool budgeting.
        out = filter_tools_by_name(tools, allowed=set())
        names = [t["function"]["name"] for t in out]
        assert names == ["mcp_notion_notion_search"]

    def test_preserves_input_order(self, tools):
        # Filter against a set that matches multiple tools; the output
        # preserves the input order, not the set's hash order. The mcp_
        # tool rides along via passthrough, still in input order.
        out = filter_tools_by_name(
            tools,
            allowed={"clarify", "write_file"},
        )
        names = [t["function"]["name"] for t in out]
        assert names == ["clarify", "write_file", "mcp_notion_notion_search"]

    def test_skips_malformed_entries(self):
        bad = [
            _tool("clarify"),
            "not a dict",
            {"type": "function"},  # missing function.name
            {"type": "function", "function": "not a dict"},  # function not dict
            _tool("write_file"),
        ]
        out = filter_tools_by_name(bad, allowed={"clarify", "write_file"})
        names = [t["function"]["name"] for t in out]
        assert names == ["clarify", "write_file"]
