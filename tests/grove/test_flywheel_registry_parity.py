"""B1 flywheel-spine-v1 — handler-registry parity proofs (Fork A, FULL).

Characterization tests: each existing proposal type must render its diff and
its one-line summary byte-identically through the registry as it did through
the if/elif ladders. Written and made green against the PRE-refactor ladders,
then re-run against the registry — same golden values both sides == parity.

The skill_synthesis row (Fork B) and the source_patterns field (Fork D) are
NEW behavior and live in their own test modules / classes, not here.
"""

from __future__ import annotations

import pytest

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PATTERN_DEMOTION,
    PROPOSAL_TYPE_PATTERN_PROMOTION,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_SKILL_PROMOTION,
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    compute_proposal_id,
)


def _proposal(ptype: str, payload: dict, evidence=("t_a", "t_b")) -> RoutingProposal:
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=ptype, payload=payload, evidence=tuple(evidence),
        ),
        type=ptype,
        payload=payload,
        evidence=tuple(evidence),
        eval_hash="sha256:eval",
        created_at="2026-06-15T00:00:00+00:00",
    )


# Per-type fixtures + golden diff dicts + golden summary bodies, lifted
# verbatim from the pre-refactor ladders.
_ROUTING = _proposal(
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    {"rule": "downward", "add_intents": ["conversation"]},
)
_ZONE = _proposal(
    PROPOSAL_TYPE_ZONE_PROMOTION,
    {
        "tool": "terminal",
        "pattern": r".*\.grove/skills/cal/.*",
        "zone": "green",
        "reason": "allow cal",
    },
)
_SKILL = _proposal(
    PROPOSAL_TYPE_SKILL_PROMOTION,
    {
        "skill_name": "foo",
        "skill_path": "~/.grove/skills/.andon/foo/",
        "execution_turn_id": "t1",
        "suggested_action": "promote",
    },
)
_PAT_PROMO = _proposal(
    PROPOSAL_TYPE_PATTERN_PROMOTION,
    {
        "pattern_id": "sha256:p",
        "intent_class": "weather",
        "cacheable_type": "static",
        "sample_queries": ["what's the weather"],
        "promotion_evidence": {"hits": 5},
    },
)
_PAT_DEMO = _proposal(
    PROPOSAL_TYPE_PATTERN_DEMOTION,
    {
        "pattern_id": "sha256:p",
        "intent_class": "weather",
        "cacheable_type": "static",
        "suggested_action": "demote",
        "trigger": "correction_drift",
        "correction_turn_id": "t9",
    },
)

_EXPECTED_DIFF = {
    _ROUTING.proposal_id: {
        "routing": {
            "routing_rules": {
                "downward": {"match": {"intents": ["conversation"]}},
            },
        },
    },
    _ZONE.proposal_id: {
        "tool_zones": {
            "terminal": {
                "rules": [
                    {
                        "match_pattern": r".*\.grove/skills/cal/.*",
                        "zone": "green",
                        "reason": "allow cal",
                    },
                ],
            },
        },
    },
    _SKILL.proposal_id: {
        "skill_promotion": {
            "skill_name": "foo",
            "from": "~/.grove/skills/.andon/foo/",
            "to": "~/.grove/skills/foo/",
            "zone_rule": {
                "match_pattern": r".*\.grove/skills/foo/.*",
                "zone": "green",
            },
        },
    },
    _PAT_PROMO.proposal_id: {
        "pattern_promotion": {
            "intent_class": "weather",
            "cacheable_type": "static",
            "tier": "T1 → T0 (deterministic; no model call)",
            "evidence": {"hits": 5},
            "sample_queries": ["what's the weather"],
        },
    },
    _PAT_DEMO.proposal_id: {
        "pattern_demotion": {
            "intent_class": "weather",
            "tier": "T0 → T1 (drift: corrected after a cache hit)",
            "trigger": "correction_drift",
            "correction_turn_id": "t9",
            "reverse_with": "autonomaton flywheel reject <id>",
        },
    },
}

_EXPECTED_BODY = {
    _ROUTING.proposal_id: "add conversation to routing.downward",
    _ZONE.proposal_id: r"greenlight terminal pattern='.*\\.grove/skills/cal/.*'",
    _SKILL.proposal_id: "promote quarantined skill 'foo' → trusted",
    _PAT_PROMO.proposal_id: "retire weather [static] pattern “what's the weather” to T0 cache",
    _PAT_DEMO.proposal_id: "demote weather pattern (drift: corrected after a T0 hit)",
}

_ALL = [_ROUTING, _ZONE, _SKILL, _PAT_PROMO, _PAT_DEMO]


@pytest.mark.parametrize("p", _ALL, ids=lambda p: p.type)
def test_diff_renderer_parity(p: RoutingProposal) -> None:
    assert flywheel_cli._proposal_to_diff(p) == _EXPECTED_DIFF[p.proposal_id]


@pytest.mark.parametrize("p", _ALL, ids=lambda p: p.type)
def test_summary_renderer_parity(p: RoutingProposal) -> None:
    short_id = p.proposal_id.split(":")[-1][:12]
    expected = (
        f"{short_id}  {p.type:<22}  "
        f"{_EXPECTED_BODY[p.proposal_id]}  "
        f"(evidence: {len(p.evidence)} turn(s))  "
        f"{p.created_at}"
    )
    assert flywheel_cli._format_summary(p) == expected


def test_routing_update_alias_renders_as_routing_adjustment() -> None:
    """The legacy spelling must keep rendering through the routing path."""
    legacy = _proposal("routing_update", {"rule": "upward", "add_intents": ["x"]})
    assert flywheel_cli._proposal_to_diff(legacy) == {
        "routing": {"routing_rules": {"upward": {"match": {"intents": ["x"]}}}},
    }
    assert "add x to routing.upward" in flywheel_cli._format_summary(legacy)


# ── registry shape (Fork A) ──────────────────────────────────────────


def test_registry_covers_exactly_the_registered_types() -> None:
    # consolidation-ratchet-v1 added the seventh row (consolidation_proposal,
    # the Stage 2 policy-graduation apply path). dock-as-mutation-target-v1
    # added the eighth (dock_mutation, the Memory→Dock goal-proposal apply
    # path). Updated inline by the sprint that registers the type — the registry
    # parity contract still holds, the closed set just grew by one.
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_CONSOLIDATION,
        PROPOSAL_TYPE_DOCK_MUTATION,
        PROPOSAL_TYPE_SKILL_SYNTHESIS,
    )

    assert set(flywheel_cli.PROPOSAL_HANDLERS) == {
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        PROPOSAL_TYPE_CONSOLIDATION,
        PROPOSAL_TYPE_DOCK_MUTATION,
        PROPOSAL_TYPE_ZONE_PROMOTION,
        PROPOSAL_TYPE_SKILL_PROMOTION,
        PROPOSAL_TYPE_PATTERN_PROMOTION,
        PROPOSAL_TYPE_PATTERN_DEMOTION,
        PROPOSAL_TYPE_SKILL_SYNTHESIS,
    }


def test_handler_for_raises_on_unknown_type() -> None:
    with pytest.raises(ValueError, match="unsupported proposal type"):
        flywheel_cli._handler_for("does_not_exist")


def test_handler_for_resolves_routing_update_alias() -> None:
    assert (
        flywheel_cli._handler_for("routing_update")
        is flywheel_cli.PROPOSAL_HANDLERS[PROPOSAL_TYPE_ROUTING_ADJUSTMENT]
    )


def test_apply_dispatch_wiring_and_label_prefixes() -> None:
    """Each row routes to the expected apply function with the expected prefix."""
    expect = {
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT: (flywheel_cli._approve_routing_adjustment, "Applied to: "),
        PROPOSAL_TYPE_ZONE_PROMOTION: (flywheel_cli._approve_zone_promotion, "Applied to: "),
        PROPOSAL_TYPE_SKILL_PROMOTION: (flywheel_cli._approve_skill_promotion, "Promoted: "),
        PROPOSAL_TYPE_PATTERN_PROMOTION: (flywheel_cli._approve_pattern_promotion, "Promoted to T0: "),
        PROPOSAL_TYPE_PATTERN_DEMOTION: (flywheel_cli._approve_pattern_demotion, "Demoted from T0: "),
    }
    for ptype, (fn, prefix) in expect.items():
        row = flywheel_cli.PROPOSAL_HANDLERS[ptype]
        assert row.apply_callback is fn
        assert row.apply_label_prefix == prefix
    # Only skill_promotion declares a strict gate; only pattern_* declare reject.
    assert flywheel_cli.PROPOSAL_HANDLERS[PROPOSAL_TYPE_SKILL_PROMOTION].strict_gate is not None
    assert flywheel_cli.PROPOSAL_HANDLERS[PROPOSAL_TYPE_PATTERN_PROMOTION].reject_callback is not None
    assert flywheel_cli.PROPOSAL_HANDLERS[PROPOSAL_TYPE_PATTERN_DEMOTION].reject_callback is not None
    assert flywheel_cli.PROPOSAL_HANDLERS[PROPOSAL_TYPE_ROUTING_ADJUSTMENT].reject_callback is None


# ── source_patterns evidence-cluster field (Fork D) ──────────────────


def test_source_patterns_excluded_from_proposal_id() -> None:
    """CRITICAL: adding cluster lineage must NOT change a proposal's identity."""
    payload = {"rule": "downward", "add_intents": ["conversation"]}
    evidence = ("t1", "t2")
    bare = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        eval_hash="sha256:e", created_at="2026-06-15T00:00:00+00:00",
    )
    with_clusters = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=evidence,
        eval_hash="sha256:e", created_at="2026-06-15T00:00:00+00:00",
        source_patterns=("cluster:abc", "cluster:def"),
    )
    assert bare.proposal_id == with_clusters.proposal_id
    # compute_proposal_id does not even accept source_patterns as a kwarg.
    with pytest.raises(TypeError):
        compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload,
            evidence=evidence, source_patterns=("x",),
        )


def test_source_patterns_round_trips_through_queue(tmp_path) -> None:
    from grove.eval.proposal_queue import append, read_all

    queue = tmp_path / "proposals.jsonl"
    payload = {"rule": "downward", "add_intents": ["x"]}
    p = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=("t1",),
        ),
        type=PROPOSAL_TYPE_ROUTING_ADJUSTMENT, payload=payload, evidence=("t1",),
        eval_hash="", created_at="2026-06-15T00:00:00+00:00",
        source_patterns=("cluster:abc", "cluster:def"),
    )
    append(p, path=queue)
    loaded = read_all(path=queue)
    assert len(loaded) == 1
    assert loaded[0].source_patterns == ("cluster:abc", "cluster:def")
    assert loaded[0] == p


def test_source_patterns_defaults_empty_for_legacy_records(tmp_path) -> None:
    """A record written before the field existed loads with ``()`` — no break."""
    import json

    from grove.eval.proposal_queue import read_all

    queue = tmp_path / "proposals.jsonl"
    legacy = {
        "proposal_id": "sha256:legacy",
        "type": PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        "payload": {"rule": "downward", "add_intents": ["x"]},
        "evidence": ["t1"],
        "eval_hash": "",
        "created_at": "2026-06-15T00:00:00+00:00",
    }
    queue.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    loaded = read_all(path=queue)
    assert loaded[0].source_patterns == ()
