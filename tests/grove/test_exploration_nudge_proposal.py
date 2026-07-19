"""kaizen-exploration-proposals-v1 Phase 2 — exploration_nudge proposal type.

Follows the model_binding landing pattern (test_model_binding_proposal.py).
Proves:

* IDENTITY — payload is {slug, tier} ONLY; catalog pricing rides the id-EXCLUDED
  ``detail`` envelope, so a repriced model does NOT fork into a duplicate nudge.
* RENDERERS — summary reads "cataloged and untried — try it interactively?";
  auto-seeded into RENDER_REGISTRY via seed_from_handlers (no manual register).
* REGISTRY — _handler_for dispatches the row automatically.
* APPLY — delegates to the sanctioned RoutingConfigWriter.swap_tier_model (the
  interactive one-tap writer); NO direct routing.config.yaml touch; files the
  type-specific exploration_nudge_applied ledger event.
* REJECT — writes the OWN-NAMESPACE (slug-keyed) exploration tombstone, never
  binding_tombstones.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grove import flywheel_cli
from grove.config.routing_writer import TierSwapResult
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_EXPLORATION_NUDGE,
    RoutingProposal,
    compute_proposal_id,
)

_SLUG = "moonshotai/kimi-k3"
_TIER = "T2"


def _proposal(
    slug: str = _SLUG,
    tier: str = _TIER,
    detail: dict | None = None,
) -> RoutingProposal:
    payload = {"slug": slug, "tier": tier}
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_EXPLORATION_NUDGE, payload=payload, evidence=(),
        ),
        type=PROPOSAL_TYPE_EXPLORATION_NUDGE,
        payload=payload,
        evidence=(),
        eval_hash="",
        created_at=datetime.now(timezone.utc).isoformat(),
        detail=detail,
    )


_DETAIL = {
    "display_name": "Kimi K3",
    "provider": "openrouter",
    "input_cost_per_mtok": 0.5,
    "output_cost_per_mtok": 2.0,
}


def _ledger_events(home: Path, event_type: str) -> list[dict]:
    events: list[dict] = []
    ledger_dir = home / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return events
    for f in sorted(ledger_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("event_type") == event_type:
                events.append(ev)
    return events


# ── identity: pricing must NOT fork the proposal id ─────────────────────────────


def test_pricing_in_detail_does_not_fork_identity():
    """A repriced model keeps the SAME proposal id — pricing rides id-EXCLUDED
    detail, only {slug, tier} is hashed."""
    a = _proposal(detail=_DETAIL)
    b = _proposal(detail={**_DETAIL, "input_cost_per_mtok": 99.0})
    no_detail = _proposal(detail=None)
    assert a.proposal_id == b.proposal_id == no_detail.proposal_id


def test_different_tier_is_a_distinct_proposal():
    assert _proposal(tier="T2").proposal_id != _proposal(tier="T1").proposal_id


def test_detail_is_excluded_from_compute_proposal_id_signature():
    # compute_proposal_id does not even accept detail as a kwarg (identity is
    # type|payload|evidence only).
    with pytest.raises(TypeError):
        compute_proposal_id(
            type=PROPOSAL_TYPE_EXPLORATION_NUDGE,
            payload={"slug": _SLUG, "tier": _TIER},
            evidence=(),
            detail=_DETAIL,
        )


# ── renderers (auto-seeded) ─────────────────────────────────────────────────────


def test_renderer_auto_seeded_into_registry():
    from grove.kaizen.rendering import RENDER_REGISTRY, get_renderer

    assert PROPOSAL_TYPE_EXPLORATION_NUDGE in RENDER_REGISTRY
    assert get_renderer(PROPOSAL_TYPE_EXPLORATION_NUDGE) is (
        flywheel_cli._summary_exploration_nudge
    )


def test_summary_reads_the_nudge_with_pricing():
    s = flywheel_cli._summary_exploration_nudge(_proposal(detail=_DETAIL))
    assert f"Model Kimi K3 ({_SLUG}) is cataloged and untried" in s
    assert "try it interactively on T2?" in s
    assert "Pricing: $0.5/$2.0 per Mtok (in/out)." in s
    assert "Provider: openrouter." in s


def test_summary_degrades_without_detail():
    """A hand-filed nudge with no detail still renders — slug stands in for the
    display name, no pricing/provider clause."""
    s = flywheel_cli._summary_exploration_nudge(_proposal(detail=None))
    assert f"Model {_SLUG} ({_SLUG}) is cataloged and untried" in s
    assert "Pricing:" not in s
    assert "Provider:" not in s


def test_diff_shows_after_target_and_catalog_meta():
    diff = flywheel_cli._exploration_nudge_to_diff(_proposal(detail=_DETAIL))
    assert diff["interactive tier: T2"]["model"]["+after"] == _SLUG
    assert diff["catalog"]["display_name"] == "Kimi K3"
    assert diff["catalog"]["input_cost_per_mtok"] == 0.5


# ── registry dispatch ───────────────────────────────────────────────────────────


def test_handler_for_resolves_the_row():
    handler = flywheel_cli._handler_for(PROPOSAL_TYPE_EXPLORATION_NUDGE)
    assert handler.apply_callback is flywheel_cli._approve_exploration_nudge
    assert handler.reject_callback is flywheel_cli._reject_exploration_nudge
    assert handler.apply_label_prefix == "Interactive selection flipped: "


# ── apply: delegates to the sanctioned writer, no direct config touch ───────────


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def swap_tier_model(self, tier: str, new_slug: str) -> TierSwapResult:
        self.calls.append((tier, new_slug))
        return TierSwapResult(status="swapped", tier=tier, model=new_slug)


def test_apply_delegates_to_writer_and_writes_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    fake = _FakeWriter()
    monkeypatch.setattr(
        "grove.config.routing_writer.get_writer", lambda: fake
    )

    proposal = _proposal(detail=_DETAIL)
    target, applied = flywheel_cli._approve_exploration_nudge(proposal)

    # Delegation happened, exactly once, with (tier, slug).
    assert fake.calls == [(_TIER, _SLUG)]
    assert applied == {"tier": _TIER, "model": _SLUG, "status": "swapped"}
    assert target == f"{_TIER} -> {_SLUG}"
    # NO direct config touch — the writer is the sole path; nothing wrote a
    # routing.config.yaml under the hermetic home.
    assert not (tmp_path / "routing.config.yaml").exists()

    # Type-specific audit event fired, carrying the exploration provenance.
    applied_events = _ledger_events(tmp_path, "exploration_nudge_applied")
    assert len(applied_events) == 1
    ev = applied_events[0]
    assert ev["slug"] == _SLUG
    assert ev["tier"] == _TIER
    assert ev["surface"] == "proposal_apply"
    assert ev["proposal_id"] == proposal.proposal_id


def test_apply_refuses_payload_missing_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    bad = RoutingProposal(
        proposal_id="sha256:bad",
        type=PROPOSAL_TYPE_EXPLORATION_NUDGE,
        payload={"tier": _TIER},
        evidence=(),
        eval_hash="",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ValueError, match="missing a non-empty slug"):
        flywheel_cli._approve_exploration_nudge(bad)


# ── reject: own-namespace slug-keyed tombstone, never binding_tombstones ────────


def test_reject_writes_own_namespace_tombstone(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))

    flywheel_cli._reject_exploration_nudge(_proposal())

    from grove.eval.exploration_scan import (
        _load_tombstones,
        _suppressed,
        default_tombstone_path,
    )

    store = default_tombstone_path()
    assert store.exists()
    assert store.name == "exploration_tombstones.json"
    entries = _load_tombstones()
    assert [e["slug"] for e in entries] == [_SLUG]
    assert _suppressed(entries, _SLUG) is True
    assert _suppressed(entries, "other/model") is False

    # OWN namespace — the binding tombstone store is untouched (F-4).
    assert not (tmp_path / "binding_tombstones.json").exists()
