"""Composer + Dock tier context gating — Sprint 73 declarative-jit-budget-v1
(Phase 3).

Isolated unit matrix for the D5 context gate: the centralized compose() gate
drops gateable blocks (claude_contract / skills_index) absent from the tier
allow-list while baseline providers always compose, and records the exclusions
in ComposedPrompt.gated_context_blocks (D10 provenance). Plus the shared
admission predicate the Dock seam (run_agent.py:12706) uses for goal_record.

The gate is a NO-OP when no allow-list is supplied (Phase 3 isolation default).
Behavioral Dock injection + live /context land in Phase 6.
"""

from __future__ import annotations

from grove.prompt.composer import PromptComposer, SectionResult
from grove.tier_budget import tier_admits_context_block


def _provider(label, text):
    def provide(ctx):
        return SectionResult(label=label, text=text)

    return provide


def _composer():
    """A composer with the two gateable providers (keyed by their real
    registration names) plus two always-on baseline providers."""
    c = PromptComposer()
    c.register_section("identity", _provider("identity", "I AM GROVE"), order=10, tier="stable")
    c.register_section("skills_index", _provider("skills_index", "SKILLS BLOCK"), order=50, tier="stable")
    c.register_section("context_files", _provider("context_files", "CLAUDE CONTRACT"), order=20, tier="context")
    c.register_section("timestamp", _provider("timestamp", "NOW"), order=100, tier="volatile")
    return c


# ── no allow-list ⇒ no gating (Phase 3 isolation default / legacy) ─────────


def test_no_allowlist_composes_everything():
    res = _composer().compose()
    assert set(res.sections) == {"identity", "skills_index", "context_files", "timestamp"}
    assert res.gated_context_blocks == frozenset()
    assert "CLAUDE CONTRACT" in res.text and "SKILLS BLOCK" in res.text


def test_explicit_none_allowlist_is_noop():
    res = _composer().compose(tier_context_blocks=None)
    assert "context_files" in res.sections and "skills_index" in res.sections
    assert res.gated_context_blocks == frozenset()


# ── gating by tier allow-list (D5) ─────────────────────────────────────────


def test_gate_excludes_claude_contract():
    res = _composer().compose(tier_context_blocks=frozenset({"skills_index", "goal_record"}))
    assert "context_files" not in res.sections          # claude_contract gated off
    assert "CLAUDE CONTRACT" not in res.text
    assert "skills_index" in res.sections                # admitted
    assert res.gated_context_blocks == frozenset({"claude_contract"})


def test_gate_excludes_skills_index():
    res = _composer().compose(tier_context_blocks=frozenset({"claude_contract"}))
    assert "skills_index" not in res.sections
    assert "SKILLS BLOCK" not in res.text
    assert "context_files" in res.sections
    assert res.gated_context_blocks == frozenset({"skills_index"})


def test_empty_allowlist_gates_both_gateable_blocks_baseline_survives():
    # T1-style: context: [] ⇒ frozenset() admits no gateable block.
    res = _composer().compose(tier_context_blocks=frozenset())
    assert "context_files" not in res.sections
    assert "skills_index" not in res.sections
    assert res.gated_context_blocks == frozenset({"claude_contract", "skills_index"})
    # baseline providers are never in the gate map — always compose
    assert "identity" in res.sections and "timestamp" in res.sections
    assert "I AM GROVE" in res.text and "NOW" in res.text


def test_full_allowlist_admits_both():
    res = _composer().compose(
        tier_context_blocks=frozenset({"claude_contract", "skills_index", "goal_record"})
    )
    assert "context_files" in res.sections and "skills_index" in res.sections
    assert res.gated_context_blocks == frozenset()


def test_baseline_providers_never_gated_even_when_unlisted():
    # identity / timestamp are not gateable blocks; an allow-list that names
    # neither still composes them.
    res = _composer().compose(tier_context_blocks=frozenset({"goal_record"}))
    assert "identity" in res.sections and "timestamp" in res.sections
    assert res.gated_context_blocks == frozenset({"claude_contract", "skills_index"})


# ── shared admission predicate (Dock seam + composer, single source) ───────


def test_tier_admits_context_block_none_is_admit():
    # None allow-list ⇒ admitted (isolation / legacy default).
    assert tier_admits_context_block("goal_record", None) is True
    assert tier_admits_context_block("claude_contract", None) is True


def test_tier_admits_context_block_named_only():
    blocks = frozenset({"goal_record"})
    assert tier_admits_context_block("goal_record", blocks) is True
    assert tier_admits_context_block("claude_contract", blocks) is False
    assert tier_admits_context_block("skills_index", frozenset()) is False
