"""retrieval-ambient-class-v1 P1 — baseline-class parity golden + unit tests.

Pattern precedent: tests/grove/test_scope_membership_declarative.py — the
membership is DECLARATIVE (capability records carry ``disclosure: baseline``),
the loader fails loud on an incoherent declaration (baseline + non-green
zone), and THIS golden pins the derived membership so drift is a reviewed
change, never an accident. The golden IS the ratified slate: a record joining
or leaving the ambient class must edit this file in the same diff.
"""

import pytest
import yaml

from grove.capability import Capability, TriggerDisclosure, Zone
from grove.capability_registry import load_capabilities
from grove.context_budget import (
    _registry_allowed_names,
    reset_caps_index_cache,
)

# ── The ratified slate (PM adjudication, retrieval-ambient-class-v1) ────────
# Record-id golden: the DERIVED baseline set must equal this literal exactly.
BASELINE_RECORD_GOLDEN = frozenset({
    "cellar_search",
    "clarify",
    "grove_browser_read",
    "read_capability_state",   # P3 — effective-state read (Exhibit 5 remedy)
    "read_file",
    "search_files",
    "skills_read",
    "web_extract",
    "web_search",
    "workspace_read",
})

# Bound-tool projection golden — every verb the ambient class carries.
BASELINE_TOOL_GOLDEN = frozenset({
    # web
    "web_search",
    "web_extract",
    # cellar + files + skills index + socratic backbone
    "cellar_search",
    "read_capability_state",   # P3 — ambient by necessity (self-invisibility guard)
    "read_file",
    "search_files",
    "skill_view",
    "skills_list",
    "clarify",
    # workspace_read (grouped record — all 10 verbs ride, PM-adjudicated)
    "gmail_search",
    "gmail_get",
    "gmail_labels",
    "calendar_list",
    "drive_search",
    "drive_get",
    "drive_download",
    "contacts_list",
    "sheets_get",
    "docs_get",
    # grove-browser MCP read surface (5 tools)
    "mcp_grove_browser_browser_search",
    "mcp_grove_browser_browser_read_page",
    "mcp_grove_browser_browser_extract",
    "mcp_grove_browser_browser_screenshot",
    "mcp_grove_browser_browser_session",
    # NOTE (P1 revised): memory read + crystallization-propose are NOT tools on
    # the live path — memory rides the context-provider/pipeline side (Kaizen
    # flywheel -> accumulated_domain_memory substrate). The honcho_* provider
    # tools were demolished (never executed on prod; liveness-gated). Nothing
    # is invented to fill a slot — cellar_search carries substrate retrieval.
})


def _baseline_records():
    return {
        rid: c
        for rid, c in load_capabilities().items()
        if c.trigger.disclosure is TriggerDisclosure.BASELINE
    }


# ── Parity golden ───────────────────────────────────────────────────────────


def test_baseline_record_membership_matches_golden():
    derived = frozenset(_baseline_records())
    assert derived == BASELINE_RECORD_GOLDEN, (
        f"ambient baseline class drifted: "
        f"unexpected={sorted(derived - BASELINE_RECORD_GOLDEN)} "
        f"missing={sorted(BASELINE_RECORD_GOLDEN - derived)}"
    )


def test_baseline_tool_projection_matches_golden():
    derived = frozenset(
        t for c in _baseline_records().values() for t in c.bindings.tools
    )
    assert derived == BASELINE_TOOL_GOLDEN, (
        f"ambient baseline tool slate drifted: "
        f"unexpected={sorted(derived - BASELINE_TOOL_GOLDEN)} "
        f"missing={sorted(BASELINE_TOOL_GOLDEN - derived)}"
    )


def test_baseline_records_are_all_green():
    # Loader validation enforces this per record; the golden re-asserts it at
    # the collection level so a validation regression cannot pass unseen.
    for rid, c in _baseline_records().items():
        assert c.zone is Zone.GREEN, f"{rid}: baseline record must be green"


# ── Loader validation (fail loud) ───────────────────────────────────────────


def _minimal_record(zone: str, disclosure: str = "baseline") -> dict:
    return {
        "id": "probe_record",
        "kind": "verb",
        "trigger": {
            "intents": [],
            "keywords": [],
            "dock_affinity": [],
            "always": True,
            "disclosure": disclosure,
        },
        "bindings": {"tools": ["probe_tool"], "credentials": None,
                     "toolset_key": None},
        "tier_rule": {
            "eligible": [1, 2, 3], "preferred": 1, "promotion_criteria": {},
            "validation": {"strategy": "shadow_compare",
                           "confidence_threshold": 0.95, "shadow_window": 20},
        },
        "zone": zone,
        "telemetry": {"feed": "intent_feed", "track": ["invocation"]},
        "context": {"disclosure": "eager", "payload": "probe",
                    "dock_composition": "none"},
        "lifecycle": {"state": "approved", "provenance": "operator_authored",
                      "created_at": "2026-07-21T00:00:00+00:00",
                      "last_used": None, "use_count": 0,
                      "flywheel_eligible": True},
        "lineage": {"source_patterns": [], "parent_id": None,
                    "decision_log": []},
        "failure": {"fallback": "halt_and_surface", "diagnostic_context": [],
                    "circuit_breaker": {"threshold": 3, "window_seconds": 300}},
    }


@pytest.mark.parametrize("zone", ["yellow", "red"])
def test_baseline_non_green_zone_is_a_load_error(zone):
    with pytest.raises(ValueError, match="baseline requires zone: green"):
        Capability.from_dict(_minimal_record(zone))


def test_baseline_green_with_empty_trigger_loads():
    # A baseline record's trigger is moot (unconditional) — an empty trigger
    # is legitimate, unlike proactive/complexity records.
    rec = _minimal_record("green")
    rec["trigger"]["always"] = False
    cap = Capability.from_dict(rec)
    assert cap.trigger.disclosure is TriggerDisclosure.BASELINE


# ── G5 precedence (baseline > complexity > always) ──────────────────────────


def test_baseline_admitted_on_simple_turn_every_intent():
    reset_caps_index_cache()
    for intent in ("conversation", "translation", "system_admin", "retrieval"):
        allowed = _registry_allowed_names(
            intent_class=intent, complexity_signal="simple")
        assert "web_search" in allowed, intent   # was cost-HOLD withheld pre-P1
        assert "read_file" in allowed, intent
        assert "cellar_search" in allowed, intent


def test_baseline_admitted_on_unknown_turn():
    # The ambient class is the floor the Andon-on-uncertainty path stands on.
    reset_caps_index_cache()
    allowed = _registry_allowed_names(
        intent_class="unknown", complexity_signal="simple")
    for tool in ("web_search", "cellar_search", "gmail_search"):
        assert tool in allowed, tool


def test_non_baseline_complexity_behavior_unchanged():
    # browser_read (native record) stays disclosure: complexity — withheld on
    # a simple turn, admitted on a complex one. P1 must not leak it.
    reset_caps_index_cache()
    simple = _registry_allowed_names(
        intent_class="retrieval", complexity_signal="simple")
    assert "browser_navigate" not in simple
    complex_ = _registry_allowed_names(
        intent_class="retrieval", complexity_signal="complex")
    assert "browser_navigate" in complex_


# ── Honcho demolition (P1 revised) — injection path gone, no schema leaks ───


def test_memory_tool_injection_path_demolished():
    # retrieval-ambient-class-v1 P1 (revised): the unconditional memory-tool
    # injection seam is DELETED, not governed — zero honcho_* executions ever
    # on prod (VM feed grep, liveness gate). No dispatcher method, no schema
    # source, no ungoverned surface mint.
    from grove.dispatcher import Dispatcher

    assert not hasattr(Dispatcher, "_inject_memory_tool_schemas"), (
        "memory-tool schema injection seam must stay demolished"
    )


def test_no_honcho_schema_leaks_into_registry_or_records():
    caps = load_capabilities()
    for rid, c in caps.items():
        assert not any(t.startswith("honcho") for t in c.bindings.tools), rid
    assert not any(rid.startswith("honcho") for rid in caps)


# ── memory.yaml orphan stays deleted ────────────────────────────────────────


def test_memory_record_absent():
    caps = load_capabilities()
    assert "memory" not in caps, (
        "config/capabilities/memory.yaml was deleted in P1 (bound tool retired "
        "b363128fe; registration forbidden by tools/registry.py) — it must not "
        "return"
    )
