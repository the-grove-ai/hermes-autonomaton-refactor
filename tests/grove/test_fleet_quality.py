"""drafter-quality-checks-v1 P2 — fleet quality evaluator pins.

Pin families:

* VERDICT — pass / fail (below threshold; complete=False; accurate=False),
  clamped score, issues carried verbatim.
* MALFORMED — structurally invalid verdicts raise MalformedVerdict loudly
  (the cellar _validate_verdict precedent), no retry.
* SIZE GUARD (R-B3) — oversize COMBINED input (A1) returns skipped_oversize
  WITHOUT calling the evaluator; truncated content is never evaluated.
* TIER RESOLUTION (R-A5) — the record's evaluator_tier reaches both the
  call_t1 transport and the model-id resolution; default T1; unknown tier
  raises loudly.
* PROMPT FRAME (A1) — task context → rubric criteria → staged draft;
  context block absent when no context is passed.
* DECLARATION — quality_gate_declaration mirrors _emit_declaration
  (absent / error-flagged / non-mapping → None).
* GENERALIZABILITY (R-A11) — zero producer names in the module.
"""
from __future__ import annotations

import pytest

from grove.fleet import quality
from grove.fleet.quality import (
    MalformedVerdict,
    evaluate_draft,
    quality_gate_declaration,
)

_GATE = {
    "rubric_version": "1.0",
    "criteria": ["makes one falsifiable claim", "evidence is specific"],
    "threshold": 0.7,
    "redraft_limit": 1,
    "evaluator_tier": "T1",
}


class _Cap:
    id = "skill.test.gated"

    def __init__(self, gate=_GATE, error=None):
        self.governance = {}
        if gate is not None:
            self.governance["quality_gate"] = dict(gate)
        if error is not None:
            self.governance["quality_gate_error"] = error


_FILES = {"draft-unit1.md": "# Draft\n\nOne falsifiable claim, with numbers."}

_GOOD_VERDICT = {
    "complete": True,
    "accurate": True,
    "quality_score": 0.85,
    "issues": [],
}


@pytest.fixture
def eval_env(monkeypatch):
    """Stub the transport + model resolution; capture the call."""
    calls = {}

    def fake_call_t1(prompt, *, system=None, tool=None, max_tokens=None, tier=None):
        calls["prompt"] = prompt
        calls["tool"] = tool
        calls["max_tokens"] = max_tokens
        calls["tier"] = tier
        return dict(calls["verdict"])

    def fake_tier_model(tier):
        calls["model_tier"] = tier
        return "stub/model-1"

    monkeypatch.setattr(quality, "call_t1", fake_call_t1)
    monkeypatch.setattr(quality, "_tier_model", fake_tier_model)
    calls["verdict"] = dict(_GOOD_VERDICT)
    return calls


_ENVELOPE_KEYS = {
    "status", "quality_score", "complete", "accurate", "issues",
    "rubric_version", "threshold", "evaluator_tier", "evaluator_model",
    "context_keys_used", "context_keys_missing", "detail",
}


# ── VERDICT ───────────────────────────────────────────────────────────────────


def test_pass_verdict(eval_env):
    v = evaluate_draft(_Cap(), _FILES)
    assert v["status"] == "pass"
    assert v["quality_score"] == 0.85
    assert v["complete"] is True and v["accurate"] is True
    assert v["rubric_version"] == "1.0"
    assert v["threshold"] == 0.7
    assert v["evaluator_tier"] == "T1"
    assert v["evaluator_model"] == "stub/model-1"
    assert set(v) == _ENVELOPE_KEYS


def test_fail_below_threshold(eval_env):
    eval_env["verdict"] = dict(
        _GOOD_VERDICT, quality_score=0.55, issues=["buries the lede", "generic evidence"]
    )
    v = evaluate_draft(_Cap(), _FILES)
    assert v["status"] == "fail"
    assert v["quality_score"] == 0.55
    assert v["issues"] == ["buries the lede", "generic evidence"]


@pytest.mark.parametrize("flag", ["complete", "accurate"])
def test_fail_on_boolean_gate_despite_high_score(eval_env, flag):
    eval_env["verdict"] = dict(_GOOD_VERDICT, **{flag: False})
    v = evaluate_draft(_Cap(), _FILES)
    assert v["status"] == "fail"
    assert v["quality_score"] == 0.85


def test_out_of_range_score_clamped(eval_env):
    eval_env["verdict"] = dict(_GOOD_VERDICT, quality_score=1.7)
    v = evaluate_draft(_Cap(), _FILES)
    assert v["quality_score"] == 1.0
    assert v["status"] == "pass"


def test_forced_tool_shape(eval_env):
    evaluate_draft(_Cap(), _FILES)
    tool = eval_env["tool"]
    assert tool["name"] == "quality_verdict"
    props = tool["input_schema"]["properties"]
    assert set(props) == {"complete", "accurate", "quality_score", "issues"}
    assert tool["input_schema"]["required"] == [
        "complete", "accurate", "quality_score", "issues",
    ]
    assert eval_env["max_tokens"] == quality._EVAL_MAX_TOKENS


# ── MALFORMED (loud, no retry) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "prose instead of a verdict",
        {"complete": True, "accurate": True, "quality_score": 0.9},  # missing issues
        {"complete": True, "quality_score": 0.9, "issues": []},      # missing accurate
        dict(_GOOD_VERDICT, quality_score="high"),
        dict(_GOOD_VERDICT, issues="none"),
    ],
)
def test_malformed_verdict_raises(eval_env, monkeypatch, bad):
    monkeypatch.setattr(quality, "call_t1", lambda *a, **k: bad)
    with pytest.raises(MalformedVerdict):
        evaluate_draft(_Cap(), _FILES)


def test_transport_error_propagates(eval_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider 500")

    monkeypatch.setattr(quality, "call_t1", boom)
    with pytest.raises(RuntimeError, match="provider 500"):
        evaluate_draft(_Cap(), _FILES)


# ── SIZE GUARD (R-B3, combined input per A1) ─────────────────────────────────


def test_oversize_draft_skips_without_calling(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("evaluator must NOT be called on oversize input")

    monkeypatch.setattr(quality, "call_t1", forbidden)
    monkeypatch.setattr(quality, "_tier_model", forbidden)
    big = {"draft.md": "x" * (quality._EVAL_INPUT_BUDGET_CHARS + 1)}
    v = evaluate_draft(_Cap(), big)
    assert v["status"] == "skipped_oversize"
    assert v["quality_score"] is None
    assert v["complete"] is None and v["accurate"] is None
    assert v["issues"] == []
    assert v["evaluator_model"] is None
    assert "exceeds" in v["detail"]
    assert set(v) == _ENVELOPE_KEYS


def test_combined_input_counts_context(monkeypatch):
    """A draft under budget still skips when context pushes the COMBINED
    input over (A1: the guard applies to the whole assembled prompt)."""
    monkeypatch.setattr(
        quality, "call_t1",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    gate = dict(_GATE, context_inputs=["source_digest"])
    half = quality._EVAL_INPUT_BUDGET_CHARS // 2
    v = evaluate_draft(
        _Cap(gate),
        {"draft.md": "y" * half},
        task_context={"source_digest": "z" * (half + 1000)},
    )
    assert v["status"] == "skipped_oversize"


# ── TIER RESOLUTION (R-A5) ───────────────────────────────────────────────────


def test_declared_tier_reaches_transport_and_model_resolution(eval_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        quality, "_tier_model", lambda tier: seen.setdefault("tier", tier) or "m/x"
    )
    v = evaluate_draft(_Cap(dict(_GATE, evaluator_tier="T2")), _FILES)
    assert eval_env["tier"] == "T2"
    assert seen["tier"] == "T2"
    assert v["evaluator_tier"] == "T2"


def test_absent_tier_defaults_to_t1(eval_env):
    gate = {k: v for k, v in _GATE.items() if k != "evaluator_tier"}
    v = evaluate_draft(_Cap(gate), _FILES)
    assert eval_env["tier"] == "T1"
    assert v["evaluator_tier"] == "T1"


def test_unknown_tier_raises_loudly(eval_env, monkeypatch):
    def raising(tier):
        raise KeyError(f"unknown tier {tier!r}")

    monkeypatch.setattr(quality, "_tier_model", raising)
    with pytest.raises(KeyError, match="unknown tier"):
        evaluate_draft(_Cap(dict(_GATE, evaluator_tier="T9")), _FILES)


def test_tier_model_uses_public_router(monkeypatch):
    class _TC:
        model = "provider/model-slug"

    import grove.router as grove_router

    monkeypatch.setattr(grove_router, "get_tier_config", lambda t: _TC() if t == "T1" else None)
    assert quality._tier_model("T1") == "provider/model-slug"


def test_call_t1_tier_passthrough(monkeypatch):
    """t1_call resolves the caller-named tier (additive param; None → T1)."""
    import grove.t1_call as t1

    seen = {}

    def fake_resolve(tier_name=t1._T1_TIER):
        seen["tier"] = tier_name
        raise _Stop()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(t1, "_resolve_t1_runtime", fake_resolve)
    with pytest.raises(_Stop):
        t1.call_t1("p", tier="T2")
    assert seen["tier"] == "T2"
    with pytest.raises(_Stop):
        t1.call_t1("p")
    assert seen["tier"] == "T1"


# ── PROMPT FRAME (A1: context → criteria → draft) ────────────────────────────


def test_prompt_frame_order_with_context(eval_env):
    gate = dict(_GATE, context_inputs=["angle", "source"])
    evaluate_draft(
        _Cap(gate), _FILES, task_context={"angle": "contrarian", "source": "notes"}
    )
    prompt = eval_env["prompt"]
    i_ctx = prompt.index("=== TASK CONTEXT ===")
    i_crit = prompt.index("=== RUBRIC CRITERIA ===")
    i_draft = prompt.index("=== STAGED DRAFT ===")
    assert i_ctx < i_crit < i_draft
    assert "angle: contrarian" in prompt
    assert "- makes one falsifiable claim" in prompt
    assert "--- draft-unit1.md ---" in prompt


def test_prompt_omits_context_block_when_absent(eval_env):
    evaluate_draft(_Cap(), _FILES)
    prompt = eval_env["prompt"]
    assert "=== TASK CONTEXT ===" not in prompt
    assert prompt.index("=== RUBRIC CRITERIA ===") < prompt.index("=== STAGED DRAFT ===")


def test_context_keys_split_and_missing_never_fails(eval_env):
    gate = dict(_GATE, context_inputs=["angle", "source_digest"])
    v = evaluate_draft(_Cap(gate), _FILES, task_context={"angle": "x"})
    assert v["status"] == "pass"  # missing key is noted, never a failure
    assert v["context_keys_used"] == ["angle"]
    assert v["context_keys_missing"] == ["source_digest"]


def test_no_declared_context_yields_empty_splits(eval_env):
    v = evaluate_draft(_Cap(), _FILES)
    assert v["context_keys_used"] == []
    assert v["context_keys_missing"] == []


# ── DECLARATION helper ────────────────────────────────────────────────────────


def test_declaration_valid_block():
    assert quality_gate_declaration(_Cap()) == _GATE


def test_declaration_absent_and_flagged_resolve_none():
    assert quality_gate_declaration(_Cap(gate=None)) is None
    assert quality_gate_declaration(_Cap(error="bad shape")) is None

    class _NoGov:
        pass

    class _NonMapping:
        governance = "nope"

    assert quality_gate_declaration(_NoGov()) is None
    assert quality_gate_declaration(_NonMapping()) is None


def test_evaluate_without_gate_raises():
    with pytest.raises(ValueError, match="quality_gate"):
        evaluate_draft(_Cap(gate=None), _FILES)


# ── GENERALIZABILITY (R-A11) ─────────────────────────────────────────────────


def test_module_names_no_producers():
    import inspect

    src = inspect.getsource(quality)
    for producer in ("drafter", "cultivator", "scout", "researcher", "forge"):
        assert producer not in src, f"producer name {producer!r} leaked into quality.py"
