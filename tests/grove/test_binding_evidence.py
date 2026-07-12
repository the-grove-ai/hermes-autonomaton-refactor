"""binding-telemetry-v1 P1 — binding evidence reader pins.

Pin families:

* ARM MATH — n / success_rate over success+failed runs; score mean /
  population variance / redraft_rate over scored successes.
* EXCLUSION FLAGS — self_judged (R-A2), family_judged (R-B5).
* MIXED JUDGE (R-A8) — multiple (rubric_version, evaluator_model) keys →
  top-level score fields None, per-key judge_groups retained, never averaged.
* WINDOW — out-of-window events excluded; boundary honored.
* TOLERANCE — malformed events (bad JSON / non-mapping / bad ts) are skipped
  loud-logged and counted; the LIVE pre-rider legacy shape (no quality keys)
  counts toward success_rate and contributes no score; no_work and
  model-null events excluded and counted.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from grove.kaizen.binding_evidence import collect_arms

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _ev(
    skill="skill.fleet.alpha",
    status="success",
    model="prov-a/model-1",
    ts=None,
    quality_score=None,
    rubric_version=None,
    redraft_count=None,
    evaluator_model=None,
    **extra,
):
    e = {
        "worker_id": "w1",
        "run_id": "r",
        "skill": skill,
        "status": status,
        "detail": "",
        "staged": [],
        "check": None,
        "slug": None,
        "row_id": None,
        "fit_score": None,
        "raw_text_path": None,
        "model": model,
        "tier": "T2",
        "binding_source": "pinned",
        "quality_score": quality_score,
        "rubric_version": rubric_version,
        "redraft_count": redraft_count,
        "evaluator_model": evaluator_model,
        "ts": (ts or NOW - timedelta(days=1)).isoformat(),
    }
    e.update(extra)
    return e


# The VERBATIM pre-quality-rider legacy shape (live on the VM, Jul 10): the
# binding rider is present, the four quality keys are ABSENT ENTIRELY.
def _legacy_ev(status="success", model="prov-a/model-1"):
    return {
        "worker_id": "w1",
        "run_id": "legacy",
        "skill": "skill.fleet.alpha",
        "status": status,
        "detail": "completed=True; slug=x; transport=tool",
        "staged": ["/x"],
        "check": None,
        "slug": "x",
        "row_id": None,
        "fit_score": None,
        "raw_text_path": None,
        "model": model,
        "tier": "T2",
        "binding_source": "pinned",
        "ts": (NOW - timedelta(days=2)).isoformat(),
    }


@pytest.fixture()
def events_root(tmp_path):
    root = tmp_path / "fleet"
    (root / "w1" / "events").mkdir(parents=True)
    return root


def _write(root, name, event, worker="w1"):
    d = root / worker / "events"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(
        event if isinstance(event, str) else json.dumps(event), encoding="utf-8"
    )
    return p


def _arm(res, skill, model):
    m = [a for a in res["arms"] if a["skill"] == skill and a["model"] == model]
    assert len(m) == 1, res["arms"]
    return m[0]


# ── ARM MATH ─────────────────────────────────────────────────────────────────


def test_success_rate_and_score_math(events_root):
    for i, (status, score, redrafted) in enumerate(
        [("success", 0.8, 0), ("success", 0.6, 1), ("success", None, None),
         ("failed", None, None)]
    ):
        _write(events_root, f"e{i}", _ev(
            status=status, quality_score=score, redraft_count=redrafted,
            rubric_version="1.0" if score is not None else None,
            evaluator_model="prov-q/judge-1" if score is not None else None,
        ))
    res = collect_arms(events_root=events_root, now=NOW)
    a = _arm(res, "skill.fleet.alpha", "prov-a/model-1")
    assert a["n"] == 4 and a["successes"] == 3 and a["failures"] == 1
    assert a["success_rate"] == 0.75
    assert a["scored_n"] == 2
    assert a["score_mean"] == 0.7            # (0.8 + 0.6) / 2
    assert a["score_variance"] == 0.01       # population variance
    assert a["redraft_rate"] == 0.5
    assert a["comparability_key"] == ["1.0", "prov-q/judge-1"]
    assert a["mixed_judge"] is False
    assert res["counts"]["runs_counted"] == 4


def test_arms_keyed_per_skill_and_model(events_root):
    _write(events_root, "a1", _ev(model="prov-a/model-1"))
    _write(events_root, "a2", _ev(model="prov-a/model-2"))
    _write(events_root, "b1", _ev(skill="skill.fleet.beta", model="prov-a/model-1"),
           worker="w2")
    res = collect_arms(events_root=events_root, now=NOW)
    assert len(res["arms"]) == 3
    assert _arm(res, "skill.fleet.alpha", "prov-a/model-2")["n"] == 1
    assert _arm(res, "skill.fleet.beta", "prov-a/model-1")["n"] == 1


# ── EXCLUSION FLAGS ──────────────────────────────────────────────────────────


def test_self_judged_flag(events_root):
    _write(events_root, "s1", _ev(
        model="prov-a/model-1", quality_score=0.9, rubric_version="1.0",
        redraft_count=0, evaluator_model="prov-a/model-1",
    ))
    a = _arm(collect_arms(events_root=events_root, now=NOW),
             "skill.fleet.alpha", "prov-a/model-1")
    assert a["self_judged"] is True
    assert a["family_judged"] is False
    assert a["judge_groups"][0]["self_judged"] is True


def test_family_judged_flag(events_root):
    _write(events_root, "f1", _ev(
        model="prov-a/model-1", quality_score=0.9, rubric_version="1.0",
        redraft_count=0, evaluator_model="prov-a/model-9",
    ))
    a = _arm(collect_arms(events_root=events_root, now=NOW),
             "skill.fleet.alpha", "prov-a/model-1")
    assert a["family_judged"] is True and a["self_judged"] is False


def test_cross_provider_judge_neither_flag(events_root):
    _write(events_root, "c1", _ev(
        quality_score=0.9, rubric_version="1.0", redraft_count=0,
        evaluator_model="prov-q/judge-1",
    ))
    a = _arm(collect_arms(events_root=events_root, now=NOW),
             "skill.fleet.alpha", "prov-a/model-1")
    assert a["self_judged"] is False and a["family_judged"] is False


# ── MIXED JUDGE (R-A8) ───────────────────────────────────────────────────────


def test_mixed_judge_never_averaged(events_root):
    _write(events_root, "m1", _ev(
        quality_score=0.9, rubric_version="1.0", redraft_count=0,
        evaluator_model="prov-q/judge-1",
    ))
    _write(events_root, "m2", _ev(
        quality_score=0.3, rubric_version="1.1", redraft_count=0,
        evaluator_model="prov-q/judge-1",
    ))
    a = _arm(collect_arms(events_root=events_root, now=NOW),
             "skill.fleet.alpha", "prov-a/model-1")
    assert a["mixed_judge"] is True
    assert a["score_mean"] is None and a["score_variance"] is None
    assert a["comparability_key"] is None and a["redraft_rate"] is None
    assert len(a["judge_groups"]) == 2
    means = {g["rubric_version"]: g["score_mean"] for g in a["judge_groups"]}
    assert means == {"1.0": 0.9, "1.1": 0.3}   # per-key stats retained verbatim
    assert a["scored_n"] == 2


def test_judge_swap_is_also_mixed(events_root):
    _write(events_root, "j1", _ev(
        quality_score=0.9, rubric_version="1.0", redraft_count=0,
        evaluator_model="prov-q/judge-1",
    ))
    _write(events_root, "j2", _ev(
        quality_score=0.8, rubric_version="1.0", redraft_count=0,
        evaluator_model="prov-q/judge-2",
    ))
    a = _arm(collect_arms(events_root=events_root, now=NOW),
             "skill.fleet.alpha", "prov-a/model-1")
    assert a["mixed_judge"] is True and a["score_mean"] is None


# ── WINDOW ───────────────────────────────────────────────────────────────────


def test_window_filter(events_root):
    _write(events_root, "in", _ev(ts=NOW - timedelta(days=29)))
    _write(events_root, "out", _ev(ts=NOW - timedelta(days=31)))
    res = collect_arms(events_root=events_root, now=NOW, window_days=30)
    assert _arm(res, "skill.fleet.alpha", "prov-a/model-1")["n"] == 1
    assert res["counts"]["skipped_out_of_window"] == 1
    assert res["arms"][0]["window"]["days"] == 30


# ── TOLERANCE ────────────────────────────────────────────────────────────────


def test_malformed_events_skipped_loud_never_crash(events_root, caplog):
    _write(events_root, "bad-json", "{not json")
    _write(events_root, "non-mapping", json.dumps(["a", "list"]))
    _write(events_root, "bad-ts", _ev(ts=None) | {"ts": "not-a-timestamp"})
    _write(events_root, "ok", _ev())
    with caplog.at_level("WARNING"):
        res = collect_arms(events_root=events_root, now=NOW)
    assert res["counts"]["skipped_malformed"] == 3
    assert _arm(res, "skill.fleet.alpha", "prov-a/model-1")["n"] == 1
    assert sum("binding_evidence" in r.name for r in caplog.records) >= 3


def test_pre_rider_legacy_shape_counts_scoreless(events_root):
    """The LIVE Jul-10 pre-rider shape verbatim: quality keys ABSENT — counts
    toward the success_rate arm, contributes no score."""
    _write(events_root, "legacy", _legacy_ev())
    _write(events_root, "legacy-fail", _legacy_ev(status="failed"))
    res = collect_arms(events_root=events_root, now=NOW)
    a = _arm(res, "skill.fleet.alpha", "prov-a/model-1")
    assert a["n"] == 2 and a["success_rate"] == 0.5
    assert a["scored_n"] == 0
    assert a["score_mean"] is None and a["judge_groups"] == []
    assert a["mixed_judge"] is False


def test_no_work_and_null_model_excluded(events_root):
    _write(events_root, "nw", _ev(status="no_work", model=None))
    _write(events_root, "nm", _ev(status="failed", model=None))
    _write(events_root, "ok", _ev())
    res = collect_arms(events_root=events_root, now=NOW)
    assert len(res["arms"]) == 1
    assert res["counts"]["skipped_non_run"] == 1
    assert res["counts"]["skipped_unattributed"] == 1


def test_empty_root_and_absent_root(tmp_path):
    res = collect_arms(events_root=tmp_path / "nope", now=NOW)
    assert res["arms"] == [] and res["counts"]["events_seen"] == 0
