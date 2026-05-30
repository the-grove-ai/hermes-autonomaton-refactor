"""GRV-008 § I hero-prompts regression harness.

Sprint 46 — pushes a curated set of hero prompts through the
classification + routing + tool-composition + compose-time preamble
read path and reports per-prompt pass/fail. No Agent generation loop;
no tool execution. The only LLM call is the T-telemetry classifier
(whatever model ``routing.config.yaml`` binds to the telemetry tier).

GRV-008 § I assertions per prompt:

* (a) Classified intent matches expected.
* (b) Selected tier matches or optimizes expected.
* (c) Tool composition sequence unbroken (must-include / must-not-
      include sets respected).
* (d) No Andon halt on golden-path prompts.

Sprint 46 also ships the ``gate_proposal`` entry point Sprint 47 will
extend with proposed-state swap-and-restore. The v0.1 stub evaluates
against the CURRENT state and raises ``NotImplementedError`` when
called with a non-None ``proposed_state`` — fail-loud signal that the
half-implementation MUST be lifted in Sprint 47.

Model independence
------------------
The harness does NOT name a model. The classifier reads its model
binding from ``routing.config.yaml``'s telemetry tier; the harness
calls ``classify_for_routing`` and accepts whatever the binding
resolves to. All cost reporting in the harness reads from the
classifier's response usage data + ``routing.config.yaml`` tier
pricing where available; the harness does not hardcode a per-model
USD-per-token constant.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from grove.classify import (
    ClassificationResult,
    classify_for_routing,
)
from grove.context_budget import (
    load_taxonomy,
    resolve_tool_set,
)
from grove.providers import _ensure_router
from grove.router import CognitiveRouter, RoutingDecision
from grove.prompt.composer import build_default_composer


# ── Public dataclasses ────────────────────────────────────────────────


@dataclass(frozen=True)
class HeroPrompt:
    """One curated hero prompt + its expected pipeline outputs.

    Loaded from ``config/hero_prompts.yaml``. The ``expected`` block
    expresses the assertions the harness MUST run against the
    pipeline's observed values for this prompt.
    """

    id: str
    message: str
    expected: Dict[str, Any]


@dataclass(frozen=True)
class AssertionFailure:
    """One assertion that did not hold for a hero prompt.

    The ``kind`` matches GRV-008 § I's lettered assertions where
    applicable: ``intent`` (a), ``tier`` (b), ``tools`` (c),
    ``andon`` (d). ``is_correction`` and ``preamble`` are the
    learning-envelope and feed-loop assertions Sprint 46 added.
    """

    kind: str
    expected: Any
    observed: Any
    detail: str = ""


@dataclass(frozen=True)
class PromptResult:
    """The harness's observed pipeline output for one hero prompt."""

    prompt_id: str
    observed_intent: Optional[str]
    observed_complexity: Optional[str]
    observed_confidence: Optional[float]
    observed_register: Optional[str]
    observed_tier: Optional[str]
    observed_reason: Optional[str]
    observed_tools: Optional[Set[str]]
    observed_is_correction: Optional[bool]
    preamble_hit: bool
    preamble_chars: int
    duration_ms: float
    andon_halt: bool
    andon_reason: Optional[str]
    failures: Tuple[AssertionFailure, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.failures and not self.andon_halt


@dataclass(frozen=True)
class EvalReport:
    """Aggregate report from one harness run."""

    results: Tuple[PromptResult, ...]
    duration_seconds: float
    preamble_enabled: bool

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def n_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def n_total(self) -> int:
        return len(self.results)


@dataclass(frozen=True)
class GateResult:
    """Outcome of a ``gate_proposal`` call (GRV-008 § I)."""

    passed: bool
    prompts_failed: Tuple[str, ...]
    eval_report: EvalReport
    summary: str


# ── Loader ────────────────────────────────────────────────────────────


def _default_prompts_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "hero_prompts.yaml"


def load_hero_prompts(path: Optional[Path] = None) -> List[HeroPrompt]:
    """Parse ``config/hero_prompts.yaml`` into ``HeroPrompt`` objects.

    Raises ``FileNotFoundError`` if the file is missing,
    ``ValueError`` if the top-level shape is wrong. Schema-shape
    validation is intentionally loose at load time; per-prompt
    assertion mechanics catch malformed expected blocks at evaluate
    time so the operator sees one error per prompt rather than a
    single load-time bail.
    """
    target = Path(path) if path is not None else _default_prompts_path()
    if not target.exists():
        raise FileNotFoundError(
            f"hero_prompts.yaml not found at {target}; "
            f"GRV-008 § I requires a curated set"
        )
    with open(target, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "hero_prompts" not in data:
        raise ValueError(
            f"hero_prompts.yaml at {target} missing top-level "
            f"'hero_prompts' list"
        )
    entries = data["hero_prompts"]
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"hero_prompts.yaml at {target} 'hero_prompts' must be a "
            f"non-empty list"
        )
    out: List[HeroPrompt] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"hero_prompts entry is not a mapping: {entry!r}")
        for required in ("id", "message", "expected"):
            if required not in entry:
                raise ValueError(
                    f"hero_prompts entry missing {required!r}: {entry!r}"
                )
        out.append(HeroPrompt(
            id=str(entry["id"]),
            message=str(entry["message"]),
            expected=dict(entry["expected"]),
        ))
    return out


# ── Pipeline runner ──────────────────────────────────────────────────


def _classify(message: str) -> Optional[ClassificationResult]:
    """Default classifier hook — the production classify_for_routing.

    Isolated as a module-level seam so meta-tests can inject a fake
    classification deterministically without burning a T-telemetry
    call.
    """
    return classify_for_routing(message)


def _route(
    classification: Optional[ClassificationResult],
    *,
    router: Optional[CognitiveRouter] = None,
) -> Optional[RoutingDecision]:
    """Route via the supplied router, falling back to the production one.

    Sprint 47 — ``router`` is the gate-proposal sandbox injection seam.
    Passing a fresh ``CognitiveRouter(tmp_path)`` lets ``gate_proposal``
    evaluate a proposed routing diff against the hero suite without
    mutating the module-level ``_default_router``. Production callers
    leave ``router=None``.
    """
    active = router if router is not None else _ensure_router()
    if active is None:
        return None
    return active.route(
        intent=classification.intent_class if classification else None,
        confidence=classification.confidence if classification else None,
        complexity_signal=(
            classification.complexity_signal if classification else None
        ),
    )


def _tools(classification: Optional[ClassificationResult]) -> Optional[Set[str]]:
    """Compute the tool set via the production resolver."""
    taxonomy = load_taxonomy()
    return resolve_tool_set(
        intent_class=classification.intent_class if classification else None,
        complexity_signal=(
            classification.complexity_signal if classification else None
        ),
        taxonomy=taxonomy,
    )


def _compose_preamble_slot(
    classification: Optional[ClassificationResult],
    *,
    preamble_enabled: bool,
) -> Tuple[bool, int]:
    """Run compose() and return (preamble_hit, preamble_chars).

    Builds an isolated composer with overrides for the preamble's
    enabled flag so Phase 2's baseline capture can run two passes
    deterministically.
    """
    config: Dict[str, Any] = {
        "sections": {
            "contextual_preamble": {"enabled": preamble_enabled},
        },
    }
    composer = build_default_composer(config=config)
    pattern_hash = classification.pattern_hash if classification else None
    intent_class = classification.intent_class if classification else None
    composed = composer.compose(
        valid_tool_names=set(),
        model="",
        provider="",
        platform="cli",
        session_id="hero_runner",
        skip_context_files=True,
        load_soul_identity=False,
        memory_enabled=False,
        user_profile_enabled=False,
        pass_session_id=False,
        system_message=None,
        session_register=None,
        tool_use_enforcement=None,
        memory_store=None,
        memory_manager=None,
        terminal_cwd=None,
        pattern_hash=pattern_hash,
        intent_class=intent_class,
    )
    preamble_text = composed.sections.get("contextual_preamble", "")
    return (bool(preamble_text), len(preamble_text))


def _check_assertions(
    prompt: HeroPrompt,
    *,
    classification: Optional[ClassificationResult],
    routing: Optional[RoutingDecision],
    tools: Optional[Set[str]],
) -> List[AssertionFailure]:
    """Run the GRV-008 § I assertions for one prompt.

    Returns the list of failures (empty list = pass). Each failure
    carries the assertion ``kind`` so the report aggregates by
    GRV-008 § I letter.
    """
    failures: List[AssertionFailure] = []
    expected = prompt.expected

    # (a) intent_class — EXACT match required.
    expected_intent = expected.get("intent_class")
    observed_intent = (
        classification.intent_class if classification else "unknown"
    )
    if expected_intent is not None and observed_intent != expected_intent:
        failures.append(AssertionFailure(
            kind="intent",
            expected=expected_intent,
            observed=observed_intent,
            detail="GRV-008 § I.a: classified intent must match",
        ))

    # complexity_signal — set membership.
    expected_complexity = expected.get("complexity_signal_in")
    observed_complexity = (
        classification.complexity_signal if classification else "unknown"
    )
    if expected_complexity and observed_complexity not in expected_complexity:
        failures.append(AssertionFailure(
            kind="complexity",
            expected=list(expected_complexity),
            observed=observed_complexity,
            detail="complexity_signal not in allowed set",
        ))

    # is_correction — assert only when the prompt declares an expected.
    expected_correction = expected.get("is_correction")
    observed_correction = (
        classification.is_correction if classification else None
    )
    if expected_correction is not None and observed_correction is not None:
        if bool(observed_correction) != bool(expected_correction):
            failures.append(AssertionFailure(
                kind="is_correction",
                expected=expected_correction,
                observed=observed_correction,
                detail="learning_envelope.is_correction divergence",
            ))

    # (b) tier — set membership.
    expected_tier = expected.get("tier_in")
    observed_tier = routing.tier if routing else None
    if expected_tier and observed_tier is not None and observed_tier not in expected_tier:
        failures.append(AssertionFailure(
            kind="tier",
            expected=list(expected_tier),
            observed=observed_tier,
            detail="GRV-008 § I.b: tier not in allowed set",
        ))

    # (c) tools — must-include / must-not-include / maximal-fallback.
    expected_must_include = set(expected.get("tools_must_include") or [])
    expected_must_not_include = set(
        expected.get("tools_must_not_include") or []
    )
    expected_maximal = bool(expected.get("tool_set_is_maximal_fallback"))
    if expected_maximal:
        if tools is not None:
            failures.append(AssertionFailure(
                kind="tools",
                expected="maximal-fallback (None)",
                observed=f"explicit set of {len(tools)} tools",
                detail="GRV-008 § I.c: unknown-intent must trip maximal fallback",
            ))
    else:
        if tools is None:
            failures.append(AssertionFailure(
                kind="tools",
                expected="explicit tool set",
                observed="maximal-fallback (None)",
                detail="GRV-008 § I.c: known-intent must produce explicit set",
            ))
        else:
            missing = expected_must_include - tools
            if missing:
                failures.append(AssertionFailure(
                    kind="tools",
                    expected=f"must include {sorted(missing)}",
                    observed=f"tools={sorted(tools)}",
                    detail="GRV-008 § I.c: required tools missing",
                ))
            present_forbidden = expected_must_not_include & tools
            if present_forbidden:
                failures.append(AssertionFailure(
                    kind="tools",
                    expected=f"must not include {sorted(present_forbidden)}",
                    observed=f"tools={sorted(tools)}",
                    detail="GRV-008 § I.c: forbidden tools present",
                ))

    return failures


def _evaluate_one(
    prompt: HeroPrompt,
    *,
    preamble_enabled: bool,
    classifier=_classify,
    router: Optional[CognitiveRouter] = None,
) -> PromptResult:
    """Run one hero prompt through the pipeline, capture observed values."""
    t0 = time.monotonic()
    andon_halt = False
    andon_reason: Optional[str] = None
    classification: Optional[ClassificationResult] = None
    routing: Optional[RoutingDecision] = None
    tools: Optional[Set[str]] = None
    preamble_hit = False
    preamble_chars = 0
    failures: List[AssertionFailure] = []

    try:
        classification = classifier(prompt.message)
    except Exception as exc:
        andon_halt = True
        andon_reason = f"classify: {exc!r}"

    if not andon_halt:
        try:
            routing = _route(classification, router=router)
        except Exception as exc:
            andon_halt = True
            andon_reason = f"route: {exc!r}"

    if not andon_halt:
        try:
            tools = _tools(classification)
        except Exception as exc:
            andon_halt = True
            andon_reason = f"tools: {exc!r}"

    if not andon_halt:
        try:
            preamble_hit, preamble_chars = _compose_preamble_slot(
                classification, preamble_enabled=preamble_enabled,
            )
        except Exception as exc:
            andon_halt = True
            andon_reason = f"compose: {exc!r}"

    if andon_halt and bool(prompt.expected.get("andon_halt")) is False:
        # (d) golden-path prompts MUST NOT halt.
        failures.append(AssertionFailure(
            kind="andon",
            expected="no andon halt (golden path)",
            observed=andon_reason or "unknown halt",
            detail="GRV-008 § I.d: golden-path prompt MUST NOT halt",
        ))

    if not andon_halt:
        failures.extend(_check_assertions(
            prompt,
            classification=classification,
            routing=routing,
            tools=tools,
        ))

    duration_ms = (time.monotonic() - t0) * 1000.0
    return PromptResult(
        prompt_id=prompt.id,
        observed_intent=(
            classification.intent_class if classification else None
        ),
        observed_complexity=(
            classification.complexity_signal if classification else None
        ),
        observed_confidence=(
            classification.confidence if classification else None
        ),
        observed_register=(
            classification.register_class if classification else None
        ),
        observed_tier=routing.tier if routing else None,
        observed_reason=routing.reason if routing else None,
        observed_tools=tools,
        observed_is_correction=(
            classification.is_correction if classification else None
        ),
        preamble_hit=preamble_hit,
        preamble_chars=preamble_chars,
        duration_ms=duration_ms,
        andon_halt=andon_halt,
        andon_reason=andon_reason,
        failures=tuple(failures),
    )


def evaluate(
    prompts: List[HeroPrompt],
    *,
    preamble_enabled: bool = True,
    classifier=_classify,
    router: Optional[CognitiveRouter] = None,
) -> EvalReport:
    """Run the full pipeline over ``prompts`` and return an EvalReport.

    ``classifier`` is an injection seam used by meta-tests; production
    callers leave it at the default (the live T-telemetry classifier).

    Sprint 47 — ``router`` is the gate-proposal sandbox seam. When
    supplied, the harness routes against this router instead of the
    module-level production one; the sandbox path constructs a fresh
    ``CognitiveRouter(tmp_path)`` from the proposed routing config and
    threads it through evaluate without mutating any module-level
    state.
    """
    t0 = time.monotonic()
    results = tuple(
        _evaluate_one(
            prompt,
            preamble_enabled=preamble_enabled,
            classifier=classifier,
            router=router,
        )
        for prompt in prompts
    )
    duration = time.monotonic() - t0
    return EvalReport(
        results=results,
        duration_seconds=duration,
        preamble_enabled=preamble_enabled,
    )


# ── Sprint 47 entry point (v0.1 stub) ────────────────────────────────


def gate_proposal(
    proposed_state: Optional[Dict[str, Any]] = None,
    *,
    prompts_path: Optional[Path] = None,
    operator_config_path: Optional[Path] = None,
    machine_config_path: Optional[Path] = None,
) -> GateResult:
    """GRV-008 § I gate-before-propose entry point.

    ``proposed_state=None`` — evaluate against the current production
    state (the module-level ``_default_router``). This is the path
    Sprint 46's ``gate_proposal`` shipped; meta-tests and direct
    operator runs use it.

    ``proposed_state`` not None — Sprint 47 lift. The ``proposed_state``
    is a routing-config DIFF dict (``{"routing": {"routing_rules":
    {...}}}`` or a partial within that). The gate:

    1. Loads the operator's ``routing.config.yaml`` (read-only) and the
       machine's ``routing.autonomaton.yaml`` (read-only).
    2. Deep-merges them per GRV-008 § III with operator-wins precedence.
    3. Deep-merges ``proposed_state`` ON TOP — emulating the post-
       approval merged state.
    4. Writes the merged config to a per-call ``tempfile.TemporaryDirectory``.
    5. Constructs a fresh ``CognitiveRouter(tmp_path)`` and runs the
       hero suite against it (no module-level state mutated).
    6. Returns ``GateResult`` carrying the EvalReport.

    Per GRV-008 § I, a failing ``GateResult`` MUST silently drop the
    proposal at the call site — TierRatchet honors that.
    """
    if proposed_state is None:
        prompts = load_hero_prompts(prompts_path)
        report = evaluate(prompts)
        failed = tuple(r.prompt_id for r in report.results if not r.passed)
        summary = (
            f"hero_runner: {report.n_passed}/{report.n_total} passed "
            f"in {report.duration_seconds:.2f}s "
            f"(preamble_enabled={report.preamble_enabled})"
        )
        return GateResult(
            passed=report.passed,
            prompts_failed=failed,
            eval_report=report,
            summary=summary,
        )

    # ── Sprint 47 sandbox path ──────────────────────────────────────
    import tempfile
    import yaml
    from grove.router_merge import (
        _deep_merge,
        load_merged_routing_config,
    )

    if operator_config_path is None:
        from hermes_constants import get_hermes_home
        operator_config_path = Path(get_hermes_home()) / "routing.config.yaml"
    if machine_config_path is None:
        from hermes_constants import get_hermes_home
        machine_config_path = Path(get_hermes_home()) / "routing.autonomaton.yaml"

    base = load_merged_routing_config(
        operator_path=operator_config_path,
        machine_path=machine_config_path if machine_config_path.exists() else None,
    )
    merged = _deep_merge(base, proposed_state)

    prompts = load_hero_prompts(prompts_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_config = Path(tmpdir) / "routing.merged.yaml"
        tmp_config.write_text(
            yaml.safe_dump(merged, sort_keys=False), encoding="utf-8",
        )
        sandbox_router = CognitiveRouter(tmp_config)
        report = evaluate(prompts, router=sandbox_router)

    failed = tuple(r.prompt_id for r in report.results if not r.passed)
    summary = (
        f"hero_runner (proposed): {report.n_passed}/{report.n_total} "
        f"passed in {report.duration_seconds:.2f}s "
        f"(preamble_enabled={report.preamble_enabled})"
    )
    return GateResult(
        passed=report.passed,
        prompts_failed=failed,
        eval_report=report,
        summary=summary,
    )
