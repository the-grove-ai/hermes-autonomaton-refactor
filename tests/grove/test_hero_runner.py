"""Sprint 46 — meta-tests for the hero-prompts regression harness.

The harness itself is what we test here, not the live pipeline.
Production classification is mocked at the ``classifier`` injection
seam so meta-tests run without burning a T-telemetry call.

Coverage (operator-mandated at GATE-A):

* Does the harness correctly detect a broken classification?
* Does the harness correctly flag an unexpected Andon halt?
* Does ``gate_proposal`` enforce the Sprint 47 fail-loud contract?
* Does ``gate_proposal`` evaluate against the current state and
  return a structured ``GateResult``?
* Does ``load_hero_prompts`` reject malformed shapes?
* Does the harness exercise GRV-008 § I's lettered assertions
  (intent / tier / tools / andon)?
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from grove.classify import ClassificationResult
from grove.eval import (
    AssertionFailure,
    EvalReport,
    GateResult,
    HeroPrompt,
    PromptResult,
    evaluate,
    gate_proposal,
    load_hero_prompts,
)


_PATTERN_HASH = "a" * 64


def _classification(
    *,
    intent_class: str = "planning",
    complexity: str = "moderate",
    register: str = "strategic",
    confidence: float = 0.85,
    is_correction: Optional[bool] = False,
) -> ClassificationResult:
    return ClassificationResult(
        intent_class=intent_class,
        pattern_hash=_PATTERN_HASH,
        confidence=confidence,
        register_class=register,
        complexity_signal=complexity,
        goal_alignment=None,
        is_correction=is_correction,
    )


def _prompt(**expected) -> HeroPrompt:
    base = {
        "intent_class": "planning",
        "complexity_signal_in": ["moderate", "complex"],
        "tier_in": ["T2", "T3"],
        "tools_must_include": [],
        "tools_must_not_include": [],
        "is_correction": False,
        "andon_halt": False,
    }
    base.update(expected)
    return HeroPrompt(
        id="meta-test",
        message="draft a plan",
        expected=base,
    )


# ── Live hero prompts file loads cleanly ─────────────────────────────


class TestLoadHeroPrompts:
    def test_default_path_loads_full_v2_coverage(self) -> None:
        # Sprint 54 (intent-taxonomy-v2): 15-of-15 INTENT_CLASSES coverage
        # plus 1 correction-trigger and 1 unknown-fallback = 18 prompts.
        prompts = load_hero_prompts()
        assert len(prompts) == 18
        ids = {p.id for p in prompts}
        # Knowledge / architect work — Sprint 12 intents that survive
        # into v2 unchanged.
        assert "code-gen-moderate" in ids
        assert "debug-simple" in ids
        assert "debug-novel" in ids
        assert "analysis-complex" in ids
        assert "planning-moderate" in ids
        assert "creative-moderate" in ids
        assert "sysadmin-simple" in ids
        assert "conversation-simple" in ids
        assert "correction-trigger" in ids
        assert "unknown-fallback" in ids
        # Sprint 54 — daily-driver / T1-native (and one new T2-native:
        # ``research``).  Mandatory operator requirement: every new
        # intent must have at least one hero entry so the Flywheel's
        # TierRatchet has evaluation surface for proposals against it.
        assert "factual-simple" in ids
        assert "memory-operation-simple" in ids
        assert "scheduling-simple" in ids
        assert "messaging-simple" in ids
        assert "retrieval-cached-artifact" in ids
        assert "summarization-simple" in ids
        assert "translation-simple" in ids
        assert "research-moderate" in ids

    def test_missing_path_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_hero_prompts(tmp_path / "nonexistent.yaml")

    def test_malformed_root_raises_value_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("[]\n")
        with pytest.raises(ValueError, match="hero_prompts"):
            load_hero_prompts(bad)

    def test_entry_missing_required_key_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("hero_prompts:\n  - id: x\n    message: y\n")
        with pytest.raises(ValueError, match="expected"):
            load_hero_prompts(bad)


# ── GRV-008 § I.a: intent assertion ──────────────────────────────────


class TestIntentAssertion:
    def test_exact_match_passes(self) -> None:
        report = evaluate(
            [_prompt(intent_class="planning")],
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        assert report.passed
        assert report.results[0].failures == ()

    def test_intent_divergence_reports_failure(self) -> None:
        report = evaluate(
            [_prompt(intent_class="planning")],
            classifier=lambda msg: _classification(intent_class="code_generation"),
        )
        result = report.results[0]
        assert not result.passed
        intent_failures = [f for f in result.failures if f.kind == "intent"]
        assert len(intent_failures) == 1
        assert intent_failures[0].expected == "planning"
        assert intent_failures[0].observed == "code_generation"


# ── GRV-008 § I.b: tier assertion ────────────────────────────────────


class TestTierAssertion:
    def test_tier_in_set_passes(self) -> None:
        # Live router routes "planning" + "moderate" + 0.85 confidence to T2.
        report = evaluate(
            [_prompt(intent_class="planning", tier_in=["T2", "T3"])],
            classifier=lambda msg: _classification(
                intent_class="planning", complexity="moderate"
            ),
        )
        result = report.results[0]
        tier_failures = [f for f in result.failures if f.kind == "tier"]
        assert not tier_failures
        assert result.observed_tier in ("T2", "T3")

    def test_tier_out_of_set_reports_failure(self) -> None:
        # The router puts moderate-planning at T2; expecting only T1 fails.
        report = evaluate(
            [_prompt(intent_class="planning", tier_in=["T1"])],
            classifier=lambda msg: _classification(
                intent_class="planning", complexity="moderate"
            ),
        )
        result = report.results[0]
        tier_failures = [f for f in result.failures if f.kind == "tier"]
        assert len(tier_failures) == 1
        assert tier_failures[0].expected == ["T1"]


# ── GRV-008 § I.c: tools assertion ──────────────────────────────────


class TestToolsAssertion:
    def test_must_include_present_passes(self) -> None:
        report = evaluate(
            [_prompt(
                intent_class="planning",
                tools_must_include=["write_file"],
            )],
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        result = report.results[0]
        tool_failures = [f for f in result.failures if f.kind == "tools"]
        assert not tool_failures

    def test_must_include_missing_reports_failure(self) -> None:
        # execute_code is not offered for conversation intent — it requires
        # code_generation or debugging context.  write_file was used here
        # previously but is now admitted broadly (propose_governance_change
        # trigger.always: true pulled it into the conversation surface as
        # part of tool-admission-unification).
        report = evaluate(
            [_prompt(
                intent_class="conversation",
                tools_must_include=["execute_code"],
            )],
            classifier=lambda msg: _classification(intent_class="conversation"),
        )
        result = report.results[0]
        tool_failures = [f for f in result.failures if f.kind == "tools"]
        assert any("must include" in f.expected for f in tool_failures)

    def test_must_not_include_present_reports_failure(self) -> None:
        report = evaluate(
            [_prompt(
                intent_class="planning",
                tools_must_not_include=["write_file"],
            )],
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        result = report.results[0]
        tool_failures = [f for f in result.failures if f.kind == "tools"]
        assert any("must not include" in f.expected for f in tool_failures)

    def test_unknown_intent_core_only_passes(self) -> None:
        # fallback-retirement-v1: an unknown / None classification yields the
        # always:true CORE (never None / maximal). The prompt asserts the
        # deterministic core via must-include / must-not-include — the REAL
        # GRV-008 § I(c) ("tool composition sequences remain unbroken"). No phantom
        # tool_set_is_maximal_fallback flag; observed_tools is the core set.
        prompt = HeroPrompt(
            id="meta",
            message=" ",
            expected={
                "intent_class": "unknown",
                "complexity_signal_in": ["simple"],
                "tier_in": ["T2"],
                "tools_must_include": ["clarify", "read_file"],
                "tools_must_not_include": ["execute_code", "delegate_task"],
                "is_correction": False,
                "andon_halt": False,
            },
        )
        # Classification returns None (degenerate path) -> core-only surface.
        report = evaluate([prompt], classifier=lambda msg: None)
        result = report.results[0]
        tool_failures = [f for f in result.failures if f.kind == "tools"]
        assert not tool_failures
        assert result.observed_tools is not None
        assert {"clarify", "read_file"} <= result.observed_tools
        assert "execute_code" not in result.observed_tools

    def test_known_intent_with_no_fallback_passes(self) -> None:
        report = evaluate(
            [_prompt(intent_class="planning")],
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        result = report.results[0]
        tool_failures = [f for f in result.failures if f.kind == "tools"]
        assert not tool_failures
        assert isinstance(result.observed_tools, set)


# ── GRV-008 § I.d: Andon halt assertion ──────────────────────────────


class TestAndonHaltAssertion:
    def test_classifier_exception_flagged_as_andon(self) -> None:
        def _boom(_message):
            raise RuntimeError("boom")
        report = evaluate(
            [_prompt(intent_class="planning")],
            classifier=_boom,
        )
        result = report.results[0]
        assert result.andon_halt is True
        assert "boom" in (result.andon_reason or "")
        andon_failures = [f for f in result.failures if f.kind == "andon"]
        assert len(andon_failures) == 1

    def test_andon_halt_means_prompt_fails(self) -> None:
        def _boom(_message):
            raise RuntimeError("boom")
        report = evaluate(
            [_prompt(intent_class="planning")],
            classifier=_boom,
        )
        assert not report.passed


# ── learning-envelope: is_correction assertion ───────────────────────


class TestIsCorrectionAssertion:
    def test_match_passes(self) -> None:
        report = evaluate(
            [_prompt(is_correction=True)],
            classifier=lambda msg: _classification(is_correction=True),
        )
        result = report.results[0]
        ic_failures = [f for f in result.failures if f.kind == "is_correction"]
        assert not ic_failures

    def test_divergence_reports_failure(self) -> None:
        report = evaluate(
            [_prompt(is_correction=True)],
            classifier=lambda msg: _classification(is_correction=False),
        )
        result = report.results[0]
        ic_failures = [f for f in result.failures if f.kind == "is_correction"]
        assert len(ic_failures) == 1

    def test_none_observed_does_not_fail(self) -> None:
        # If classifier returns is_correction=None (graceful) we don't
        # fail — the operator's docstring contract for is_correction
        # treats None as "missing information, bias toward success."
        report = evaluate(
            [_prompt(is_correction=True)],
            classifier=lambda msg: _classification(is_correction=None),
        )
        result = report.results[0]
        ic_failures = [f for f in result.failures if f.kind == "is_correction"]
        assert not ic_failures


# ── Preamble compose-time read ────────────────────────────────────────


class TestPreambleSlot:
    def test_disabled_preamble_records_zero(self) -> None:
        report = evaluate(
            [_prompt(intent_class="planning")],
            preamble_enabled=False,
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        result = report.results[0]
        assert result.preamble_hit is False
        assert result.preamble_chars == 0

    def test_enabled_preamble_runs(self) -> None:
        # Preamble may or may not hit depending on live ~/.grove store
        # state; the assertion is that compose() runs without crashing.
        report = evaluate(
            [_prompt(intent_class="planning")],
            preamble_enabled=True,
            classifier=lambda msg: _classification(intent_class="planning"),
        )
        result = report.results[0]
        # If hit, chars > 0; if not hit, chars == 0. Either is valid.
        assert (result.preamble_hit and result.preamble_chars > 0) or (
            not result.preamble_hit and result.preamble_chars == 0
        )


# ── gate_proposal: Sprint 47 lifted the fail-loud; both paths return ──


class TestGateProposalContract:
    def test_non_none_proposed_state_lifted_in_sprint_47(self, monkeypatch, tmp_path) -> None:
        """Sprint 46 raised NotImplementedError on non-None
        proposed_state; Sprint 47 lifts that and runs the sandbox.
        Detailed sandbox behavior is in test_gate_proposal_lift.py;
        this meta-test just asserts the lift happened."""
        import grove.eval.hero_runner as _hr
        import yaml
        op = tmp_path / "routing.config.yaml"
        op.write_text(yaml.safe_dump({
            "routing": {
                "schema_version": 1,
                "default_tier": "T2",
                "zone_overrides": {},
                "tier_preferences": {
                    "T1": {"provider": "anthropic", "model": "test-haiku", "max_tokens": 4096},
                    "T2": {"provider": "anthropic", "model": "test-sonnet", "max_tokens": 8192},
                    "T3": {"provider": "anthropic", "model": "test-opus", "max_tokens": 16384},
                },
                "routing_rules": {
                    "downward": {"enabled": True, "match": {"intents": ["conversation"]}, "target_tier": "T1"},
                    "upward": {"enabled": True, "match": {"intents": ["debugging"], "complexity": ["complex", "novel"]}, "target_tier": "T3"},
                    "escalation": {"enabled": True, "match": {"max_confidence": 0.6}, "action": "step_up"},
                },
                "telemetry": {"tier": "T1"},
                "escalation": {"threshold": 0.6},
            },
        }, sort_keys=False))
        monkeypatch.setattr(
            _hr, "_classify",
            lambda msg: _classification(intent_class="planning"),
        )
        # The Sprint 46 NotImplementedError MUST be gone.
        result = gate_proposal(
            proposed_state={"routing": {"routing_rules": {"downward": {"match": {"intents": ["creative_writing"]}}}}},
            operator_config_path=op,
            machine_config_path=None,
        )
        assert isinstance(result, GateResult)

    def test_none_proposed_state_returns_gate_result(self, monkeypatch) -> None:
        # Use a small subset by injecting a tmp prompts file.
        # We use the meta-test fake classifier here by patching
        # the harness's classifier seam at the module level so the
        # live T-telemetry call is bypassed during this meta-test.
        import grove.eval.hero_runner as _hr
        monkeypatch.setattr(
            _hr, "_classify",
            lambda msg: _classification(intent_class="planning"),
        )
        result = gate_proposal(proposed_state=None)
        assert isinstance(result, GateResult)
        assert isinstance(result.eval_report, EvalReport)
        assert isinstance(result.summary, str)
        assert "passed" in result.summary
