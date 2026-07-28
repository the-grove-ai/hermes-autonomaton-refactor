"""researcher-retrieval-broker-v1 P3b — record-driven L1 offered allowlist.

_resolve_offered_allowlist makes the fleet offered surface record-DECLARED
(governance.offered_allowlist) with fallback to the transport-keyed default.
Proves: the three LIVE workers (forge, drafter, cultivator) resolve BYTE-IDENTICAL
to the pre-P3b hardcode (set-diff); a declared list becomes the surface; an empty
declaration passes [] through (→ L1 fleet_allowlist_empty, not a silent pass); a
malformed declaration Andons. The out-of-floor subset Andon lives in
tests/test_fleet_offering_override.py::test_record_declaring_tool_outside_floor_andons.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from grove.capability_registry import load_capabilities
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.worker_entry import _emit_declaration, _resolve_offered_allowlist


def _old_pre_p3b_hardcode(transport):
    # The EXACT pre-P3b branch removed from worker_entry.py:1198-1200 (@ f46422cf6).
    if transport == "tool":
        return ["read_file", "skill_view", "emit_package"]
    return ["read_file", "skill_view"]


# ── (a) SET-DIFF: the live workers are unchanged ─────────────────────────────
# P4 (instance-cold-start-parity-v1): the operator-private forge worker exited
# the repo; the generic reference workers still resolve byte-identically.
@pytest.mark.parametrize(
    "cap_id",
    ["skill.fleet.drafter", "skill.fleet.cultivator"],
)
def test_live_worker_resolved_allowlist_byte_identical(cap_id):
    cap = load_capabilities()[cap_id]
    transport = (_emit_declaration(cap) or {}).get("transport", "sentinel")
    before = set(_old_pre_p3b_hardcode(transport))
    after = set(_resolve_offered_allowlist(cap, transport))
    # SET-DIFF, explicit — not a count band.
    assert after == before, f"{cap_id}: before={sorted(before)} after={sorted(after)} diff={before ^ after}"
    # and they reach it via the FALLBACK: none declares the new field.
    gov = cap.governance or {}
    assert "offered_allowlist" not in gov


# ── absent → transport-keyed default (byte-identical fallback) ───────────────
def test_absent_declaration_falls_back_to_default():
    assert _resolve_offered_allowlist(SimpleNamespace(id="x", governance=None), "tool") == [
        "read_file", "skill_view", "emit_package"
    ]
    assert _resolve_offered_allowlist(SimpleNamespace(id="x", governance={}), "sentinel") == [
        "read_file", "skill_view"
    ]


# ── present → that list becomes the surface ──────────────────────────────────
def test_declared_list_becomes_the_surface():
    cap = SimpleNamespace(id="x", governance={"offered_allowlist": ["skill_view"]})
    assert _resolve_offered_allowlist(cap, "sentinel") == ["skill_view"]
    # a declaration overrides the transport default on the tool path too
    assert _resolve_offered_allowlist(cap, "tool") == ["skill_view"]


# ── (c) empty declared → [] passes through (L1 fleet_allowlist_empty) ────────
def test_empty_declaration_passes_empty_through_not_default():
    cap = SimpleNamespace(id="x", governance={"offered_allowlist": []})
    # NOT substituted with the default — [] reaches fleet_offered_allowlist and
    # hits fleet_allowlist_empty at run_agent.py:3442 (see
    # test_fleet_offering_override.test_empty_allowlist_andons for the L1 side).
    assert _resolve_offered_allowlist(cap, "sentinel") == []
    assert _resolve_offered_allowlist(cap, "tool") == []


# ── P3c ADDENDUM: the researcher's RESOLVED surface is exactly ["skill_view"] ──
def test_researcher_resolved_offered_surface_is_exactly_skill_view():
    # The RESOLVED value the resolver returns for the REAL researcher record —
    # NOT that the YAML contains the key, NOT that the record parses. A misspelled
    # key (offered_allowlst) is simply absent → silent fallback to the default
    # WITH read_file, and every other test stays green. This asserts the actual
    # surface and names read_file so it fails LOUD on a fallback rather than
    # passing on a subset check.
    cap = load_capabilities()["skill.fleet.researcher"]
    transport = (_emit_declaration(cap) or {}).get("transport", "sentinel")
    resolved = _resolve_offered_allowlist(cap, transport)
    assert resolved == ["skill_view"]
    assert "read_file" not in resolved


# ── malformed declaration → loud Andon ───────────────────────────────────────
def test_malformed_declaration_andons():
    cap = SimpleNamespace(id="x", governance={"offered_allowlist": "skill_view"})
    with pytest.raises(FleetWorkerAndon) as ei:
        _resolve_offered_allowlist(cap, "sentinel")
    assert ei.value.check == "offered_allowlist_malformed"
