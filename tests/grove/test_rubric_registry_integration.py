"""artifact-review-v1 P2 — REAL registry + REAL records integration guard.

This is the test that protects the ship (RULING A.2). The schema pins in
test_quality_gate_schema.py resolve against a STUB registry, so they cannot
see a key-format drift in the real config/rubrics.yaml or a rubric_ref typo
in a migrated record. This file asserts against the REAL artifacts.

Two halves, landing where each can be honestly green:

* REGISTRY-SIDE (live now): config/rubrics.yaml is well-formed and declares
  the two Phase-1 classes with non-empty criteria and valid thresholds.
  Catches a key-format drift / malformed criterion in the registry.

* RECORD-SIDE (skipped until P5): each live fleet record loads with no
  quality_gate_error and its rubric_ref resolves to a present entry. Until
  the P5 migration, the live records still embed the rubric (criteria +
  rubric_version) and validate to a quality_gate_error BY DESIGN — so this
  guard is skipped now and UNSKIPPED as part of P5. It is the test that
  catches a P5 migration typo, so it must be a real (not xfail-masked)
  assertion at the moment the records change.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from grove.capability import Capability
from grove.fleet.rubric_registry import load_rubric_registry

_REPO = Path(__file__).resolve().parents[2]
_CAPS = _REPO / "config" / "capabilities"

# The Phase-1 artifact classes (R-5). CLASS keys, not producer names (R-12).
_PHASE1_CLASSES = ["resume-package@1", "longform-argument@1"]

# Live record file -> the class its rubric_ref must name after P5 migration.
_MIGRATED_RECORDS = [
    ("skill__fleet__forge_jobsearch.yaml", "resume-package@1"),
    ("skill__fleet__drafter.yaml", "longform-argument@1"),
]


# ── REGISTRY-SIDE (live now) ─────────────────────────────────────────────────


@pytest.mark.parametrize("class_key", _PHASE1_CLASSES)
def test_registry_declares_phase1_class(class_key):
    reg = load_rubric_registry()  # the REAL config/rubrics.yaml
    rubric = reg.get(class_key)
    assert rubric is not None, f"config/rubrics.yaml must declare {class_key!r}"
    assert rubric.criteria, f"{class_key} criteria must be non-empty"
    assert all(c.type == "llm" for c in rubric.criteria), "Phase 1 criteria are llm"
    ids = [c.id for c in rubric.criteria]
    assert len(ids) == len(set(ids)), f"{class_key} criterion ids must be unique"
    assert all(c.id and c.definition for c in rubric.criteria), "ids/definitions non-empty"
    assert 0.0 <= rubric.default_threshold <= 1.0, "default_threshold in [0.0, 1.0]"


def test_registry_loads_without_error():
    """The real registry parses (no ValueError from the loader)."""
    reg = load_rubric_registry()
    assert set(_PHASE1_CLASSES).issubset(set(reg.rubrics))


# ── RECORD-SIDE (skipped until P5 migration) ─────────────────────────────────


@pytest.mark.parametrize("filename,expected_class", _MIGRATED_RECORDS)
def test_live_record_rubric_ref_resolves(filename, expected_class):
    d = _yaml.safe_load((_CAPS / filename).read_text(encoding="utf-8"))
    cap = Capability.from_dict(d)
    gov = cap.governance
    assert "quality_gate" in gov, f"{filename} must declare a quality_gate"
    assert "quality_gate_error" not in gov, (
        f"{filename} quality_gate must load clean: {gov.get('quality_gate_error')!r}"
    )
    ref = gov["quality_gate"]["rubric_ref"]
    assert ref == expected_class, f"{filename} rubric_ref must be {expected_class!r}"
    rubric = load_rubric_registry().get(ref)
    assert rubric is not None, f"{ref} must resolve in config/rubrics.yaml"
    assert rubric.criteria, f"{ref} resolved criteria must be non-empty"
