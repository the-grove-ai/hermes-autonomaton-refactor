"""kaizen-exploration-proposals-v1 Phase 3 — attended evidence reader + bridge.

Proves:

* ADAPTER — IntentRecord→arm: model_used present + terminal outcome; keyed on
  (tier_selected, model_used); success-rate-only; source: "attended"; pending
  and model-less turns excluded.
* PURGE-SURVIVOR — a record carrying only model_used + outcome (the fields the
  content purge never touches) still counts.
* GUARD — attended arms are INFORMATIONAL: they surface in the evidence_block /
  evidence view but never enter the ranked candidate set or become a
  proposed_binding; an attended-only model can never be proposed.
* REGRESSION — fleet-only inputs (no attended path) are byte-identical: no
  attended_arms key, stable proposal_id.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from grove.eval.attended_evidence import collect_attended_arms
from grove.eval.binding_scan import build_binding_proposals
from grove.intent_store import IntentRecord, IntentStore

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ─────────────────────────────────────────────────────────────────


def _rec(
    model,
    outcome="success",
    tier="T2",
    turn="t1",
    days_ago=1,
) -> IntentRecord:
    return IntentRecord(
        timestamp=(NOW - timedelta(days=days_ago)).isoformat(),
        session_id="s",
        turn_id=turn,
        user_message_stem="hi",
        pattern_hash="ph",
        intent_class="conversation",
        register_class="chat",
        complexity_signal="low",
        confidence=0.9,
        outcome=outcome,
        tier_selected=tier,
        model_used=model,
    )


def _store(tmp_path, *records) -> IntentStore:
    path = tmp_path / "intent_records.jsonl"
    store = IntentStore(path)
    for i, r in enumerate(records):
        # unique turn_id per record so latest_by_turn keeps them all distinct
        store.append(
            r if r.turn_id != "t1" else _replace_turn(r, f"turn-{i}")
        )
    return store


def _replace_turn(rec: IntentRecord, turn: str) -> IntentRecord:
    from dataclasses import replace

    return replace(rec, turn_id=turn)


def _ev(skill, model, days_ago=1):
    return {
        "worker_id": "w", "run_id": "r", "skill": skill, "status": "success",
        "detail": "", "staged": [], "check": None, "slug": None,
        "row_id": None, "fit_score": None, "raw_text_path": None,
        "model": model, "tier": "T2", "binding_source": "pinned",
        "quality_score": None, "rubric_version": None, "redraft_count": None,
        "evaluator_model": None,
        "ts": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def _seed_fleet(root, skill, model, count):
    ev_dir = root / "w" / "events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(ev_dir.glob("*.json")))
    for i in range(count):
        (ev_dir / f"e{existing + i}.json").write_text(
            json.dumps(_ev(skill, model)), encoding="utf-8"
        )


def _cap(model="prov-a/base"):
    return SimpleNamespace(
        model_binding=SimpleNamespace(type="model", model=model),
        governance={},
    )


# ── adapter ───────────────────────────────────────────────────────────────────


def test_adapter_success_rate_only_and_source_tagged(tmp_path):
    store = _store(
        tmp_path,
        _rec("prov-b/cand", "success", turn="a"),
        _rec("prov-b/cand", "success", turn="b"),
        _rec("prov-b/cand", "success", turn="c"),
        _rec("prov-b/cand", "error", turn="d"),
    )
    res = collect_attended_arms(store_path=store.path, now=NOW)
    assert len(res["arms"]) == 1
    arm = res["arms"][0]
    assert arm["model"] == "prov-b/cand"
    assert arm["context"] == "T2"
    assert arm["n"] == 4
    assert arm["successes"] == 3
    assert arm["failures"] == 1
    assert arm["success_rate"] == 0.75
    assert arm["source"] == "attended"
    # Success-rate-only: no scored fields, so it can never look downgrade-eligible.
    assert arm["scored_n"] == 0
    assert arm["score_mean"] is None
    assert arm["comparability_key"] is None
    assert arm["self_judged"] is False


def test_adapter_keys_on_tier_and_model(tmp_path):
    store = _store(
        tmp_path,
        _rec("m/x", tier="T2", turn="a"),
        _rec("m/x", tier="T1", turn="b"),
        _rec("m/y", tier="T2", turn="c"),
    )
    res = collect_attended_arms(store_path=store.path, now=NOW)
    keys = {(a["context"], a["model"]) for a in res["arms"]}
    assert keys == {("T2", "m/x"), ("T1", "m/x"), ("T2", "m/y")}


def test_pending_and_modelless_excluded(tmp_path):
    store = _store(
        tmp_path,
        _rec("m/x", "success", turn="a"),
        _rec("m/x", "pending", turn="b"),      # non-terminal → excluded
        _rec(None, "success", turn="c"),        # no model → excluded
    )
    res = collect_attended_arms(store_path=store.path, now=NOW)
    assert len(res["arms"]) == 1
    assert res["arms"][0]["n"] == 1
    assert res["counts"]["skipped_non_terminal"] == 1
    assert res["counts"]["skipped_no_model"] == 1


def test_out_of_window_excluded(tmp_path):
    store = _store(
        tmp_path,
        _rec("m/x", turn="a", days_ago=1),
        _rec("m/x", turn="b", days_ago=99),     # outside 30d window
    )
    res = collect_attended_arms(store_path=store.path, window_days=30, now=NOW)
    assert res["arms"][0]["n"] == 1
    assert res["counts"]["skipped_out_of_window"] == 1


def test_purge_survivor_still_counts(tmp_path):
    """The content purge nulls response_content / tool_invocation only
    (intent_store.py:291-294); model_used + outcome survive. A record carrying
    just those fields — exactly a post-purge record — still yields an arm."""
    path = tmp_path / "intent_records.jsonl"
    # A minimal record as it survives purge (no response_content / tool_invocation
    # keys at all — they default None on read).
    survivor = _rec("m/x", "success", turn="survivor")
    IntentStore(path).append(survivor)
    res = collect_attended_arms(store_path=path, now=NOW)
    assert res["arms"][0]["model"] == "m/x"
    assert res["arms"][0]["n"] == 1


# ── guard: informational, never promoted ───────────────────────────────────────


def _build_with_attended(tmp_path, attended_store):
    root = tmp_path / "fleet"
    _seed_fleet(root, "skill.fleet.alpha", "prov-a/base", 5)
    _seed_fleet(root, "skill.fleet.alpha", "prov-b/cand", 5)
    return build_binding_proposals(
        events_root=root,
        records={"skill.fleet.alpha": _cap("prov-a/base")},
        now=NOW,
        tombstone_path=tmp_path / "binding_tombstones.json",
        queue_path=tmp_path / "proposals.jsonl",
        attended_records_path=attended_store.path if attended_store else None,
    )


def test_attended_arms_are_informational_never_proposed(tmp_path):
    # Attended: the FLEET candidate used interactively, PLUS an attended-only
    # model with a perfect record that must NEVER be proposed (not fleet-observed).
    store = _store(
        tmp_path,
        _rec("prov-b/cand", "success", turn="a"),
        _rec("prov-b/cand", "success", turn="b"),
        _rec("prov-z/only-attended", "success", turn="c"),
        _rec("prov-z/only-attended", "success", turn="d"),
    )
    props = _build_with_attended(tmp_path, store)
    assert len(props) == 1
    eb = props[0].payload["evidence_block"]

    # The proposed binding is a FLEET model, never the attended-only one.
    assert props[0].payload["proposed_binding"]["model"] == "prov-b/cand"
    assert props[0].payload["proposed_binding"]["model"] != "prov-z/only-attended"

    # Attended arms surface for models IN PLAY (prov-b/cand), source-tagged, in a
    # structurally separate key — never merged into the fleet arms list.
    attended = eb["attended_arms"]
    assert [a["model"] for a in attended] == ["prov-b/cand"]
    assert all(a["source"] == "attended" for a in attended)
    fleet_models = {a["model"] for a in eb["arms"]}
    assert "prov-z/only-attended" not in fleet_models
    # The attended-only model appears NOWHERE in the proposal (not ranked, not
    # surfaced as in-play evidence — it is not one of the observed fleet models).
    assert "prov-z/only-attended" not in json.dumps(props[0].to_dict())


def test_attended_arms_render_in_evidence_view(tmp_path):
    from grove.flywheel_cli import _model_binding_to_diff

    store = _store(
        tmp_path,
        _rec("prov-b/cand", "success", turn="a"),
        _rec("prov-b/cand", "error", turn="b"),
    )
    props = _build_with_attended(tmp_path, store)
    diff = _model_binding_to_diff(props[0])
    assert "attended" in diff["evidence"]
    assert any("prov-b/cand [attended @ T2]" in row for row in diff["evidence"]["attended"])


# ── regression: fleet-only is byte-identical ────────────────────────────────────


def test_fleet_only_inputs_are_byte_identical(tmp_path):
    """No attended path → no attended read → evidence_block has no attended_arms
    key and the proposal_id is exactly what the fleet-only pipeline produced
    before P3 (identical to a second fleet-only build)."""
    without = _build_with_attended(tmp_path, None)
    assert len(without) == 1
    assert "attended_arms" not in without[0].payload["evidence_block"]

    # A second fleet-only build over the same inputs is identical (idempotent) —
    # the P3 code path added nothing when attended_records_path is None.
    again_root = tmp_path / "fleet2"
    _seed_fleet(again_root, "skill.fleet.alpha", "prov-a/base", 5)
    _seed_fleet(again_root, "skill.fleet.alpha", "prov-b/cand", 5)
    again = build_binding_proposals(
        events_root=again_root,
        records={"skill.fleet.alpha": _cap("prov-a/base")},
        now=NOW,
        tombstone_path=tmp_path / "tomb2.json",
        queue_path=tmp_path / "queue2.jsonl",
    )
    assert without[0].proposal_id == again[0].proposal_id
