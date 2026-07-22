"""GRV-008 § II proposal queue — ~/.grove/proposals.jsonl.

Sprint 47. Defines the ``RoutingProposal`` dataclass and the queue I/O
the Flywheel pipeline writes to and the operator review CLI reads
from. Append-only JSON Lines; one record per line; idempotent on
duplicate ``proposal_id`` (content-addressable hashes are the GRV-008
§ II uniqueness contract).

Hashes
------
``proposal_id`` is a SHA-256 over ``type | sorted-payload-JSON |
sorted-evidence-CSV``. Same logical proposal — same id, regardless of
when the detector ran or how many sessions contributed evidence.

``eval_hash`` is a SHA-256 over the EvalReport's deterministic
projection: per-prompt ``(prompt_id, observed_intent,
observed_complexity, observed_tier, sorted-tools, passed)``.
Confidence is deliberately excluded — small-band T-telemetry variance
must not invalidate the gate signature on otherwise structurally
identical outcomes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


__all__ = [
    "RoutingProposal",
    "PROPOSAL_TYPE_ROUTING_ADJUSTMENT",
    "PROPOSAL_TYPE_ZONE_PROMOTION",
    "PROPOSAL_TYPE_SKILL_PROMOTION",
    "PROPOSAL_TYPE_PATTERN_PROMOTION",
    "PROPOSAL_TYPE_PATTERN_DEMOTION",
    "PROPOSAL_TYPE_SKILL_SYNTHESIS",
    "PROPOSAL_TYPE_MEMORY_CONTEXT",
    "PROPOSAL_TYPE_CONSOLIDATION",
    "PROPOSAL_TYPE_DOCK_MUTATION",
    "PROPOSAL_TYPE_MODEL_BINDING",
    "PROPOSAL_TYPE_PORTAL_ACTION_FAILURE",
    "PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING",
    "PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING",
    "PROPOSAL_TYPE_ADMISSION_FRICTION",
    "PROPOSAL_TYPE_GOAL_ATTACHMENT",
    "PROPOSAL_TYPE_EXPLORATION_NUDGE",
    "compute_proposal_id",
    "compute_eval_hash",
    "default_queue_path",
    "append",
    "read_all",
    "read",
    "remove",
    "file_agentless_proposal",
]


# ── Proposal type discriminators (Sprint 32 2a) ──────────────────────
#
# Each value lives in the ``type`` field on the queue's JSON Lines
# records. The CLI approve handler routes on this string; new proposal
# classes register a new value and a matching translator. Sprint 47
# shipped the queue with the literal ``"routing_update"``; Sprint 32
# renames that to ``"routing_adjustment"`` to align with GRV-008 § II's
# naming. The legacy string is honored on read for back-compat with any
# live queue entries an operator might have already accumulated.
PROPOSAL_TYPE_ROUTING_ADJUSTMENT = "routing_adjustment"
PROPOSAL_TYPE_ZONE_PROMOTION = "zone_promotion"
# Sprint 53.2 — a quarantined (.andon) skill ran successfully under an
# "allow once" disposition and the operator (or a headless surface) wants
# it promoted to the trusted set. Payload shape:
#   {"skill_name": str, "skill_path": str, "execution_turn_id": str,
#    "suggested_action": "promote"}
# CLI approve routes this to grove.sovereignty.promote() + a green zone
# rule for the promoted path (see grove/flywheel_cli.py).
PROPOSAL_TYPE_SKILL_PROMOTION = "skill_promotion"
# Sprint 48 — a stable T1 pattern the compiler proposes retiring to the
# deterministic T0 cache. Payload shape:
#   {"pattern_id", "t0_key", "intent_class", "cacheable_type",
#    "evidence_hash", "promotion_evidence": {...}, "sample_queries": [...]}
# The compiled entry already lives (status=suspended) in pattern_cache.db;
# approve flips it to active (see grove/flywheel_cli.py).
PROPOSAL_TYPE_PATTERN_PROMOTION = "pattern_promotion"
# Sprint 49 — an active T0 pattern drifted: it was served from cache and the
# operator corrected the very next turn. The Dispatcher auto-suspends it on
# the spot (suspended patterns stop serving) and queues this proposal so the
# operator confirms the demotion or reverses it (re-activates). Payload shape:
#   {"pattern_id", "intent_class", "cacheable_type",
#    "suggested_action": "demote", "trigger": "correction_drift",
#    "correction_turn_id": str}
# CLI approve sets the pattern to demoted; reject re-activates it (see
# grove/flywheel_cli.py).
PROPOSAL_TYPE_PATTERN_DEMOTION = "pattern_demotion"
# Sprint 63 — the Kaizen pattern synthesizer observed a recurring multi-tool
# sequence across sessions and drafted a parametrized SKILL.md for it. Unlike
# skill_promotion (an already-quarantined skill the operator ran), this stages
# an off-disk draft for the operator to accept. Payload shape:
#   {"skill_name": str, "skill_md": str (full SKILL.md text),
#    "when_to_use": str, "goal": str (concierge one-liner for the quiet append),
#    "tool_sequence": [str, ...]}
# Acceptance (B1 — unified): the operator approves through the flywheel gate
# (``flywheel approve <id>`` → grove.flywheel_cli._approve_skill_synthesis),
# which materializes skill_md into .andon/ and mints the proposed record. The
# skill stays proposed (non-executable); a follow-on skill_promotion (or
# ``hermes andon promote``) takes it active. This is the SOLE door a synthesis
# draft becomes a proposed record — the old invoke_skill-triggered chat
# materialization path was retired in B1.
PROPOSAL_TYPE_SKILL_SYNTHESIS = "skill_synthesis"
# memory-substrate-v1 (epic memory-lifecycle-engine-v1) — the Context
# Persistence Detector's staged memory proposals. UNLIKE every type above,
# memory_context proposals do NOT flow through this RoutingProposal queue
# (~/.grove/proposals.jsonl) or the flywheel CLI approve path. They live in
# their own ~/.grove/memory_proposals.jsonl with their own record shape and
# are applied by grove.memory.digest.MemoryProposalHandler. This constant is
# the canonical type string so kaizen_disposition recording stays uniform
# (one envelope, one ledger event) across every proposal class. It is
# deliberately NOT added to the flywheel _handler_for registry — there is no
# RoutingProposal apply path for it, by design.
PROPOSAL_TYPE_MEMORY_CONTEXT = "memory_context"
# consolidation-ratchet-v1 — Stage 2 routing policy graduation. UNLIKE
# memory_context, these DO flow through this RoutingProposal queue and the
# flywheel CLI approve path (a routing change is a routing proposal); the apply
# handler performs the two-file atomic write to routing.config.yaml.
PROPOSAL_TYPE_CONSOLIDATION = "consolidation_proposal"
# dock-as-mutation-target-v1 — the DockMutationDetector's proposal to add a
# system-authored staging goal to the Dock. Flows through THIS RoutingProposal
# queue and the flywheel CLI approve path; the apply handler appends the goal to
# dock.autonomaton.yaml (the machine file — a GREEN granted workspace, never the
# RED operator dock.yaml). Payload shape:
#   {"action": "create_goal",
#    "goal": {"id", "name", "keywords", "vector", "status", "definition_of_done",
#             "source_record_ids"}}
PROPOSAL_TYPE_DOCK_MUTATION = "dock_mutation"
# binding-governance-surfaces-v1 — a proposed model_binding change on a
# kind=skill capability record. Flows through THIS RoutingProposal queue and
# the flywheel CLI approve path; the apply handler is the ONE sanctioned
# writer (grove.capability_registry.set_model_binding), called with
# surface="proposal_apply" + this proposal's id so the writer's own
# capability_binding_mutation ledger event joins the kaizen_disposition.
# Payload shape (identity-bearing — a proposal to move A→B is distinct from
# None→B by design):
#   {"skill": str (slug-tail-resolvable name),
#    "proposed_binding": dict|None (None = clear the pin),
#    "previous_binding": dict|None (binding at stage time),
#    "evidence_summary": str (optional, renderer fallback)}
PROPOSAL_TYPE_MODEL_BINDING = "model_binding"
# portal-action-error-surfacing-v1 (Phase 1) — a portal action handler's failure
# disposition, filed AGENTLESSLY (no LLM turn) straight from the handler's error
# branch via :func:`file_agentless_proposal`, so the Kaizen flywheel can
# recommend the structural fix for a recurring failure. Payload shape (the STABLE
# dedup key — hashed by :func:`compute_proposal_id`):
#   {"failure_class": str, "action": str}
# The ephemeral per-instance data (timestamp, slug, folder link, exact error)
# rides in ``semantic_justification`` — an EXCLUDED field — so every recurrence of
# the same failure_class+action collapses to ONE queue entry (the flood-guard).
# Render-only for Phase 1: registered in ``flywheel_cli.RENDER_REGISTRY`` but NOT
# in ``PROPOSAL_HANDLERS`` (no apply path yet — approve/apply wiring is a later
# phase), matching how ``memory_context`` surfaces without a RoutingProposal
# apply handler.
PROPOSAL_TYPE_PORTAL_ACTION_FAILURE = "portal_action_failure"
# fleet-pipeline-v1 P2 — a fleet worker staged a Yellow Option-2 package into
# pending_review; the operator must promote (publish) or reject it. Emitted
# agentlessly from the ticker reap SUCCESS branch (manager.py). Payload:
#   {"slug": str, "row_id": str, "skill_id": str, "fit_score": int|None}
# RENDER-ONLY w.r.t. the generic SYNC approve machinery (deliberately NOT a
# PROPOSAL_HANDLERS row → _type_offers_approve is False → no generic Approve
# button). The promote tap is the bespoke async route (P3); the affordances are
# the verb set below, not approve/reject/dismiss.
PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING = "forge_artifact_pending"
# fleet-review-unification-v1 C1b-2 — the GENERIC file-producer analog of the
# forge type. Emitted for any action_surface_publish worker whose canonical_sink
# is NOT "forge" (drafter, cultivator, …). Payload mirrors forge's shape but is
# keyed on the stable unit_id (no Notion row_id):
#   {"slug": str, "unit_id": str, "skill_id": str, "canonical_sink": str}
# Same RENDER-ONLY posture as forge (no generic Approve; the promote tap is the
# bespoke async route, canonical_sink-dispatched). The disposition INTERNALS
# (promote / reject / suggest_revision) are producer-parametrized in C1a/C1b-1;
# forge keeps forge_artifact_pending, byte-identical.
PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING = "fleet_artifact_pending"
# kaizen-fault-triage-v1 — a recurring Andon-class fault pattern detected in
# the Kaizen ledger (grove.eval.fault_triage.FaultTriageDetector). A fault
# REPORT, not a fix: identity is the fault signature only —
#   {"source": "fleet_worker"|"dispatcher_halt"|"red_resolution",
#    <key fields per source>, "error_signature": str}
# — so any future remediation enrichment (proposed_config_diff,
# remediation_plan, …) rides identity-EXCLUDED fields and never forks the
# dedup. RENDER-ONLY (no PROPOSAL_HANDLERS row → structurally unapprovable);
# affordances are the verb set below: acknowledge ("seen, keep watching,
# tell me if it changes" — the detector re-stages only on material growth)
# and dismiss (strictly negative feedback — suppressed for the window).
PROPOSAL_TYPE_FAULT_TRIAGE = "fault_triage"
# operator-mutable-admission-v1 P4 — recurring C-SEAM5 admission friction observed
# in the capability_refusals feed (grove.eval.admission_friction). ADDITIVE-only
# and APPROVABLE: the approval writes the ~/.grove admission overlay via the
# sanctioned set_admission_overlay writer. payload identity is
#   {"record": <governing_record>, "verb": "add_intents"|"force_always",
#    "add_intents": [<intent>]}  (force_always omits add_intents)
# — the evidence_block (recurrence arms) rides identity-EXCLUDED fields.
PROPOSAL_TYPE_ADMISSION_FRICTION = "admission_friction"
# goal-spine-v1 P3 — a BATCHED goal-attachment offer: N adjudicated artifacts
# grouped under ONE Dock goal, one row, one disposition (J5 ruling — whole-row
# Approve/Reject; grove.dock.attachment_store.detach_attachment is the
# per-entry undo after approval, and the card copy must say so). Flows through
# THIS RoutingProposal queue and the flywheel CLI approve path; the apply
# handler mints every entry through the ONE sanctioned writer
# (attachment_store.mint_attachment) with this proposal's id; the reject
# handler files per-(artifact_id, goal_id) artifact_goal_suppressed events
# ("not this goal," never "not any goal" — J3 ruling).
# Identity (J2 ruling — a SEPARATE identity dict, the DockMutationDetector
# precedent; compute_proposal_id sorts dict keys but NOT list order, so
# artifact_ids is pre-sorted):
#   {"goal_id": str, "artifact_ids": [sorted str]}
# Payload (rides the row; entries sorted by artifact_id):
#   {"goal_id": str, "goal_name": str,
#    "entries": [{"artifact_id", "excerpt", "rationale", "verdict"}]}
PROPOSAL_TYPE_GOAL_ATTACHMENT = "goal_attachment"
# kaizen-exploration-proposals-v1 — the Kaizen nudge to try a cataloged-but-
# UNTRIED model interactively (attended, Mylo-path). NOT a binding and NOT a
# trial directive (the operator reframe dissolved the trial-execution universe):
# it proposes FLIPPING the interactive tier selection so the operator's own
# attended turns start generating honest evidence arms. Approve delegates to the
# SAME sanctioned writer the catalog one-tap dropdown uses
# (RoutingConfigWriter.swap_tier_model) — a real, reversible config write, so the
# "apply_callback = approved write" contract holds with ZERO novel execution.
#
# Payload is IDENTITY-BEARING and MINIMAL — {slug, tier} ONLY — so a model's
# proposal id is stable across catalog repricings. The card-only display fields
# (display_name / provider / input_cost_per_mtok / output_cost_per_mtok) ride the
# id-EXCLUDED ``detail`` envelope (compute_proposal_id hashes type|payload|
# evidence only, :460-480), NEVER the payload, so a repriced model does not fork
# into a duplicate nudge.
#   payload: {"slug": str, "tier": str}
#   detail:  {"display_name": str, "provider": str,
#             "input_cost_per_mtok": float, "output_cost_per_mtok": float}
PROPOSAL_TYPE_EXPLORATION_NUDGE = "exploration_nudge"
# detector-sweep-resilience-v1 P3 (R-3b/R-3c) — a sweep producer keeps
# failing: the recurrence detector found producer_failure events on >=
# distinct_days DISTINCT UTC days inside the window, and offers the operator
# a PAUSE (approve delegates to grove.eval.producer_pauses.set_producer_pause
# — the sanctioned writer of ~/.grove/flywheel/producer_pauses.yaml — with
# this card's proposal_id as provenance; reject suppresses the card for the
# window via the standard kaizen_disposition read, after which the SAME
# content-addressed id re-stages if the producer is still failing — the
# no-tombstone queue + windowed disposition read, P3 gate ruling a).
#
# Payload is IDENTITY-BEARING and MINIMAL — {producer} ONLY — so the card is
# stable across failure counts and re-detections. The evidence fields
# (failure_count / distinct_dates / last_error / window_days) ride the
# id-EXCLUDED ``detail`` envelope, NEVER the payload, so a growing failure
# tally does not fork duplicate cards.
#   payload: {"producer": str}
#   detail:  {"failure_count": int, "distinct_dates": [str],
#             "last_error": str | null, "window_days": int}
PROPOSAL_TYPE_PRODUCER_FAILURE_RECURRENCE = "producer_failure_recurrence"
# fleet-receipt-custody-v1 P3b — the breaker's AUTO-pause notification. The fleet
# manager pauses a producer the moment a receipt whose check maps to
# pause_producer lands (N=1); this card tells the operator it happened and
# carries the sole UNPAUSE action (approve = unpause). Deduped by producer, so
# one card per paused producer regardless of receipt count.
#   payload: {"producer": str}
#   detail:  {"check": str, "run_id": str}
PROPOSAL_TYPE_PRODUCER_AUTO_PAUSED = "producer_auto_paused"
# fleet-receipt-custody-v1 P3b — a receipt carried a failure class absent from
# config/fleet_failure_policy.yaml. It defaulted to retry; this card asks the
# operator to classify it. Deduped by class, so a class seen N times raises ONE
# card. Approve DISMISSES the alert — the mapping is applied by editing the YAML.
#   payload: {"check": str}
PROPOSAL_TYPE_UNMAPPED_FAILURE_CLASS = "unmapped_failure_class"
# Verb affordances per proposal type. The portal iterates this to render action
# buttons; extend a tuple to add a verb (e.g. "suggest_revision") with NO change
# to the iterator — the shape is deliberately open. The generic fleet type
# MIRRORS forge's verb shape (C1b-2).
PROPOSAL_VERBS: Dict[str, Tuple[str, ...]] = {
    PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING: ("promote", "reject"),
    PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING: ("promote", "reject"),
    # kaizen-fault-triage-v1 — directions, not receipts: acknowledge keeps
    # watching (re-raise on material growth), dismiss suppresses the class
    # for the window. No approve — there is no apply path at T1.
    PROPOSAL_TYPE_FAULT_TRIAGE: ("acknowledge", "dismiss"),
}
_LEGACY_ROUTING_TYPE = "routing_update"  # Sprint 47 spelling


# ── Approve-affordance resolver (portal-action-error-surfacing-v1 P3.6) ──


def _type_offers_approve(proposal_type: str) -> bool:
    """Single source of truth: does an ``approve`` affordance have an HONORED
    apply path for this proposal type?

    Both the in-chat push gate (:meth:`RoutingProposal.offers_approve`) and the
    portal pending-proposals card gate (``_proposal_actions_html``) resolve
    through here — one rule, no drift.

    * A ``PROPOSAL_HANDLERS`` row (``_handler_for`` resolves, legacy alias
      honored) → True.
    * ``memory_context`` → True: memory applies through ``MemoryProposalHandler``,
      a SEPARATE registry that is deliberately NOT in ``PROPOSAL_HANDLERS``. This
      branch is the TEMPORARY bridge for the forked memory registry; the queued
      memory-into-kaizen-protocol sprint folds memory into ``PROPOSAL_HANDLERS``
      and collapses this to pure ``_handler_for``.
    * Everything else — render-only types like ``portal_action_failure`` — → False
      (approve would dead-end at ``_handler_for``, so no affordance is offered).

    Deferred import: ``flywheel_cli`` imports this module at load, so resolving
    ``_handler_for`` at module scope would cycle; by call time both are loaded."""
    from grove.flywheel_cli import _handler_for
    try:
        _handler_for(proposal_type)
        return True
    except ValueError:
        pass
    return proposal_type == PROPOSAL_TYPE_MEMORY_CONTEXT


# ── Public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingProposal:
    """One Flywheel-authored routing change waiting for operator review.

    Schema invariants per GRV-008 § II:

    * ``proposal_id``: content-addressable SHA-256 (see
      :func:`compute_proposal_id`).
    * ``type``: ``"routing_update"`` for Sprint 47; future proposal
      classes register additional values here.
    * ``payload``: structured diff. For ``routing_update`` the shape
      is ``{"rule": "downward"|"upward", "add_intents": [str]}``.
      Removal-from-list is intentionally out of scope for v1
      (operator GATE-A revision).
    * ``evidence``: the ``turn_id`` values that triggered the
      detector. Carried as a tuple so the dataclass is hashable.
    * ``eval_hash``: SHA-256 over the EvalReport projection that
      gated this proposal (see :func:`compute_eval_hash`).
    * ``created_at``: ISO 8601 UTC.
    * ``source_patterns`` (B1 Fork D): the pattern-cluster ids this
      proposal derives from — the first-class slot for GRV Invariant 3
      ("no pattern cluster, no proposal"). Distinct from ``evidence``
      (turn ids): clusters are the *what-recurred*, turns are the
      *where-observed*. Defaults to ``()`` so every existing producer
      stays valid unchanged, and — critically — it is EXCLUDED from
      :func:`compute_proposal_id` so adding cluster lineage never
      changes a proposal's identity. B2 populates it; the empty-cluster
      gate stays OFF until B2 ships.
    """

    proposal_id: str
    type: str
    payload: Dict[str, Any]
    evidence: Tuple[str, ...]
    eval_hash: str
    created_at: str
    source_patterns: Tuple[str, ...] = ()
    # machine-sink-generalization-v1 — optional memory-enriched rationale the
    # Kaizen offering renders so promotions read with domain context, not just
    # mechanics. Top-level and EXCLUDED from :func:`compute_proposal_id` (like
    # ``source_patterns``), so enriching a proposal never changes its identity;
    # old ``proposals.jsonl`` records written before this field deserialize to
    # the default "" (no coercion needed — it is a plain string).
    semantic_justification: str = ""
    # fleet-pipeline-v1 P1 — operator-tap LEASE. id-EXCLUDED: compute_proposal_id
    # hashes only type|payload|evidence (:319-323), so leasing never changes a
    # proposal's identity, and records written before this field deserialize to
    # None. A non-None lease marks the proposal as being processed by an in-flight
    # operator tap; ``set_lease`` is a CAS (409 on a held lease) and the
    # startup-only ``sweep_stuck_leases`` reverts any lease still held at boot.
    # ``detail`` (below) rides the SAME exclusion — enriching a proposal with a
    # structured render envelope never forks its identity.
    lease: Optional[Dict[str, Any]] = None
    # proposal-proposer-attribution-v1 — the PRODUCER that emitted this proposal
    # (fleet ``skill_id`` / detector name / ``"governance"`` / ``"portal_failure"`` /
    # ...). id-EXCLUDED: NOT a parameter to :func:`compute_proposal_id` (mirrors
    # ``source_patterns`` exactly), so stamping attribution never forks a proposal's
    # identity. Legacy on-disk records (no key) deserialize to ``"unattributed"`` via
    # this default — no migration; on-disk records untouched.
    proposer: str = "unattributed"
    # proposal-card-legibility-v1 Phase 2 — structured, RENDER-READY evidence
    # envelope (e.g. fault_triage normalized samples), typed per-type by the
    # codecs in grove.kaizen.rendering. id-EXCLUDED like ``lease``/``proposer``
    # (compute_proposal_id inputs are unchanged — see the lease comment above);
    # serialized present-key-only (omit when None, never ``"detail": null``);
    # absent and null both deserialize to None. Appended LAST so no positional
    # construction shifts. semantic_justification remains the verbatim
    # operator-facing fallback source — detail supplements, never replaces it.
    detail: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(data["evidence"])
        data["source_patterns"] = list(data["source_patterns"])
        # Present-key only: an UNHELD proposal serializes exactly as before (no
        # ``lease`` key), so existing records are byte-identical and no golden
        # snapshot churns. A held proposal carries its lease dict.
        if data.get("lease") is None:
            data.pop("lease", None)
        # Present-key only (mirrors ``lease``): an UNATTRIBUTED proposal serializes
        # WITHOUT a ``proposer`` key — byte-identical to legacy records, no golden
        # churn. Only an attributed proposal carries the key.
        if data.get("proposer") == "unattributed":
            data.pop("proposer", None)
        # Present-key only (mirrors ``lease``): a detail-less proposal serializes
        # WITHOUT a ``detail`` key — never ``"detail": null`` (proposal-card-
        # legibility-v1 Phase 2).
        if data.get("detail") is None:
            data.pop("detail", None)
        return data

    # ── KaizenRenderable (kaizen-proposal-surface-unification-v1) ─────────
    # Frozen dataclasses allow properties/methods; these let a RoutingProposal
    # be surfaced through the unified Kaizen renderer + push without a wrapper.

    @property
    def short_id(self) -> str:
        """The 12-char id the operator/model sees (dedup + reference key)."""
        return self.proposal_id.split(":")[-1][:12]

    @property
    def sort_key(self) -> float:
        """Within-priority tiebreak — created_at as epoch seconds (oldest
        first), uniform float so it never collides with another type's key."""
        try:
            dt = datetime.fromisoformat(self.created_at)
        except (ValueError, TypeError):
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @property
    def requires_portal_review(self) -> bool:
        """Consolidation (policy-graduation) proposals review in the portal, not
        in chat (portal-reader-contract-fix-v1). Routing/zone/skill/pattern and
        dock-mutation proposals keep their in-chat approve/dismiss affordance."""
        return self.type == PROPOSAL_TYPE_CONSOLIDATION

    @property
    def offers_approve(self) -> bool:
        """Whether the in-chat push may offer an ``approve`` affordance.

        Delegates to :func:`_type_offers_approve` — the ONE source of truth
        shared with the portal pending-proposals card gate (portal-action-error-
        surfacing-v1 P3.6), so the two surfaces can never drift. Structural, not
        an enumerated denylist: approve is offered iff this type has an honored
        apply path (a ``PROPOSAL_HANDLERS`` row). Behavior-preserving for every
        routing/zone/skill/pattern/dock type (all resolve True) and
        ``portal_action_failure`` (False)."""
        return _type_offers_approve(self.type)

    def is_push_eligible(self, session_start: Optional["datetime"]) -> bool:
        """Routing proposals push only when created THIS session (the
        anti-nag current-session rule). No anchor -> not eligible."""
        if session_start is None:
            return False
        try:
            created = datetime.fromisoformat(self.created_at)
        except (ValueError, TypeError):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created >= session_start

    def push_body(self, core: str) -> str:
        """Routing-family push clause — the generic 'I noticed I could …' that
        fits every routing/zone/skill/pattern verb-phrase body (preserved).

        consolidation-ratchet-v1: a policy graduation speaks in its own frame —
        it is not an opportunistic 'I could', it is a governance recommendation
        the operator ratifies into permanent policy."""
        if self.type == PROPOSAL_TYPE_CONSOLIDATION:
            return f"I'm recommending a routing policy change — {core}"
        # dock-as-mutation-target-v1 — a Dock-goal proposal is an observation
        # the operator ratifies into a tracked goal, not an opportunistic 'I
        # could'. Its own frame.
        if self.type == PROPOSAL_TYPE_DOCK_MUTATION:
            return f"I've observed a pattern worth tracking — {core}"
        # binding-governance-surfaces-v1 — a model binding is a governance
        # recommendation the operator ratifies (like consolidation), not an
        # opportunistic 'I could'. Its own frame.
        if self.type == PROPOSAL_TYPE_MODEL_BINDING:
            return f"I'm recommending a model binding change — {core}"
        # portal-action-error-surfacing-v1 — an incident report, not an
        # opportunistic 'I could'. The summary core is already a full clause
        # (``portal action 'x' keeps failing …``), so it stands on its own —
        # wrapping it in 'I noticed I could' would read as broken grammar.
        if self.type == PROPOSAL_TYPE_PORTAL_ACTION_FAILURE:
            return core
        # kaizen-fault-triage-v1 — the core IS the interpreted judgment line
        # ("<subject> is hitting the same <fault> repeatedly — one defect,
        # active, worsening."); it stands on its own, Kaizen speaking.
        if self.type == PROPOSAL_TYPE_FAULT_TRIAGE:
            return core
        # goal-spine-v1 P3 (J6 ruling) — one batched row pushes as ONE offer,
        # in its own frame: an adjudicated observation the operator ratifies,
        # not an opportunistic 'I could'.
        if self.type == PROPOSAL_TYPE_GOAL_ATTACHMENT:
            return f"I've matched work to your goals — {core}"
        return f"I noticed I could {core}"


# ── Hashing ──────────────────────────────────────────────────────────


def compute_proposal_id(
    *,
    type: str,
    payload: Dict[str, Any],
    evidence: Tuple[str, ...],
) -> str:
    """Compute the content-addressable proposal_id.

    Deterministic across runs: sorted JSON for the payload, sorted CSV
    for evidence. The same logical proposal — same id — even when the
    detector reruns or evidence accumulates from new sessions.

    B1 Fork D — ``source_patterns`` is intentionally NOT a parameter here
    and never enters the seed: cluster lineage can accrete on a proposal
    without changing its identity, so an existing proposal's id is stable
    whether or not B2 has attached its clusters.
    """
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    evidence_csv = ",".join(sorted(evidence))
    seed = f"{type}|{payload_json}|{evidence_csv}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compute_eval_hash(report: Any) -> str:
    """Compute the EvalReport projection hash for the gate signature.

    Confidence is excluded so small-band T-telemetry variance does not
    invalidate the signature on otherwise identical structural
    outcomes. Tools are sorted for set-comparison stability.
    """
    parts: List[Dict[str, Any]] = []
    for r in getattr(report, "results", ()):
        tools = getattr(r, "observed_tools", None)
        tools_sorted = sorted(tools) if tools is not None else None
        parts.append({
            "prompt_id": r.prompt_id,
            "intent": r.observed_intent,
            "complexity": r.observed_complexity,
            "tier": r.observed_tier,
            "tools": tools_sorted,
            "passed": bool(r.passed),
        })
    seed = json.dumps(parts, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue I/O ────────────────────────────────────────────────────────


_lock = threading.Lock()


def default_queue_path() -> Path:
    """Resolve ``~/.grove/proposals.jsonl`` via the standard hermes_home."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "proposals.jsonl"


# silent-degradation-sweep-v1 — one filing per (check, file-signature), where
# the signature is a digest of the file's unparseable lines: repeated reads of
# the same damage file ONCE per gateway process; a genuinely new corruption
# files fresh. In-process only, no new persistent state (gateway is a single
# systemd process; the memo is per-gateway by construction).
_DROP_FILING_MEMO: set = set()


def _drop_check(raw_line: str) -> str:
    """``check`` value for a schema-mismatch drop — best-effort ``type``
    extraction from the raw row; anything short of a typed dict is
    ``schema_mismatch:unknown``."""
    try:
        data = json.loads(raw_line)
    except json.JSONDecodeError:
        return "schema_mismatch:unknown"
    if isinstance(data, dict) and data.get("type"):
        return f"schema_mismatch:{data.get('type')}"
    return "schema_mismatch:unknown"


def _file_drop_andon(
    path: Path, dropped: List[Tuple[int, str, str, str]],
) -> None:
    """File the read's drops as FACTS — one throttled ``andon_halt`` per
    ``check`` class (source=proposal_queue) into the Kaizen ledger, NEVER into
    this queue (no re-entry into the store that failed). Best-effort with an
    error-log floor (the grove/fleet/observability.py precedent). The scan has
    no conversation session → the cli-<utc-timestamp> sentinel."""
    try:
        digest = hashlib.sha256(
            "\n".join(raw for (_no, raw, _check, _detail) in dropped)
            .encode("utf-8")
        ).hexdigest()
        by_check: Dict[str, List[Tuple[int, str, str, str]]] = {}
        for item in dropped:
            by_check.setdefault(item[2], []).append(item)
        ledger = None
        for check in sorted(by_check):
            key = (check, str(path), digest)
            if key in _DROP_FILING_MEMO:
                continue
            _DROP_FILING_MEMO.add(key)
            if ledger is None:
                from grove.kaizen_ledger import KaizenLedger
                session_id = "cli-" + datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%S%fZ"
                )
                ledger = KaizenLedger(session_id=session_id)
            items = by_check[check]
            ledger.record(
                "andon_halt",
                source="proposal_queue",
                check=check,
                detail=(
                    f"{len(items)} unparseable line(s) in {path} "
                    f"(lines {', '.join(str(i[0]) for i in items)}); "
                    f"sample: {items[0][3]}"
                )[:500],
            )
    except Exception as exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error("[proposal_queue] kaizen filing leg failed: %r", exc)


def _read_records(path: Path) -> Tuple[List[RoutingProposal], List[str]]:
    """Stream RoutingProposals from ``path``.

    silent-degradation-sweep-v1 — returns ``(records, dropped_lines)``: the
    parseable rows plus every unparseable raw line VERBATIM. A drop is no
    longer invisible: nonzero drops surface as ONE WARNING per read (count +
    line numbers) and a throttled per-check ``andon_halt`` filing
    (:func:`_file_drop_andon`). Read-only callers discard element two;
    mutating callers quarantine it (:func:`_quarantine_lines`) before
    rewriting, so a damaged row is preserved on disk — never destroyed by an
    unrelated rewrite.
    """
    if not path.exists():
        logger.info("[proposal_queue] queue file does not exist: %s", path)
        return [], []
    out: List[RoutingProposal] = []
    dropped: List[Tuple[int, str, str, str]] = []  # (line_no, raw, check, detail)
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "[proposal_queue] malformed record line %d in %s: %r",
                    line_no, path, exc,
                )
                dropped.append((line_no, line, "json_decode", repr(exc)))
                continue
            if isinstance(data.get("evidence"), list):
                data["evidence"] = tuple(data["evidence"])
            # B1 Fork D — source_patterns is optional; records written before
            # the field existed simply omit it and fall back to the dataclass
            # default ``()``. JSON carries it as a list; coerce to tuple.
            if isinstance(data.get("source_patterns"), list):
                data["source_patterns"] = tuple(data["source_patterns"])
            # Sprint 32 2a — backward compat for queue entries that
            # predate the ``type`` field. The Sprint 47 legacy spelling
            # ``routing_update`` round-trips as-is; the CLI dispatch
            # accepts both ``routing_update`` and the Sprint 32
            # canonical ``routing_adjustment`` so existing live queue
            # entries continue to approve correctly.
            if data.get("type") is None:
                data["type"] = PROPOSAL_TYPE_ROUTING_ADJUSTMENT
            try:
                out.append(RoutingProposal(**data))
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "[proposal_queue] schema mismatch line %d in %s: %r",
                    line_no, path, exc,
                )
                dropped.append((line_no, line, _drop_check(line), repr(exc)))
    if dropped:
        logger.warning(
            "[proposal_queue] %d unparseable line(s) in %s (lines: %s) — "
            "preserved in file until quarantined on the next queue mutation",
            len(dropped), path,
            ", ".join(str(item[0]) for item in dropped),
        )
        _file_drop_andon(path, dropped)
    return out, [item[1] for item in dropped]


def append(
    proposal: RoutingProposal,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Append ``proposal`` to the queue; return True on append, False
    on duplicate.

    Idempotent on duplicate ``proposal_id``: a re-run of the detector
    that produces the same logical proposal does NOT pollute the queue.
    """
    # proposal-proposer-attribution-v1 — defense-in-depth (NOT a strict assert). A
    # proposal reaching the persist seam with no attribution means a producer failed
    # to stamp it: log LOUD (type + id) and write it as "unattributed" — never crash,
    # never drop the proposal. The 14 producers are covered by per-producer tests.
    _proposer = getattr(proposal, "proposer", None)
    if not _proposer or _proposer == "unattributed":
        logger.warning(
            "[proposal_queue] proposal %s (type=%s) reached append() with no proposer "
            "attribution — writing as 'unattributed'; a producer failed to stamp it.",
            proposal.proposal_id, proposal.type,
        )
        if _proposer != "unattributed":
            proposal = replace(proposal, proposer="unattributed")
    target = Path(path) if path is not None else default_queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        # silent-degradation-sweep-v1 — dedup blindness DOCUMENTED-UNCHANGED:
        # the scan sees only parseable rows, so a duplicate of an unparseable
        # row can re-append. The drop itself now WARNs + files (inside
        # _read_records); append never rewrites, so dropped lines stay put.
        existing, _dropped = _read_records(target)
        for existing_proposal in existing:
            if existing_proposal.proposal_id == proposal.proposal_id:
                return False
        line = json.dumps(proposal.to_dict(), sort_keys=True, default=str) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    return True


def read_all(*, path: Optional[Path] = None) -> List[RoutingProposal]:
    """Return all pending proposals in append order.

    Signature byte-compatible: drops are surfaced (WARN + filing) inside
    ``_read_records``; the read-only path discards them."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        records, _dropped = _read_records(target)
        return records


def read(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Look up one proposal by ``proposal_id``."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        records, _dropped = _read_records(target)
        for proposal in records:
            if proposal.proposal_id == proposal_id:
                return proposal
    return None


def remove(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Remove the proposal with ``proposal_id`` from the queue.

    Returns True on removal, False when no proposal matched.
    Rewrites the file omitting the matched record so the queue stays
    JSON-Lines-clean (no tombstones, no commented-out lines). Unparseable
    lines are quarantined before the rewrite (silent-degradation-sweep-v1) —
    the rewrite writes ONLY parseable rows, and never destroys a damaged one.
    """
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        existing, dropped_lines = _read_records(target)
        keep = [p for p in existing if p.proposal_id != proposal_id]
        if len(keep) == len(existing):
            return False
        _quarantine_lines(target, dropped_lines)
        _write_records(target, keep)
    return True


# ── Lease + finalize (fleet-pipeline-v1 P1, safety-critical) ─────────
#
# The operator-tap lease serializes concurrent Promote taps on ONE proposal.
# set_lease / clear_lease / finalize / sweep_stuck_leases ALL mutate the queue
# under the SAME synchronous ``_lock`` (no await inside the critical section), so
# two taps racing on the event loop serialize: the second observes the held lease
# and is refused (409). There is NO wall-clock TTL — the startup-only sweep is the
# sole recoverer of a lease stranded by a crash (a lease held at boot is
# definitionally orphaned because the ticker has not yet spawned anything).

LEASE_ACQUIRED = "acquired"
LEASE_ALREADY_HELD = "already_held"
LEASE_NOT_FOUND = "not_found"


def _write_records(target: Path, records: List["RoutingProposal"]) -> None:
    """Atomically rewrite the queue from *records* (tmp + os.replace), or unlink
    when empty. Mirrors :func:`remove`'s rewrite; the CALLER must hold ``_lock``."""
    if records:
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for proposal in records:
                fh.write(
                    json.dumps(proposal.to_dict(), sort_keys=True, default=str) + "\n"
                )
        tmp.replace(target)
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def _quarantine_lines(target: Path, dropped_lines: List[str]) -> None:
    """silent-degradation-sweep-v1 — append unparseable raw lines VERBATIM to
    the append-only quarantine sidecar (``<queue>.quarantine``, same
    directory) before a rewrite destroys them. The CALLER must hold ``_lock``.
    Deliberately NOT best-effort: a quarantine write failure aborts the
    mutation rather than silently destroying a damaged row (Jidoka — the
    rewrite must not proceed over unpreserved data).

    kaizen-ledger-retention-v1 P4 — single-generation size-cap rotation:
    when the sidecar already exceeds ``ledger_retention.sidecar_max_bytes``
    it rotates to ``.quarantine.1`` (clobbering any prior ``.1``) before the
    append. The ROTATION leg alone is best-effort — quarantine rows are
    corruption evidence, so a rotation failure (bad config, filesystem
    error) never blocks the append; the size breach surfaces on the next
    retention run's report instead."""
    if not dropped_lines:
        return
    qpath = target.parent / (target.name + ".quarantine")
    try:
        if qpath.exists():
            from grove.ledger_retention import load_retention_config
            cap = load_retention_config().sidecar_max_bytes
            if qpath.stat().st_size > cap:
                import os
                os.replace(qpath, qpath.parent / (qpath.name + ".1"))
                logger.warning(
                    "[proposal_queue] quarantine sidecar exceeded %d bytes — "
                    "rotated to %s.1 (single generation)", cap, qpath.name,
                )
    except Exception as exc:  # noqa: BLE001 — rotation leg only; append proceeds
        logger.error(
            "[proposal_queue] sidecar rotation failed: %r — appending anyway",
            exc,
        )
    with open(qpath, "a", encoding="utf-8") as fh:
        for raw in dropped_lines:
            fh.write(raw + "\n")
    logger.warning(
        "[proposal_queue] quarantined %d unparseable line(s) from %s -> %s",
        len(dropped_lines), target, qpath,
    )


def set_lease(
    proposal_id: str,
    *,
    holder: str = "",
    at: Optional[str] = None,
    path: Optional[Path] = None,
) -> str:
    """Compare-and-set the lease on one proposal. Returns ``LEASE_ACQUIRED``,
    ``LEASE_ALREADY_HELD`` (a tap already holds it → the route 409s), or
    ``LEASE_NOT_FOUND`` (already disposed / never existed → the route 404s).

    The read → check → set → rewrite runs under ``_lock`` with NO ``await``, so a
    second concurrent tap observes the first tap's lease and is refused — the
    double-tap guard."""
    target = Path(path) if path is not None else default_queue_path()
    stamp = at or datetime.now(timezone.utc).isoformat()
    with _lock:
        records, dropped_lines = _read_records(target)
        for i, p in enumerate(records):
            if p.proposal_id == proposal_id:
                if p.lease is not None:
                    return LEASE_ALREADY_HELD
                records[i] = replace(p, lease={"held_by": holder, "held_at": stamp})
                _quarantine_lines(target, dropped_lines)
                _write_records(target, records)
                return LEASE_ACQUIRED
    return LEASE_NOT_FOUND


def clear_lease(proposal_id: str, *, path: Optional[Path] = None) -> bool:
    """Drop the lease, reverting the proposal to actionable. Used by the
    completed-failure path (future returned → thread dead → safe to re-tap) and by
    the startup sweep. Returns True iff a held lease was cleared. NOT called on
    TIMEOUT (the executor thread survives; clearing would let a re-tap double-write)."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        records, dropped_lines = _read_records(target)
        for i, p in enumerate(records):
            if p.proposal_id == proposal_id:
                if p.lease is None:
                    return False
                records[i] = replace(p, lease=None)
                _quarantine_lines(target, dropped_lines)
                _write_records(target, records)
                return True
    return False


def finalize_proposal_state(
    proposal_id: str,
    status: str,
    applied_result: Optional[Dict[str, Any]] = None,
    *,
    reason: Optional[str] = None,
    path: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
) -> bool:
    """The SINGLE disposition path for BOTH operator verbs (approve → "applied",
    reject → "rejected"). Atomically pops the proposal from the queue under
    ``_lock``, then records ONE ``kaizen_disposition`` ledger event (outside the
    queue lock — the ledger has its own). ``applied_result`` rides the ledger
    verbatim (folder_link / archive_path). Returns True on disposition, False if
    the proposal was already gone (idempotent — a double-finalize is a no-op)."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        records, dropped_lines = _read_records(target)
        proposal = next((p for p in records if p.proposal_id == proposal_id), None)
        if proposal is None:
            return False
        keep = [p for p in records if p.proposal_id != proposal_id]
        _quarantine_lines(target, dropped_lines)
        _write_records(target, keep)
    from grove.flywheel_cli import _record_kaizen_disposition

    _record_kaizen_disposition(
        proposal,
        disposition=status,
        applied_result=applied_result,
        reason=reason,
        ledger_dir=ledger_dir,
    )
    return True


def sweep_stuck_leases(*, path: Optional[Path] = None) -> List["RoutingProposal"]:
    """STARTUP-ONLY reap of stranded leases. Runs in the pre-ticker slot (before
    the cron thread spawns anything), so ANY lease still held is definitionally
    orphaned — no live tap could own it. Reverts each to pending (clears the
    lease) and returns them (with their original lease) for the caller to Andon.
    NEVER call this periodically — that would race a live in-flight publish."""
    target = Path(path) if path is not None else default_queue_path()
    reverted: List["RoutingProposal"] = []
    with _lock:
        records, dropped_lines = _read_records(target)
        changed = False
        for i, p in enumerate(records):
            if p.lease is not None:
                reverted.append(p)  # keep the ORIGINAL (lease intact) for the Andon
                records[i] = replace(p, lease=None)
                changed = True
        if changed:
            _quarantine_lines(target, dropped_lines)
            _write_records(target, records)
    for p in reverted:
        logger.warning(
            "[proposal_queue] stuck lease reverted at startup: %s (was %r)",
            p.proposal_id, p.lease,
        )
    return reverted


# ── Agentless filing (portal-action-error-surfacing-v1) ──────────────


def file_agentless_proposal(
    *,
    failure_class: str,
    action: str,
    evidence: str,
    justification: str,
    instance: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Tuple[str, bool]:
    """File a ``portal_action_failure`` proposal from a NON-agent caller.

    The public agentless entry point (portal-action-error-surfacing-v1): an
    aiohttp handler's failure branch — no LLM turn, no detector, no Dispatcher
    context — constructs and enqueues a RoutingProposal so the Kaizen flywheel
    can recommend the structural fix.

    Dedup is content-addressable on the STABLE fields only. ``failure_class``
    and ``action`` (the payload) plus ``evidence`` feed
    :func:`compute_proposal_id`; ``justification`` and the ephemeral ``instance``
    data land in ``semantic_justification`` — a field :func:`compute_proposal_id`
    does NOT hash — so every recurrence of the same class collapses to one queue
    entry. ``evidence`` must therefore be a stable *class-level* descriptor
    (e.g. the failure signature), NOT a per-instance turn id.

    Args:
        failure_class: Stable failure category (e.g. ``"notion_cold_session"``).
        action: The portal action that failed (e.g. ``"forge_publish"``).
        evidence: Stable class-level evidence string (hashed into the id).
        justification: Operator-facing rationale for the recommended fix.
        instance: Optional ephemeral per-occurrence detail (timestamp, slug,
            folder link, exact error). Folded into ``semantic_justification``
            (an EXCLUDED field) so it never perturbs proposal identity.
        path: Optional queue path override (defaults to the standard queue);
            present for isolated testing, mirroring :func:`append`.

    Returns:
        ``(proposal_id, was_appended)``. ``was_appended`` is ``False`` when an
        identical proposal already sits in the queue — the flood-guard.
    """
    # portal_action_failure is now a thin convenience over the generic emission
    # path — one path, type + payload parameterized (fleet-pipeline-v1 P2).
    return file_agentless(
        type=PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
        payload={"failure_class": failure_class, "action": action},
        evidence=(evidence,),
        justification=justification,
        instance=instance,
        proposer="portal_failure",  # proposal-proposer-attribution-v1 (producer #14)
        path=path,
    )


def file_agentless(
    *,
    type: str,
    payload: Dict[str, Any],
    evidence: Tuple[str, ...],
    justification: str = "",
    instance: Optional[Dict[str, Any]] = None,
    proposer: str = "unattributed",
    path: Optional[Path] = None,
) -> Tuple[str, bool]:
    """The ONE agentless emission path (P2 generalization of
    file_agentless_proposal — do not fork). Builds a RoutingProposal of *type*
    with *payload* and enqueues it: no LLM turn, no detector, no Dispatcher.

    Identity is content-addressable on ``type|payload|evidence``
    (:func:`compute_proposal_id`); *justification* + the ephemeral *instance*
    ride ``semantic_justification`` (id-excluded), so recurrences of the same
    logical proposal collapse to one queue entry. Returns
    ``(proposal_id, was_appended)``; ``was_appended`` is False on a duplicate
    (the flood-guard)."""
    rationale = justification
    if instance:
        detail = "; ".join(f"{k}={instance[k]}" for k in sorted(instance))
        rationale = f"{justification} [{detail}]" if justification else detail

    evidence_tuple = tuple(evidence)
    proposal_id = compute_proposal_id(type=type, payload=payload, evidence=evidence_tuple)
    proposal = RoutingProposal(
        proposal_id=proposal_id,
        type=type,
        payload=payload,
        evidence=evidence_tuple,
        eval_hash="",
        created_at=_now_iso(),
        semantic_justification=rationale,
        proposer=proposer,
    )
    return proposal_id, append(proposal, path=path)
