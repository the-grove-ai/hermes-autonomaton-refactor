"""capability-mutation-surface-v1 T4 — canonical admission overlay (FAILING).

Gate P0 ruling A-3 contract:

* Canonical admission-field STATE keys: ``intents`` and ``tiers`` —
  ABSOLUTE-STATE full-list replacement via the present-key merge in
  ``_compose_state`` (state key present -> replaces the definition list
  wholesale; absent -> definition untouched). Mapping: state ``intents`` ->
  ``trigger.intents``; state ``tiers`` -> ``tier_rule.eligible``.
* Admission-field state writes carry a required ``provenance`` block
  {approval_id, timestamp, surface, write_class}; the writer refuses
  stampless writes (shape validation itself is T6).
* ``added_intents`` is LEGACY: loader-honored, never writer-emitted. The
  writer emits only the canonical keys.
* Orphaned overlay slugs (state file whose id matches no loaded definition)
  are DETECTED, not merely warn-logged: ``orphaned_state_slugs(records,
  state_dir)`` returns them.

Sole sanctioned writer (T1 pin): ``grove.capability_registry.
write_admission_state(record_id, *, intents=None, tiers=None, provenance,
state_dir=None) -> Path`` — one state file per record
(``<state_dir>/<id . -> __>.yaml``), absolute-state per-record replacement.

Hermetic: definitions come from the repo's own browser_read.yaml record dict
(read-only), state dirs are per-test tmp paths.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import grove.capability_registry as capreg
from grove.capability import Capability

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROVENANCE = {
    "approval_id": "red-1234abcd",
    "timestamp": "2026-07-21T12:00:00+00:00",
    "surface": "portal_confirm",
    "write_class": "capability_admission",
}


def _definition_cap() -> Capability:
    """A valid definition Capability with non-empty intents and tiers, built
    from the repo's real browser_read record shape (loader-faithful)."""
    doc = yaml.safe_load(
        (_REPO_ROOT / "config" / "capabilities" / "browser_read.yaml")
        .read_text(encoding="utf-8")
    )
    doc["trigger"]["intents"] = ["research_request", "code_analysis"]
    doc["tier_rule"]["eligible"] = [3]
    return Capability.from_dict(doc)


def test_state_allowlist_admits_canonical_keys():
    missing = {"intents", "tiers", "provenance"} - set(capreg._STATE_TOP_KEYS)
    assert not missing, (
        f"CONTRACT: _STATE_TOP_KEYS must admit the canonical admission keys; "
        f"missing {sorted(missing)} (added_intents stays legacy-honored)"
    )


def test_compose_state_intents_absolute_replacement():
    cap = _definition_cap()
    composed = capreg._compose_state(
        cap, {"id": cap.id, "intents": ["memory_operation"]}
    )
    assert composed.trigger.intents == ["memory_operation"], (
        "CONTRACT: state `intents` is ABSOLUTE-STATE — full-list replacement "
        f"of trigger.intents, no union; got {composed.trigger.intents!r}"
    )
    # Present-key semantics: `tiers` absent -> definition tiers untouched.
    assert composed.tier_rule.eligible == [3], (
        "present-key merge: absent `tiers` key must leave tier_rule.eligible"
    )


def test_compose_state_tiers_absolute_replacement():
    cap = _definition_cap()
    composed = capreg._compose_state(cap, {"id": cap.id, "tiers": [1, 2]})
    assert composed.tier_rule.eligible == [1, 2], (
        "CONTRACT: state `tiers` is ABSOLUTE-STATE — full-list replacement of "
        f"tier_rule.eligible; got {composed.tier_rule.eligible!r}"
    )
    # Present-key semantics: `intents` absent -> definition intents untouched.
    assert composed.trigger.intents == ["research_request", "code_analysis"], (
        "present-key merge: absent `intents` key must leave trigger.intents"
    )


def test_writer_absolute_state_per_record_replacement(tmp_path):
    writer = getattr(capreg, "write_admission_state", None)
    assert writer is not None, (
        "CONTRACT: sanctioned admission writer "
        "grove.capability_registry.write_admission_state is not implemented"
    )
    writer(
        "browser_read", intents=["research_request"],
        provenance=dict(_PROVENANCE), state_dir=tmp_path,
    )
    writer(
        "browser_read", intents=["memory_operation"],
        provenance=dict(_PROVENANCE), state_dir=tmp_path,
    )
    files = sorted(tmp_path.glob("*.yaml"))
    assert len(files) == 1, "per-record replacement: ONE state file per id"
    doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert doc["intents"] == ["memory_operation"], (
        "CONTRACT: second write REPLACES the list absolutely (no union with "
        f"the first write); got {doc.get('intents')!r}"
    )


def test_writer_rejects_stampless_write(tmp_path):
    import pytest

    writer = getattr(capreg, "write_admission_state", None)
    assert writer is not None, (
        "CONTRACT: sanctioned admission writer missing (see T1 pin)"
    )
    with pytest.raises(ValueError):
        writer(
            "browser_read", intents=["research_request"],
            provenance=None, state_dir=tmp_path,
        )
    assert not list(tmp_path.glob("*.yaml")), (
        "a refused stampless write must leave no state file behind"
    )


def test_writer_emits_only_canonical_keys_never_added_intents(tmp_path):
    writer = getattr(capreg, "write_admission_state", None)
    assert writer is not None, (
        "CONTRACT: sanctioned admission writer missing (see T1 pin)"
    )
    writer(
        "browser_read", intents=["research_request"], tiers=[2, 3],
        provenance=dict(_PROVENANCE), state_dir=tmp_path,
    )
    files = sorted(tmp_path.glob("*.yaml"))
    assert len(files) == 1
    doc = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    assert "added_intents" not in doc, (
        "CONTRACT: added_intents is legacy — loader-honored, NEVER "
        "writer-emitted"
    )
    assert set(doc) <= {"id", "intents", "tiers", "provenance"}, (
        f"CONTRACT: the writer emits only canonical keys; got {sorted(doc)}"
    )


def test_orphaned_overlay_slug_detected(tmp_path):
    detect = getattr(capreg, "orphaned_state_slugs", None)
    assert detect is not None, (
        "CONTRACT: grove.capability_registry.orphaned_state_slugs(records, "
        "state_dir) must exist — ghost state files (id matches no loaded "
        "definition) are detected, not only warn-logged at compose time"
    )
    (tmp_path / "ghost_record.yaml").write_text(
        "id: ghost_record\nintents: [research_request]\n", encoding="utf-8"
    )
    cap = _definition_cap()
    orphans = detect({cap.id: cap}, state_dir=tmp_path)
    assert any("ghost_record" in str(o) for o in orphans), (
        f"orphan detection missed the ghost slug; got {orphans!r}"
    )
