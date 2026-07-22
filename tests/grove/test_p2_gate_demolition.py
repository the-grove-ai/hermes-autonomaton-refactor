"""retrieval-ambient-class-v1 P2 — gate-demolition proofs.

Pins the post-demolition contracts:
* G2 retired — the capability hook fires on EVERY intent class (unknown
  included); red always:true records attach nothing (empty intents).
* G9 fixed — derived native units carry record-derived triggers (parity with
  MCP units); one disclose-on-match rule, exempt classes eager/complexity.
* G11 closed — baseline units are eager at every tier, never pull-demoted.
* G14 deleted — no tier gate, no min_covering_tier, no stripped threading.
"""

import pytest

from grove.capability_registry import load_capabilities
from grove.context_budget import (
    ToolResolution,
    _registry_allowed_names,
    reset_caps_index_cache,
    resolve_tools_for_tier,
)
from grove.disclosure import (
    build_disclosure_units,
    disclosure_split_sets,
    reset_disclosure_split_cache,
)

from grove.classify import INTENT_CLASSES


@pytest.fixture(autouse=True)
def _fresh_projections():
    reset_caps_index_cache()
    reset_disclosure_split_cache()
    yield
    reset_caps_index_cache()
    reset_disclosure_split_cache()


# ── G2 retirement — hook fires on every class ───────────────────────────────


def _hook_agent(surface):
    import run_agent

    agent = object.__new__(run_agent.AIAgent)
    agent._tools_for_turn = surface
    agent._last_tool_selection = {}
    agent._dispatcher_singleton = None
    return agent


def _tool(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc}}


@pytest.mark.parametrize("intent", sorted(INTENT_CLASSES) + ["unknown", None])
def test_hook_fires_on_every_intent_class(intent):
    # The 4-intent carrier frozenset is deleted: the hook runs (and stamps
    # fired=True) on every class, unknown/None included. Selection stays
    # intent-driven, so unknown simply selects no records.
    agent = _hook_agent([_tool("gmail_search")])
    agent._apply_capability_hook(intent)
    sel = agent._last_tool_selection
    assert sel["capability_hook_fired"] is True, intent


def test_workspace_frozenset_is_deleted():
    import run_agent

    assert not hasattr(run_agent.AIAgent, "_WORKSPACE_CAPABILITY_INTENTS"), (
        "the G2 carrier frozenset must stay demolished"
    )


def test_hook_on_system_admin_attaches_guidance_without_surface_delta():
    # Pre-G2-retirement, system_admin was outside the carrier set. Now records
    # with system_admin in trigger.intents attach guidance — description text
    # + provenance/zone bookkeeping ONLY; surface membership never changes.
    surface = [_tool("gmail_search", "Search Gmail.")]
    agent = _hook_agent(surface)
    agent._apply_capability_hook("system_admin")
    assert agent._capability_records_applied, "system_admin records must select"
    names_after = [t["function"]["name"] for t in agent._tools_for_turn]
    assert names_after == ["gmail_search"], "no admission delta — text only"


def test_red_always_records_never_attach():
    # andon_write / fleet_purge / propose_governance_change carry intents=[]
    # (always:true): intent-driven selection can NEVER pick them, on any class.
    caps = load_capabilities()
    red_always = [
        rid for rid, c in caps.items()
        if c.zone.value == "red" and c.trigger.always and c.bindings.tools
    ]
    assert {"andon_write", "fleet_purge", "propose_governance_change"} <= set(
        red_always
    )
    for rid in red_always:
        assert not caps[rid].trigger.intents, (
            f"{rid}: a red always record with non-empty intents would attach "
            f"guidance post-G2-retirement — adjudicate before adding intents"
        )


# ── G9 — native trigger parity ──────────────────────────────────────────────


class _Reg:
    def __init__(self, names):
        self._names = names

    def get_all_tool_names(self):
        return list(self._names)

    def get_definitions(self, names, quiet=True):
        return [
            {"type": "function", "function": {"name": n, "description": f"{n} d"}}
            for n in self._names
            if n in names
        ]


def test_derived_native_units_carry_record_triggers():
    units = build_disclosure_units(_Reg(["execute_code", "web_search", "browser_navigate"]))
    by_id = {u.id: u for u in units if u.kind == "tool"}
    ec = by_id["execute_code"]
    assert ec.disclosure_mode == "triggered"
    assert set(ec.trigger.intents) == {
        "code_generation", "debugging", "system_admin",
    }, "trigger derives from the governing record — the empty-trigger hardcode is dead"
    assert by_id["web_search"].disclosure_mode == "eager"        # baseline
    # P6.1: browser_read flipped to baseline — navigate is eager now; the
    # complexity exemplar is delegate_task.
    assert by_id["browser_navigate"].disclosure_mode == "eager"


def test_one_disclose_on_match_rule_native_and_mcp_alike():
    from grove.manifest import DisclosableUnit, UnitTrigger

    # triggered + no trigger fails loud for BOTH kinds (the native exemption
    # is retired); eager/complexity are the only trigger-exempt classes.
    for kind, payload in (("tool", "tool_schema:x"), ("mcp", "mcp_schema:x")):
        with pytest.raises(ValueError, match="triggered unit with no trigger"):
            DisclosableUnit(
                id="x", kind=kind, oneline="x.", payload=payload,
                tiers=("T2",), trigger=UnitTrigger((), (), None),
            )
    for mode in ("eager", "complexity"):
        u = DisclosableUnit(
            id="x", kind="tool", oneline="x.", payload="tool_schema:x",
            tiers=("T2",), trigger=UnitTrigger((), (), None),
            disclosure_mode=mode,
        )
        assert u.disclosure_mode == mode


# ── G11 — baseline eager at every tier ──────────────────────────────────────


def test_baseline_set_in_split():
    baseline, core, _ = disclosure_split_sets()
    assert "web_search" in baseline and "cellar_search" in baseline
    assert "calendar_list" in baseline          # workspace_read grouped record
    assert not (baseline & core), "a tool is baseline OR core, never both"


def test_baseline_admitted_at_admission_layer_every_signal():
    for cx in ("simple", "moderate", "complex", "novel"):
        allowed = _registry_allowed_names("conversation", cx)
        assert "web_search" in allowed, cx


def test_resolver_admits_baseline_on_unknown():
    res = resolve_tools_for_tier(
        [_tool("web_search"), _tool("execute_code")], "unknown", "simple"
    )
    assert "web_search" in res.allowed_names
    assert res.fallback is True


# ── G14 — dead gate stays dead ──────────────────────────────────────────────


def test_min_covering_tier_deleted():
    import grove.context_budget as cb

    assert not hasattr(cb, "min_covering_tier")


def test_tool_resolution_has_no_stripped_field():
    res = ToolResolution(
        tools=(), allowed_names=frozenset(), excluded_mcp=frozenset(),
        unparseable_mcp=(), fallback=False,
    )
    assert not hasattr(res, "stripped_capabilities")


def test_resolver_rejects_current_tier_kwarg():
    with pytest.raises(TypeError):
        resolve_tools_for_tier([], "research", "simple", current_tier=2)
