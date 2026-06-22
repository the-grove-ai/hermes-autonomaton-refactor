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
from dataclasses import asdict, dataclass, field
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
    "compute_proposal_id",
    "compute_eval_hash",
    "default_queue_path",
    "append",
    "read_all",
    "read",
    "remove",
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
_LEGACY_ROUTING_TYPE = "routing_update"  # Sprint 47 spelling


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

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(data["evidence"])
        data["source_patterns"] = list(data["source_patterns"])
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


def _read_records(path: Path) -> List[RoutingProposal]:
    """Stream RoutingProposals from ``path``; skip malformed lines.

    Malformed lines log at debug and are skipped — the queue is
    operator-facing and must not crash on a damaged entry.
    """
    if not path.exists():
        return []
    out: List[RoutingProposal] = []
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
    return out


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
    target = Path(path) if path is not None else default_queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        existing = _read_records(target)
        for existing_proposal in existing:
            if existing_proposal.proposal_id == proposal.proposal_id:
                return False
        line = json.dumps(proposal.to_dict(), sort_keys=True, default=str) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    return True


def read_all(*, path: Optional[Path] = None) -> List[RoutingProposal]:
    """Return all pending proposals in append order."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        return _read_records(target)


def read(
    proposal_id: str,
    *,
    path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Look up one proposal by ``proposal_id``."""
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        for proposal in _read_records(target):
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
    JSON-Lines-clean (no tombstones, no commented-out lines).
    """
    target = Path(path) if path is not None else default_queue_path()
    with _lock:
        existing = _read_records(target)
        keep = [p for p in existing if p.proposal_id != proposal_id]
        if len(keep) == len(existing):
            return False
        if keep:
            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for proposal in keep:
                    fh.write(
                        json.dumps(
                            proposal.to_dict(), sort_keys=True, default=str,
                        )
                        + "\n"
                    )
            tmp.replace(target)
        else:
            target.unlink()
    return True
