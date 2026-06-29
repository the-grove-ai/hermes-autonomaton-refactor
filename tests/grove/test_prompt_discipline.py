"""Sprint 55 Block B Phase 5 — prompt discipline (T25).

grove_agent_help must carry the positive "always attempt the tool call"
advisor directive and must NOT teach the agent to predict governance
outcomes (zone rules, surface-specific auto-allow, dispositions). The
quarantined architecture/governance skills must not leak their vocabulary
into the system prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove import skills as gskills
from agent.prompt_builder import (
    GROVE_AGENT_HELP_GUIDANCE,
    _build_andon_proposals_section,
    _is_governance_skill,
)


# ── T25: the advisor directive is present ─────────────────────────────


def test_T25_grove_agent_help_has_advisor_directive():
    g = GROVE_AGENT_HELP_GUIDANCE
    assert "You are an advisor" in g
    assert "MUST emit the corresponding tool call" in g
    # prompt-governance-rationalization-v1 — anti-prediction directive preserved,
    # de-architected: "Dispatcher" naming removed, "it"→"the system".
    assert "never predict the outcome" in g
    assert "You act; the system governs" in g


def test_T25_grove_agent_help_has_no_governance_prediction_language():
    g = GROVE_AGENT_HELP_GUIDANCE.lower()
    # The directive NAMES permissions/zones/halts only to tell the agent NOT
    # to warn about them — but it must not teach prediction/classification.
    forbidden = [
        "yellow zone", "red zone", "green zone", "auto-allow", "auto allow",
        "requires approval", "will be allowed", "will prompt you", "zone rule",
        "disposition", "sovereignty halt", "classify", "green/yellow/red",
    ]
    leaked = [t for t in forbidden if t in g]
    assert not leaked, f"grove_agent_help leaks governance-prediction language: {leaked}"


# ── governance-skill filter ───────────────────────────────────────────


def test_governance_skills_are_flagged():
    assert _is_governance_skill("grove-zone-ux", "green/yellow/red boundaries")
    assert _is_governance_skill("grove-operations", "zones, routing, gateway, sovereignty")
    assert _is_governance_skill("grove-architecture", "cognitive router, dispatch, sovereignty")
    assert _is_governance_skill("grove-identity-governance", "edit identity files")


def test_task_skills_are_not_flagged():
    assert not _is_governance_skill("weekly-team-sync", "Schedule a recurring weekly team sync.")
    assert not _is_governance_skill("anthropic-api-knowledge", "Reference for the Anthropic Claude API.")


# ── governance skills don't leak into the andon proposals section ─────


@pytest.fixture
def fake_grove_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(gskills, "get_hermes_home", lambda: fake)
    return fake


def _proposal(name: str, desc: str) -> str:
    return f"---\nname: {name}\ndescription: {desc}\nzone: yellow\n---\n# {name}\n"


def test_andon_section_excludes_governance_skills(fake_grove_home: Path):
    gskills.write_proposal(
        "grove-zone-ux",
        _proposal("grove-zone-ux",
                  "Zone UX: Jidoka/Andon/Kaizen governance across green/yellow/red boundaries."),
    )
    gskills.write_proposal(
        "weekly-team-sync",
        _proposal("weekly-team-sync", "Schedule a recurring weekly team sync."),
    )

    section = _build_andon_proposals_section()

    # The legit task skill is surfaced; the governance skill is filtered out.
    assert "weekly-team-sync" in section
    assert "grove-zone-ux" not in section
    # And its governance vocabulary never reaches the prompt.
    for term in ("green/yellow/red", "sovereignty", "Jidoka"):
        assert term not in section
