"""DoD post-condition 7 (Sprint 73 declarative-jit-budget-v1).

Deterministic, no API: a T2-profile turn loads NEITHER the hosted Notion MCP NOR
claude_contract; a T3-profile turn loads BOTH. The budgets are loaded from the
COMMITTED template (config/routing.config.yaml) so the test tracks the real
config — the tool side via resolve_tools_for_tier, the context side via the
PromptComposer gate + the shared admission predicate.
"""

from __future__ import annotations

from pathlib import Path

from grove.context_budget import resolve_tools_for_tier
from grove.prompt.composer import PromptComposer, SectionResult
from grove.tier_budget import load_tier_budgets, tier_admits_context_block

_REPO = Path(__file__).resolve().parents[2]
_CFG = _REPO / "config" / "routing.config.yaml"
_TAX = _REPO / "config" / "tool_groups.yaml"

# The real committed budgets + taxonomy drive the behavior — not hand-typed copies.
BUDGETS = load_tier_budgets(_CFG)
T2, T3 = BUDGETS["T2"], BUDGETS["T3"]
TAXONOMY = None  # GRV-009 E5b C2 — tool_groups.yaml retired; resolver ignores taxonomy


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


def _reg_allow(tier, intent="code_generation", message="update the notion page"):
    """GRV-009 E4 — the registry-driven mcp_allow for a tier on a notion-matching
    turn. The turn is a code_generation turn (so write_file selects) whose
    message keyword-matches the notion record's trigger. neuter-tier-eligible-
    gate: the tier ceiling is retired, so notion discloses on the keyword match
    at EVERY tier (T1/T2/T3) — disclosure is tier-independent now."""
    import run_agent
    import grove.providers as P
    a = object.__new__(run_agent.AIAgent)
    a.tools = []
    a._current_messages = [{"role": "user", "content": message}]
    P._last_routed_tier = tier
    return a._compute_mcp_allow(intent, None)


# A turn's candidate registry: core + code tools, a hosted Notion MCP tool, and
# another MCP server (to prove only Notion is excluded, not MCP wholesale).
TURN_TOOLS = _mk(
    "clarify", "terminal", "read_file",
    "write_file", "patch", "execute_code",
    "mcp_notion_API_post_page", "mcp_notion_API_post_search",
    "mcp_other_do_thing",
)


def _contract_composer():
    """A composer carrying the two gateable context providers (claude_contract
    via the context_files registration, plus skills_index) and a baseline."""
    c = PromptComposer()
    c.register_section("identity", lambda ctx: SectionResult(label="identity", text="I AM"), order=10, tier="stable")
    c.register_section("context_files", lambda ctx: SectionResult(label="context_files", text="THE CLAUDE.md CONTRACT"), order=20, tier="context")
    c.register_section("skills_index", lambda ctx: SectionResult(label="skills_index", text="SKILLS INDEX"), order=50, tier="stable")
    return c


# ── tool side: hosted Notion MCP ───────────────────────────────────────────


def test_t2_profile_loads_notion_mcp_gate_neutered():
    # neuter-tier-eligible-gate: Notion MCP now discloses at T2 on a notion-
    # matching turn (trigger keyword-match), exactly as at T3 — the tier ceiling
    # is retired, so disclosure is tier-independent. 'other' (no record) is still
    # withheld under disclose-on-match (the trigger gate stays live).
    res = resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate",
                                 mcp_allow=_reg_allow("T2"))
    names = _names(res.tools)
    assert any(n.startswith("mcp_notion") for n in names)       # discloses on match at T2
    assert "mcp_other_do_thing" not in names                    # no record -> withheld
    assert "write_file" in names                                # code tools still load


def test_t3_profile_includes_notion_mcp():
    res = resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate")
    names = _names(res.tools)
    assert any(n.startswith("mcp_notion") for n in names)       # full Notion MCP
    assert res.excluded_mcp == frozenset()


# ── context side: claude_contract ──────────────────────────────────────────


def test_t2_profile_gates_claude_contract():
    blocks = frozenset(T2.context)
    assert tier_admits_context_block("claude_contract", blocks) is False
    composed = _contract_composer().compose(tier_context_blocks=blocks)
    assert "context_files" not in composed.sections             # the ~4.5K cut
    assert "claude_contract" in composed.gated_context_blocks


def test_t3_profile_loads_claude_contract():
    blocks = frozenset(T3.context)
    assert tier_admits_context_block("claude_contract", blocks) is True
    composed = _contract_composer().compose(tier_context_blocks=blocks)
    assert "context_files" in composed.sections
    assert composed.gated_context_blocks == frozenset()


# ── the post-condition, stated directly ────────────────────────────────────


def test_dod_postcondition_7_notion_tier_independent_contract_still_t3():
    # neuter-tier-eligible-gate: the TOOL side (Notion MCP) is now tier-
    # INDEPENDENT — it discloses on the matching turn at T2 AND T3. The CONTEXT
    # side (claude_contract block) is a separate tier-budget mechanism, untouched
    # by the eligible-gate neuter: still T3-only.
    t2_tools = _names(
        resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate",
                               mcp_allow=_reg_allow("T2")).tools
    )
    t2_blocks = frozenset(T2.context)
    assert any(n.startswith("mcp_notion") for n in t2_tools)            # tool side: now at T2
    assert not tier_admits_context_block("claude_contract", t2_blocks)  # context side: still T3-only

    # T3: notion discloses on the matching turn; claude_contract context loads.
    t3_tools = _names(
        resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate",
                               mcp_allow=_reg_allow("T3")).tools
    )
    t3_blocks = frozenset(T3.context)
    assert any(n.startswith("mcp_notion") for n in t3_tools)
    assert tier_admits_context_block("claude_contract", t3_blocks)
