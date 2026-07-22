"""retrieval-ambient-class-v1 P3 — effective-state read + shared derivation.

Proofs:
* the tool and the portal fragment bind the SAME derivation
  (``capability_registry.effective_admission_state`` → ``_compose_state``) —
  no parallel merge logic survives on either consumer;
* an overlay-carrying record (the browser_read live-state shape) reports its
  effective intents/tiers with ``overlay · approval <id>`` source tags;
* the tool itself rides the ambient baseline class (a visibility diagnostic
  that could be invisible recreates the problem it answers).
"""

import json

import pytest
import yaml

import grove.capability_registry as capreg
from grove.capability_registry import effective_admission_state
from grove.context_budget import (
    _registry_allowed_names,
    reset_caps_index_cache,
)
from grove.disclosure import disclosure_split_sets, reset_disclosure_split_cache
from tools.read_capability_state_tool import (
    READ_CAPABILITY_STATE_SCHEMA,
    read_capability_state,
)

# The live browser_read overlay shape (VM state, approval 6e191599…) — the
# Exhibit 5 question this tool exists to answer.
BROWSER_READ_STATE = {
    "id": "browser_read",
    "intents": ["research", "retrieval", "factual_lookup", "system_admin"],
    "tiers": [1, 2, 3],
    "provenance": {
        "approval_id": "6e191599cef89c599260152962b4efcb",
        "timestamp": "2026-07-21T20:26:21.357030+00:00",
        "surface": "red_approval",
        "write_class": "capability_admission",
    },
}


@pytest.fixture(autouse=True)
def _fresh():
    reset_caps_index_cache()
    reset_disclosure_split_cache()
    yield
    reset_caps_index_cache()
    reset_disclosure_split_cache()


def _state_dir(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "browser_read.yaml").write_text(
        yaml.safe_dump(BROWSER_READ_STATE), encoding="utf-8"
    )
    return sd


# ── overlay state resolves through the composed path ────────────────────────


def test_browser_read_overlay_reports_effective_state(tmp_path):
    composed = effective_admission_state(state_dir=_state_dir(tmp_path))
    rec = composed["records"]["browser_read"]
    assert rec["has_state"] is True
    by_field = {f["field"]: f for f in rec["fields"]}
    assert by_field["intents"]["effective"] == [
        "research", "retrieval", "factual_lookup", "system_admin",
    ]
    assert by_field["tiers"]["effective"] == [1, 2, 3]
    src = "overlay · approval 6e191599cef89c599260152962b4efcb"
    assert by_field["intents"]["source"] == src
    assert by_field["tiers"]["source"] == src
    # browser_read definition prefers T3, which survives the widened [1,2,3]
    # eligible set — preferred stays definition-sourced (no re-anchor).
    assert by_field["preferred"]["source"] == "definition"
    assert rec["disclosure"] == "baseline"      # P6.1 flip; overlay can't change class
    assert rec["zone"] == "green"


def test_effective_values_flow_through_compose_state(tmp_path, monkeypatch):
    # The helper must call _compose_state — the sole merge authority — not
    # re-derive. Sentinel-patch it and watch the sentinel value land.
    sd = _state_dir(tmp_path)
    calls = []
    real = capreg._compose_state

    def _spy(cap, state):
        calls.append(cap.id)
        return real(cap, state)

    monkeypatch.setattr(capreg, "_compose_state", _spy)
    effective_admission_state(state_dir=sd)
    assert calls == ["browser_read"], "effective values must come from _compose_state"


# ── shared derivation: tool and fragment bind the SAME function ─────────────


def test_tool_and_fragment_share_one_derivation(monkeypatch):
    from grove.api import fragments

    hits = []
    sentinel = {
        "records": {
            "probe": {
                "zone": "green", "disclosure": "baseline", "kind": "verb",
                "has_state": True,
                "fields": [{"field": "intents", "base": [], "effective": ["x"],
                            "source": "overlay · approval abc"}],
                "provenance": {"approval_id": "abc", "timestamp": "t"},
                "legacy_added_intents": False,
            }
        },
        "invalid": [], "orphans": [], "state_dir_missing": False,
    }

    def _stub(definitions_dirs=None, state_dir=None):
        hits.append("call")
        return sentinel

    monkeypatch.setattr(capreg, "effective_admission_state", _stub)

    tool_out = json.loads(read_capability_state("probe"))
    html = fragments.render_admission_state_html()

    assert hits == ["call", "call"], (
        "BOTH consumers must bind capability_registry.effective_admission_state"
    )
    assert tool_out["fields"][0]["source"] == "overlay · approval abc"
    assert "overlay · approval abc" in html
    assert "probe" in html


def test_fragment_has_no_residual_merge_logic():
    import inspect

    from grove.api import fragments

    src = inspect.getsource(fragments.render_admission_state_html)
    # The render-side merge fingerprints must be gone.
    assert "_read_state_file" not in src
    assert "max(state" not in src
    assert "effective_admission_state" in src


# ── the tool itself is baseline (self-invisibility guard) ───────────────────


def test_read_capability_state_is_baseline_everywhere():
    baseline, _core, _ = disclosure_split_sets()
    assert "read_capability_state" in baseline
    for intent in ("conversation", "system_admin", "unknown"):
        allowed = _registry_allowed_names(intent, "simple")
        assert "read_capability_state" in allowed, intent


# ── contract text (GATE-B ruling F) ─────────────────────────────────────────


def test_description_is_spec_locked_not_a_preauth_check():
    d = READ_CAPABILITY_STATE_SCHEMA["description"]
    assert "NOT a pre-authorization check" in d
    assert "the dispatcher is the sole authority" in d
    assert "attempt the action and let governance rule" in d


def test_unknown_record_id_is_a_loud_error():
    out = json.loads(read_capability_state("no_such_record_zzz"))
    assert "error" in out and "no_such_record_zzz" in out["error"]
