"""Sprint 32 Phase 2 — Kaizen zone-promotion proposal generator tests."""

from __future__ import annotations

import re

import pytest

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ZONE_PROMOTION,
)
from grove.kaizen_promotion import (
    build_zone_promotion_proposal,
    normalize_pattern,
)


# ── Pattern normalization ────────────────────────────────────────────


class TestNormalizePattern:
    def test_terminal_skill_path_extracts_skill_name(self):
        cmd = "python3 /Users/x/.grove/skills/google-workspace/calendar.py today"
        pattern = normalize_pattern("terminal", cmd)
        assert pattern == r".*\.grove/skills/google\-workspace/.*"
        # And it must match the original command + any other command
        # under the same skill directory.
        assert re.fullmatch(pattern, cmd)
        assert re.fullmatch(
            pattern,
            "bash /opt/somewhere/.grove/skills/google-workspace/run.sh foo",
        )
        # MUST NOT match an unrelated skill.
        assert not re.fullmatch(
            pattern,
            "python3 /Users/x/.grove/skills/other-skill/run.py",
        )

    def test_terminal_non_skill_command_escapes_literal(self):
        cmd = "rm -rf /tmp/foo"
        pattern = normalize_pattern("terminal", cmd)
        # Anchored exact match of the literal command.
        assert pattern == "^" + re.escape(cmd) + "$"
        assert re.fullmatch(pattern, cmd)
        assert not re.fullmatch(pattern, "rm -rf /tmp/bar")

    def test_terminal_empty_command_falls_through_safely(self):
        pattern = normalize_pattern("terminal", "")
        # Degenerate "^.*$" — will fail the loader's safety check;
        # surfacing the issue is the point (operator must edit the
        # proposal manually).
        assert pattern == "^.*$"

    def test_non_terminal_tool_encodes_tool_name(self):
        pattern = normalize_pattern("write_file", "{}")
        assert pattern == re.escape("write_file")


# ── build_zone_promotion_proposal ────────────────────────────────────


class TestBuildZonePromotionProposal:
    def test_skill_path_proposal_payload_shape(self):
        proposal, payload = build_zone_promotion_proposal(
            tool_name="terminal",
            command_string=(
                "python3 /Users/x/.grove/skills/cal/run.py today"
            ),
            evidence_turn_id="s_001#1",
        )
        assert proposal.type == PROPOSAL_TYPE_ZONE_PROMOTION
        assert payload == {
            "tool": "terminal",
            "pattern": r".*\.grove/skills/cal/.*",
            "zone": "green",
            "reason": "Operator approved: allow cal to execute via terminal.",
        }
        assert proposal.payload == payload
        assert proposal.evidence == ("s_001#1",)
        assert proposal.proposal_id.startswith("sha256:")
        assert proposal.created_at  # ISO 8601 set

    def test_non_skill_proposal_uses_literal_pattern(self):
        proposal, payload = build_zone_promotion_proposal(
            tool_name="terminal",
            command_string="git log --oneline",
            evidence_turn_id="s_002#1",
        )
        assert payload["pattern"] == "^" + re.escape("git log --oneline") + "$"
        assert "this terminal command pattern" in payload["reason"]

    def test_non_terminal_tool_uses_tool_name_pattern(self):
        proposal, payload = build_zone_promotion_proposal(
            tool_name="execute_code",
            command_string="",
            evidence_turn_id="s_003#1",
        )
        assert payload["tool"] == "execute_code"
        assert payload["pattern"] == re.escape("execute_code")
        assert "execute_code actions" in payload["reason"]

    def test_proposal_id_deterministic(self):
        """Same inputs → same proposal_id. The queue uses this for
        idempotent append."""
        a, _ = build_zone_promotion_proposal(
            tool_name="terminal",
            command_string="python3 /x/.grove/skills/foo/run.py",
            evidence_turn_id="s_001#1",
        )
        b, _ = build_zone_promotion_proposal(
            tool_name="terminal",
            command_string="python3 /x/.grove/skills/foo/run.py",
            evidence_turn_id="s_001#1",
        )
        # The created_at field differs between calls (ISO timestamps
        # are time-of-call), so the dataclass equality differs even
        # though the content-addressable proposal_id matches.
        assert a.proposal_id == b.proposal_id

    def test_zone_is_always_green_for_promotion_proposals(self):
        # The "always" disposition only ever promotes TO green — there
        # is no operator path to promote a yellow → red.
        proposal, payload = build_zone_promotion_proposal(
            tool_name="terminal",
            command_string="some command",
            evidence_turn_id="t",
        )
        assert payload["zone"] == "green"
