"""Grove Kaizen — Skill Synthesizer (Sprint 63 §2, the PROPOSE stage).

The DETECT stage (``IntentPatternDetector.detect_skill_candidates``) surfaces
recurring multi-tool sequences plus the operator prompts that triggered them.
This module turns a candidate into a drafted, parametrized ``SKILL.md`` via a
Tier-3 (Apex Cognition) synthesis call, validates it, and stages it in the
Flywheel PR queue (``proposals.jsonl``) as a ``skill_synthesis`` proposal.

Validation (Sprint 63 Ruling 2) is a structural check plus a T3 self-review —
NOT the hero-prompts gate. The hero gate evaluates routing-config diffs
through the intent/tier/tool pipeline; it has no notion of a SKILL.md body.
So a synthesized skill is gated on (1) parseable frontmatter with the required
keys and a procedural body, and (2) a T3 pass confirming it is coherent,
parametrized, and non-destructive.

Everything here runs off the main conversation cycle (Sprint 63 §3 spawns it
in a background daemon after FinalResponse), so it must never block T0 routing.
It is best-effort: a tier mis-config, an API error, or a validation failure
logs loudly and stages nothing — it never raises into the caller.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_SYNTHESIS_MAX_TOKENS = 2048
_REVIEW_MAX_TOKENS = 512

# Maximum operator prompts and prompt length fed into a single synthesis call —
# enough for semantic context without unbounded token spend on a pathological
# pattern. Logged, not silent, when truncation happens.
_MAX_PROMPTS = 5
_MAX_PROMPT_CHARS = 1200


_SYNTHESIS_SYSTEM_PROMPT = """\
You are the Grove Autonomaton's skill synthesizer. The operator has repeatedly \
performed the same multi-step task. Your job is to draft ONE reusable skill \
that captures that task as a parametrized procedure.

You are given (a) the operator's own prompts that triggered the task across \
several sessions, and (b) the ordered sequence of tools that were used. The \
prompts supply the GOAL and semantics; the tool sequence supplies the MECHANICS.

Output a single SKILL.md file and NOTHING else — no preamble, no code fences. \
It MUST have:

1. A YAML frontmatter block delimited by `---` lines, containing at minimum:
   - `name`: a short kebab-case identifier
   - `description`: one sentence describing what the skill does
2. A markdown body containing:
   - A `## When to use` section describing the triggering situation
   - A `## Procedure` section with numbered, parametrized steps

Requirements:
- Parametrize anything that varied across the operator's prompts (names, paths, \
targets) as `{placeholders}` with a short note on what each is.
- Describe the procedure in terms of the observed tools. Do NOT inline raw \
destructive shell commands; describe the step and let the operator's tools run it.
- Be concise. The skill is a recipe, not an essay.
"""


_REVIEW_SYSTEM_PROMPT = """\
You are a strict reviewer for auto-drafted Grove skills. You are given a \
SKILL.md a synthesizer produced from observed operator behavior. Judge it on \
three axes and return ONLY a JSON object (no prose):

{
  "coherent": <true if the procedure is internally consistent and actually \
accomplishes what its description claims>,
  "parametrized": <true if variable inputs are captured as placeholders rather \
than hard-coded to one operator's specifics>,
  "safe": <true if it contains no destructive, irreversible, or exfiltrating \
actions that would run without the operator's per-use approval>,
  "reason": "<one sentence; if any axis is false, say which and why>"
}

Default any axis to false when uncertain. A skill that is unsafe or incoherent \
must not pass.
"""


# ── Tier-3 call surface ──────────────────────────────────────────────────


def _resolve_t3_runtime() -> Optional[dict]:
    """Resolve the T3 (Apex Cognition) runtime dict, or None if unavailable.

    Mirrors ``grove.classify._telemetry_tier_runtime`` but binds to T3. Returns
    None (logged) rather than raising when no router is configured, T3 is not
    declared, or T3 does not resolve to an Anthropic-native tier — synthesis is
    a background nicety and must degrade quietly at the tier boundary.
    """
    try:
        from grove.providers import _ensure_router, resolve_tier_to_runtime

        router = _ensure_router()
        if router is None:
            logger.warning(
                "[kaizen.synthesizer] no Cognitive Router; cannot resolve T3."
            )
            return None
        tier_config = router.get_tier_config("T3")
        runtime = resolve_tier_to_runtime(tier_config)
        if runtime.get("api_mode") != "anthropic_messages":
            logger.warning(
                "[kaizen.synthesizer] T3 resolves api_mode %r; synthesizer "
                "requires an Anthropic-native tier — skipping.",
                runtime.get("api_mode"),
            )
            return None
        return runtime
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[kaizen.synthesizer] T3 runtime resolution failed: %r", exc,
        )
        return None


def _t3_call(
    runtime: dict,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    prefill: Optional[str] = None,
) -> Optional[str]:
    """Issue one Anthropic-native T3 call; return the text or None on error.

    Reuses ``agent.anthropic_adapter.build_anthropic_client`` for
    credential-aware client construction, matching ``grove.classify``'s
    classifier call. ``prefill`` forces a leading token (e.g. ``"{"`` to coerce
    JSON) and is re-prepended to the returned text.
    """
    try:
        from agent.anthropic_adapter import build_anthropic_client

        client = build_anthropic_client(
            api_key=runtime.get("api_key") or "",
            base_url=runtime.get("base_url") or None,
        )
        messages: List[Dict[str, str]] = [
            {"role": "user", "content": user_content},
        ]
        if prefill is not None:
            messages.append({"role": "assistant", "content": prefill})
        response = client.messages.create(
            model=runtime["model"],
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        text = response.content[0].text if response.content else ""
        return (prefill + text) if prefill is not None else text
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kaizen.synthesizer] T3 call failed: %r", exc)
        return None


# ── Synthesis ────────────────────────────────────────────────────────────


def _build_synthesis_input(candidate: Dict[str, Any]) -> str:
    """Compose the operator prompts + tool sequence into the synthesis input."""
    prompts = candidate.get("prompts") or []
    if len(prompts) > _MAX_PROMPTS:
        logger.info(
            "[kaizen.synthesizer] candidate has %d prompts; using first %d.",
            len(prompts), _MAX_PROMPTS,
        )
        prompts = prompts[:_MAX_PROMPTS]
    rendered = []
    for i, p in enumerate(prompts, start=1):
        text = p if len(p) <= _MAX_PROMPT_CHARS else (p[:_MAX_PROMPT_CHARS] + " …")
        rendered.append(f"{i}. {text}")
    sequence = " → ".join(candidate.get("tool_sequence") or ())
    return (
        "The operator issued these prompts (one per recurring occurrence):\n"
        + "\n".join(rendered)
        + f"\n\nThe tools used, in order, were: {sequence}\n\n"
        + "Draft the SKILL.md."
    )


def synthesize_skill_md(
    candidate: Dict[str, Any], runtime: Optional[dict] = None,
) -> Optional[str]:
    """Synthesize a SKILL.md for ``candidate`` via a T3 call; None on failure."""
    rt = runtime or _resolve_t3_runtime()
    if rt is None:
        return None
    skill_md = _t3_call(
        rt,
        _SYNTHESIS_SYSTEM_PROMPT,
        _build_synthesis_input(candidate),
        max_tokens=_SYNTHESIS_MAX_TOKENS,
    )
    if not skill_md or not skill_md.strip():
        logger.warning("[kaizen.synthesizer] T3 returned empty synthesis.")
        return None
    return _strip_code_fence(skill_md.strip())


def _strip_code_fence(text: str) -> str:
    """Remove a wrapping ```...``` fence if the model added one despite the rule."""
    if text.startswith("```"):
        # Drop the first fence line and a trailing fence if present.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


# ── Validation (Sprint 63 Ruling 2 — structural + T3 self-review) ─────────


def validate_skill_md(
    skill_md: str, runtime: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Validate a synthesized SKILL.md. Returns ``(ok, reason)``.

    Two gates, structural first (cheap, no API):
      1. Frontmatter parses and carries non-empty ``name`` + ``description``;
         the body has a "when to use" cue and a "procedure"/"steps" cue.
      2. A T3 self-review confirms coherent + parametrized + safe.

    Skips the hero-prompts gate by design (Sprint 63 Ruling 2).
    """
    ok, reason = _structural_check(skill_md)
    if not ok:
        return False, reason

    rt = runtime or _resolve_t3_runtime()
    if rt is None:
        return False, "T3 self-review unavailable (tier unresolved)."
    raw = _t3_call(
        rt,
        _REVIEW_SYSTEM_PROMPT,
        f"Review this SKILL.md:\n\n{skill_md}",
        max_tokens=_REVIEW_MAX_TOKENS,
        prefill="{",
    )
    if not raw:
        return False, "T3 self-review call failed."
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return False, "T3 self-review returned unparseable JSON."
    if not (
        verdict.get("coherent")
        and verdict.get("parametrized")
        and verdict.get("safe")
    ):
        return False, str(verdict.get("reason") or "T3 self-review rejected the skill.")
    return True, "passed structural + T3 self-review"


def _structural_check(skill_md: str) -> Tuple[bool, str]:
    """Frontmatter keys + body section cues. No API."""
    try:
        from grove.skills import parse_frontmatter

        fm, body = parse_frontmatter(skill_md)
    except Exception as exc:  # noqa: BLE001
        return False, f"frontmatter does not parse: {exc}"
    if not isinstance(fm, dict):
        return False, "frontmatter is not a mapping"
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "frontmatter missing a non-empty 'name'"
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return False, "frontmatter missing a non-empty 'description'"
    low = body.lower()
    if "when to use" not in low:
        return False, "body missing a 'When to use' section"
    if "procedure" not in low and "steps" not in low:
        return False, "body missing a 'Procedure'/'Steps' section"
    return True, "structural ok"


# ── Staging ──────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    """Normalize a frontmatter name into a quarantine-dir-safe slug."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "synthesized-skill"


def _extract_goal(fm: Dict[str, Any]) -> str:
    """A short concierge-register goal phrase for the quiet append."""
    desc = (fm.get("description") or "").strip()
    if not desc:
        return "do this task"
    # First clause, lowercased, trimmed of a trailing period.
    goal = re.split(r"[.\n]", desc, maxsplit=1)[0].strip().rstrip(".")
    return goal[:1].lower() + goal[1:] if goal else "do this task"


def stage_proposal(
    candidate: Dict[str, Any], skill_md: str,
) -> Optional[str]:
    """Stage a validated SKILL.md as a ``skill_synthesis`` proposal.

    Returns the ``proposal_id`` on append, None on duplicate or error.
    Idempotent via the queue's content-addressable id: re-running the
    synthesizer on the same pattern does not re-queue.
    """
    try:
        from grove.skills import parse_frontmatter

        fm, body = parse_frontmatter(skill_md)
        skill_name = _slugify(str(fm.get("name") or ""))
        when_to_use = _extract_when_to_use(body)
        goal = _extract_goal(fm)

        from grove.eval.proposal_queue import (
            PROPOSAL_TYPE_SKILL_SYNTHESIS,
            RoutingProposal,
            append as queue_append,
            compute_proposal_id,
        )
        import hashlib

        payload = {
            "skill_name": skill_name,
            "skill_md": skill_md,
            "when_to_use": when_to_use,
            "goal": goal,
            "tool_sequence": list(candidate.get("tool_sequence") or ()),
        }
        evidence = tuple(candidate.get("evidence_turns") or ())
        proposal = RoutingProposal(
            proposal_id=compute_proposal_id(
                type=PROPOSAL_TYPE_SKILL_SYNTHESIS,
                payload=payload,
                evidence=evidence,
            ),
            type=PROPOSAL_TYPE_SKILL_SYNTHESIS,
            payload=payload,
            evidence=evidence,
            eval_hash="sha256:" + hashlib.sha256(skill_md.encode("utf-8")).hexdigest(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        appended = queue_append(proposal)
        if appended:
            logger.info(
                "[kaizen.synthesizer] staged skill_synthesis proposal %r "
                "(%s).", skill_name, proposal.proposal_id.split(":")[-1][:12],
            )
            return proposal.proposal_id
        logger.info(
            "[kaizen.synthesizer] skill_synthesis proposal for %r already "
            "queued — no duplicate.", skill_name,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kaizen.synthesizer] staging failed: %r", exc)
        return None


def _extract_when_to_use(body: str) -> str:
    """Pull the 'When to use' section text for the proposal payload."""
    match = re.search(
        r"#+\s*when to use\s*\n(.+?)(?:\n#+\s|\Z)",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


# ── Orchestrator (called by the §3 background daemon) ─────────────────────


def run_synthesis_pass(
    *,
    detector: Optional[Any] = None,
    session_db: Optional[Any] = None,
    n: int = 3,
    m: int = 2,
    window_days: int = 30,
    max_candidates: int = 3,
) -> int:
    """Detect → synthesize → validate → stage. Returns proposals staged.

    Best-effort end to end: any single candidate that fails synthesis or
    validation is logged and skipped; the pass still processes the rest.
    Bounds work at ``max_candidates`` per pass (logged when it truncates) so a
    busy operator's history can't spawn an unbounded fan-out of T3 calls in one
    background run.
    """
    from grove.kaizen.detector import IntentPatternDetector

    det = detector or IntentPatternDetector()
    candidates = det.detect_skill_candidates(
        n=n, m=m, window_days=window_days, session_db=session_db,
    )
    if not candidates:
        return 0
    if len(candidates) > max_candidates:
        logger.info(
            "[kaizen.synthesizer] %d candidates found; synthesizing top %d "
            "this pass (the rest surface on a later pass).",
            len(candidates), max_candidates,
        )
        candidates = candidates[:max_candidates]

    runtime = _resolve_t3_runtime()
    if runtime is None:
        return 0

    staged = 0
    for candidate in candidates:
        skill_md = synthesize_skill_md(candidate, runtime=runtime)
        if not skill_md:
            continue
        ok, reason = validate_skill_md(skill_md, runtime=runtime)
        if not ok:
            logger.info(
                "[kaizen.synthesizer] candidate %s failed validation: %s",
                candidate.get("tool_sequence"), reason,
            )
            continue
        if stage_proposal(candidate, skill_md):
            staged += 1
    return staged
