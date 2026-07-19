"""kaizen-exploration-proposals-v1 — exploration_nudge suppression tombstones.

Own-namespace store (R-A / F-4 collision analysis): a rejected "try model X
interactively?" nudge must NOT re-surface for that slug, and must NEVER share
the ``binding_tombstones.json`` namespace (whose key is skill/model — a rejected
nudge could otherwise suppress a legitimate model_binding proposal on the same
skill/model pair, or vice versa). Keyed on the catalog ``slug`` alone, beside
``proposals.jsonl`` in ``~/.grove`` so a ``git reset --hard`` deploy cannot wipe
a dismissal and resurrect the nudge (the admission_friction tombstone precedent).

The zero-arm producer (``run_exploration_scan``, added in a later phase) reads
:func:`_suppressed` to subtract tombstoned slugs from the candidate set. This
module ships the store first so the reject_callback has a durable home in
Phase 2, before the producer exists.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_EXPLORATION_NUDGE,
    RoutingProposal,
    compute_proposal_id,
    default_queue_path,
    read_all,
)

logger = logging.getLogger(__name__)

_TOMBSTONE_FILENAME = "exploration_tombstones.json"

# Match the binding-telemetry evidence window (grove.eval.binding_scan.WINDOW_DAYS)
# so "untried" means untried over the SAME horizon the promotion pipeline reads.
WINDOW_DAYS = 30

# The interactive daily-driver tier the nudge flips (R-P0-1). A cataloged-untried
# model is offered for attended use here; approve delegates to swap_tier_model.
DEFAULT_TARGET_TIER = "T2"


def default_tombstone_path() -> Path:
    """``~/.grove/exploration_tombstones.json`` — beside the proposal queue,
    OUTSIDE the repo tree, so a deploy git-reset cannot wipe a dismissal and
    resurrect the nudge (admission_friction precedent)."""
    return default_queue_path().with_name(_TOMBSTONE_FILENAME)


def _load_tombstones(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or default_tombstone_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Fail LOUD but never crash a rejection or a scan on one bad store file —
        # an unreadable store suppresses nothing (nudges may re-surface; the
        # operator re-rejects) rather than suppressing everything.
        logger.warning(
            "[exploration_scan] tombstone store unreadable at %s (%s) — treating "
            "as empty; dismissed nudges may re-surface until repaired.", p, exc,
        )
        return []
    entries = data.get("tombstones") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _write_tombstones(
    entries: List[Dict[str, Any]], path: Optional[Path] = None
) -> None:
    p = path or default_tombstone_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tombstones": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def record_tombstone(
    proposal: Any, *, path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Reject-disposition hook — called by the exploration_nudge handler row's
    ``reject_callback`` BEFORE queue removal. Keys on the catalog ``slug`` alone.
    Returns the entry, or None when the payload carries no slug (logged)."""
    payload = getattr(proposal, "payload", None) or {}
    slug = payload.get("slug")
    if not slug:
        logger.warning(
            "[exploration_scan] rejected exploration_nudge %s carries no slug — "
            "no tombstone written",
            getattr(proposal, "proposal_id", "?"),
        )
        return None
    entry = {
        "slug": slug,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "proposal_id": getattr(proposal, "proposal_id", None),
    }
    entries = _load_tombstones(path)
    entries.append(entry)
    _write_tombstones(entries, path)
    logger.info("[exploration_scan] tombstone recorded: %s", slug)
    return entry


def _suppressed(tombstones: List[Dict[str, Any]], slug: str) -> bool:
    """A slug is suppressed iff any tombstone names it."""
    return any(t.get("slug") == slug for t in tombstones)


# ── producer ────────────────────────────────────────────────────────────


def build_exploration_proposals(
    *,
    catalog: Optional[List[Dict[str, Any]]] = None,
    events_root: Optional[Path] = None,
    attended_records_path: Optional[Path] = None,
    referrers: Optional[Dict[str, List[str]]] = None,
    tombstone_path: Optional[Path] = None,
    queue_path: Optional[Path] = None,
    window_days: int = WINDOW_DAYS,
    now: Optional[datetime] = None,
    target_tier: str = DEFAULT_TARGET_TIER,
) -> List[RoutingProposal]:
    """The zero-arm producer (R-D). One exploration_nudge per cataloged model
    that is genuinely untried and not otherwise accounted for.

    Predicate — a slug qualifies iff it is in the merged catalog and in NONE of::

        {fleet arm models}                       # observed on the fleet plane
        ∪ {attended arm models}                   # observed interactively
        ∪ {bound: routing tier_preferences[*].model ∪ capability model pins}
        ∪ {exploration-tombstoned slugs}          # a rejected nudge (durable)
        ∪ {pending exploration_nudge slugs}        # already queued, not stacked

    The three disposition histories are realized by EXISTING signals, no new
    ledger (R-D): an APPLIED nudge flips T2 → the slug becomes a routing referrer
    (bound); a REJECTED nudge writes the slug tombstone; a PENDING nudge sits in
    the queue. Poll-only; the caller runs it on the manual scan cadence.

    All sources are injectable so the predicate is unit-testable without touching
    live config. READ-only; never writes the queue (the caller appends).
    """
    if catalog is None:
        from grove.config.model_catalog import load_catalog

        catalog = load_catalog()
    by_slug = {m["slug"]: m for m in catalog if m.get("slug")}
    catalog_slugs = set(by_slug)

    # − observed models (fleet ∪ attended). A model tried on EITHER plane is not
    #   "untried" (the predicate refinement: operator-tried ≠ untried).
    from grove.kaizen.binding_evidence import collect_arms

    tried = {
        a["model"]
        for a in collect_arms(
            events_root=events_root, window_days=window_days, now=now
        )["arms"]
    }
    from grove.eval.attended_evidence import collect_attended_arms

    tried |= {
        a["model"]
        for a in collect_attended_arms(
            store_path=attended_records_path, window_days=window_days, now=now
        )["arms"]
    }

    # − bound models (routing tier_preferences bindings ∪ capability model pins).
    #   Reuses the catalog referential guard (READ-ONLY, ratchet-safe — it lazily
    #   imports the router inside itself, preserving G-1b dispatch isolation).
    if referrers is None:
        from grove.config.model_catalog import collect_catalog_referrers

        referrers = collect_catalog_referrers()
    bound = set(referrers)

    # − pending exploration_nudge slugs (already queued — never stack a duplicate).
    pending = {
        (p.payload or {}).get("slug")
        for p in read_all(path=queue_path)
        if getattr(p, "type", None) == PROPOSAL_TYPE_EXPLORATION_NUDGE
    }

    tombstones = _load_tombstones(tombstone_path)
    created_at = (now or datetime.now(timezone.utc)).isoformat()

    proposals: List[RoutingProposal] = []
    for slug in sorted(catalog_slugs - tried - bound - pending):
        if _suppressed(tombstones, slug):
            logger.info(
                "[exploration_scan] %s: rejected-nudge tombstone — no proposal",
                slug,
            )
            continue
        entry = by_slug[slug]
        payload = {"slug": slug, "tier": target_tier}
        # Card-only display/pricing rides the id-EXCLUDED detail envelope, from the
        # MERGED catalog view — so a repriced model never forks the nudge identity.
        detail = {
            "display_name": entry.get("display_name"),
            "provider": entry.get("provider"),
            "input_cost_per_mtok": entry.get("input_cost_per_mtok"),
            "output_cost_per_mtok": entry.get("output_cost_per_mtok"),
        }
        proposals.append(
            RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_EXPLORATION_NUDGE,
                    payload=payload,
                    evidence=(),
                ),
                type=PROPOSAL_TYPE_EXPLORATION_NUDGE,
                payload=payload,
                evidence=(),
                eval_hash="",
                created_at=created_at,
                detail=detail,
                proposer="exploration_scan",
            )
        )
    return proposals
