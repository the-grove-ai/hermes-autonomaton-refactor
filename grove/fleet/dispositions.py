"""fleet-receipt-custody-v1 P4b-0 — the ledger disposition projection.

``{unit_id -> disposition}`` (and ``{slug -> unit_id}``) over the kaizen
disposition ledger, filtered to fleet artifact proposals. This is the single
committed authority for a unit's terminal disposition — the same projection the
portal auto-close relies on — moved here to the FLEET PLANE so eligibility
(``grove.fleet.resolvers``, P4a) and emission (P4b) import it FORWARD, and the
API layer imports it forward too. It depends only on ``grove.kaizen_ledger`` and
``grove.eval.proposal_queue`` — never on ``grove.api``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Set

from grove.eval import proposal_queue

_ARTIFACT_PROPOSAL_TYPES = (
    proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
    proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
)


def _artifact_unit_id(payload: Optional[dict]) -> Optional[str]:
    """The stable unit identity a proposal/ledger event keys on: unit_id (file
    producer) → row_id (forge) → slug (last resort). The single authority the
    portal auto-close (``grove.api.portal``) and the P4b-1 emission carded-set
    both read — moved to the FLEET PLANE so emission imports it FORWARD (never
    grove.api)."""
    pl = payload or {}
    return pl.get("unit_id") or pl.get("row_id") or pl.get("slug")


def live_artifact_carded_unit_ids() -> Set[str]:
    """The set of unit_ids carrying a LIVE artifact card (fleet-receipt-custody-v1
    P4b-1 R2 — emit-once-and-skip). ONE ``read_all()``; ``read_all`` returns only
    live proposals (terminals are popped into the ledger), so a disposed unit is
    absent here and its re-emission is prevented by STATE (Done), not this set."""
    out: Set[str] = set()
    for p in proposal_queue.read_all():
        if getattr(p, "type", None) not in _ARTIFACT_PROPOSAL_TYPES:
            continue
        uid = _artifact_unit_id(getattr(p, "payload", None))
        if uid:
            out.add(uid)
    return out


def _iter_ledger_terminal_events():
    """Yield ``(uid, slug, disposition)`` for every terminal artifact
    disposition in the kaizen ledger, in file/line order (callers apply
    later-events-win). The shared parse body for
    :func:`_ledger_terminal_dispositions` and :func:`_ledger_slug_to_uid`.
    ``slug`` is the ``applied_result`` slug when carried (C2/P1 enrich both
    promote and reject with unit_id + slug), else None."""
    from grove.kaizen_ledger import default_ledger_dir
    ledger_dir = default_ledger_dir()
    if not ledger_dir.is_dir():
        return
    for lf in sorted(ledger_dir.glob("*.jsonl")):
        try:
            lines = lf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event_type") != "kaizen_disposition":
                continue
            if ev.get("proposal_type") not in _ARTIFACT_PROPOSAL_TYPES:
                continue
            disp = ev.get("disposition")
            if disp not in ("applied", "rejected"):
                continue  # suggest_revision is not a terminal
            ar = ev.get("applied_result") or {}
            slug = ar.get("slug")
            uid = ar.get("unit_id") or slug
            if not uid and ar.get("archive_path"):
                base = Path(str(ar["archive_path"])).name  # <slug>-<utc-ts>
                uid = base.rsplit("-", 1)[0] if "-" in base else base
            if uid:
                yield uid, slug, disp


def _ledger_terminal_dispositions() -> Dict[str, str]:
    """``{unit_id -> 'applied'|'rejected'}`` from the kaizen_disposition ledger, for
    artifact proposals — the remote-publish sink's terminal source of truth. Keyed on
    the unit identity the disposition's ``applied_result`` carries (C2 enriches
    promote/reject with unit_id + slug); a reject's ``archive_path`` slug is the
    fallback key. Later events win (the last disposition is authoritative)."""
    out: Dict[str, str] = {}
    for uid, _slug, disp in _iter_ledger_terminal_events():
        out[uid] = disp
    return out


def _ledger_slug_to_uid() -> Dict[str, str]:
    """``{slug -> unit_id}`` from the same terminal events (P2, additive) — the
    join key for the P1 canonical subdirs, whose dir NAME is the slug while the
    unit list keys on unit_id. Later events win."""
    out: Dict[str, str] = {}
    for uid, slug, _disp in _iter_ledger_terminal_events():
        if slug:
            out[slug] = uid
    return out
