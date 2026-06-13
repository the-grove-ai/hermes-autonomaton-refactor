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
BUDGETS = load_tier_budgets(_CFG, taxonomy_path=_TAX)
T2, T3 = BUDGETS["T2"], BUDGETS["T3"]
TAXONOMY = None  # GRV-009 E5b C2 — tool_groups.yaml retired; resolver ignores taxonomy


def _mk(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(tools):
    return [t["function"]["name"] for t in tools]


def _reg_allow(tier, intent="code_generation", message="update the notion page"):
    """GRV-009 E4 — the registry-driven mcp_allow for a tier on a notion-matching
    turn. The turn is a code_generation turn (so write_file selects) whose
    message keyword-matches the notion record's trigger. T2 is not tier-eligible
    (notion record tier_rule.eligible:[3]) so notion is withheld; T3 is eligible
    and the keyword matches so notion discloses. (The exclude_mcp ceiling that
    used to do this is retired.)"""
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


def test_t2_profile_excludes_notion_mcp():
    # GRV-009 E4 C4 — notion is withheld on T2 because it is not tier-eligible
    # (tier_rule.eligible:[3]), via the registry mcp_allow, not the retired
    # exclude_mcp ceiling. excluded_mcp is always empty now; 'other' (no record)
    # is also withheld under disclose-on-match.
    res = resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate", TAXONOMY, T2,
                                 mcp_allow=_reg_allow("T2"))
    names = _names(res.tools)
    assert not any(n.startswith("mcp_notion") for n in names)   # the ~18.4K cut
    assert "write_file" in names                                # code tools still load


def test_t3_profile_includes_notion_mcp():
    res = resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate", TAXONOMY, T3)
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


def test_dod_postcondition_7_t2_excludes_both_t3_loads_both():
    # T2: neither Notion MCP (not tier-eligible) nor claude_contract.
    t2_tools = _names(
        resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate", TAXONOMY, T2,
                               mcp_allow=_reg_allow("T2")).tools
    )
    t2_blocks = frozenset(T2.context)
    assert not any(n.startswith("mcp_notion") for n in t2_tools)
    assert not tier_admits_context_block("claude_contract", t2_blocks)

    # T3: both present (notion discloses on the matching turn).
    t3_tools = _names(
        resolve_tools_for_tier(TURN_TOOLS, "code_generation", "moderate", TAXONOMY, T3,
                               mcp_allow=_reg_allow("T3")).tools
    )
    t3_blocks = frozenset(T3.context)
    assert any(n.startswith("mcp_notion") for n in t3_tools)
    assert tier_admits_context_block("claude_contract", t3_blocks)
