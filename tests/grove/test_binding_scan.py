"""binding-telemetry-v1 P2 — binding scan producer + suppression tombstones.

Pin families:

* LADDER (R-B1) — success-only arms reach the parity class ONLY; downgrade
  requires scored + shared comparability key + non-self-judged (R-A2) +
  redraft parity (R-B3) + score/success at-or-above baseline.
* TOMBSTONES (R-B2) — written on rejection via the handler-row
  reject_callback; suppress re-filing; re-arm on each of the three material
  changes (new observed model explicit; binding-changed and rubric-bumped
  structural via the key).
* IDEMPOTENCY — identical evidence → identical proposal_id → queue dedup.
* HONEST NO-OPS — <2 observed models, n<threshold, unpinned baseline,
  unobserved baseline, pending proposal for the skill.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grove.eval import binding_scan
from grove.eval.binding_scan import (
    build_binding_proposals,
    record_tombstone,
)
from grove.eval.proposal_queue import PROPOSAL_TYPE_MODEL_BINDING

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
JUDGE = "prov-q/judge-1"


def _cap(model="prov-a/base", rubric="1.0"):
    gov = {}
    if rubric is not None:
        gov["quality_gate"] = {"rubric_version": rubric}
    return SimpleNamespace(
        model_binding=SimpleNamespace(type="model", model=model),
        governance=gov,
    )


def _ev(skill, model, status="success", score=None, judge=JUDGE, rubric="1.0",
        redrafted=0, days_ago=1):
    e = {
        "worker_id": "w", "run_id": "r", "skill": skill, "status": status,
        "detail": "", "staged": [], "check": None, "slug": None,
        "row_id": None, "fit_score": None, "raw_text_path": None,
        "model": model, "tier": "T2", "binding_source": "pinned",
        "quality_score": score,
        "rubric_version": rubric if score is not None else None,
        "redraft_count": redrafted if score is not None else None,
        "evaluator_model": judge if score is not None else None,
        "ts": (NOW - timedelta(days=days_ago)).isoformat(),
    }
    return e


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "fleet"
    (root / "w" / "events").mkdir(parents=True)
    return SimpleNamespace(
        root=root,
        queue=tmp_path / "proposals.jsonl",
        tombs=tmp_path / "binding_tombstones.json",
        seq=[0],
    )


def _seed(env, skill, model, count, **kw):
    for _ in range(count):
        env.seq[0] += 1
        p = env.root / "w" / "events" / f"e{env.seq[0]}.json"
        p.write_text(json.dumps(_ev(skill, model, **kw)), encoding="utf-8")


def _build(env, records):
    return build_binding_proposals(
        events_root=env.root, records=records, now=NOW,
        tombstone_path=env.tombs, queue_path=env.queue,
    )


SKILL = "skill.fleet.alpha"


# ── LADDER ───────────────────────────────────────────────────────────────────


def test_success_only_arms_reach_parity_class_only(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    props = _build(env, {SKILL: _cap()})
    assert len(props) == 1
    p = props[0]
    assert p.type == PROPOSAL_TYPE_MODEL_BINDING
    assert p.payload["skill"] == "alpha"
    assert p.payload["proposed_binding"] == {"type": "model", "model": "prov-b/cand"}
    assert p.payload["previous_binding"] == {"type": "model", "model": "prov-a/base"}
    assert p.payload["evidence_block"]["class"] == "parity"
    assert p.proposer == "binding_telemetry"


def test_scored_comparable_nonselfjudged_is_downgrade(env):
    _seed(env, SKILL, "prov-a/base", 5, score=0.7)
    _seed(env, SKILL, "prov-b/cand", 5, score=0.8)
    props = _build(env, {SKILL: _cap()})
    assert props[0].payload["evidence_block"]["class"] == "downgrade"
    arms = props[0].payload["evidence_block"]["arms"]
    assert arms[0]["model"] == "prov-a/base"  # baseline row first
    assert arms[1]["score_mean"] == 0.8


def test_self_judged_arm_caps_at_parity(env):
    """R-A2 — the candidate is judged by ITSELF: excluded from downgrade."""
    _seed(env, SKILL, "prov-a/base", 5, score=0.7)
    _seed(env, SKILL, "prov-b/cand", 5, score=0.9, judge="prov-b/cand")
    props = _build(env, {SKILL: _cap()})
    eb = props[0].payload["evidence_block"]
    assert eb["class"] == "parity"
    assert "self_judged:prov-b/cand" in eb["annotations"]


def test_mixed_judge_caps_at_parity(env):
    """R-A8 — a mixed-judge window has no comparable score; parity ceiling."""
    _seed(env, SKILL, "prov-a/base", 5, score=0.7)
    _seed(env, SKILL, "prov-b/cand", 3, score=0.9, rubric="1.0")
    _seed(env, SKILL, "prov-b/cand", 2, score=0.9, rubric="1.1")
    props = _build(env, {SKILL: _cap()})
    eb = props[0].payload["evidence_block"]
    assert eb["class"] == "parity"
    assert "mixed_judge:prov-b/cand" in eb["annotations"]
    # per-key groups ride the mixed arm row for honesty:
    mixed_row = [a for a in eb["arms"] if a["model"] == "prov-b/cand"][0]
    assert len(mixed_row["judge_groups"]) == 2


def test_redraft_disparity_caps_at_parity(env):
    """R-B3 — a candidate leaning on the redraft cycle is not comparable."""
    _seed(env, SKILL, "prov-a/base", 5, score=0.7, redrafted=0)
    _seed(env, SKILL, "prov-b/cand", 5, score=0.8, redrafted=1)
    props = _build(env, {SKILL: _cap()})
    assert props[0].payload["evidence_block"]["class"] == "parity"


def test_lower_scoring_candidate_with_equal_success_is_parity(env):
    _seed(env, SKILL, "prov-a/base", 5, score=0.9)
    _seed(env, SKILL, "prov-b/cand", 5, score=0.5)
    props = _build(env, {SKILL: _cap()})
    assert props[0].payload["evidence_block"]["class"] == "parity"


def test_worse_success_rate_candidate_files_nothing(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 4)
    _seed(env, SKILL, "prov-b/cand", 1, status="failed")
    props = _build(env, {SKILL: _cap()})
    assert props == []


# ── HONEST NO-OPS ────────────────────────────────────────────────────────────


def test_single_observed_model_no_proposal(env):
    _seed(env, SKILL, "prov-a/base", 10)
    assert _build(env, {SKILL: _cap()}) == []


def test_under_threshold_arm_no_proposal(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 4)  # n=4 < 5
    assert _build(env, {SKILL: _cap()}) == []


def test_unpinned_baseline_skipped(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    cap = SimpleNamespace(model_binding=None, governance={})
    assert _build(env, {SKILL: cap}) == []


def test_unobserved_baseline_skipped(env):
    """Pin points at a model with no qualifying arm — no honest comparison."""
    _seed(env, SKILL, "prov-a/old", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    assert _build(env, {SKILL: _cap(model="prov-c/never-ran")}) == []


def test_pending_proposal_not_stacked(env):
    from grove.eval.proposal_queue import append as _append

    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    first = _build(env, {SKILL: _cap()})
    assert len(first) == 1
    assert _append(first[0], path=env.queue)
    # more evidence arrives — the hash would differ, but a proposal PENDS:
    _seed(env, SKILL, "prov-b/cand", 1)
    assert _build(env, {SKILL: _cap()}) == []


# ── IDEMPOTENCY ──────────────────────────────────────────────────────────────


def test_identical_evidence_identical_id_and_queue_dedup(env):
    from grove.flywheel_cli import run_binding_scan

    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    a = _build(env, {SKILL: _cap()})
    b = _build(env, {SKILL: _cap()})
    assert a[0].proposal_id == b[0].proposal_id

    # through the CLI wrapper with the REAL registry-free path: monkeypatch not
    # needed — records param defaults to load_capabilities, so drive dedup at
    # the queue layer directly instead:
    from grove.eval.proposal_queue import append as _append

    assert _append(a[0], path=env.queue) is True
    assert _append(b[0], path=env.queue) is False  # deduped


# ── TOMBSTONES (R-B2) ────────────────────────────────────────────────────────


def _reject(env, proposal):
    entry = record_tombstone(proposal, path=env.tombs)
    assert entry is not None
    return entry


def test_rejection_writes_tombstone_and_suppresses(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    p = _build(env, {SKILL: _cap()})[0]
    entry = _reject(env, p)
    assert entry["skill"] == "alpha"
    assert entry["baseline_model"] == "prov-a/base"
    assert entry["proposed_model"] == "prov-b/cand"
    assert entry["rubric_version"] == "1.0"
    assert entry["observed_models"] == ["prov-a/base", "prov-b/cand"]
    # same evidence world → suppressed:
    assert _build(env, {SKILL: _cap()}) == []
    # even with MORE runs of the SAME models (no time cooldown, no delta):
    _seed(env, SKILL, "prov-b/cand", 3)
    assert _build(env, {SKILL: _cap()}) == []


def test_rearm_on_new_observed_model(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    _reject(env, _build(env, {SKILL: _cap()})[0])
    _seed(env, SKILL, "prov-c/new", 1)  # ANY observation of a new model
    props = _build(env, {SKILL: _cap()})
    assert len(props) == 1  # re-armed


def test_rearm_on_binding_change(env):
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    _reject(env, _build(env, {SKILL: _cap()})[0])
    # operator rebinds to prov-b/cand; later evidence argues back the other way
    _seed(env, SKILL, "prov-a/base", 1)
    props = _build(env, {SKILL: _cap(model="prov-b/cand")})
    assert len(props) == 1
    assert props[0].payload["proposed_binding"]["model"] == "prov-a/base"


def test_rearm_on_rubric_bump(env):
    _seed(env, SKILL, "prov-a/base", 5, score=0.7)
    _seed(env, SKILL, "prov-b/cand", 5, score=0.8)
    _reject(env, _build(env, {SKILL: _cap()})[0])
    assert _build(env, {SKILL: _cap()}) == []          # suppressed on 1.0
    props = _build(env, {SKILL: _cap(rubric="2.0")})   # rubric bumped
    assert len(props) == 1


def test_reject_callback_wired_on_handler_row(env):
    """cli_reject's registry dispatch reaches record_tombstone (the
    pattern_promotion precedent, ninth-row wiring)."""
    from grove.flywheel_cli import PROPOSAL_HANDLERS
    from grove.eval.proposal_queue import PROPOSAL_TYPE_MODEL_BINDING as T

    handler = PROPOSAL_HANDLERS[T]
    assert handler.reject_callback is not None

    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    p = _build(env, {SKILL: _cap()})[0]
    import grove.eval.binding_scan as bs

    calls = {}
    orig = bs.record_tombstone

    def spy(proposal, **kw):
        calls["proposal"] = proposal
        return orig(proposal, path=env.tombs)

    import unittest.mock as mock

    with mock.patch.object(bs, "record_tombstone", spy):
        handler.reject_callback(p)
    assert calls["proposal"] is p
    assert json.loads(env.tombs.read_text())["tombstones"][0]["skill"] == "alpha"


def test_handfiled_proposal_without_evidence_block_tombstones(env):
    p = SimpleNamespace(
        proposal_id="sha256:x",
        payload={
            "skill": "alpha",
            "proposed_binding": {"type": "model", "model": "prov-b/cand"},
            "previous_binding": {"type": "model", "model": "prov-a/base"},
        },
    )
    entry = record_tombstone(p, path=env.tombs)
    assert entry["observed_models"] is None  # unknown set: only key-mismatch re-arms
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    # rubric key mismatch (tombstone has None, record declares "1.0") → files:
    assert len(_build(env, {SKILL: _cap(rubric="1.0")})) == 1
    # matching rubric=None world → suppressed despite unknown observed set:
    assert _build(env, {SKILL: _cap(rubric=None)}) == []


def test_unreadable_tombstone_store_fails_open_loud(env, caplog):
    env.tombs.write_text("{broken", encoding="utf-8")
    _seed(env, SKILL, "prov-a/base", 5)
    _seed(env, SKILL, "prov-b/cand", 5)
    with caplog.at_level("WARNING"):
        props = _build(env, {SKILL: _cap()})
    assert len(props) == 1  # suppresses nothing rather than everything
    assert any("tombstone store unreadable" in r.message for r in caplog.records)
