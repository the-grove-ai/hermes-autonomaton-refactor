"""grove/fleet/quality.py — the generic fleet HOW-WELL quality evaluator.

Sprint: the fleet quality-gate sprint, P2 (the sprint tag itself contains a
producer name, which R-A11 bans from this module — the commit message carries
the full tag). Any capability record declaring
``governance.quality_gate`` gets its staged draft evaluated by ONE forced-tool
structured verdict call before the worker's success event (the P3 gate site).

GENERALIZABILITY INVARIANT (R-A11): this module keys on record-block presence
only — the rubric is data, the verdict envelope is fixed, and no producer is
named anywhere in it.

Transport is :func:`grove.t1_call.call_t1` forced-tool (call site #4). The
tier is resolved BY NAME from the record's ``quality_gate.evaluator_tier``
(default ``"T1"``) via the public router API — the cellar precedent: no
classification, and independent of the producer's own model pin (R-A5). An
unknown tier raises the router's loud KeyError.

Prompt frame (SPEC amendment A1 / R-A12), mirroring the cellar
``_eval_prompt`` source+body shape: task context block (when the gate site
passes one) → rubric criteria → staged draft.

Input-size guard (R-B3): when the COMBINED evaluator input (task context +
criteria + staged draft) exceeds the input budget, the evaluator is NOT
called — the verdict reports ``status: skipped_oversize`` with a null score.
Truncated content is never evaluated: a verdict on half a draft is noise
dressed as signal.

Structural verdict validation rides the cellar ``_validate_verdict``
precedent — a malformed verdict raises :class:`MalformedVerdict` loudly, no
retry. The P3 gate site converts any evaluator exception into a
FleetWorkerAndon; catch-and-log here would be a SPEC violation (R-A10).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from grove.t1_call import call_t1

logger = logging.getLogger(__name__)

# Consumer-side default when the record omits the optional evaluator_tier key
# (schema: grove/capability.py _quality_gate_shape_error).
_DEFAULT_EVALUATOR_TIER = "T1"

# Verdict output ceiling — the cellar Evaluator precedent
# (grove/wiki/pipeline.py _EVAL_MAX_TOKENS).
_EVAL_MAX_TOKENS = 1024

# R-B3 — the COMBINED input budget (task context + criteria + staged draft),
# in characters. ~50K tokens at the 4-chars/token heuristic: comfortably
# inside every catalog tier's context window while refusing pathological
# megafile evaluation. A draft over this budget is skipped LOUDLY
# (status=skipped_oversize), never truncated-and-scored.
_EVAL_INPUT_BUDGET_CHARS = 200_000


class MalformedVerdict(ValueError):
    """The evaluator returned a structurally invalid verdict — loud, no retry."""


# Verdict tool schema — mirrors the cellar _EVAL_TOOL shape exactly
# (complete / accurate / quality_score / issues, all required).
_VERDICT_TOOL: Dict[str, Any] = {
    "name": "quality_verdict",
    "description": (
        "Record a structured verdict on whether the staged draft meets the "
        "declared rubric criteria."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "complete": {
                "type": "boolean",
                "description": "Does the draft fully deliver what the criteria ask for?",
            },
            "accurate": {
                "type": "boolean",
                "description": (
                    "Is the draft free of claims the provided task context or "
                    "source material does not support?"
                ),
            },
            "quality_score": {
                "type": "number",
                "description": "Overall fit against the rubric criteria, 0.0-1.0.",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific problems a redraft should fix.",
            },
        },
        "required": ["complete", "accurate", "quality_score", "issues"],
    },
}


def quality_gate_declaration(cap) -> Optional[Dict[str, Any]]:
    """The record's validated quality_gate declaration, or None.

    None (absent, non-mapping governance, or loader-flagged
    ``quality_gate_error``) means UNGATED — the worker proceeds with no
    evaluation. The loader already validated shape at load
    (grove/capability.py:_validate_quality_gate); the error check here keeps
    this seam fail-closed rather than trusting a block the loader flagged.
    getattr (not attribute access): a record with no governance at all is the
    ABSENT case, not an error — the _emit_declaration precedent.
    """
    gov = getattr(cap, "governance", None)
    if not isinstance(gov, dict):
        return None
    gate = gov.get("quality_gate")
    if not isinstance(gate, dict) or gov.get("quality_gate_error"):
        return None
    return gate


def evaluate_draft(
    record,
    staged_files: Dict[str, str],
    task_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate a staged draft against the record's declared rubric.

    Returns the FIXED verdict envelope (always the same keys):

    * ``status`` — ``"pass"`` | ``"fail"`` | ``"skipped_oversize"``
    * ``quality_score`` — float in [0, 1], or None when skipped
    * ``complete`` / ``accurate`` — bool, or None when skipped
    * ``issues`` — list of str (empty when skipped)
    * ``rubric_version`` / ``threshold`` / ``evaluator_tier`` — echoed from
      the declaration (the event rider's provenance fields)
    * ``evaluator_model`` — the resolved model id (R-B2), or None when
      skipped (no call was made, so no model evaluated anything)
    * ``context_keys_used`` / ``context_keys_missing`` — the declared
      ``context_inputs`` split by presence in *task_context* (A1; the gate
      site notes absent keys here instead of failing the run)
    * ``detail`` — human-readable skip reason, empty otherwise

    Pass = ``complete AND accurate AND quality_score >= threshold`` (the
    cellar ``_passed`` predicate, thresholded by the record). Raises
    :class:`MalformedVerdict` on a structurally invalid verdict and
    propagates transport errors untouched — the gate site owns Andon
    conversion.
    """
    gate = quality_gate_declaration(record)
    if gate is None:
        raise ValueError(
            "evaluate_draft called without a valid governance.quality_gate "
            f"declaration on record {getattr(record, 'id', '<no id>')!r} — "
            "the gate site must check quality_gate_declaration() first."
        )

    evaluator_tier = gate.get("evaluator_tier", _DEFAULT_EVALUATOR_TIER)
    threshold = float(gate["threshold"])
    declared_context: List[str] = list(gate.get("context_inputs") or [])
    ctx = task_context or {}
    context_keys_used = [k for k in declared_context if k in ctx]
    context_keys_missing = [k for k in declared_context if k not in ctx]

    envelope: Dict[str, Any] = {
        "status": None,
        "quality_score": None,
        "complete": None,
        "accurate": None,
        "issues": [],
        "rubric_version": gate["rubric_version"],
        "threshold": threshold,
        "evaluator_tier": evaluator_tier,
        "evaluator_model": None,
        "context_keys_used": context_keys_used,
        "context_keys_missing": context_keys_missing,
        "detail": "",
    }

    prompt = _eval_prompt(list(gate["criteria"]), staged_files, ctx)

    # R-B3 — combined-input size guard, checked BEFORE any call or tier
    # resolution. Oversize is a disposition, not an error: the worker's output
    # stands, the event says WHY no score rides it.
    if len(prompt) > _EVAL_INPUT_BUDGET_CHARS:
        envelope["status"] = "skipped_oversize"
        envelope["detail"] = (
            f"combined evaluator input {len(prompt)} chars exceeds the "
            f"{_EVAL_INPUT_BUDGET_CHARS}-char budget; evaluation skipped "
            "(truncated content is never evaluated)"
        )
        logger.warning(
            "[fleet.quality] %s: %s",
            getattr(record, "id", "<no id>"),
            envelope["detail"],
        )
        return envelope

    # Tier resolution by name (R-A5) — the model id rides the verdict (R-B2).
    # KeyError on an unknown tier propagates loudly.
    envelope["evaluator_model"] = _tier_model(evaluator_tier)

    raw = call_t1(
        prompt,
        tool=_VERDICT_TOOL,
        max_tokens=_EVAL_MAX_TOKENS,
        tier=evaluator_tier,
    )
    verdict = _validate_verdict(raw)

    score = _clamp01(float(verdict["quality_score"]))
    passed = bool(verdict["complete"]) and bool(verdict["accurate"]) and score >= threshold
    envelope["status"] = "pass" if passed else "fail"
    envelope["quality_score"] = score
    envelope["complete"] = bool(verdict["complete"])
    envelope["accurate"] = bool(verdict["accurate"])
    envelope["issues"] = [str(i) for i in verdict["issues"]]
    return envelope


# ── prompt assembly (A1 frame: context → criteria → draft) ──────────────


def _eval_prompt(
    criteria: List[str],
    staged_files: Dict[str, str],
    task_context: Dict[str, Any],
) -> str:
    parts = [
        "Evaluate the staged draft against the rubric criteria. "
        "Call quality_verdict with your verdict.\n"
    ]
    if task_context:
        ctx_lines = "\n".join(f"{k}: {task_context[k]}" for k in sorted(task_context))
        parts.append(f"=== TASK CONTEXT ===\n{ctx_lines}\n")
    bullets = "\n".join(f"- {c}" for c in criteria)
    parts.append(f"=== RUBRIC CRITERIA ===\n{bullets}\n")
    files = "\n\n".join(
        f"--- {name} ---\n{staged_files[name]}" for name in sorted(staged_files)
    )
    parts.append(f"=== STAGED DRAFT ===\n{files}\n")
    return "\n".join(parts)


# ── tier resolution (public API, by name; initialize-and-retry) ─────────


def _tier_model(tier_name: str) -> Optional[str]:
    """The resolved model id for *tier_name* (R-B2), via the public router.

    Mirrors t1_call._resolve_t1_runtime's initialize-and-retry: a fresh
    worker subprocess may not have initialized the router yet. KeyError on an
    unknown tier propagates — never a fallback model.
    """
    from grove import router as grove_router

    try:
        tier_config = grove_router.get_tier_config(tier_name)
    except RuntimeError:
        grove_router.initialize()
        tier_config = grove_router.get_tier_config(tier_name)
    return tier_config.model


# ── verdict validation (the cellar _validate_verdict precedent) ─────────


def _validate_verdict(verdict: Any) -> Dict[str, Any]:
    if not isinstance(verdict, dict):
        raise MalformedVerdict(f"evaluator verdict is not an object: {verdict!r}")
    for key in ("complete", "accurate", "quality_score", "issues"):
        if key not in verdict:
            raise MalformedVerdict(f"evaluator verdict missing {key!r}")
    try:
        float(verdict["quality_score"])
    except (TypeError, ValueError) as exc:
        raise MalformedVerdict(
            f"evaluator quality_score is not a number: {verdict['quality_score']!r}"
        ) from exc
    if not isinstance(verdict["issues"], list):
        raise MalformedVerdict("evaluator 'issues' must be a list")
    return verdict


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
