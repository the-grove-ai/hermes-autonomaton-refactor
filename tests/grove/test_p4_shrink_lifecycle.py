"""retrieval-ambient-class-v1 P4 — seal-time shrink guard + lifecycle wall.

* GATE-B ruling D: a write_admission_state claim that removes any present
  intent/tier seals with ``revocation: true`` + a removed-grants manifest —
  computed through the P3 SHARED derivation (spy-pinned). Additive/identical
  proposals seal unchanged. Silent shrink is structurally impossible;
  deliberate shrink stays legal.
* GATE-B ruling B (narrowed): DEPRECATED/SUSPENDED yields NULL effective
  capability — not admitted, not disclosed, lifecycle outranks the disclosure
  class (baseline included). The effective-state read reports the reason.
"""

from types import SimpleNamespace

import pytest
import yaml

import grove.capability_registry as capreg
from grove.capability import Capability, LifecycleState, NULL_CAPABILITY_STATES
from grove.context_budget import (
    _registry_allowed_names,
    reset_caps_index_cache,
)
from grove.disclosure import disclosure_split_sets, reset_disclosure_split_cache
from grove.red_pending_store import _admission_shrink_diff


@pytest.fixture(autouse=True)
def _fresh():
    reset_caps_index_cache()
    reset_disclosure_split_cache()
    yield
    reset_caps_index_cache()
    reset_disclosure_split_cache()


def _effective_stub(intents, tiers, src="definition"):
    return {
        "records": {
            "probe_rec": {
                "zone": "green", "disclosure": "baseline", "kind": "verb",
                "lifecycle_state": "approved", "admissible": True,
                "null_capability_reason": None,
                "has_state": src != "definition",
                "fields": [
                    {"field": "intents", "base": intents, "effective": intents,
                     "source": src},
                    {"field": "tiers", "base": tiers, "effective": tiers,
                     "source": src},
                ],
                "provenance": None, "legacy_added_intents": False,
            }
        },
        "invalid": [], "orphans": [], "state_dir_missing": False,
    }


# ── shrink detection (ruling D) ─────────────────────────────────────────────


def test_intent_removal_seals_revocation(monkeypatch):
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["research", "retrieval"], [1, 2, 3]),
    )
    verdict = _admission_shrink_diff({"id": "probe_rec",
                                      "intents": ["research"], "tiers": None})
    assert verdict["revocation"] is True
    assert verdict["removed_grants"]["intents_removed"] == ["retrieval"]
    assert verdict["removed_grants"]["tiers_removed"] == []


def test_tier_removal_seals_revocation(monkeypatch):
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["research"], [1, 2, 3]),
    )
    verdict = _admission_shrink_diff({"id": "probe_rec",
                                      "intents": None, "tiers": [2, 3]})
    assert verdict["revocation"] is True
    assert verdict["removed_grants"]["tiers_removed"] == [1]
    assert verdict["removed_grants"]["intents_removed"] == []


def test_both_removed(monkeypatch):
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["a", "b"], [1, 2]),
    )
    verdict = _admission_shrink_diff({"id": "probe_rec",
                                      "intents": ["a"], "tiers": [2]})
    assert verdict["removed_grants"]["intents_removed"] == ["b"]
    assert verdict["removed_grants"]["tiers_removed"] == [1]


def test_additive_and_identical_seal_unflagged(monkeypatch):
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["a"], [2]),
    )
    assert _admission_shrink_diff(
        {"id": "probe_rec", "intents": ["a", "b"], "tiers": [1, 2, 3]}
    ) == {}
    assert _admission_shrink_diff(
        {"id": "probe_rec", "intents": ["a"], "tiers": [2]}
    ) == {}


def test_unknown_record_and_absent_fields_unflagged(monkeypatch):
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["a"], [2]),
    )
    assert _admission_shrink_diff({"id": "ghost", "intents": [], "tiers": None}) == {}
    # a doc omitting intents proposes no intents change — no intent removal.
    assert _admission_shrink_diff({"id": "probe_rec", "intents": None,
                                   "tiers": [2]}) == {}


def test_pre_canonical_source_annotated(monkeypatch):
    src = "overlay · NO PROVENANCE (pre-canonical)"
    monkeypatch.setattr(
        capreg, "effective_admission_state",
        lambda *a, **k: _effective_stub(["a", "b"], [2], src=src),
    )
    verdict = _admission_shrink_diff({"id": "probe_rec", "intents": ["a"],
                                      "tiers": None})
    assert verdict["removed_grants"]["present_state_source"]["intents"] == src


def test_guard_binds_shared_derivation(monkeypatch):
    hits = []

    def _spy(*a, **k):
        hits.append("call")
        return _effective_stub(["a"], [2])

    monkeypatch.setattr(capreg, "effective_admission_state", _spy)
    _admission_shrink_diff({"id": "probe_rec", "intents": [], "tiers": None})
    assert hits == ["call"], "the guard must bind the P3 shared derivation"


# ── card legibility (ruling D + c29a658b8 precedent) ────────────────────────


class _FakeStore:
    def __init__(self, manifest):
        self._m = manifest

    def masked_description(self, pid):
        return "admission-state write: browser_read"

    def is_credential_write(self, pid):
        return False

    def is_opaque(self, pid):
        return False

    def card_title(self, pid):
        return "RED — governance write"

    def card_reason(self, pid):
        return None

    def revocation_manifest(self, pid):
        return self._m


def test_card_renders_removed_grants_explicitly():
    from grove.api.fragments import _render_red_proposal_card

    store = _FakeStore({
        "intents_removed": ["system_admin"],
        "tiers_removed": [1],
        "present_state_source": {
            "intents": "overlay · approval 6e191599",
            "tiers": "overlay · approval 6e191599",
        },
    })
    req = SimpleNamespace(app={"red_pending_store": store,
                               "red_nonce_key": b"k" * 32})
    html = _render_red_proposal_card(req, "redpending:abc123", "abc123")
    assert "REVOCATION" in html
    assert "intents removed: system_admin" in html
    assert "tiers removed: 1" in html
    assert "overlay · approval 6e191599" in html


def test_card_without_revocation_has_no_block():
    from grove.api.fragments import _render_red_proposal_card

    store = _FakeStore(None)
    req = SimpleNamespace(app={"red_pending_store": store,
                               "red_nonce_key": b"k" * 32})
    html = _render_red_proposal_card(req, "redpending:abc123", "abc123")
    assert "REVOCATION" not in html


# ── lifecycle wall (ruling B, narrowed) ─────────────────────────────────────


def _full_doc(rid, tools, disclosure="baseline", state="approved"):
    return {
        "id": rid, "kind": "verb",
        "trigger": {"intents": [], "keywords": [], "dock_affinity": [],
                    "always": True, "disclosure": disclosure},
        "bindings": {"tools": tools, "credentials": None, "toolset_key": None},
        "tier_rule": {"eligible": [1, 2, 3], "preferred": 1,
                      "promotion_criteria": {},
                      "validation": {"strategy": "shadow_compare",
                                     "confidence_threshold": 0.95,
                                     "shadow_window": 20}},
        "zone": "green",
        "telemetry": {"feed": "intent_feed", "track": ["invocation"]},
        "context": {"disclosure": "eager", "payload": "p",
                    "dock_composition": "none"},
        "lifecycle": {"state": state, "provenance": "operator_authored",
                      "created_at": "2026-07-21T00:00:00+00:00",
                      "last_used": None, "use_count": 0,
                      "flywheel_eligible": True},
        "lineage": {"source_patterns": [], "parent_id": None,
                    "decision_log": []},
        "failure": {"fallback": "halt_and_surface", "diagnostic_context": [],
                    "circuit_breaker": {"threshold": 3, "window_seconds": 300}},
    }


def _patched_registry(monkeypatch, docs):
    caps = {d["id"]: Capability.from_dict(d) for d in docs}
    monkeypatch.setattr(capreg, "load_capabilities", lambda *a, **k: caps)
    reset_caps_index_cache()
    reset_disclosure_split_cache()
    return caps


@pytest.mark.parametrize("state", ["deprecated", "suspended"])
def test_null_lifecycle_blocks_admission_even_for_baseline(monkeypatch, state):
    _patched_registry(monkeypatch, [
        _full_doc("dead_rec", ["dead_tool"], disclosure="baseline", state=state),
        _full_doc("live_rec", ["live_tool"], disclosure="baseline"),
    ])
    for intent in ("retrieval", "unknown"):
        allowed = _registry_allowed_names(intent, "simple")
        assert "live_tool" in allowed, intent
        assert "dead_tool" not in allowed, (
            f"{state}: lifecycle must outrank the baseline disclosure class"
        )


@pytest.mark.parametrize("state", ["deprecated", "suspended"])
def test_null_lifecycle_blocks_disclosure_split(monkeypatch, state):
    _patched_registry(monkeypatch, [
        _full_doc("dead_rec", ["dead_tool"], disclosure="baseline", state=state),
        _full_doc("live_rec", ["live_tool"], disclosure="baseline"),
    ])
    baseline, core, _ = disclosure_split_sets()
    assert "live_tool" in baseline
    assert "dead_tool" not in baseline and "dead_tool" not in core


def test_active_states_unaffected(monkeypatch):
    _patched_registry(monkeypatch, [
        _full_doc("a", ["t_approved"], state="approved"),
        _full_doc("b", ["t_active"], state="active"),
    ])
    allowed = _registry_allowed_names("retrieval", "simple")
    assert {"t_approved", "t_active"} <= allowed


def test_suspended_has_no_transition_edges():
    from grove.capability import LEGAL_TRANSITIONS

    assert LEGAL_TRANSITIONS.get(LifecycleState.SUSPENDED, frozenset()) == frozenset()
    for _from, tos in LEGAL_TRANSITIONS.items():
        assert LifecycleState.SUSPENDED not in tos, (
            "SUSPENDED is definition-side only — no runtime path in"
        )


def test_effective_state_read_reports_null_capability(tmp_path):
    d = tmp_path / "defs"
    d.mkdir()
    (d / "dead.yaml").write_text(
        yaml.safe_dump(_full_doc("dead_rec", ["dead_tool"], state="suspended")),
        encoding="utf-8",
    )
    composed = capreg.effective_admission_state(definitions_dirs=[d])
    rec = composed["records"]["dead_rec"]
    assert rec["admissible"] is False
    assert rec["lifecycle_state"] == "suspended"
    assert "null effective capability" in rec["null_capability_reason"]
    assert "lifecycle outranks baseline" in rec["null_capability_reason"]


def test_null_states_are_exactly_deprecated_and_suspended():
    assert {s.value for s in NULL_CAPABILITY_STATES} == {"deprecated", "suspended"}
