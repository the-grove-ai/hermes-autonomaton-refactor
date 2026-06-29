"""GRV-008 § IV operator review surface for Sprint 47 routing proposals.

CLI renderers reachable via ``autonomaton flywheel
{list,show,approve,reject}``. Each renderer prints to stdout/stderr,
sets a UNIX exit code, and writes telemetry events the Kaizen Ledger
records as operator sovereignty acts.

The four operations:

* :func:`cli_list` — show every pending proposal in
  ``~/.grove/proposals.jsonl`` as one human-readable line each.
* :func:`cli_show` — show one proposal's payload, evidence, and the
  diff it would apply to ``routing.autonomaton.yaml``.
* :func:`cli_approve` — apply the proposal's payload to the machine
  file (set-union per GRV-008 § III); remove from queue.
* :func:`cli_reject` — remove from queue; no config change.

Per GRV-008 § III the machine file is the only path the renderers
write to. ``routing.config.yaml`` is never opened in write mode.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import yaml

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_CONSOLIDATION,
    PROPOSAL_TYPE_DOCK_MUTATION,
    PROPOSAL_TYPE_PATTERN_DEMOTION,
    PROPOSAL_TYPE_PATTERN_PROMOTION,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_SKILL_PROMOTION,
    PROPOSAL_TYPE_SKILL_SYNTHESIS,
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    default_queue_path,
    read,
    read_all,
    remove,
)
from grove.router_merge import _MACHINE_HEADER, apply_diff_to_machine_config

# The Sprint 47 v0.1 spelling. Honored as an alias for routing_adjustment on
# read (proposal_queue back-compat) and resolved to the routing_adjustment
# handler in ONE place — :func:`_handler_for`. B1 GATE-B: keep + flag (the live
# VM queue could not be verified empty of this spelling at build time).
_LEGACY_ROUTING_TYPE = "routing_update"

logger = logging.getLogger(__name__)


__all__ = [
    "cli_list",
    "cli_show",
    "cli_approve",
    "cli_reject",
    "run_tier_ratchet_scan",
    "run_disposition_promotion_scan",
    "compose_offering",
]


def _machine_config_path() -> Path:
    """The hermes_home routing.autonomaton.yaml path."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "routing.autonomaton.yaml"


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


def _proposal_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a proposal payload into the diff the operator reviews.

    B1 — single registry dispatch (no if/elif ladder). Unknown type raises
    via :func:`_handler_for` — never a silent fallback render.
    """
    return _handler_for(proposal.type).diff_renderer(proposal)


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


# ── offering composer (kaizen-offerings Cut B — one voice chokepoint) ─
#
# C1 — deterministic on-register prefixes, hardcoded in Python. NO markdown
# file, NO sync-operator.sh, curator-voice.md UNTOUCHED (that governs the LLM
# curator review only). The composer is self-contained: it adds NO per-offering
# model call and NO per-SURFACE branch — only the sanctioned push/pull split.
_OFFERING_PUSH_PREFIX = "Shop floor note —"          # the conversational interrupt lead
_OFFERING_PUSH_ASK = "want me to stage it for your review?"  # the foreman's offer

# C3 — fixed type-priority for the post-turn push (NOT a learned ranker). Lower
# = surfaced first. kaizen-proposal-surface-unification-v1: memory_context slots
# at 1 (Gemini ruling) — confirming the system's understanding of the operator's
# world outranks mechanical routing optimizations; everything else bumps down.
# Unknown types sort last.
_PUSH_PRIORITY = {
    PROPOSAL_TYPE_SKILL_SYNTHESIS: 0,
    "memory_context": 1,                      # == PROPOSAL_TYPE_MEMORY_CONTEXT
    # consolidation-ratchet-v1 — a permanent-policy graduation outranks a
    # transient routing tweak but yields to a fresh memory insight. A fractional
    # slot keeps it strictly BETWEEN memory (1) and routing_adjustment (2)
    # without renumbering the latter (whose ==2 value is contract-tested).
    PROPOSAL_TYPE_CONSOLIDATION: 1.5,
    # dock-as-mutation-target-v1 — a Dock-goal proposal is a strategic
    # observation the operator ratifies; it ranks just after a policy
    # graduation and ahead of a routing tweak. Fractional slot keeps the
    # contract-tested integers (routing_adjustment ==2) intact.
    PROPOSAL_TYPE_DOCK_MUTATION: 1.7,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT: 2,
    PROPOSAL_TYPE_ZONE_PROMOTION: 3,
    PROPOSAL_TYPE_SKILL_PROMOTION: 3,
    PROPOSAL_TYPE_PATTERN_PROMOTION: 4,
    PROPOSAL_TYPE_PATTERN_DEMOTION: 4,
}


# ── render registry (kaizen-proposal-surface-unification-v1) ──────────
#
# The ONE renderer surface, DECOUPLED from the apply-coupled PROPOSAL_HANDLERS.
# Maps a proposal type to a callable that turns a KaizenRenderable of that type
# into its one-line body. Routing types reuse their existing summary_renderer
# (which reads the RoutingProposal directly); memory_context is registered
# lazily (its renderer unwraps the adapter -> the proposal dict). Future types
# register here + in _PUSH_PRIORITY and inherit the unified surface.
RENDER_REGISTRY: Dict[str, Callable[[Any], str]] = {}


def register_renderer(type_name: str, renderer: Callable[[Any], str]) -> None:
    RENDER_REGISTRY[type_name] = renderer


def _ensure_memory_renderer() -> None:
    """Lazy-register the memory_context renderer (avoids an import cycle —
    flywheel_cli must not import the memory package at module load)."""
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
    this one place, mirroring :func:`_handler_for` (queue entries predating the
    Sprint 32 rename still render).
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
    note = (
        f"{_OFFERING_PUSH_PREFIX} {proposal.push_body(core)} — {_OFFERING_PUSH_ASK} "
        f"Reply 'approve' to apply this, or 'dismiss' to skip."
    )
    # portal-link-reliability-v1 (P1) — ready-made review deep link, embedded
    # mechanically (never a template the model fills). Appended only when the
    # caller resolved a base URL from the resident config (I2: missing → no link).
    if portal_base_url:
        note += f" 📋 [Review]({portal_base_url}/portal#fragments/proposals/pending)"
    return note


def _format_summary(proposal: RoutingProposal) -> str:
    """One-line operator-facing summary of a proposal (the structured index).

    B1 — single registry dispatch. kaizen-offerings — the human clause is the
    composer's bare pull form (``compose_offering(is_push=False)``), so the
    voiced and structured surfaces share one source; the id/evidence/timestamp
    framing stays here for the index the agent and CLI need.
    """
    short_id = proposal.proposal_id.split(":")[-1][:12]
    n_evidence = len(proposal.evidence)
    body = compose_offering(proposal, is_push=False)
    return (
        f"{short_id}  {proposal.type:<22}  "
        f"{body}  "
        f"(evidence: {n_evidence} turn(s))  "
        f"{proposal.created_at}"
    )


# ── list ─────────────────────────────────────────────────────────────


def cli_list(*, queue_path: Optional[Path] = None) -> int:
    """Show every pending proposal in the queue.

    Returns exit code 0. Empty queue prints a friendly message.
    """
    proposals = read_all(path=queue_path or default_queue_path())
    if not proposals:
        print(
            "No pending Flywheel proposals. The TierRatchet emits "
            "proposals once usage patterns shift a class into the "
            "qualifying band (see GRV-008 § I)."
        )
        return 0
    print(f"{len(proposals)} pending proposal(s) in the queue:")
    print()
    for proposal in proposals:
        print("  " + _format_summary(proposal))
    print()
    print(
        "Inspect: autonomaton flywheel show <id>\n"
        "Approve: autonomaton flywheel approve <id>\n"
        "Reject:  autonomaton flywheel reject <id> [--reason \"...\"]"
    )
    return 0


# ── TierRatchet routing scan (B2 — wire the dark detector) ───────────


def _load_current_routing_rules() -> Optional[Dict[str, Any]]:
    """Best-effort merged ``routing.routing_rules`` for the TierRatchet detector.

    Operator ``routing.config.yaml`` (precedence) deep-merged with the machine
    ``routing.autonomaton.yaml``; returns the ``routing.routing_rules`` block so
    the detector skips intents already listed. None on any failure (a fresh
    install with no operator config) — the detector then treats every relevant
    intent as a fresh addition. Read-only — never writes routing.config.yaml.
    """
    try:
        from grove.router_merge import load_merged_routing_config

        op = Path.home() / ".grove" / "routing.config.yaml"
        if not op.exists():
            return None
        merged = load_merged_routing_config(op, _machine_config_path())
        return (merged.get("routing") or {}).get("routing_rules") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[flywheel] could not load routing rules for the ratchet scan "
            "(treating all intents as fresh): %r", exc,
        )
        return None


def run_tier_ratchet_scan(
    *,
    store: Optional[Any] = None,
    current_routing_rules: Optional[Dict[str, Any]] = None,
    queue_path: Optional[Path] = None,
) -> Tuple[int, int]:
    """Run the TierRatchet detector over the intent store; queue its
    ``routing_adjustment`` proposals. B2 — the cadence the dark detector lacked.

    Mirrors ``pattern_compiler.propose_pattern_promotions``: read the intent
    store (collapsed latest-by-turn view), detect, append directly to the
    proposal queue (no hero-suite gate on the production propose path, same as
    the pattern compiler). Idempotent — ``proposal_id`` is stable for the same
    detected cluster (``source_patterns`` is hash-excluded; evidence turn-ids
    fold in), so a re-run over unchanged store state DEDUPS via
    ``proposal_queue.append`` returning False rather than stacking duplicates.

    Returns ``(queued_new, deduped)``.
    """
    from grove.eval.proposal_queue import append as _append
    from grove.eval.tier_ratchet import propose_routing_adjustments

    if store is None:
        from grove.intent_store import get_store
        store = get_store()
    if current_routing_rules is None:
        current_routing_rules = _load_current_routing_rules()

    # machine-sink-generalization-v1 — one MemoryStore for the whole scan,
    # reused across every proposal (query() is a fast in-memory index read;
    # construction is the one log replay). Best-effort: if the store cannot be
    # built (no ~/.grove, corrupt index), enrichment is skipped — proposals
    # still generate without context.
    memory_store: Optional[Any] = None
    try:
        from hermes_constants import get_hermes_home
        from grove.memory.store import MemoryStore
        memory_store = MemoryStore(base_dir=Path(get_hermes_home()))
    except Exception as exc:  # noqa: BLE001 — enrichment is additive
        logger.warning(
            "[flywheel] memory store unavailable for ratchet enrichment "
            "(proposals will generate without context): %r", exc,
        )

    records = list(store.latest_by_turn())
    proposals = propose_routing_adjustments(
        records, current_routing_rules=current_routing_rules,
        memory_store=memory_store,
    )
    target = queue_path or default_queue_path()
    queued_new = deduped = 0
    for proposal in proposals:
        if _append(proposal, path=target):
            queued_new += 1
        else:
            deduped += 1
    return queued_new, deduped


def run_disposition_promotion_scan(
    *,
    ledger_dir: Optional[Path] = None,
    queue_path: Optional[Path] = None,
    thresholds: Optional[Any] = None,
    now: Optional[Any] = None,
) -> Tuple[int, int]:
    """Run the YELLOW promotion detector over the Kaizen ledger; queue its
    ``zone_promotion`` proposals. learning-loop-bridge-v1 (Strike 2).

    An INDEPENDENT Flywheel signal that shares the ``flywheel scan --propose``
    cadence with TierRatchet without coupling enable flags — mirroring how the
    pattern-cache and TierRatchet scans coexist. Reads ``andon_disposition``
    events (repeated operator approvals at the Sovereign Prompt), not the
    intent store. Idempotent: ``proposal_id`` is stable per ``(tool, rule)``,
    so a re-run over unchanged ledger state DEDUPS via
    ``proposal_queue.append`` returning False rather than stacking duplicates.

    Returns ``(queued_new, deduped)``.
    """
    from grove.eval.proposal_queue import append as _append
    from grove.eval.disposition_promotion import (
        DispositionPromotionDetector,
        load_promotion_thresholds,
    )

    if thresholds is None:
        thresholds = load_promotion_thresholds()
    detector = DispositionPromotionDetector(
        ledger_dir=ledger_dir, thresholds=thresholds,
    )
    proposals = detector.detect(now=now)
    target = queue_path or default_queue_path()
    queued_new = deduped = 0
    for proposal in proposals:
        if _append(proposal, path=target):
            queued_new += 1
        else:
            deduped += 1
    return queued_new, deduped


# ── scan (T0 pattern cache — Sprint 48) ──────────────────────────────


def cli_scan(
    *,
    store: Optional[Any] = None,
    propose: bool = False,
    queue_path: Optional[Path] = None,
) -> int:
    """Scan the intent store for T0 pattern-cache candidates.

    Read-only by default: groups Flywheel evidence by a conservative
    normalized key and prints the patterns that meet the configured
    thresholds. With ``propose=True`` it also compiles each safely-compilable
    candidate and queues a ``pattern_promotion`` proposal for operator
    approval (skipping patterns already known/rejected).

    B2 — the same ``--propose`` invocation also runs the TierRatchet routing
    scan, an INDEPENDENT signal: it queues ``routing_adjustment`` proposals
    regardless of ``pattern_cache.enabled`` or whether any T0 candidate exists,
    so the two flywheel detectors share one operator cadence without coupling
    their enable flags."""
    if propose:
        rq_new, rq_dup = run_tier_ratchet_scan(store=store, queue_path=queue_path)
        if rq_new or rq_dup:
            print(
                f"TierRatchet: queued {rq_new} routing_adjustment proposal(s)"
                + (f", {rq_dup} already pending (deduped)" if rq_dup else "")
                + "."
            )
        else:
            print("TierRatchet: no routing adjustments meet the threshold.")
        print()

        dp_new, dp_dup = run_disposition_promotion_scan(queue_path=queue_path)
        if dp_new or dp_dup:
            print(
                f"YELLOW promotions: queued {dp_new} zone_promotion proposal(s)"
                + (f", {dp_dup} already pending (deduped)" if dp_dup else "")
                + "."
            )
        else:
            print(
                "YELLOW promotions: no repeated approvals meet the threshold."
            )
        print()

    from grove.eval.pattern_compiler import (
        scan_candidates, load_pattern_cache_config,
    )
    cfg = load_pattern_cache_config()
    if not cfg.get("enabled", True):
        print("Pattern cache disabled (pattern_cache.enabled: false).")
        return 0
    if store is None:
        from grove.intent_store import get_store
        store = get_store()

    candidates = scan_candidates(store, cfg)
    if not candidates:
        print("No T0 pattern-cache candidates.")
        print(
            f"  Thresholds: >={cfg['min_repetitions']} reps within "
            f"{cfg['within_days']}d, <={cfg['max_rejections']} corrections; "
            f"excluding {cfg['exclude_intents']}."
        )
        return 0

    print(f"{len(candidates)} T0 pattern-cache candidate(s):")
    print()
    for c in candidates:
        print(
            f"  [{c.cacheable_type}] {c.intent_class}  "
            f"{c.repetition_count}x over {c.time_span_days}d  "
            f"corrections={c.rejection_count}  "
            f"key={c.t0_key.split(':')[-1][:12]}"
        )
        for q in c.sample_queries:
            print(f"      • {q[:80]}")
        print()

    if not propose:
        print("These are candidates only. Re-run with --propose to queue "
              "promotion proposals for approval.")
        return 0

    from grove.eval.pattern_compiler import (
        propose_pattern_promotions, DISPOSITION_PROPOSED, DISPOSITION_SKIPPED_KNOWN,
    )
    from grove.pattern_cache import PatternCacheStore
    result = propose_pattern_promotions(
        store, PatternCacheStore(), queue_path=queue_path, config=cfg,
    )
    proposed = [d for d in result.dispositions if d.status == DISPOSITION_PROPOSED]
    known = [d for d in result.dispositions if d.status == DISPOSITION_SKIPPED_KNOWN]
    dropped = [
        d for d in result.dispositions
        if d.status not in (DISPOSITION_PROPOSED, DISPOSITION_SKIPPED_KNOWN)
    ]

    # Loud feedback (Sprint 56 Fix #1): every candidate is accounted for —
    # nothing is dropped silently.
    print(
        f"\nProposed {len(proposed)} of {len(result.dispositions)} candidate(s)"
        + (f" ({len(known)} already in cache)" if known else "")
        + (f" ({len(dropped)} dropped)" if dropped else "")
        + ":"
    )
    for d in proposed:
        print(f"  ✓ proposed  [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  → {d.proposal_id.split(':')[-1][:12]}")
    for d in known:
        print(f"  • skipped   [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  — {d.detail}")
    for d in dropped:
        print(f"  ✗ dropped   [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  — {d.detail}")
    if proposed:
        print("\nReview: autonomaton flywheel list / approve <id>.")
    return 0


# ── patterns (T0 cache operator controls — Sprint 49) ────────────────

# Estimated average T1 interaction size, used by ``patterns stats`` to turn a
# per-million-token price into a per-interaction savings estimate. These are
# deliberately conservative rough averages, NOT measured — the stats output
# labels the savings as an estimate so the operator reads it as such.
_T1_AVG_INPUT_TOKENS = 1800
_T1_AVG_OUTPUT_TOKENS = 400


def _t1_interaction_cost_usd() -> Optional[float]:
    """Estimated USD cost of one averted T1 interaction.

    Reads ``tier_preferences.T1.cost_per_mtok_input/output`` from
    routing.config.yaml (operator copy wins over the repo default, same
    precedence as the pattern_cache config) and multiplies by the assumed
    average interaction size. Returns None when the T1 tier declares no
    cost — the caller then reports savings as unavailable rather than $0."""
    candidates = [
        Path.home() / ".grove" / "routing.config.yaml",
        Path(__file__).resolve().parents[1] / "config" / "routing.config.yaml",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        t1 = (
            ((data.get("routing") or {}).get("tier_preferences") or {}).get("T1")
            or {}
        )
        cost_in = t1.get("cost_per_mtok_input")
        cost_out = t1.get("cost_per_mtok_output")
        if cost_in is None or cost_out is None:
            return None
        return (
            _T1_AVG_INPUT_TOKENS / 1_000_000 * float(cost_in)
            + _T1_AVG_OUTPUT_TOKENS / 1_000_000 * float(cost_out)
        )
    return None


def cli_patterns_list(*, store: Optional[Any] = None) -> int:
    """Show every compiled T0 pattern with its lifecycle + hit telemetry."""
    if store is None:
        from grove.pattern_cache import PatternCacheStore
        store = PatternCacheStore()
    patterns = store.all()
    if not patterns:
        print(
            "No compiled T0 patterns. The compiler proposes them once a "
            "query repeats with a stable result — see "
            "`autonomaton flywheel scan`."
        )
        return 0
    active = sum(1 for p in patterns if p.status == "active")
    print(f"{len(patterns)} compiled T0 pattern(s) ({active} active):")
    print()
    print(
        f"  {'pattern':<14}{'intent_class':<18}{'type':<11}"
        f"{'status':<11}{'hits':>5}  {'last_hit':<20}sample"
    )
    for p in patterns:
        # promotion_evidence is JSON; show a short sample query if present.
        sample_q = ""
        try:
            ev = json.loads(p.promotion_evidence) if p.promotion_evidence else {}
            sqs = ev.get("sample_queries") if isinstance(ev, dict) else None
            if isinstance(sqs, list) and sqs:
                sample_q = str(sqs[0])[:40]
        except (ValueError, TypeError):
            sample_q = ""
        last_hit = (p.last_hit_at or "—")[:19]
        print(
            f"  {p.pattern_id.split(':')[-1][:12]:<14}"
            f"{p.intent_class:<18}{p.cacheable_type:<11}"
            f"{p.status:<11}{p.hit_count:>5}  {last_hit:<20}{sample_q}"
        )
    print()
    print(
        "Demote: autonomaton flywheel patterns demote <pattern>\n"
        "Stats:  autonomaton flywheel patterns stats"
    )
    return 0


def cli_patterns_demote(
    partial_id: str,
    *,
    store: Optional[Any] = None,
    assume_yes: bool = False,
) -> int:
    """Manually demote an active T0 pattern back to T1 (GATE-A D4 / 3b).

    Resolves ``partial_id`` against compiled pattern ids (full ``sha256:``,
    bare hash, or a ≥8-char prefix), confirms with the operator (unless
    ``assume_yes`` or stdin is not a TTY in an already-confirmed flow), sets
    the status to demoted, and logs a ``pattern_demoted`` event."""
    from grove.pattern_cache import PatternCacheStore, STATUS_DEMOTED
    from grove.telemetry import log_pattern_cache_event

    if store is None:
        store = PatternCacheStore()
    pattern = _resolve_pattern(partial_id, store)
    if pattern is None:
        print(
            f"No compiled pattern matches {partial_id!r}.", file=sys.stderr,
        )
        return 1
    if pattern.status == STATUS_DEMOTED:
        print(f"Pattern {pattern.pattern_id.split(':')[-1][:12]} is already demoted.")
        return 0

    short = pattern.pattern_id.split(":")[-1][:12]
    if not assume_yes:
        prompt = (
            f"Demote {pattern.intent_class} [{pattern.cacheable_type}] "
            f"pattern {short} (served {pattern.hit_count}x)? It falls back to "
            f"T1 inference. [y/N]: "
        )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n(no input — leaving the pattern active)")
            return 1
        if answer not in ("y", "yes"):
            print("Cancelled — pattern left active.")
            return 1

    store.set_status(pattern.pattern_id, STATUS_DEMOTED)
    log_pattern_cache_event(
        event_type="pattern_demoted",
        pattern_id=pattern.pattern_id,
        intent_class=pattern.intent_class,
        cacheable_type=pattern.cacheable_type,
    )
    print(
        f"Demoted {pattern.intent_class} pattern {short} → T1. "
        f"It no longer serves from cache."
    )
    return 0


def cli_patterns_stats(*, store: Optional[Any] = None) -> int:
    """Show T0 hit volume, hit rate, and estimated inference savings (3c)."""
    if store is None:
        from grove.pattern_cache import PatternCacheStore
        store = PatternCacheStore()
    patterns = store.all()
    active = [p for p in patterns if p.status == "active"]
    total_hits = sum(p.hit_count for p in active)

    print("T0 Pattern Cache — stats")
    print()
    print(f"  Active patterns:      {len(active)}")
    print(f"  Total patterns:       {len(patterns)}")
    print(f"  Total hits (active):  {total_hits}")

    # Hit rate is derived from the intent store (T0 hits write tier_selected
    # == "T0"); telemetry itself is log-only, not a queryable store.
    try:
        from grove.intent_store import get_store as _get_intent_store
        records = list(_get_intent_store().records())
        total_turns = len(records)
        t0_turns = sum(1 for r in records if (r.tier_selected or "") == "T0")
        if total_turns:
            rate = t0_turns / total_turns * 100.0
            print(
                f"  T0 hit rate:          {rate:.1f}%  "
                f"({t0_turns}/{total_turns} recorded turns)"
            )
        else:
            print("  T0 hit rate:          n/a (no recorded turns yet)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[flywheel] hit-rate read failed: %r", exc)
        print("  T0 hit rate:          n/a (intent store unavailable)")

    per_interaction = _t1_interaction_cost_usd()
    if per_interaction is None:
        print(
            "  Estimated savings:    n/a (T1 tier declares no cost_per_mtok "
            "in routing.config.yaml)"
        )
    else:
        savings = total_hits * per_interaction
        print(
            f"  Estimated savings:    ~${savings:.4f}  "
            f"(={total_hits} hits × ~${per_interaction:.5f}/interaction, "
            f"assuming ~{_T1_AVG_INPUT_TOKENS}in/{_T1_AVG_OUTPUT_TOKENS}out "
            f"T1 tokens — rough estimate)"
        )
    return 0


def _resolve_pattern(partial_id: str, store: Any) -> Optional[Any]:
    """Resolve a compiled pattern by full id, bare hash, or ≥8-char prefix."""
    # Exact id (with or without the sha256: prefix).
    direct = store.get(partial_id)
    if direct is not None:
        return direct
    if not partial_id.startswith("sha256:"):
        direct = store.get(f"sha256:{partial_id}")
        if direct is not None:
            return direct
    bare = partial_id.split(":")[-1]
    if len(bare) < 8:
        return None
    matches = [
        p for p in store.all()
        if p.pattern_id.split(":")[-1].startswith(bare)
    ]
    return matches[0] if len(matches) == 1 else None


# ── show ─────────────────────────────────────────────────────────────


def _resolve_proposal(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Resolve a proposal by full or short id.

    Accepts the full ``sha256:...`` id, the bare hash, or a unique
    short prefix (≥ 8 chars).
    """
    target = queue_path or default_queue_path()
    proposal = read(partial_id, path=target)
    if proposal is not None:
        return proposal
    bare = partial_id.split(":")[-1]
    if len(bare) < 8:
        return None
    matches = [
        p for p in read_all(path=target)
        if p.proposal_id.split(":")[-1].startswith(bare)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def cli_show(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Show one proposal's payload, evidence, and the YAML diff it
    would apply to the machine routing file.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}. "
            f"Run `autonomaton flywheel list` to see pending ids.",
            file=sys.stderr,
        )
        return 1

    # Sprint 60 / kaizen-offerings — concierge recommendation register. The
    # lead is the composer's bare pull form (one voice chokepoint; the per-type
    # _LEAD dict folded in), keeping the verbatim payload + diff (the operator
    # approves the REAL change, never a paraphrase) and the id/hash/evidence
    # reference footer.
    lead = compose_offering(proposal, is_push=False)
    short_id = proposal.proposal_id.split(":")[-1][:12]

    print(f"{lead} — your review before anything changes.")
    print()
    print("Here's what I'd put in place:")
    print(yaml.safe_dump(proposal.payload, sort_keys=False, default_flow_style=False))

    diff = _proposal_to_diff(proposal)
    print(f"What changes if you approve (run `flywheel approve {short_id}`):")
    print(yaml.safe_dump(diff, sort_keys=False, default_flow_style=False))

    print(
        f"Reference · ID {proposal.proposal_id} · type {proposal.type} · "
        f"eval hash {proposal.eval_hash or '(unset — pre-gate)'} · "
        f"{len(proposal.evidence)} turn(s)"
    )
    return 0


# ── approve ──────────────────────────────────────────────────────────


def _approve_routing_adjustment(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Apply a routing_adjustment proposal to routing.autonomaton.yaml.

    Returns the (target_path, applied_diff) pair so the caller can
    print the result. Per GRV-008 § III this NEVER touches
    ``routing.config.yaml``.
    """
    diff = _routing_adjustment_to_diff(proposal)
    target = machine_path or _machine_config_path()
    apply_diff_to_machine_config(diff, target)
    return target, diff


# ── consolidation (consolidation-ratchet-v1) ─────────────────────────────


def _operator_config_path() -> Path:
    """The hermes_home operator routing.config.yaml — the graduation target."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "routing.config.yaml"


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


def _approve_dock_mutation(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,
    dock_dir: Optional[Path] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Apply a dock_mutation proposal — append the staging goal to
    ``dock.autonomaton.yaml`` (the machine Dock file, a GREEN granted workspace).

    Delegates the read-modify-write to :func:`grove.dock.append_machine_goal`
    (atomic temp+replace, id dedup, ``.bak`` backup). NO reload needed —
    ``load_dock`` is fresh-per-call, so the goal is live on the next load. The
    ``machine_path`` kwarg (the routing machine file) is unused here; the dock
    writer resolves its own path (``dock_dir`` is injectable for tests).
    """
    from grove.dock import append_machine_goal

    goal = (proposal.payload or {}).get("goal")
    if not isinstance(goal, dict) or not goal.get("id"):
        raise ValueError(
            f"dock_mutation proposal {proposal.proposal_id} payload missing a "
            f"goal with an id"
        )
    target = append_machine_goal(goal, dock_dir=dock_dir)
    applied = {
        "goal_id": goal["id"],
        "goal_name": goal.get("name", ""),
        "status": goal.get("status", "staging"),
        "machine_file": str(target),
    }
    return target, applied


def _reload_default_router() -> None:
    """Hot-reload the live Cognitive Router so the graduated policy takes
    effect this session. No live router (e.g. an offline CLI approve) is not an
    error — the operator file is the source of truth, re-read at next init."""
    import grove.router as _router_mod

    router = _router_mod._default_router
    if router is None:
        logger.info(
            "[flywheel] consolidation applied; no live router to hot-reload "
            "(graduated policy applies on next init)"
        )
        return
    router.reload()


def _remove_intent_from_machine_sink(
    machine_path: Path, sink_name: str, intent_class: str
) -> None:
    """Drop ``intent_class`` from the named sink in the machine file.

    If the sink's intents list empties, the sink rule is removed entirely
    (A6: a missing rule is a clean set-union no-op; an empty intents list would
    also be harmless but we prune for tidiness). Writes the machine file with
    its standard header banner, mirroring ``apply_diff_to_machine_config``.
    """
    machine_path = Path(machine_path)
    if not machine_path.exists():
        return
    existing = yaml.safe_load(machine_path.read_text(encoding="utf-8"))
    if existing is None:
        return
    if not isinstance(existing, dict):
        raise ValueError(
            f"machine routing config at {machine_path} is not a YAML mapping"
        )
    rules = ((existing.get("routing") or {}).get("routing_rules")) or {}
    rule = rules.get(sink_name)
    if not isinstance(rule, dict):
        return
    match = rule.get("match") or {}
    intents = match.get("intents")
    if not isinstance(intents, list) or intent_class not in intents:
        return
    remaining = [i for i in intents if i != intent_class]
    if remaining:
        match["intents"] = remaining
    else:
        rules.pop(sink_name, None)
    rendered = yaml.safe_dump(existing, sort_keys=False, default_flow_style=False)
    machine_path.write_text(_MACHINE_HEADER + "\n" + rendered, encoding="utf-8")


def _approve_consolidation(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,
    operator_path: Optional[Path] = None,
    reload_fn: Optional[Callable[[], None]] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Graduate a sink intent to permanent operator policy — two-file atomic.

    The operator-file write — BACKUP → ruamel round-trip → sandbox-VALIDATE →
    atomic REPLACE → HOT-RELOAD — is delegated to the one sanctioned writer of
    ``routing.config.yaml`` (``grove.config.routing_writer``; C1 / GRV-008 § III).
    This function owns only the SECOND file: it cleans the graduated intent out
    of the machine sink, with its own backup/restore (R2). The machine sink is
    cleaned FIRST so the writer's sandbox validation and hot-reload observe the
    final merged state (operator gains the rule, machine drops the sink) and so
    the reload — the last step, inside the writer — never runs ahead of the
    machine edit. Any failure restores BOTH files and re-raises (fail loud, no
    partial state): the writer restores the operator file on its own failure;
    this function restores the machine sink it touched.
    """
    from grove.config.routing_writer import RoutingConfigWriter

    payload = proposal.payload
    intent_class = payload["intent_class"]
    target_tier = payload["target_tier"]
    source_sink = payload["source_sink"]

    op_path = Path(operator_path) if operator_path else _operator_config_path()
    mac_path = Path(machine_path) if machine_path else _machine_config_path()
    if not op_path.exists():
        raise FileNotFoundError(
            f"operator routing config not found at {op_path}; cannot graduate "
            f"a policy without the operator root (GRV-008 § III)"
        )

    def _graduate(data: Any) -> None:
        """Add the graduated intent's rule to the operator routing_rules."""
        if not isinstance(data, dict) or "routing" not in data:
            raise ValueError(f"{op_path} has no 'routing' mapping to graduate into")
        routing_rules = data["routing"].setdefault("routing_rules", {})
        routing_rules[intent_class] = {
            "enabled": True,
            "match": {"intents": [intent_class]},
            "target_tier": target_tier,
        }

    # R2 — this function owns the machine-sink file's backup/restore; the
    # operator file's backup/restore lives inside the writer.
    mac_backup = mac_path.read_bytes() if mac_path.exists() else None
    writer = RoutingConfigWriter(
        op_path,
        machine_path=mac_path,
        reload_fn=reload_fn or _reload_default_router,
    )

    try:
        # Step 1 — CLEANUP the machine sink first (the intent now lives in
        # operator policy), so the writer validates and reloads the final state.
        _remove_intent_from_machine_sink(mac_path, source_sink, intent_class)
        # Step 2 — operator-file write through the sole sanctioned writer:
        # BACKUP → ruamel mutate → sandbox-VALIDATE → atomic REPLACE → HOT-RELOAD.
        writer.apply_mutation(
            _graduate, label=f"graduate {intent_class} -> {target_tier}"
        )
    except Exception:
        # The writer restored the operator file on its own failure; restore the
        # machine sink this function touched, then fail loud (no partial state).
        if mac_backup is not None:
            mac_path.write_bytes(mac_backup)
        elif mac_path.exists():
            mac_path.unlink()
        raise

    applied = {
        "intent_class": intent_class,
        "target_tier": target_tier,
        "graduated_from": source_sink,
        "operator_file": str(op_path),
    }
    return op_path, applied


def _approve_zone_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Apply a zone_promotion proposal to zones.schema.yaml.

    Sprint 32 Phase 2c — delegates to
    :func:`grove.zone_rules.save_zone_rule` which already exists from
    Sprint 22 and writes through ruamel.yaml (preserving comments)
    with a synchronous ``zones.reload()`` at the tail. Returns the
    (rendered-rule-summary, applied-rule-dict) pair so the caller can
    print the result.
    """
    from grove.zone_rules import save_zone_rule

    tool = proposal.payload.get("tool")
    pattern = proposal.payload.get("pattern")
    zone = proposal.payload.get("zone", "green")
    reason = proposal.payload.get("reason", "")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(
            f"zone_promotion payload missing 'tool': {proposal.payload!r}"
        )
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(
            f"zone_promotion payload missing 'pattern': {proposal.payload!r}"
        )
    save_zone_rule(
        tool_id=tool, pattern=pattern, zone=zone, reason=reason,
    )
    applied = {
        "match_pattern": pattern,
        "zone": zone,
        "reason": reason,
    }
    return f"tool_zones.{tool}.rules", applied


def _approve_skill_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Apply a skill_promotion proposal (Sprint 53.2).

    Moves the skill out of quarantine via :func:`grove.sovereignty.promote`
    (NOT re-implemented) and writes a green zone rule for the promoted
    path via :func:`grove.zone_rules.save_zone_rule`, then drops the
    skills prompt cache so the promoted skill appears active. Returns the
    (target-label, applied-dict) pair for the caller to print.
    """
    from grove.sovereignty import promote as _promote
    from grove.zone_rules import save_zone_rule

    name = proposal.payload.get("skill_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"skill_promotion payload missing 'skill_name': {proposal.payload!r}"
        )

    _promote(name)
    pattern = rf".*\.grove/skills/{name}/.*"
    save_zone_rule(
        tool_id="terminal",
        pattern=pattern,
        zone="green",
        reason=f"Skill '{name}' promoted from quarantine (Sprint 53.2).",
    )
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[flywheel] skills prompt cache invalidation after promoting "
            "%r failed (non-fatal): %r", name, exc,
        )
    applied = {
        "skill_name": name,
        "promoted_to": f"~/.grove/skills/{name}/",
        "zone_rule": {"match_pattern": pattern, "zone": "green"},
    }
    return f"skill '{name}' (move + green rule)", applied


def _has_successful_quarantine_execution(skill_name: str) -> bool:
    """True if a quarantine_skill_disposition('once') event for ``skill_name``
    exists in any Kaizen ledger (Sprint 53.2 Phase 4 — strict gate).

    Scans every session file under ``~/.grove/.kaizen_ledger/`` because the
    "allow once" execution may have happened in any session before the
    operator runs ``flywheel approve --strict``.
    """
    from hermes_constants import get_hermes_home
    ledger_dir = Path(get_hermes_home()) / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return False
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        event.get("event_type") == "quarantine_skill_disposition"
                        and event.get("skill_name") == skill_name
                        and event.get("disposition") == "once"
                    ):
                        return True
        except OSError:
            continue
    return False


def _enforce_strict_skill_promotion(proposal: RoutingProposal) -> bool:
    """Gate a strict skill promotion. Returns True to proceed (Phase 4b).

    Enforces: (a) a review diff of the promotion act, (b) at least one
    logged successful "allow once" execution of the skill in the Kaizen
    ledger, and (c) explicit y/N confirmation. Any failure returns False
    and the caller aborts — the skill stays quarantined.
    """
    name = proposal.payload.get("skill_name", "?")

    # (a) Review diff.
    print(f"Strict promotion review — skill {name!r}:")
    print(yaml.safe_dump(
        _proposal_to_diff(proposal), sort_keys=False, default_flow_style=False,
    ))

    # (b) Require a logged successful execution.
    if not _has_successful_quarantine_execution(name):
        print(
            f"Refusing: no successful 'allow once' execution of {name!r} is "
            f"logged in the Kaizen ledger. Run the skill once (and allow it) "
            f"before promoting under --strict.",
            file=sys.stderr,
        )
        return False

    # (c) Explicit confirmation.
    try:
        answer = input(
            f"Promote skill {name!r} to the trusted set? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer not in ("y", "yes"):
        print(f"Aborted — skill {name!r} remains quarantined.")
        return False
    return True


def _approve_pattern_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Activate a compiled T0 pattern (Sprint 48 Phase 3).

    The compiled entry already lives (status=suspended) in
    ``pattern_cache.db``; approval flips it to ``active`` so Sprint 49's T0
    path will serve it, and logs a ``pattern_promoted`` telemetry event. The
    system never self-activates — this only runs on operator approval."""
    from datetime import datetime, timezone
    from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE

    pattern_id = proposal.payload.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id.strip():
        raise ValueError(
            f"pattern_promotion payload missing 'pattern_id': {proposal.payload!r}"
        )
    store = PatternCacheStore()
    now = datetime.now(timezone.utc).isoformat()
    if not store.set_status(pattern_id, STATUS_ACTIVE, promoted_at=now):
        raise ValueError(
            f"compiled pattern {pattern_id!r} not found in pattern_cache.db — "
            f"cannot activate (was it compiled?)."
        )
    intent_class = proposal.payload.get("intent_class", "?")
    cacheable_type = proposal.payload.get("cacheable_type", "?")
    samples = proposal.payload.get("sample_queries") or []
    sample = samples[0] if samples else "?"
    # Read what the pattern will actually serve so the operator sees it.
    activated = store.get(pattern_id)
    cached = activated.cached_response if activated else None
    invocation = activated.compiled_invocation if activated else None
    logger.info(
        "[flywheel] pattern_promoted: pattern_id=%s intent_class=%s "
        "cacheable_type=%s — now active at T0",
        pattern_id, intent_class, cacheable_type,
    )
    applied = {
        "pattern_id": pattern_id,
        "sample_query": sample,
        "intent_class": intent_class,
        "cacheable_type": cacheable_type,
        "cached_response": cached,
        "compiled_invocation": invocation,
        "status": STATUS_ACTIVE,
        "tier": "T0",
        "effect": "the next matching query resolves from T0 — no model call",
    }
    if cacheable_type == "static":
        label = (
            f"pattern for “{sample}” ({intent_class}, static). "
            f"Cached response: {cached!r}. "
            f"Next matching query resolves from T0 — no model call."
        )
    else:
        label = (
            f"pattern for “{sample}” ({intent_class}, executable). "
            f"Compiled invocation: {invocation}. "
            f"Next matching query executes the tool model-free."
        )
    return label, applied


def _approve_pattern_demotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Confirm a drift-triggered demotion (Sprint 49 Phase 2).

    The Dispatcher already auto-SUSPENDED the pattern when the operator
    corrected a T0 hit. Approving the proposal confirms the disposition:
    flip suspended → demoted (the pattern falls back to T1 inference and
    stays out of the cache). Rejecting the proposal reverses it (see
    ``cli_reject``), re-activating the pattern."""
    from grove.pattern_cache import PatternCacheStore, STATUS_DEMOTED
    from grove.telemetry import log_pattern_cache_event

    pattern_id = proposal.payload.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id.strip():
        raise ValueError(
            f"pattern_demotion payload missing 'pattern_id': {proposal.payload!r}"
        )
    store = PatternCacheStore()
    if not store.set_status(pattern_id, STATUS_DEMOTED):
        raise ValueError(
            f"compiled pattern {pattern_id!r} not found in pattern_cache.db — "
            f"cannot demote."
        )
    intent_class = proposal.payload.get("intent_class", "?")
    log_pattern_cache_event(
        event_type="pattern_demoted",
        pattern_id=pattern_id,
        intent_class=intent_class,
        correction_turn_id=proposal.payload.get("correction_turn_id"),
    )
    applied = {
        "pattern_id": pattern_id,
        "intent_class": intent_class,
        "status": STATUS_DEMOTED,
        "tier": "T0 → T1 (falls back to inference)",
        "trigger": proposal.payload.get("trigger", "correction_drift"),
    }
    return f"{intent_class} pattern", applied


def _approve_skill_synthesis(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Materialize a drafted skill into quarantine (B1 Fork B — unify).

    This is the SINGLE door by which a ``skill_synthesis`` draft becomes a
    proposed (non-executable) record on disk. It performs the EXACT two writes
    the retired ``Dispatcher._maybe_materialize_synthesized_skills`` did:
    ``write_proposal`` (the SKILL.md into ``.andon/<name>/``) + the best-effort
    ``register_proposed_skill`` record mint. It does NOT chain promotion — the
    skill stays ``proposed`` and a follow-on ``skill_promotion`` (or
    ``hermes andon promote``) takes it active, exactly as before.

    Idempotent: a skill already on disk (active or quarantined) is a no-op
    staging — the proposal is still consumed (removed) by the caller.
    """
    from grove.skills import active_path, proposal_path, write_proposal

    name = proposal.payload.get("skill_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"skill_synthesis payload missing 'skill_name': {proposal.payload!r}"
        )
    name = name.strip()

    if active_path(name).exists() or proposal_path(name).exists():
        applied = {
            "skill_name": name,
            "staged_to": f"~/.grove/skills/.andon/{name}/",
            "note": "already on disk — staging is a no-op",
        }
        return f"skill {name!r} (already staged)", applied

    skill_md = proposal.payload.get("skill_md")
    if not isinstance(skill_md, str) or not skill_md.strip():
        raise ValueError(
            f"skill_synthesis payload missing 'skill_md': {proposal.payload!r}"
        )

    write_proposal(name, skill_md)
    # GRV-009 E6b C2 — mint the state:proposed record alongside the .andon body
    # (proposed is the sole review lock; non-executable behind the 4.1
    # checkpoint). Best-effort: a mint failure leaves the body staged and the
    # quarantine gate still fires; it is flagged LOUD, never silently swallowed.
    record_minted = True
    try:
        from grove.capability_registry import (
            _frontmatter_value,
            register_proposed_skill,
        )
        _cat = _frontmatter_value(skill_md, "category") or ""
        register_proposed_skill(name, _cat, skill_md)
    except Exception:  # noqa: BLE001
        record_minted = False
        logger.warning(
            "[flywheel] proposed-record mint failed for %r (proposal staged to "
            ".andon/, record not minted — reconcile manually)", name,
            exc_info=True,
        )
    applied = {
        "skill_name": name,
        "staged_to": f"~/.grove/skills/.andon/{name}/",
        "record_state": "proposed (non-executable until promoted)",
        "record_minted": record_minted,
        "next": "promote via `hermes andon promote` or a skill_promotion proposal",
    }
    return f"skill {name!r} (.andon/ + proposed record)", applied


def _record_kaizen_disposition(
    proposal: RoutingProposal,
    *,
    disposition: str,
    applied_result: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    ledger_dir: Optional[Path] = None,
) -> None:
    """Write one ``kaizen_disposition`` ledger event for an operator action.

    learning-loop-bridge-v1 (Strike 2) — closes DARK Q9: a proposal no longer
    vanishes on approval/rejection; the operator's disposition is recorded so
    the Flywheel can observe that its proposals are acted on. One write at the
    single registry-dispatch boundary covers every proposal type uniformly —
    no per-handler branching (the anti-cruft #1 contract).

    Session friction (GATE-A A3): ``cli_approve`` / ``cli_reject`` run in the
    CLI process, which has no conversation session, and ``RoutingProposal``
    carries no session_id. The event is written under a dedicated sentinel
    session id ``cli-<utc-timestamp>`` so it lands in its own ledger file
    rather than being misattributed to a conversation session.

    Fail loud: a write failure propagates. By the time this is reached the
    operator's action (apply + dequeue, or dequeue) has completed, so a ledger
    failure is surfaced with context — telemetry that cannot record is a real
    failure to see and fix, not something to swallow.
    """
    from datetime import datetime, timezone
    from grove.kaizen_ledger import KaizenLedger

    now = datetime.now(timezone.utc)
    session_id = "cli-" + now.strftime("%Y%m%dT%H%M%S%fZ")
    ledger = KaizenLedger(session_id, ledger_dir=ledger_dir)
    fields: Dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "proposal_type": proposal.type,
        "disposition": disposition,
        "evidence_count": len(proposal.evidence),
    }
    if applied_result is not None:
        fields["applied_result"] = applied_result
    if reason is not None:
        fields["reason"] = reason
    ledger.record("kaizen_disposition", **fields)


def cli_approve(
    partial_id: str,
    *,
    strict: bool = False,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
) -> int:
    """Apply the proposal; remove from queue.

    B1 — the approved-write gate. Dispatch is a single :data:`PROPOSAL_HANDLERS`
    registry lookup (:func:`_handler_for`): the row's ``apply_callback`` performs
    the write and the row's ``apply_label_prefix`` labels the result. Adding a
    new approved-write class is a new row, not a new branch here. ``--strict``
    runs only the row's optional ``strict_gate`` (today: skill_promotion).

    Per GRV-008 § III the routing path NEVER opens ``routing.config.yaml`` for
    writing — operator-authored configuration is inviolate.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    # B1 — single registry dispatch. Unknown type is a loud, NON-destructive
    # failure at the CLI boundary: stderr message + rc=1, and the proposal
    # stays in the queue for the operator to handle (Sprint 32 contract).
    try:
        handler = _handler_for(proposal.type)
    except ValueError:
        print(
            f"Cannot approve proposal type {proposal.type!r}. Supported: "
            f"routing_adjustment, zone_promotion, skill_promotion, "
            f"pattern_promotion, pattern_demotion, skill_synthesis.",
            file=sys.stderr,
        )
        return 1

    # B2 no-cluster-no-proposal gate — ALWAYS ON (not behind --strict), scoped
    # to rows that declare requires_source_patterns (today: routing_adjustment).
    # A proposal with empty source_patterns is refused loud + non-destructive
    # (rc=1, proposal retained) — matching the unknown-type contract above.
    if handler.requires_source_patterns and not proposal.source_patterns:
        print(
            f"Cannot approve {proposal.type} proposal {proposal.proposal_id}: "
            f"no source_patterns — a {proposal.type} must cite the evidence "
            f"cluster it derives from (B2 no-cluster-no-proposal gate).",
            file=sys.stderr,
        )
        return 1

    # Phase 4 — strict mode gates only the types that declare a strict_gate
    # (skill_promotion: diff + logged execution + confirmation). Normal approve
    # is unchanged; --strict is a no-op for every other type.
    if strict and handler.strict_gate is not None and not handler.strict_gate(proposal):
        return 1

    target, applied = handler.apply_callback(proposal, machine_path=machine_path)
    applied_label = f"{handler.apply_label_prefix}{target}"

    removed = remove(
        proposal.proposal_id,
        path=queue_path or default_queue_path(),
    )
    if not removed:
        logger.warning(
            "[flywheel] proposal %s was applied but had already been "
            "removed from the queue", proposal.proposal_id,
        )

    # learning-loop-bridge-v1 (Strike 2) — record the disposition. SPEC
    # amendment: written AFTER apply+remove (not before remove) so a fail-loud
    # ledger error never strands a proposal in the applied-but-still-queued
    # half-state. The apply has happened; the audit write is the last step.
    _record_kaizen_disposition(
        proposal,
        disposition="applied",
        applied_result=applied,
        ledger_dir=ledger_dir,
    )

    print(f"Approved {proposal.proposal_id}")
    print(applied_label)
    print()
    print("Applied:")
    print(yaml.safe_dump(applied, sort_keys=False, default_flow_style=False))
    return 0


# ── reject ───────────────────────────────────────────────────────────


def _reject_pattern_promotion(proposal: RoutingProposal) -> None:
    # Sprint 48 — a rejected pattern is marked rejected in pattern_cache.db so
    # the scanner NEVER re-proposes the same pattern (3e). The compiled entry
    # stays as a tombstone; the proposer skips any pattern already in the store.
    pattern_id = proposal.payload.get("pattern_id")
    if isinstance(pattern_id, str) and pattern_id:
        try:
            from grove.pattern_cache import PatternCacheStore, STATUS_REJECTED
            PatternCacheStore().set_status(pattern_id, STATUS_REJECTED)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flywheel] could not mark pattern %s rejected: %r",
                pattern_id, exc,
            )


def _reject_pattern_demotion(proposal: RoutingProposal) -> None:
    # Sprint 49 — rejecting a drift-triggered demotion REVERSES it: the
    # pattern was auto-suspended when the operator corrected a T0 hit, and
    # rejecting the demotion proposal means "keep it active" — re-activate so
    # it serves again. The operator overrules the drift signal.
    pattern_id = proposal.payload.get("pattern_id")
    if isinstance(pattern_id, str) and pattern_id:
        try:
            from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE
            from datetime import datetime, timezone
            PatternCacheStore().set_status(
                pattern_id, STATUS_ACTIVE,
                promoted_at=datetime.now(timezone.utc).isoformat(),
            )
            print(
                f"Reversed: pattern {pattern_id.split(':')[-1][:12]} "
                f"re-activated (drift signal overruled).",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flywheel] could not re-activate pattern %s: %r",
                pattern_id, exc,
            )


def cli_reject(
    partial_id: str,
    *,
    reason: Optional[str] = None,
    queue_path: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
) -> int:
    """Remove a proposal from the queue. No config change.

    ``reason`` is recorded at INFO level for the Kaizen Ledger to
    pick up. Sprint 47 does not require structured rejection
    telemetry — that lands when the Kaizen Ledger integration sprint
    ships.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    # B1 — single registry dispatch for the OPTIONAL pre-removal cleanup
    # (pattern_promotion → tombstone; pattern_demotion → reverse). Reject is
    # "remove from the queue" for every type; the cleanup is type-specific and
    # most types declare none. An unrecognised type has no handler and is still
    # removable — the operator must always be able to dismiss a queued item, so
    # this lookup is deliberately tolerant (no raise) where approve/render are
    # strict.
    try:
        handler: Optional[ProposalHandler] = _handler_for(proposal.type)
    except ValueError:
        handler = None
    if handler is not None and handler.reject_callback is not None:
        handler.reject_callback(proposal)

    target = queue_path or default_queue_path()
    removed = remove(proposal.proposal_id, path=target)
    if not removed:
        print(
            f"Proposal {proposal.proposal_id} was not in the queue "
            f"(already removed?).",
            file=sys.stderr,
        )
        return 1
    if reason:
        logger.info(
            "[flywheel] proposal %s rejected — %s",
            proposal.proposal_id, reason,
        )
    # learning-loop-bridge-v1 (Strike 2) — record the rejection disposition
    # (written after dequeue; fail-loud per _record_kaizen_disposition). This
    # supersedes the Sprint 47 deferral of structured rejection telemetry noted
    # in this function's docstring.
    _record_kaizen_disposition(
        proposal,
        disposition="rejected",
        reason=reason,
        ledger_dir=ledger_dir,
    )
    print(f"Rejected {proposal.proposal_id}")
    if reason:
        print(f"Reason:   {reason}")
    return 0


# ── proposal handler registry (B1 Fork A, FULL) ──────────────────────
#
# One row per proposal type. Adding a new approved-write class is a new row in
# this table — never a new branch in four if/elif ladders (anti-cruft #1). The
# four operator surfaces (_proposal_to_diff, _format_summary, cli_approve,
# cli_reject) all dispatch through :func:`_handler_for`; none of them branch on
# ``proposal.type`` directly.


@dataclass(frozen=True)
class ProposalHandler:
    """The per-type behavior for one proposal class.

    * ``summary_renderer`` — the per-type BODY of the one-line list summary
      (shared framing lives in :func:`_format_summary`).
    * ``diff_renderer`` — the YAML diff ``cli_show`` displays.
    * ``apply_callback`` — the approved write. Signature
      ``(proposal, *, machine_path=None) -> (target_label, applied_dict)``;
      ``machine_path`` is consumed only by routing_adjustment.
    * ``apply_label_prefix`` — prepended to the apply target for the
      ``cli_approve`` "Applied/Promoted/Demoted/Staged" line.
    * ``reject_callback`` — OPTIONAL pre-removal cleanup on reject (None for
      types whose reject is a plain queue removal).
    * ``strict_gate`` — OPTIONAL ``--strict`` gate; return False to abort the
      approve (only skill_promotion declares one).
    * ``requires_source_patterns`` — B2 no-cluster-no-proposal gate. When True,
      a proposal of this type with EMPTY ``source_patterns`` is refused at
      approve time, ALWAYS (not behind ``--strict``). Scoped per row: only
      routing_adjustment carries it, so legacy producers of every other type
      keep approving with empty ``source_patterns`` (no retrofit required).
    """

    summary_renderer: Callable[[RoutingProposal], str]
    diff_renderer: Callable[[RoutingProposal], Dict[str, Any]]
    apply_callback: Callable[..., Tuple[Any, Dict[str, Any]]]
    apply_label_prefix: str
    reject_callback: Optional[Callable[[RoutingProposal], None]] = None
    strict_gate: Optional[Callable[[RoutingProposal], bool]] = None
    requires_source_patterns: bool = False


# connector-failure-andon-v1 (C5) — the connector-failure offer is an
# EPHEMERAL, agent-resident Andon (Ruling 1: NEVER written to the proposal
# queue / proposals.jsonl — a dead connector must not pollute the Flywheel
# evaluation ledger). It is rendered by its OWN standalone function below — it
# is deliberately NOT a row in ``PROPOSAL_HANDLERS`` (that registry is the
# closed set of queue-approvable types; adding a non-queue type there would
# leak it into cli_approve/cli_show/review and break the registry parity
# contract). Storage + dispositions live on the agent.
PROPOSAL_TYPE_CONNECTOR_FAILURE = "connector_failure"


@dataclass(frozen=True)
class ConnectorFailureProposal:
    """Ephemeral connector-failure offer. Content-addressed ``proposal_id`` =
    stable hash(name + signature) so a same-signature re-trip dedups against
    the agent shown-set and a changed-signature re-trip is a new offer."""

    proposal_id: str
    server_name: str
    signature: str          # "reauth" | "unreachable"
    reauth_command: str     # DISPLAYED to the operator, NEVER executed
    type: str = PROPOSAL_TYPE_CONNECTOR_FAILURE


def render_connector_failure_offer(p: "ConnectorFailureProposal") -> str:
    """Render the connector-failure offer body (answer-then-surface).

    Standalone (not routed through ``compose_offering``/``PROPOSAL_HANDLERS``)
    because connector_failure is ephemeral, never queue-approvable. Re-auth
    boundary: the re-auth command is DISPLAYED, never executed; this renderer
    touches no token/credential material.
    """
    if p.signature == "reauth":
        return (
            f"⚠️ Shop-floor note — the **{p.server_name}** connector is "
            f"unavailable this session (its sign-in expired, so its tools are "
            f"absent — I'm not silently working around them). Re-authenticate "
            f"with:\n\n    {p.reauth_command}\n\n"
            f"then reply **Retry {p.server_name}** to reconnect, or **Dismiss** "
            f"to proceed without it. Only select Retry after you have "
            f"authenticated or restarted the service."
        )
    return (
        f"⚠️ Shop-floor note — the **{p.server_name}** connector is "
        f"unreachable this session (it failed to connect — not an auth issue — "
        f"so its tools are absent). Reply **Retry {p.server_name}** once the "
        f"service is back, or **Dismiss** to proceed without it. If it persists "
        f"it's worth a bug report."
    )


PROPOSAL_HANDLERS: Dict[str, ProposalHandler] = {
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT: ProposalHandler(
        summary_renderer=_summary_routing_adjustment,
        diff_renderer=_routing_adjustment_to_diff,
        apply_callback=_approve_routing_adjustment,
        apply_label_prefix="Applied to: ",
        # B2 — a routing_adjustment MUST cite the evidence cluster it derives
        # from (TierRatchet populates source_patterns). No-cluster-no-proposal.
        requires_source_patterns=True,
    ),
    PROPOSAL_TYPE_CONSOLIDATION: ProposalHandler(
        summary_renderer=_summary_consolidation,
        diff_renderer=_consolidation_to_diff,
        apply_callback=_approve_consolidation,
        apply_label_prefix="Graduated to operator policy: ",
    ),
    PROPOSAL_TYPE_DOCK_MUTATION: ProposalHandler(
        summary_renderer=_summary_dock_mutation,
        diff_renderer=_dock_mutation_to_diff,
        apply_callback=_approve_dock_mutation,
        apply_label_prefix="Added Dock goal: ",
    ),
    PROPOSAL_TYPE_ZONE_PROMOTION: ProposalHandler(
        summary_renderer=_summary_zone_promotion,
        diff_renderer=_diff_zone_promotion,
        apply_callback=_approve_zone_promotion,
        apply_label_prefix="Applied to: ",
    ),
    PROPOSAL_TYPE_SKILL_PROMOTION: ProposalHandler(
        summary_renderer=_summary_skill_promotion,
        diff_renderer=_diff_skill_promotion,
        apply_callback=_approve_skill_promotion,
        apply_label_prefix="Promoted: ",
        strict_gate=_enforce_strict_skill_promotion,
    ),
    PROPOSAL_TYPE_PATTERN_PROMOTION: ProposalHandler(
        summary_renderer=_summary_pattern_promotion,
        diff_renderer=_diff_pattern_promotion,
        apply_callback=_approve_pattern_promotion,
        apply_label_prefix="Promoted to T0: ",
        reject_callback=_reject_pattern_promotion,
    ),
    PROPOSAL_TYPE_PATTERN_DEMOTION: ProposalHandler(
        summary_renderer=_summary_pattern_demotion,
        diff_renderer=_diff_pattern_demotion,
        apply_callback=_approve_pattern_demotion,
        apply_label_prefix="Demoted from T0: ",
        reject_callback=_reject_pattern_demotion,
    ),
    PROPOSAL_TYPE_SKILL_SYNTHESIS: ProposalHandler(
        summary_renderer=_summary_skill_synthesis,
        diff_renderer=_diff_skill_synthesis,
        apply_callback=_approve_skill_synthesis,
        apply_label_prefix="Staged: ",
    ),
}


# kaizen-proposal-surface-unification-v1 — seed the render registry from the
# apply registry: every routing/zone/skill/pattern type reuses its existing
# summary_renderer (which reads the RoutingProposal directly). memory_context
# is registered lazily via _ensure_memory_renderer (cross-package). Render is
# now decoupled from apply — the two registries are separate by design.
for _type_name, _handler in PROPOSAL_HANDLERS.items():
    register_renderer(_type_name, _handler.summary_renderer)


def _handler_for(proposal_type: str) -> ProposalHandler:
    """Resolve a proposal type to its handler row.

    Resolves the legacy ``routing_update`` spelling to the routing_adjustment
    row in this ONE place (the alias no longer leaks into every surface).
    Raises ``ValueError`` on an unrecognised type — there is no silent
    fallback handler (fail loud, GRV operating principle #1).
    """
    canonical = (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT
        if proposal_type == _LEGACY_ROUTING_TYPE
        else proposal_type
    )
    try:
        return PROPOSAL_HANDLERS[canonical]
    except KeyError:
        raise ValueError(
            f"unsupported proposal type {proposal_type!r}; recognised: "
            f"{', '.join(sorted(PROPOSAL_HANDLERS))}"
        )
