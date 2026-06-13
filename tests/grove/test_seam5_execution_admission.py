"""GRV-009 E5 C-SEAM5 — the fifth seam (execution admission).

Offering IS the governance; execution honors it. Two layers, both fail loud
(the executor block_message idiom — refused tools return a diagnostic, never
execute):

  PRIMARY   (_invoke_tool entry): a named-but-unoffered tool — checked against
            the per-turn ADMITTED set (the resolver output via _tools_for_api),
            not the construction surface — is refused.
  SECONDARY (dispatch): a tool whose governing record marks the current tier
            ineligible is refused, independent of the offer path.

Scope is GENERAL (ruling 1): native and MCP alike, including the E4 T1 hosted
green-read leak. The A8 false-positive guard: the ONLY thing newly blocked is a
tool not admitted this turn — every legitimately-admitted tool (each disclosure
mode, the fallback path, the control tools) still executes.
"""

from __future__ import annotations

import json

import pytest

import grove.providers as P
import run_agent


def _agent(offered=None):
    """A bare AIAgent with just the C-SEAM5 surface state set."""
    a = object.__new__(run_agent.AIAgent)
    a.tools = [{"type": "function", "function": {"name": n}} for n in (
        "read_file", "memory", "clarify", "terminal", "escalate", "web_search",
        "write_file", "execute_code", "browser_navigate", "spotify_search",
        "todo", "mcp_notion_notion_search",
    )]
    a._tools_for_turn = (
        None if offered is None
        else [{"type": "function", "function": {"name": n}} for n in offered]
    )
    return a


def _refused(payload):
    if payload is None:
        return False
    d = json.loads(payload)
    return d.get("andon") == "execution_admission"


# ── PRIMARY: per-turn admitted-set gate ──────────────────────────────────────


def test_primary_admits_offered_refuses_unoffered():
    a = _agent(offered=["read_file", "web_search"])
    assert a._seam5_admission_refusal("read_file") is None          # offered -> admitted
    assert a._seam5_admission_refusal("web_search") is None
    assert _refused(a._seam5_admission_refusal("write_file"))        # exists, not offered -> refused
    assert _refused(a._seam5_admission_refusal("browser_navigate"))  # complexity, not offered


def test_primary_no_filter_admits_everything():
    # Maximal fallback / no per-turn filter: _tools_for_api == full surface, so
    # nothing is newly blocked (the A8 guard — only unoffered tools are refused).
    a = _agent(offered=None)
    for t in ("read_file", "write_file", "browser_navigate", "spotify_search", "todo"):
        assert a._seam5_admission_refusal(t) is None


def test_primary_every_disclosure_mode_executes_when_offered():
    # core/proactive, intent, complexity, fallback — each admitted when offered.
    a = _agent(offered=["clarify", "web_search", "browser_navigate", "spotify_search", "todo"])
    for t in ("clarify", "web_search", "browser_navigate", "spotify_search", "todo"):
        assert a._seam5_admission_refusal(t) is None, t


def test_primary_refusal_payload_names_tool_tier_intent():
    a = _agent(offered=["read_file"])
    P._last_routed_tier = "T1"
    payload = a._seam5_admission_refusal("write_file")
    d = json.loads(payload)
    assert d["tool"] == "write_file"
    assert d["tier"] == "T1"
    assert "not in the per-turn offered surface" in d["error"]


# ── SECONDARY: registry tier-eligibility gate (defense-in-depth) ──────────────


def test_secondary_refuses_tier_ineligible_record():
    a = _agent()
    P._last_routed_tier = "T1"
    # execute_code is eligible [2,3] — refused at T1.
    assert _refused(a._seam5_tier_refusal("execute_code"))
    # browser_navigate is eligible [3] — refused at T1.
    assert _refused(a._seam5_tier_refusal("browser_navigate"))
    # read_file / web_search are eligible [1,2,3] — allowed at T1.
    assert a._seam5_tier_refusal("read_file") is None
    assert a._seam5_tier_refusal("web_search") is None


def test_secondary_allows_at_eligible_tier():
    a = _agent()
    P._last_routed_tier = "T3"
    for t in ("execute_code", "browser_navigate", "spotify_search", "mcp_notion_notion_search"):
        assert a._seam5_tier_refusal(t) is None, t


def test_secondary_no_tier_is_no_ceiling():
    # The vanilla/cloud path (no routed tier) imposes no eligibility ceiling,
    # mirroring an empty exclude_mcp.
    a = _agent()
    P._last_routed_tier = None
    for t in ("execute_code", "browser_navigate", "spotify_search"):
        assert a._seam5_tier_refusal(t) is None, t


def test_secondary_ungoverned_tool_not_refused():
    a = _agent()
    P._last_routed_tier = "T1"
    assert a._seam5_tier_refusal("a_tool_with_no_record") is None


# ── The E4 leak, named explicitly: T1 hosted-MCP green-read no longer runs ───


def test_e4_t1_notion_green_read_no_longer_executes():
    # The E4 deploy finding: notion (kind=mcp, tier_rule.eligible:[3]) executed on
    # T1 despite being unoffered. C-SEAM5 closes it at BOTH layers.
    P._last_routed_tier = "T1"
    # PRIMARY: on a real T1 turn the resolver does not offer notion (eligible [3]),
    # so the per-turn surface excludes it -> refused.
    a = _agent(offered=["read_file", "web_search", "calendar_list"])  # a typical T1 surface, no notion
    assert _refused(a._seam5_admission_refusal("mcp_notion_notion_search"))
    # SECONDARY: independent of the offer path, the notion_read record's
    # tier_rule.eligible=[3] refuses it at T1.
    assert _refused(a._seam5_tier_refusal("mcp_notion_notion_search"))
    # ...and at T3 the green-read is allowed by both layers.
    P._last_routed_tier = "T3"
    a3 = _agent(offered=["mcp_notion_notion_search", "read_file"])
    assert a3._seam5_admission_refusal("mcp_notion_notion_search") is None
    assert a3._seam5_tier_refusal("mcp_notion_notion_search") is None
