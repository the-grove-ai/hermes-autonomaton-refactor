"""Kaizen offering render core — the ONE renderer surface.

Extracted from :mod:`grove.flywheel_cli` (proposal-card-legibility-v1
Phase 1) so surfaces that must compose an offering body — the CLI, the
post-turn push, the agent review tool, and (Phase 3) the portal card —
share one registry without importing the CLI's apply machinery.

Contents moved verbatim: the RENDER_REGISTRY + get_renderer/compose_offering
chokepoints, every per-type summary renderer, every diff renderer, and the
direct registrations (portal_action_failure / forge_artifact_pending /
fault_triage). ``PROPOSAL_HANDLERS`` (the APPLY registry) stays in
``flywheel_cli`` — it seeds this registry by calling
:func:`seed_from_handlers` at its module load, which also runs the boot
census assert. ``flywheel_cli`` re-exports every name here, so every
pre-split importer resolves unchanged.

Rendering is deterministic — no model call, no inference on the approval
surface. Renderers read the proposal object only; they cannot alter
proposal identity (``compute_proposal_id`` hashes type|payload|evidence
before any renderer runs) or disposition (apply paths resolve through
``PROPOSAL_HANDLERS``, never through rendered text).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from grove.eval.proposal_queue import (
    _LEGACY_ROUTING_TYPE,
    PROPOSAL_TYPE_FAULT_TRIAGE,
    PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
    PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
    PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_VERBS,
    RoutingProposal,
)

logger = logging.getLogger(__name__)


# machine-sink-generalization-v1 — accepted routing-rule sink names. The
# generalized ``ratchet_promoted_tX`` sinks are what the tier ratchet now
# emits; the legacy ``downward``/``upward`` stay valid so any proposal queued
# before this sprint (the live queue had pending routing proposals) still
# approves. The router merge itself is name-agnostic (GRV-001 Invariant I);
# this gate is the one place a sink name is validated.
_VALID_SINK_NAMES = frozenset({
    "downward", "upward",
    "ratchet_promoted_t1", "ratchet_promoted_t2", "ratchet_promoted_t3",
})


def _validate_routing_rule(rule: Any) -> None:
    """Raise ValueError unless ``rule`` is a known routing-rule sink name."""
    if rule not in _VALID_SINK_NAMES:
        raise ValueError(f"Unknown routing rule: {rule!r}")


def _routing_adjustment_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a routing_adjustment proposal into a routing-config diff.

    The diff is a partial routing config shape suitable for
    ``apply_diff_to_machine_config`` — the set-union semantics in the
    merger handle the intent-list combination with any pre-existing
    machine additions.
    """
    rule = proposal.payload.get("rule")
    add_intents = list(proposal.payload.get("add_intents") or [])
    _validate_routing_rule(rule)
    if not add_intents:
        raise ValueError(
            f"malformed routing_adjustment payload: {proposal.payload!r}"
        )
    return {
        "routing": {
            "routing_rules": {
                rule: {
                    "match": {
                        "intents": add_intents,
                    },
                },
            },
        },
    }


def _diff_pattern_demotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 49 — the pattern is already suspended (auto, on correction).
    # The "diff" the operator confirms is pulling it from T0 to T1.
    return {
        "pattern_demotion": {
            "intent_class": proposal.payload.get("intent_class", "?"),
            "tier": "T0 → T1 (drift: corrected after a cache hit)",
            "trigger": proposal.payload.get("trigger", "correction_drift"),
            "correction_turn_id": proposal.payload.get("correction_turn_id", "?"),
            "reverse_with": "autonomaton flywheel reject <id>",
        },
    }


def _diff_pattern_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 48 — the "diff" is retiring a stable pattern to the
    # deterministic T0 cache (the compiled entry already exists,
    # suspended, in pattern_cache.db; approve flips it to active).
    ev = proposal.payload.get("promotion_evidence", {})
    return {
        "pattern_promotion": {
            "intent_class": proposal.payload.get("intent_class", "?"),
            "cacheable_type": proposal.payload.get("cacheable_type", "?"),
            "tier": "T1 → T0 (deterministic; no model call)",
            "evidence": ev,
            "sample_queries": proposal.payload.get("sample_queries", []),
        },
    }


def _diff_skill_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 53.2 — the "diff" the operator reviews is the promotion
    # act: move the skill out of quarantine and greenlight its path.
    name = proposal.payload.get("skill_name", "?")
    return {
        "skill_promotion": {
            "skill_name": name,
            "from": f"~/.grove/skills/.andon/{name}/",
            "to": f"~/.grove/skills/{name}/",
            "zone_rule": {
                "match_pattern": rf".*\.grove/skills/{name}/.*",
                "zone": "green",
            },
        },
    }


def _diff_zone_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Zone promotions don't translate to a routing-config diff —
    # they write directly to zones.schema.yaml via save_zone_rule.
    # The "diff" displayed to the operator is the YAML-shaped
    # rule that would be appended.
    return {
        "tool_zones": {
            proposal.payload.get("tool", "?"): {
                "rules": [
                    {
                        "match_pattern": proposal.payload.get("pattern", ""),
                        "zone": proposal.payload.get("zone", "?"),
                        "reason": proposal.payload.get("reason", ""),
                    },
                ],
            },
        },
    }


def _diff_skill_synthesis(proposal: RoutingProposal) -> Dict[str, Any]:
    # B1 (Fork B) — the "diff" the operator reviews is staging the drafted
    # SKILL.md into quarantine. Approve materializes it to .andon/ and mints
    # the proposed (non-executable) record; a follow-on skill_promotion takes
    # it active. The full SKILL.md text rides in the payload (shown by cli_show).
    name = proposal.payload.get("skill_name", "?")
    return {
        "skill_synthesis": {
            "skill_name": name,
            "stages_to": f"~/.grove/skills/.andon/{name}/",
            "record_state": "proposed (non-executable until promoted)",
            "when_to_use": proposal.payload.get("when_to_use", ""),
            "tool_sequence": proposal.payload.get("tool_sequence", []),
            "next": "promote via `hermes andon promote` or a skill_promotion proposal",
        },
    }


def _summary_routing_adjustment(proposal: RoutingProposal) -> str:
    rule = proposal.payload.get("rule", "?")
    intents = ", ".join(proposal.payload.get("add_intents", []))
    base = f"add {intents} to routing.{rule}"
    # machine-sink-generalization-v1 — memory-enriched rationale, when present.
    justification = getattr(proposal, "semantic_justification", "") or ""
    if justification:
        return f"{base} ({justification})"
    return base


def _summary_pattern_promotion(proposal: RoutingProposal) -> str:
    ic = proposal.payload.get("intent_class", "?")
    ct = proposal.payload.get("cacheable_type", "?")
    samples = proposal.payload.get("sample_queries") or []
    sample = f" “{samples[0][:40]}”" if samples else ""
    return f"retire {ic} [{ct}] pattern{sample} to T0 cache"


def _summary_pattern_demotion(proposal: RoutingProposal) -> str:
    ic = proposal.payload.get("intent_class", "?")
    return f"demote {ic} pattern (drift: corrected after a T0 hit)"


def _summary_skill_promotion(proposal: RoutingProposal) -> str:
    name = proposal.payload.get("skill_name", "?")
    return f"promote quarantined skill {name!r} → trusted"


def _summary_zone_promotion(proposal: RoutingProposal) -> str:
    tool = proposal.payload.get("tool", "?")
    pattern = proposal.payload.get("pattern", "?")
    return f"greenlight {tool} pattern={pattern!r}"


def _summary_skill_synthesis(proposal: RoutingProposal) -> str:
    name = proposal.payload.get("skill_name", "?")
    return f"stage drafted skill {name!r} → quarantine for review"


def _summary_portal_action_failure(proposal: RoutingProposal) -> str:
    """portal-action-error-surfacing-v1 — a repeatedly-failing portal action,
    surfaced so the operator can approve a structural fix. The stable class is
    the body; the memory-enriched rationale (the ephemeral instance detail rides
    here per file_agentless_proposal) is appended when present."""
    action = proposal.payload.get("action", "?")
    failure_class = proposal.payload.get("failure_class", "?")
    base = f"portal action {action!r} keeps failing ({failure_class})"
    justification = getattr(proposal, "semantic_justification", "") or ""
    if justification:
        return f"{base} — {justification}"
    return base


def _summary_consolidation(proposal: RoutingProposal) -> str:
    """Natural-language graduation offer — no schema, no ids."""
    p = proposal.payload
    intent = p.get("intent_class", "?")
    tier = p.get("target_tier", "?")
    stats = p.get("stats") or {}
    n = stats.get("n", "?")
    rate = stats.get("success_rate")
    pct = f"{round(float(rate) * 100)}%" if isinstance(rate, (int, float)) else "?"
    return (
        f"Intent '{intent}' stable at {tier} ({n} obs, {pct} success, zero "
        f"halts). Promote to permanent routing policy?"
    )


def _consolidation_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """The two-file change the operator reviews before approving."""
    p = proposal.payload
    intent = p.get("intent_class", "?")
    tier = p.get("target_tier", "?")
    sink = p.get("source_sink", "?")
    return {
        "routing.config.yaml": {
            "routing_rules": {
                intent: {
                    "enabled": True,
                    "match": {"intents": [intent]},
                    "target_tier": tier,
                },
            },
        },
        "routing.autonomaton.yaml": {
            "remove_from_sink": {sink: [intent]},
        },
    }


def _summary_dock_mutation(proposal: RoutingProposal) -> str:
    """Natural-language Dock-goal offer — record count + theme, no ids."""
    goal = (proposal.payload or {}).get("goal") or {}
    name = goal.get("name", "an emerging theme")
    n = len(goal.get("source_record_ids") or [])
    noun = "record" if n == 1 else "records"
    return (
        f"{n} memory {noun} accumulating around '{name}'. No Dock goal tracks "
        f"this. Add a staging goal?"
    )


def _dock_mutation_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """The one-file change the operator reviews before approving."""
    goal = (proposal.payload or {}).get("goal") or {}
    return {
        "dock.autonomaton.yaml": {
            "goals": {
                "+add": {
                    "id": goal.get("id", "?"),
                    "name": goal.get("name", "?"),
                    "keywords": goal.get("keywords", []),
                    "vector": goal.get("vector", "personal"),
                    "status": goal.get("status", "staging"),
                },
            },
        },
    }


def _binding_phrase(binding: Any) -> str:
    """Human phrase for a model_binding dict (binding-governance-surfaces-v1)."""
    if not isinstance(binding, dict):
        return "tier inheritance"
    btype = binding.get("type")
    if btype == "model":
        return f"pinned to {binding.get('model', '?')}"
    if btype == "tier_override":
        return f"tier override {binding.get('tier', '?')}"
    return f"{btype or '?'} binding"


def _summary_model_binding(proposal: RoutingProposal) -> str:
    """Natural-language binding offer — skill + before/after, no record ids."""
    p = proposal.payload or {}
    skill = p.get("skill", "?")
    proposed = p.get("proposed_binding")
    previous = p.get("previous_binding")
    was = _binding_phrase(previous) if previous else "inheriting its tier model"
    if proposed is None:
        return (
            f"Clear the model pin on '{skill}' (currently {was}) — it returns "
            f"to tier inheritance. Apply?"
        )
    if isinstance(proposed, dict) and proposed.get("type") == "model":
        return (
            f"Pin '{skill}' to {proposed.get('model', '?')} for fleet runs "
            f"(currently {was}). Apply?"
        )
    return f"Set '{skill}' to {_binding_phrase(proposed)} (currently {was}). Apply?"


def _model_binding_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """The one-record change the operator reviews before approving. Keyed on
    the skill name — the record file path resolves at apply time through the
    canonical slug-tail resolver, never at render time."""
    p = proposal.payload or {}
    return {
        f"capability record: {p.get('skill', '?')}": {
            "model_binding": {
                "-before": p.get("previous_binding"),
                "+after": p.get("proposed_binding"),
            },
        },
    }


def _summary_forge_artifact_pending(proposal: RoutingProposal) -> str:
    """fleet-pipeline-v1 P2 — a fleet worker staged a draft package for operator
    review. RENDER-ONLY w.r.t. the generic SYNC approve machinery (no
    PROPOSAL_HANDLERS row); the promote tap is the bespoke async route. The verb
    affordances (promote/reject) are rendered by the portal from the type's verb
    set, not from here."""
    pl = proposal.payload or {}
    slug = pl.get("slug", "?")
    fit = pl.get("fit_score")
    base = f"fleet draft staged for review: {slug}"
    if fit is not None:
        base += f" (fit {fit})"
    justification = getattr(proposal, "semantic_justification", "") or ""
    return f"{base} — {justification}" if justification and justification != base else base


def _summary_fleet_artifact_pending(proposal: RoutingProposal) -> str:
    """fleet-review-unification-v1 C1b-2 — the GENERIC file-producer sibling of
    the forge renderer, keyed on the stable unit_id (no Notion row_id).
    proposal-card-legibility-v1 Phase 3 — closes the GATE-A/V4 gap: the type
    had verbs (promote/reject) but NO registry entry, so the first
    action_surface_publish batch from a file producer (cultivator/drafter)
    would ValueError at get_renderer and silently kill that turn's push."""
    pl = proposal.payload or {}
    slug = pl.get("slug", "?")
    unit = pl.get("unit_id", "?")
    base = f"fleet draft staged for review: {slug} (unit {unit})"
    # drafter-quality-checks-v1 P4 — quality interpolation, the fit_score idiom
    # (see _summary_forge_artifact_pending above). A gated draft carries a
    # score; a gated-but-oversize draft carries rubric_version with a null
    # score (the skip annotation); an ungated producer carries neither and
    # renders byte-identically to pre-P4.
    score = pl.get("quality_score")
    if score is not None:
        base += f" (quality {score})"
    elif pl.get("rubric_version") is not None:
        base += " (quality skipped: oversize)"
    justification = getattr(proposal, "semantic_justification", "") or ""
    return f"{base} — {justification}" if justification and justification != base else base


def _summary_fault_triage(proposal: RoutingProposal) -> str:
    """kaizen-fault-triage-v1 — the one-line body IS the detector's judgment
    line (deterministic template over the group's evidence, byte-stable per
    group; amendment 3a). The full card body (judgment + evidence + samples)
    rides semantic_justification; this surfaces its first line."""
    body = getattr(proposal, "semantic_justification", "") or ""
    first_line = body.splitlines()[0] if body.strip() else ""
    return first_line or "recurring fault pattern detected"


# ── render registry (kaizen-proposal-surface-unification-v1) ──────────
#
# The ONE renderer surface, DECOUPLED from the apply-coupled PROPOSAL_HANDLERS.
# Maps a proposal type to a callable that turns a KaizenRenderable of that type
# into its one-line body. Routing types reuse their existing summary_renderer
# (which reads the RoutingProposal directly); memory_context is registered
# lazily (its renderer unwraps the adapter -> the proposal dict). Future types
# register here + in flywheel_cli._PUSH_PRIORITY and inherit the unified
# surface.
RENDER_REGISTRY: Dict[str, Callable[[Any], str]] = {}


def register_renderer(type_name: str, renderer: Callable[[Any], str]) -> None:
    RENDER_REGISTRY[type_name] = renderer


def _ensure_memory_renderer() -> None:
    """Lazy-register the memory_context renderer (avoids an import cycle —
    the render core must not import the memory package at module load)."""
    if "memory_context" in RENDER_REGISTRY:
        return
    try:
        from grove.memory.digest import MemoryProposalHandler
        RENDER_REGISTRY["memory_context"] = (
            lambda r: MemoryProposalHandler.summary_renderer(r.proposal_dict)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[flywheel] memory renderer registration failed: %r", exc)


def get_renderer(type_name: str) -> Callable[[Any], str]:
    """Resolve a proposal type to its body renderer. Fail loud on unknown.

    Honors the legacy ``routing_update`` -> ``routing_adjustment`` alias in
    this one place, mirroring ``flywheel_cli._handler_for`` (queue entries
    predating the Sprint 32 rename still render).
    """
    canonical = (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT
        if type_name == _LEGACY_ROUTING_TYPE
        else type_name
    )
    if canonical == "memory_context":
        _ensure_memory_renderer()
    try:
        return RENDER_REGISTRY[canonical]
    except KeyError:
        raise ValueError(f"No renderer for proposal type: {type_name!r}")


# ── offering composer (kaizen-offerings Cut B — one voice chokepoint) ─
#
# C1 — deterministic on-register prefixes, hardcoded in Python. NO markdown
# file, NO sync-operator.sh, curator-voice.md UNTOUCHED (that governs the LLM
# curator review only). The composer is self-contained: it adds NO per-offering
# model call and NO per-SURFACE branch — only the sanctioned push/pull split.
_OFFERING_PUSH_PREFIX = "Shop floor note —"          # the conversational interrupt lead
_OFFERING_PUSH_ASK = "want me to stage it for your review?"  # the foreman's offer


def compose_offering(
    proposal: Any,
    *,
    is_push: bool,
    portal_base_url: Optional[str] = None,
) -> str:
    """The ONE in-register renderer for an offering — any KaizenRenderable.

    Deterministic — no model call. The factual core is the per-type body from
    the RENDER_REGISTRY (routing/zone/skill/pattern/memory); only the framing
    differs:

    * ``is_push=True`` — a conversational interrupt for the post-turn push.
    * ``is_push=False`` — the BARE inventory body (no interrupt wrapper), so a
      pull queue / ``_format_summary`` / ``cli_show`` read as a list.

    ``portal_base_url`` (portal-link-reliability-v1, P1) — when set AND this is
    a push, a ready-made review deep link is appended to the note. None/empty
    leaves the note unchanged (I2 graceful degradation). The pull form
    (``is_push=False``) never carries a link regardless.
    """
    core = get_renderer(proposal.type)(proposal)
    if not is_push:
        return core

    # portal-reader-contract-fix-v1 — memory (all voices) and consolidation
    # proposals review in the PORTAL, not in chat: for them the conversation
    # surface is a notification channel, not a review surface. A compact
    # one-line push replaces the full-content dump + in-chat approve/dismiss.
    # The opt-in is the renderable's ``requires_portal_review`` property (no
    # type-checking here — the CLI layer stays ignorant of specific types).
    # Gated on a RESOLVED base URL: a compact note with a dead link would
    # strand the operator, so a missing URL falls back — LOUDLY — to the
    # verbose in-chat form rather than emit an unreachable notification.
    if proposal.requires_portal_review:
        if portal_base_url:
            return (
                f"📋 New proposals await your review → "
                f"{portal_base_url}/portal#fragments/proposals/pending"
            )
        logger.warning(
            "portal_base_url unresolved — falling back to verbose Kaizen rendering"
        )

    # kaizen-voice — conversational register, no CLI syntax / no id. The
    # type-specific clause comes from the renderable (routing: "I noticed I
    # could …"; memory: "I crystallized a domain insight …"); the shared frame +
    # approve/dismiss tail are the one Kaizen voice. The operator replies in
    # natural language; the model routes it via review_proposals -> approve.
    # portal-action-error-surfacing-v1 — offer only affordances the apply path
    # honors. A render-only type (offers_approve False) drops the approve reply
    # AND the stage-for-review ask (there is nothing to stage): it is a
    # notification the operator can dismiss, routed through the tolerant
    # cli_reject. The opt-out is the renderable's ``offers_approve`` property — no
    # type-checking here (the CLI layer stays ignorant of specific types).
    if proposal.offers_approve:
        note = (
            f"{_OFFERING_PUSH_PREFIX} {proposal.push_body(core)} — {_OFFERING_PUSH_ASK} "
            f"Reply 'approve' to apply this, or 'dismiss' to skip."
        )
    else:
        note = (
            f"{_OFFERING_PUSH_PREFIX} {proposal.push_body(core)} — "
            f"reply 'dismiss' to skip."
        )
    # portal-link-reliability-v1 (P1) — ready-made review deep link, embedded
    # mechanically (never a template the model fills). Appended only when the
    # caller resolved a base URL from the resident config (I2: missing → no link).
    if portal_base_url:
        note += f" 📋 [Review]({portal_base_url}/portal#fragments/proposals/pending)"
    return note


# ── registry seeding + boot census (proposal-card-legibility-v1) ──────


def seed_from_handlers(handlers: Dict[str, Any]) -> None:
    """Seed the render registry from the apply registry, census-asserted.

    kaizen-proposal-surface-unification-v1 — every routing/zone/skill/pattern
    type reuses its existing ``summary_renderer`` (which reads the
    RoutingProposal directly); render stays decoupled from apply — the two
    registries are separate by design. ``PROPOSAL_HANDLERS`` stays in
    ``grove.flywheel_cli`` (its rows carry apply callbacks); it calls this at
    its module load, so load-order semantics match the pre-split inline loop.

    Boot census (Digital Jidoka): a handlers row with no callable summary
    renderer, or an approvable row (every handlers row IS approvable) with no
    callable diff renderer, is a wiring defect — an approvable card would
    render blank or unreviewable. Fail LOUD at import, naming the type.
    """
    for type_name, handler in handlers.items():
        renderer = getattr(handler, "summary_renderer", None)
        if not callable(renderer):
            raise RuntimeError(
                f"render census: proposal type {type_name!r} has a "
                f"PROPOSAL_HANDLERS row but no callable summary_renderer — "
                f"its approvable card would render blank."
            )
        register_renderer(type_name, renderer)
        if not callable(getattr(handler, "diff_renderer", None)):
            raise RuntimeError(
                f"render census: approvable proposal type {type_name!r} has "
                f"no callable diff_renderer — the operator could not review "
                f"the mutation before approving."
            )
    # Census clause 3 (proposal-card-legibility-v1 Phase 3) — every
    # verb-bearing type (a PROPOSAL_VERBS key) must resolve a summary renderer.
    # The direct registrations run at this module's import, strictly before
    # the seed call, so by now the registry is complete: a verb-bearing type
    # missing here is exactly the fleet_artifact_pending class of gap (verbs
    # offered, body unrenderable — GATE-A Andon candidate #1).
    for type_name in PROPOSAL_VERBS:
        if type_name not in RENDER_REGISTRY:
            raise RuntimeError(
                f"render census: verb-bearing proposal type {type_name!r} "
                f"(PROPOSAL_VERBS) has no registered summary renderer — its "
                f"card would offer dispositions on an unrenderable body."
            )


# portal-action-error-surfacing-v1 (Phase 1) — a RENDER-ONLY type: it composes
# and pushes like any Kaizen offering, but has no apply handler yet (approve/apply
# wiring is a later phase), so it is registered straight into RENDER_REGISTRY
# rather than seeded from PROPOSAL_HANDLERS — the same shape memory_context uses.
register_renderer(
    PROPOSAL_TYPE_PORTAL_ACTION_FAILURE, _summary_portal_action_failure
)

# fleet-pipeline-v1 P2 — RENDER-ONLY w.r.t. the generic SYNC approve machinery
# (no PROPOSAL_HANDLERS row); the promote tap is the bespoke async route.
register_renderer(
    PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, _summary_forge_artifact_pending
)

# proposal-card-legibility-v1 Phase 3 — the generic fleet sibling, same
# RENDER-ONLY posture as forge (verbs are promote/reject via PROPOSAL_VERBS).
register_renderer(
    PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, _summary_fleet_artifact_pending
)

# kaizen-fault-triage-v1 — RENDER-ONLY (no PROPOSAL_HANDLERS row, so approve
# is structurally unoffered); registered straight into RENDER_REGISTRY, the
# portal_action_failure shape. Dispositions are the acknowledge/dismiss verb
# set (PROPOSAL_VERBS), not approve/apply.
register_renderer(PROPOSAL_TYPE_FAULT_TRIAGE, _summary_fault_triage)


# ── structured detail codecs (proposal-card-legibility-v1 Phase 2) ────
#
# A proposal's ``detail`` envelope (RoutingProposal.detail — identity-excluded,
# omit-when-None) carries STRUCTURED, render-ready evidence so render surfaces
# never parse semantic_justification text. Codecs map a proposal type to a
# ``from_dict`` decoder; a type with no codec simply has no typed detail
# (decode_detail → None). Malformed detail raises ValueError — fail loud; the
# render caller owns the fallback (Phase 3: verbatim sj + warning log).


@dataclass(frozen=True)
class FaultSample:
    """One normalized fault event: ``ts`` (YYYY-MM-DD date only — no
    microsecond ISO noise on the card), ``subject`` (who faulted), ``outcome``
    (what happened). Field mapping per source is the producer's contract
    (``fault_triage._normalize_sample``)."""

    ts: str
    subject: str
    outcome: str

    def to_dict(self) -> Dict[str, str]:
        return {"ts": self.ts, "subject": self.subject, "outcome": self.outcome}

    @classmethod
    def from_dict(cls, data: Any) -> "FaultSample":
        if not isinstance(data, dict):
            raise ValueError(
                f"FaultSample must be a dict, got {type(data).__name__}"
            )
        missing = [k for k in ("ts", "subject", "outcome") if k not in data]
        if missing:
            raise ValueError(f"FaultSample missing field(s): {', '.join(missing)}")
        return cls(
            ts=str(data["ts"]),
            subject=str(data["subject"]),
            outcome=str(data["outcome"]),
        )


@dataclass(frozen=True)
class FaultTriageDetail:
    """The fault_triage detail envelope: the sampled raw events, normalized
    to compact deterministic lines at BUILD time (producer-side), so the card
    renders ``date · subject · outcome`` with zero inference."""

    samples: List[FaultSample]

    def to_dict(self) -> Dict[str, Any]:
        return {"samples": [s.to_dict() for s in self.samples]}

    @classmethod
    def from_dict(cls, data: Any) -> "FaultTriageDetail":
        if not isinstance(data, dict):
            raise ValueError(
                f"fault_triage detail must be a dict, got {type(data).__name__}"
            )
        raw = data.get("samples")
        if not isinstance(raw, list):
            raise ValueError("fault_triage detail has no 'samples' list")
        return cls(samples=[FaultSample.from_dict(s) for s in raw])


DETAIL_CODECS: Dict[str, Callable[[Dict[str, Any]], Any]] = {}


def register_detail_codec(
    type_name: str, decoder: Callable[[Dict[str, Any]], Any],
) -> None:
    DETAIL_CODECS[type_name] = decoder


def decode_detail(proposal: Any) -> Optional[Any]:
    """Typed ``detail`` for a proposal — None when the envelope is absent or
    the type has no registered codec; ValueError (from the codec) when the
    envelope is present but malformed. Read-only: never mutates the proposal.
    """
    raw = getattr(proposal, "detail", None)
    if raw is None:
        return None
    decoder = DETAIL_CODECS.get(getattr(proposal, "type", "") or "")
    if decoder is None:
        return None
    return decoder(raw)


register_detail_codec(PROPOSAL_TYPE_FAULT_TRIAGE, FaultTriageDetail.from_dict)
