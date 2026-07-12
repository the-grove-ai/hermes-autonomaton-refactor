"""binding-telemetry-v1 P4 — evidence table on the model_binding offering frame.

Pin families:

* TABLE — arms rows (n, success %, score mean ± variance, redraft %),
  annotations, class + window on both the summary tag and the diff.
* PRE-P4 PARITY — a hand-filed proposal (no evidence_block) renders the
  summary and diff byte-identically to the shipped carrier.
* DEGRADE VISIBLE — malformed evidence values render as strings; the
  renderer never raises (a diff_renderer exception = DEFECT card, withheld
  dispositions); the whole diff survives yaml.safe_dump (the portal's
  escaped <pre> path).
"""
from __future__ import annotations

from types import SimpleNamespace

import yaml

from grove.kaizen.rendering import (
    _model_binding_to_diff,
    _summary_model_binding,
)


def _proposal(payload):
    return SimpleNamespace(payload=payload, semantic_justification="")


_BASE = {
    "skill": "alpha",
    "proposed_binding": {"type": "model", "model": "prov-b/cand"},
    "previous_binding": {"type": "model", "model": "prov-a/base"},
}

_EB = {
    "class": "downgrade",
    "window_days": 30,
    "baseline_model": "prov-a/base",
    "rubric_version": "1.0",
    "observed_models": ["prov-a/base", "prov-b/cand"],
    "arms": [
        {
            "model": "prov-a/base", "n": 5, "success_rate": 1.0,
            "scored_n": 5, "score_mean": 0.7, "score_variance": 0.002,
            "redraft_rate": 0.2, "comparability_key": ["1.0", "prov-q/judge-1"],
            "self_judged": False, "family_judged": False, "mixed_judge": False,
        },
        {
            "model": "prov-b/cand", "n": 5, "success_rate": 0.8,
            "scored_n": 4, "score_mean": 0.85, "score_variance": 0.001,
            "redraft_rate": 0.25, "comparability_key": ["1.0", "prov-q/judge-1"],
            "self_judged": True, "family_judged": False, "mixed_judge": True,
        },
    ],
    "annotations": ["self_judged:prov-b/cand", "mixed_judge:prov-b/cand"],
}


# ── TABLE ────────────────────────────────────────────────────────────────────


def test_summary_carries_evidence_tag():
    s = _summary_model_binding(_proposal({**_BASE, "evidence_block": _EB}))
    assert s.startswith("Pin 'alpha' to prov-b/cand for fleet runs")
    assert s.endswith("Evidence: downgrade class, 2 arm(s) over 30d.")


def test_diff_carries_formatted_arm_rows_and_annotations():
    d = _model_binding_to_diff(_proposal({**_BASE, "evidence_block": _EB}))
    ev = d["evidence"]
    assert ev["class"] == "downgrade" and ev["window"] == "30d"
    assert ev["rubric_version"] == "1.0"
    assert ev["observed_models"] == ["prov-a/base", "prov-b/cand"]
    assert ev["arms"][0] == (
        "prov-a/base: n=5, success 100%, score 0.7 ±0.002, redraft 20%"
    )
    assert ev["arms"][1] == (
        "prov-b/cand: n=5, success 80%, score 0.85 ±0.001, redraft 25% "
        "[self-judged, mixed-judge]"
    )
    assert ev["annotations"] == ["self_judged:prov-b/cand", "mixed_judge:prov-b/cand"]
    # the record-change half is untouched by the evidence addition:
    assert d["capability record: alpha"]["model_binding"]["+after"] == (
        _BASE["proposed_binding"]
    )


def test_scoreless_parity_arms_render_dashes():
    eb = {
        "class": "parity", "window_days": 30, "rubric_version": None,
        "observed_models": ["a/x", "b/y"],
        "arms": [{
            "model": "a/x", "n": 6, "success_rate": 1.0, "scored_n": 0,
            "score_mean": None, "score_variance": None, "redraft_rate": None,
            "comparability_key": None, "self_judged": False,
            "family_judged": False, "mixed_judge": False,
        }],
        "annotations": [],
    }
    d = _model_binding_to_diff(_proposal({**_BASE, "evidence_block": eb}))
    assert d["evidence"]["arms"][0] == "a/x: n=6, success 100%, score —, redraft —"


# ── PRE-P4 PARITY ────────────────────────────────────────────────────────────


def test_handfiled_proposal_renders_byte_identical_to_pre_p4():
    p = _proposal(dict(_BASE))
    assert _summary_model_binding(p) == (
        "Pin 'alpha' to prov-b/cand for fleet runs (currently pinned to "
        "prov-a/base). Apply?"
    )
    d = _model_binding_to_diff(p)
    assert "evidence" not in d
    assert set(d) == {"capability record: alpha"}


def test_clear_pin_summary_unchanged():
    p = _proposal({"skill": "alpha", "proposed_binding": None,
                   "previous_binding": {"type": "model", "model": "prov-a/base"}})
    s = _summary_model_binding(p)
    assert s == (
        "Clear the model pin on 'alpha' (currently pinned to prov-a/base) — "
        "it returns to tier inheritance. Apply?"
    )


# ── DEGRADE VISIBLE ──────────────────────────────────────────────────────────


def test_malformed_evidence_values_render_as_strings_never_raise():
    eb = {
        "class": None,                      # → "None" via str
        "window_days": None,
        "observed_models": "not-a-list",    # falsy-guarded shapes vary
        "arms": [
            "just a string arm",
            {"model": "x/y", "n": "many", "success_rate": "high",
             "score_mean": "0.9?", "score_variance": None,
             "redraft_rate": object()},
        ],
        "annotations": [1, 2],
    }
    d = _model_binding_to_diff(_proposal({**_BASE, "evidence_block": eb}))
    ev = d["evidence"]
    assert ev["arms"][0] == "just a string arm"
    assert "x/y: n=many, success high, score 0.9?" in ev["arms"][1]
    assert ev["annotations"] == ["1", "2"]
    s = _summary_model_binding(_proposal({**_BASE, "evidence_block": eb}))
    assert "Evidence: None class" in s  # visible, honest, not a 500


def test_diff_survives_yaml_safe_dump():
    """The portal's approvable card path: yaml.safe_dump(diff) inside an
    escaped <pre>. A non-serializable value would DEFECT-card the proposal."""
    d = _model_binding_to_diff(_proposal({**_BASE, "evidence_block": _EB}))
    text = yaml.safe_dump(d, sort_keys=False, default_flow_style=False)
    assert "prov-b/cand: n=5, success 80%" in text
    # yaml may soft-wrap long rows — assert the tokens, not the joined pair
    assert "self-judged" in text and "mixed-judge" in text
