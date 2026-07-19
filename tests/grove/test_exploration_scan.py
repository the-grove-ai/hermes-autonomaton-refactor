"""kaizen-exploration-proposals-v1 Phase 4 — run_exploration_scan producer.

Predicate: {catalog} ∖ {fleet ∪ attended arm models} ∖ {bound: routing tiers +
capability pins} ∖ {tombstoned} ∖ {pending exploration_nudge}. One nudge per
qualifying slug, tier=T2, display/pricing in the id-EXCLUDED detail envelope.

One test per subtraction term + pending-not-duplicated + shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from grove.eval.exploration_scan import build_exploration_proposals, record_tombstone
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_EXPLORATION_NUDGE,
    RoutingProposal,
    append,
    compute_proposal_id,
)
from grove.intent_store import IntentRecord, IntentStore

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _cat(*slugs):
    return [
        {
            "slug": s,
            "display_name": s.split("/")[-1].upper(),
            "provider": "openrouter",
            "input_cost_per_mtok": 0.5,
            "output_cost_per_mtok": 2.0,
        }
        for s in slugs
    ]


@pytest.fixture()
def paths(tmp_path):
    from types import SimpleNamespace

    nofleet = tmp_path / "nofleet"
    nofleet.mkdir()
    return SimpleNamespace(
        events_root=nofleet,                         # empty → no fleet arms
        attended=tmp_path / "intent_records.jsonl",  # absent → no attended arms
        tombs=tmp_path / "exploration_tombstones.json",
        queue=tmp_path / "proposals.jsonl",
        tmp=tmp_path,
    )


def _build(paths, catalog, *, referrers=None):
    return build_exploration_proposals(
        catalog=catalog,
        events_root=paths.events_root,
        attended_records_path=paths.attended,
        referrers=referrers if referrers is not None else {},
        tombstone_path=paths.tombs,
        queue_path=paths.queue,
        now=NOW,
    )


def _fleet_event(paths, skill, model):
    ev_dir = paths.tmp / "fleet_seeded" / "w" / "events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(ev_dir.glob("*.json")))
    (ev_dir / f"e{n}.json").write_text(
        json.dumps({
            "worker_id": "w", "run_id": "r", "skill": skill, "status": "success",
            "detail": "", "staged": [], "check": None, "slug": None, "row_id": None,
            "fit_score": None, "raw_text_path": None, "model": model, "tier": "T2",
            "binding_source": "pinned", "quality_score": None, "rubric_version": None,
            "redraft_count": None, "evaluator_model": None,
            "ts": (NOW - timedelta(days=1)).isoformat(),
        }),
        encoding="utf-8",
    )
    return paths.tmp / "fleet_seeded"


def _attended(paths, model):
    store = IntentStore(paths.attended)
    store.append(IntentRecord(
        timestamp=(NOW - timedelta(days=1)).isoformat(),
        session_id="s", turn_id="t1", user_message_stem="hi", pattern_hash="ph",
        intent_class="conversation", register_class="chat", complexity_signal="low",
        confidence=0.9, outcome="success", tier_selected="T2", model_used=model,
    ))


def _pending_nudge(paths, slug, tier="T2"):
    payload = {"slug": slug, "tier": tier}
    append(RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_EXPLORATION_NUDGE, payload=payload, evidence=(),
        ),
        type=PROPOSAL_TYPE_EXPLORATION_NUDGE, payload=payload, evidence=(),
        eval_hash="", created_at=NOW.isoformat(),
    ), path=paths.queue)


# ── the qualifying case + shape ─────────────────────────────────────────────────


def test_untried_model_is_proposed_with_shape(paths):
    props = _build(paths, _cat("prov/x"))
    assert len(props) == 1
    p = props[0]
    assert p.type == PROPOSAL_TYPE_EXPLORATION_NUDGE
    assert p.payload == {"slug": "prov/x", "tier": "T2"}
    assert p.detail["display_name"] == "X"
    assert p.detail["provider"] == "openrouter"
    assert p.detail["input_cost_per_mtok"] == 0.5
    assert p.detail["output_cost_per_mtok"] == 2.0
    assert p.proposer == "exploration_scan"


def test_one_proposal_per_qualifying_slug_sorted(paths):
    props = _build(paths, _cat("prov/c", "prov/a", "prov/b"))
    assert [p.payload["slug"] for p in props] == ["prov/a", "prov/b", "prov/c"]


# ── one test per subtraction term ───────────────────────────────────────────────


def test_fleet_tried_excluded(paths):
    root = _fleet_event(paths, "skill.fleet.alpha", "prov/x")
    props = build_exploration_proposals(
        catalog=_cat("prov/x", "prov/y"),
        events_root=root,
        attended_records_path=paths.attended,
        referrers={},
        tombstone_path=paths.tombs,
        queue_path=paths.queue,
        now=NOW,
    )
    slugs = {p.payload["slug"] for p in props}
    assert slugs == {"prov/y"}  # fleet-tried prov/x excluded


def test_attended_tried_excluded(paths):
    _attended(paths, "prov/x")
    props = _build(paths, _cat("prov/x", "prov/y"))
    assert {p.payload["slug"] for p in props} == {"prov/y"}


def test_bound_in_any_tier_excluded(paths):
    # referrers is the referential-guard map (routing tiers ∪ capability pins).
    props = _build(paths, _cat("prov/x", "prov/y"), referrers={"prov/x": ["tier_preferences.T1"]})
    assert {p.payload["slug"] for p in props} == {"prov/y"}


def test_tombstoned_excluded(paths):
    from types import SimpleNamespace

    record_tombstone(
        SimpleNamespace(payload={"slug": "prov/x"}, proposal_id="sha256:z"),
        path=paths.tombs,
    )
    props = _build(paths, _cat("prov/x", "prov/y"))
    assert {p.payload["slug"] for p in props} == {"prov/y"}


def test_pending_not_duplicated(paths):
    _pending_nudge(paths, "prov/x")
    props = _build(paths, _cat("prov/x", "prov/y"))
    # prov/x already pending → not re-proposed; prov/y is fresh.
    assert {p.payload["slug"] for p in props} == {"prov/y"}


def test_idempotent_dedup_through_queue(paths):
    """Same slug → same proposal_id → queue append dedups on a second scan."""
    props = _build(paths, _cat("prov/x"))
    assert append(props[0], path=paths.queue) is True
    # Re-scan now sees the pending nudge and does not re-file it.
    again = _build(paths, _cat("prov/x"))
    assert again == []
