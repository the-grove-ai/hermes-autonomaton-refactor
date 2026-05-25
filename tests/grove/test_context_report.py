"""Sprint 24a — tests for grove.context_report.

The module is pure instrumentation: read agent state, tokenise, render
a table, write a JSON snapshot. The tests use a stub agent that
mirrors the AIAgent surface the handler reads (``tools``,
``ephemeral_system_prompt``, ``session_id``, ``model``,
``_build_system_prompt_parts``) so we exercise the build / format /
persist path without lifting the full agent constructor.

Snapshot writes always go to ``tmp_path`` — no test touches the real
``~/.grove/.context_snapshots/`` directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional

import pytest

from grove.context_report import (
    ContextReport,
    build_context_report,
    format_context_report,
    persist_context_report,
    snapshot_path_for,
    _tool_group_for,
)


# ── Stub agent ────────────────────────────────────────────────────────────────


class StubAgent:
    """Minimal fake AIAgent mirroring the attributes context_report reads.

    Every field is constructor-injected so individual tests can vary one
    dimension at a time (e.g. empty sections, no tools, no cellar).
    """

    def __init__(
        self,
        *,
        sections: Optional[Dict[str, str]] = None,
        tools: Optional[List[Mapping]] = None,
        ephemeral_system_prompt: str = "",
        session_id: str = "test-session",
        model: str = "claude-sonnet-4-6",
    ):
        self._sections = dict(sections or {})
        self.tools = list(tools or [])
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.session_id = session_id
        self.model = model

    def _build_system_prompt_parts(self, system_message=None):
        # Mirror the AIAgent 4-key dict shape. The legacy stable/context/volatile
        # keys are present for backward-compat but context_report only reads
        # `_sections` so the values can be empty here.
        return {
            "stable": "",
            "context": "",
            "volatile": "",
            "_sections": dict(self._sections),
        }


@pytest.fixture
def small_agent():
    """A stub agent with all four buckets populated at modest sizes."""
    return StubAgent(
        sections={
            "identity": "I am a stub identity. " * 30,             # ~150 chars
            "skills_index": "## skill A\n- alpha\n" * 200,         # ~3500 chars
            "context_files": "AGENTS.md contents " * 40,           # ~800 chars
            "memory": "MEM " * 10,                                 # ~40 chars
            "timestamp": "TS",                                     # 2 chars
        },
        tools=[
            {"function": {"name": "terminal", "description": "shell"}},
            {"function": {"name": "memory_read", "description": "read mem"}},
            {"function": {"name": "notion_search", "description": "search"}},
            {"function": {"name": "notion_fetch", "description": "fetch page"}},
        ],
        ephemeral_system_prompt="<cellar_context>tiny cellar</cellar_context>",
        session_id="abc123",
        model="claude-opus-4-7",
    )


# ── _tool_group_for ──────────────────────────────────────────────────────────


class TestToolGroupHeuristic:
    """The grouping heuristic decides which bucket each tool falls into."""

    @pytest.mark.parametrize("name,expected", [
        ("mcp__notion__search", "mcp"),
        ("notion_search", "notion"),
        ("notion-search", "notion"),
        ("gws_calendar_list", "gws"),
        ("terminal", "terminal"),
        ("memory", "memory"),
        ("", "_unknown"),
    ])
    def test_grouping(self, name, expected):
        assert _tool_group_for(name) == expected


# ── build_context_report ─────────────────────────────────────────────────────


class TestBuildContextReport:
    """The build step assembles per-section counts from agent state."""

    def test_section_breakdown_present(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            snapshot_base_dir=tmp_path,
        )
        # Every label seeded in the stub appears in the breakdown.
        for label in ("identity", "skills_index", "context_files", "memory", "timestamp"):
            assert label in report.system_prompt_sections
        # Internal _total key is populated and equals the sum of the rest.
        sp_subs = {k: v for k, v in report.system_prompt_sections.items() if k != "_total"}
        assert report.system_prompt_sections["_total"] == sum(sp_subs.values())

    def test_tool_schemas_grouped_by_prefix(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[],
            snapshot_base_dir=tmp_path,
        )
        # notion_search + notion_fetch group together; memory + terminal stand
        # alone. _total sums the per-group counts.
        groups = {k: v for k, v in report.tool_schemas.items() if k != "_total"}
        assert set(groups.keys()) == {"notion", "memory", "terminal"}
        assert report.tool_schemas["_total"] == sum(groups.values())

    def test_grand_total_sums_buckets(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[{"role": "user", "content": "x"}],
            snapshot_base_dir=tmp_path,
        )
        expected = (
            report.system_prompt_sections["_total"]
            + report.tool_schemas["_total"]
            + report.conversation_history
            + report.cellar_context
        )
        assert report.grand_total == expected

    def test_empty_state_does_not_crash(self, tmp_path):
        """A bare agent (no tools, no sections, no cellar) builds a zero report."""
        agent = StubAgent(sections={}, tools=[], ephemeral_system_prompt="")
        report = build_context_report(
            agent, conversation_history=[], snapshot_base_dir=tmp_path,
        )
        assert report.grand_total == 0
        assert report.system_prompt_sections["_total"] == 0
        assert report.tool_schemas["_total"] == 0
        assert report.conversation_history == 0
        assert report.cellar_context == 0

    def test_tokenizer_is_deterministic(self, small_agent, tmp_path):
        """Repeated build calls on the same agent state return identical counts."""
        r1 = build_context_report(
            small_agent, conversation_history=[], snapshot_base_dir=tmp_path,
        )
        r2 = build_context_report(
            small_agent, conversation_history=[], snapshot_base_dir=tmp_path,
        )
        assert r1.grand_total == r2.grand_total
        assert r1.system_prompt_sections == r2.system_prompt_sections
        assert r1.tool_schemas == r2.tool_schemas

    def test_session_id_and_turn_from_args_override_agent(self, small_agent, tmp_path):
        """Explicit session_id/turn args win over agent.session_id."""
        report = build_context_report(
            small_agent,
            conversation_history=[],
            session_id="override-sid",
            turn=42,
            snapshot_base_dir=tmp_path,
        )
        assert report.session_id == "override-sid"
        assert report.turn == 42

    def test_session_id_defaults_to_agent_attribute(self, small_agent, tmp_path):
        """When session_id arg is omitted, agent.session_id is used."""
        report = build_context_report(
            small_agent,
            conversation_history=[],
            snapshot_base_dir=tmp_path,
        )
        assert report.session_id == "abc123"  # from the small_agent fixture


# ── format_context_report ────────────────────────────────────────────────────


class TestFormatContextReport:
    """The render step produces the D5-shaped operator-facing table."""

    def test_renders_all_buckets(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[{"role": "user", "content": "hi"}],
            snapshot_base_dir=tmp_path,
        )
        output = format_context_report(report)
        assert "System prompt total" in output
        assert "Tool schemas total" in output
        assert "Conversation history" in output
        assert "Cellar context" in output
        assert "Per-turn input total" in output

    def test_sort_order_descending_within_buckets(self, small_agent, tmp_path):
        """Within the system-prompt bucket, sub-sections list largest first.

        skills_index is the largest in the small_agent fixture (3500 chars vs
        identity's 900-ish vs context_files' 800-ish vs the tiny ones).
        """
        report = build_context_report(
            small_agent, conversation_history=[], snapshot_base_dir=tmp_path,
        )
        output = format_context_report(report)
        # Find positions of the sub-section lines (they're indented with 2
        # spaces so we can grep cleanly).
        lines = output.splitlines()
        skills_idx = next(i for i, ln in enumerate(lines) if "skills_index" in ln)
        identity_idx = next(i for i, ln in enumerate(lines) if "identity" in ln)
        timestamp_idx = next(i for i, ln in enumerate(lines) if "timestamp" in ln)
        # Largest (skills_index) appears before smaller (identity, timestamp).
        assert skills_idx < identity_idx < timestamp_idx

    def test_percentages_sum_to_approximately_100(self, small_agent, tmp_path):
        """Per-line percentages of the bucket totals should sum to ~100%
        for each bucket. Single-decimal rounding can leave a tiny gap."""
        report = build_context_report(
            small_agent,
            conversation_history=[{"role": "user", "content": "x" * 200}],
            snapshot_base_dir=tmp_path,
        )
        # The four BUCKET totals (system_prompt + tool_schemas + history +
        # cellar) should sum to grand_total, which the renderer shows as 100%.
        grand = report.grand_total
        assert grand > 0
        bucket_sum = (
            report.system_prompt_sections["_total"]
            + report.tool_schemas["_total"]
            + report.conversation_history
            + report.cellar_context
        )
        assert bucket_sum == grand
        # The rendered total line shows 100.0%.
        output = format_context_report(report)
        assert "100.0%" in output

    def test_snapshot_path_line_present_and_unellipsized(self, small_agent, tmp_path):
        """The footer line shows the full snapshot path (GATE-B note 2 — the
        synthetic smoke output ellipsised for response brevity, but the
        actual format string is ``f"Snapshot: {report.snapshot_path}"``
        which prints the Path verbatim)."""
        report = build_context_report(
            small_agent,
            conversation_history=[],
            session_id="path-test",
            turn=7,
            snapshot_base_dir=tmp_path,
        )
        output = format_context_report(report)
        snapshot_line = next(ln for ln in output.splitlines() if ln.startswith("Snapshot:"))
        # The line carries the FULL path string (no `…` truncation).
        assert "…" not in snapshot_line
        assert str(report.snapshot_path) in snapshot_line
        assert "path-test_7.json" in snapshot_line


# ── persist_context_report ───────────────────────────────────────────────────


class TestPersistContextReport:
    """Snapshot persistence writes the D4-shaped JSON to the expected path."""

    def test_writes_to_expected_path(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[],
            session_id="persist-test",
            turn=3,
            snapshot_base_dir=tmp_path,
        )
        path = persist_context_report(report, base_dir=tmp_path)
        assert path.exists()
        assert path.name == "persist-test_3.json"
        assert path.parent == tmp_path

    def test_snapshot_schema_matches_d4(self, small_agent, tmp_path):
        report = build_context_report(
            small_agent,
            conversation_history=[{"role": "user", "content": "hello"}],
            session_id="schema-test",
            turn=1,
            snapshot_base_dir=tmp_path,
        )
        path = persist_context_report(report, base_dir=tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Top-level keys per D4 schema.
        assert set(payload.keys()) == {
            "session_id", "turn", "timestamp", "model", "sections", "grand_total",
        }
        # Sections sub-keys per D4 schema.
        assert set(payload["sections"].keys()) == {
            "system_prompt", "tool_schemas", "conversation_history", "cellar_context",
        }
        # system_prompt is the per-label dict (not flattened).
        assert isinstance(payload["sections"]["system_prompt"], dict)
        assert "_total" in payload["sections"]["system_prompt"]

    def test_creates_parent_directory_if_missing(self, small_agent, tmp_path):
        deep = tmp_path / "nested" / "not-yet-created"
        report = build_context_report(
            small_agent,
            conversation_history=[],
            snapshot_base_dir=deep,
        )
        path = persist_context_report(report, base_dir=deep)
        assert path.exists()
        assert path.parent == deep


# ── Handler integration ──────────────────────────────────────────────────────


class TestHandlerIntegration:
    """Exercise the build → format → persist sequence as the
    /context slash-command handler does."""

    def test_full_flow_produces_table_and_snapshot(self, small_agent, tmp_path):
        """The handler's three-call sequence yields stdout-shaped text and
        a JSON file with no other side effects on the agent."""
        # Capture the agent's pre-state.
        pre_tools = list(small_agent.tools)
        pre_ephemeral = small_agent.ephemeral_system_prompt
        pre_session = small_agent.session_id

        report = build_context_report(
            small_agent,
            conversation_history=[
                {"role": "user", "content": "what is on my schedule"},
            ],
            session_id="integration",
            turn=1,
            snapshot_base_dir=tmp_path,
        )
        text = format_context_report(report)
        path = persist_context_report(report, base_dir=tmp_path)

        # Outputs exist and have content.
        assert text and "Per-turn input total" in text
        assert path.exists() and path.stat().st_size > 0

        # Agent state unchanged.
        assert small_agent.tools == pre_tools
        assert small_agent.ephemeral_system_prompt == pre_ephemeral
        assert small_agent.session_id == pre_session


# ── snapshot_path_for helper ─────────────────────────────────────────────────


class TestSnapshotPathFor:
    """Helper is shared between format (shows the path) and persist
    (writes there). Both must agree."""

    def test_path_format(self, tmp_path):
        path = snapshot_path_for("sess-X", 5, base_dir=tmp_path)
        assert path == tmp_path / "sess-X_5.json"

    def test_empty_session_id_substituted(self, tmp_path):
        """Empty session_id becomes 'no-session' so the path is always valid."""
        path = snapshot_path_for("", 0, base_dir=tmp_path)
        assert path.name == "no-session_0.json"
