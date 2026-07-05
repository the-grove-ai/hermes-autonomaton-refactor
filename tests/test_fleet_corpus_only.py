"""fleet-corpus-only-offering-v1 P1 — the required_tools capability FIELD.

The P5 config-derived deny-complement (``worker_entry._corpus_only_admission``)
was RETIRED in fleet-corpus-only-offering-v1 P1: it keyed on a 'fleet' platform
the Dispatcher never carried (default 'cli'), so it silently never applied (the
leg-1 write_file escape). The corpus-only surface is now a config-blind L2 floor
hardcoded in the Dispatcher — see ``tests/test_fleet_tool_floor.py``.

What remains here is the ``required_tools`` record-field mechanics (round-trip +
structural fail-loud + all-records-load). NOTE: the field has NO runtime consumer
after the P5 retirement — the L2 floor is hardcoded, not derived from the record.
The forge record still declares ``[read_file, invoke_skill]``; that value is now
vestigial (and inconsistent with the {read_file, skill_view} floor) pending an
operator decision to update or retire the field.
"""

from __future__ import annotations

import pytest

from grove.capability import Capability
from grove.capability_registry import load_capabilities


def _forge():
    return load_capabilities()["skill.fleet.forge-jobsearch"]


# ── required_tools record field ──────────────────────────────────────────────


def test_required_tools_round_trip_present_key():
    f = _forge()
    assert f.required_tools == ["read_file", "invoke_skill"]
    d = f.to_dict()
    assert d["required_tools"] == ["read_file", "invoke_skill"]
    assert Capability.from_yaml(f.to_yaml()).required_tools == ["read_file", "invoke_skill"]


def test_required_tools_empty_not_emitted():
    rec = load_capabilities()["read_file"]  # a plain verb, no required_tools
    assert rec.required_tools == []
    assert "required_tools" not in rec.to_dict()  # byte-identical when empty


def test_required_tools_structural_fail_loud():
    base = _forge().to_dict()
    with pytest.raises(ValueError, match="must not repeat"):
        Capability.from_dict({**base, "required_tools": ["read_file", "read_file"]})
    with pytest.raises(ValueError, match="non-empty strings"):
        Capability.from_dict({**base, "required_tools": ["read_file", ""]})


def test_all_records_still_load():
    assert len(load_capabilities()) > 100
