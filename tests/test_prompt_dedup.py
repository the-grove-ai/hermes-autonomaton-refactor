"""prompt-dedup-v1 — content-redistribution parity + token verification.

Consolidates two overlapping always-on prompt blocks in
``agent/prompt_builder.py``: GROVE_AGENT_HELP_GUIDANCE keeps the unique
advisor / workspace / skill_view blocks (1-3); its F3 lubricant clause and
HARD RULE move to the always-on SYSTEM_SELF_AWARENESS (4, 6); its redundant
capability catalog (5) is deleted (SYSTEM_SELF_AWARENESS carries the superset).

Invariants: I1 F3 present in SYSTEM_SELF_AWARENESS; I2 no behavioral directive
lost (every moved/removed directive has a counterpart); I4 grove_agent_help is
NOT tier-gated (L4 descoped).
"""

from __future__ import annotations

from agent.prompt_builder import (
    GROVE_AGENT_HELP_GUIDANCE as G,
    SYSTEM_SELF_AWARENESS as S,
)
from agent.model_metadata import estimate_tokens_rough as tok
from grove.prompt import build_default_composer
from grove.prompt.composer import _PROVIDER_GATEABLE_BLOCK
from grove.tier_budget import GATEABLE_CONTEXT_BLOCKS


# ── Content parity: GROVE_AGENT_HELP_GUIDANCE trimmed to Blocks 1-3 ───────


def test_grove_agent_help_keeps_unique_blocks() -> None:
    assert "You are an advisor" in G                       # Block 1
    assert "granted workspace access" in G                 # Block 2
    assert "skill_view" in G                               # Block 3


def test_grove_agent_help_capability_catalog_removed() -> None:
    # Block 5 markers gone (the catalog SYSTEM_SELF_AWARENESS supersets).
    # NB: assert the catalog ENTRIES, not the bare word "memory" — Block 2's
    # kept "internal substrate (memory, intent records, proposals)" is a
    # different semantic use (corrected from the SPEC's over-broad assertion).
    assert "built-in, governed systems" not in G
    assert "Dock" not in G
    assert "Flywheel" not in G
    assert "review_proposals" not in G


def test_grove_agent_help_moved_blocks_absent() -> None:
    assert "HARD RULE" not in G                            # Block 6 moved
    assert "Never use terms like Andon" not in G           # Block 4 (F3) moved


# ── Content parity: SYSTEM_SELF_AWARENESS enriched (F3 + HARD RULE) ───────


def test_system_self_awareness_has_f3_and_hard_rule() -> None:
    # I1 — F3 lubricant clause, now always-on here.
    assert "Never use terms like Andon" in S
    assert "Paused is not failed" in S
    assert "needs your approval" in S
    # HARD RULE consolidated.
    assert "HARD RULE" in S
    assert "do not build storage for knowledge you already hold" in S


def test_system_self_awareness_retains_superset() -> None:
    for marker in ("Memory", "Dock", "Flywheel", "Skill management",
                   "Grant management", "Kaizen"):
        assert marker in S, marker


# ── I2: no behavioral directive lost ─────────────────────────────────────


def test_i2_capability_catalog_directives_covered_by_superset() -> None:
    # Every capability the deleted Block 5 named has a counterpart in S.
    for capability in ("Memory", "Dock", "Flywheel"):
        assert capability in S
    # The "don't recommend external tools" directive survives.
    assert "Never recommend external tools" in S


# ── Token verification (REAL measured values — SPEC estimates corrected) ──


def test_token_counts_reflect_real_dedup() -> None:
    g, s = tok(G), tok(S)
    # GATE-A's SPEC estimates (~138 G / ~380 S / ~518 combined, ~402 saved)
    # were off. Measured reality (chars/4): G~431, S~680, combined~1111,
    # ~279 tokens/turn saved — the deleted Block 5 catalog. F3 + HARD RULE are
    # relocated between two always-on blocks (net-zero).
    assert g < 500, f"GROVE_AGENT_HELP should be trimmed to Blocks 1-3: {g}"
    assert 600 < s < 760, f"SYSTEM_SELF_AWARENESS should carry F3+HARD RULE: {s}"
    assert g + s < 1200, f"combined should be below the ~1390 original: {g + s}"


# ── I4: grove_agent_help is NOT tier-gated (L4 descoped) ──────────────────


def test_i4_grove_agent_help_not_tier_gated() -> None:
    assert "grove_agent_help" not in _PROVIDER_GATEABLE_BLOCK
    assert "grove_agent_help" not in GATEABLE_CONTEXT_BLOCKS


# ── Compose integration: both blocks ride the composed prompt ────────────


def test_compose_carries_both_blocks_on_all_tiers() -> None:
    composer = build_default_composer(config=None)

    def _compose(tier_blocks):
        return composer.compose(
            valid_tool_names={"write_file", "memory", "review_proposals"},
            model="", provider="", platform="cli", session_id="dedup_test",
            skip_context_files=True, load_soul_identity=False,
            memory_enabled=False, user_profile_enabled=False,
            pass_session_id=False, tier_context_blocks=tier_blocks,
        )

    # Eager (no tier gate) AND a maximally-restrictive tier allow-list: in both,
    # grove_agent_help and the always-on SYSTEM_SELF_AWARENESS survive (I4).
    for tier_blocks in (None, frozenset()):
        composed = _compose(tier_blocks)
        assert "grove_agent_help" in composed.sections
        text = composed.text
        assert "You are an advisor" in text          # GROVE_AGENT_HELP Block 1
        assert "Never use terms like Andon" in text  # F3 (moved, always-on)
        assert "HARD RULE" in text                    # HARD RULE (moved)
        # grove_agent_help is never gated even when the allow-list is empty.
        assert "grove_agent_help" not in composed.gated_context_blocks
