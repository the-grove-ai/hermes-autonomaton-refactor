"""Binding-telemetry scan producer (binding-telemetry-v1 P2).

Fourth INDEPENDENT Flywheel signal (the fault-triage coexistence pattern):
aggregates the fleet worker event stream (grove.kaizen.binding_evidence) into
per-skill × per-model evidence arms and files ``model_binding`` proposals
through the SHIPPED carrier (binding-governance-surfaces-v1: payload
``{skill, proposed_binding, previous_binding}`` → operator approve →
``set_model_binding``) when observed cross-model evidence clears the
precision-first thresholds.

EVIDENCE-CLASS LADDER (R-B1):
  * ``parity``    — success_rate arms only; the ceiling for score-less
                    evidence. Informational rebind argument, operator judges.
  * ``downgrade`` — requires score evidence: BOTH arms scored on the SAME
                    sole comparability key (rubric_version, evaluator_model),
                    NEITHER self-judged (R-A2), redraft-rate parity within
                    tolerance (R-B3), candidate score AND success_rate at or
                    above baseline.
Observed-only: a skill proposes nothing until it has RUN on ≥2 models within
the window (never suggests untried models). Baseline is the record's CURRENT
pin; an unpinned skill is skipped (v1: baseline ambiguity is not guessed at).

SUPPRESSION TOMBSTONES (R-B2): written on rejection disposition (the
``reject_callback`` registry hook — the pattern_promotion tombstone
precedent), keyed ``(skill, baseline_model, proposed_model, rubric_version)``.
Binding-changed and rubric-bumped re-arms are STRUCTURAL (the key no longer
matches); a new observed model re-arms explicitly against the stored observed
set. No time cooldown, no significance delta.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_MODEL_BINDING,
    RoutingProposal,
    compute_proposal_id,
    default_queue_path,
    read_all,
)

logger = logging.getLogger(__name__)

# Precision-first thresholds (pattern_cache.promotion / fault-detector family).
MIN_ARM_N = 5
WINDOW_DAYS = 30

# R-B3 — redraft-rate parity tolerance for the downgrade class: arms whose
# redraft rates differ by more than this are not honestly comparable (one
# model is leaning on the redraft cycle to reach its scores). 0.2 == exactly
# one redraft of difference at the MIN_ARM_N=5 floor (1/5) — the smallest
# observable disparity at the smallest admissible arm. Tighten in step with
# any future n-floor rise (operator-ratified 2026-07-12).
REDRAFT_PARITY_TOLERANCE = 0.2

_TOMBSTONE_FILENAME = "binding_tombstones.json"


def default_tombstone_path() -> Path:
    """Beside the proposal queue (the SPEC's 'beside' is literal)."""
    return default_queue_path().with_name(_TOMBSTONE_FILENAME)


# ── tombstone store ─────────────────────────────────────────────────────


def _load_tombstones(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or default_tombstone_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Fail LOUD but never crash a rejection or a scan on one bad store
        # file — an unreadable store suppresses nothing (proposals may
        # re-surface; the operator re-rejects) rather than suppressing
        # everything.
        logger.warning(
            "[binding_scan] tombstone store unreadable at %s (%s) — treating "
            "as empty; rejected proposals may re-surface until the store is "
            "repaired", p, exc,
        )
        return []
    entries = data.get("tombstones") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _write_tombstones(entries: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    p = path or default_tombstone_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"tombstones": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def record_tombstone(proposal: Any, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The rejection-disposition hook (R-B2) — called by the model_binding
    handler row's ``reject_callback`` BEFORE queue removal.

    Keys the tombstone on (skill, baseline_model, proposed_model,
    rubric_version) from the proposal payload. A hand-filed carrier proposal
    without an evidence_block still tombstones (the operator rejected THAT
    rebind); its observed set is stored as None = unknown → the new-observed-
    model re-arm cannot fire for it (key-mismatch re-arms still do). Returns
    the entry, or None when the payload cannot identify a rebind (logged).
    """
    payload = getattr(proposal, "payload", None) or {}
    skill = payload.get("skill")
    proposed = (payload.get("proposed_binding") or {}).get("model")
    eb = payload.get("evidence_block") or {}
    baseline = eb.get("baseline_model") or (
        (payload.get("previous_binding") or {}).get("model")
    )
    if not skill or not proposed:
        logger.warning(
            "[binding_scan] rejected model_binding proposal %s carries no "
            "identifiable (skill, proposed model) — no tombstone written",
            getattr(proposal, "proposal_id", "?"),
        )
        return None
    entry = {
        "skill": skill,
        "baseline_model": baseline,
        "proposed_model": proposed,
        "rubric_version": eb.get("rubric_version"),
        "observed_models": (
            sorted(eb["observed_models"]) if eb.get("observed_models") else None
        ),
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "proposal_id": getattr(proposal, "proposal_id", None),
    }
    entries = _load_tombstones(path)
    entries.append(entry)
    _write_tombstones(entries, path)
    logger.info(
        "[binding_scan] tombstone recorded: %s %s→%s (rubric %s)",
        skill, baseline, proposed, entry["rubric_version"],
    )
    return entry


def _suppressed(
    tombstones: List[Dict[str, Any]],
    *,
    skill: str,
    baseline_model: Optional[str],
    proposed_model: str,
    rubric_version: Optional[str],
    observed_models: List[str],
) -> bool:
    """R-B2 suppression check. Binding-changed / rubric-bumped re-arm is
    structural (the 4-key no longer matches). New-observed-model re-arm:
    the CURRENT observed set contains a model the tombstone never saw."""
    for t in tombstones:
        if (
            t.get("skill") == skill
            and t.get("baseline_model") == baseline_model
            and t.get("proposed_model") == proposed_model
            and t.get("rubric_version") == rubric_version
        ):
            seen = t.get("observed_models")
            if seen is None:
                return True  # unknown set — only key-mismatch re-arms
            if set(observed_models) - set(seen):
                logger.info(
                    "[binding_scan] tombstone re-armed for %s %s→%s — new "
                    "observed model(s): %s",
                    skill, baseline_model, proposed_model,
                    sorted(set(observed_models) - set(seen)),
                )
                return False
            return True
    return False


# ── evidence-class ladder ───────────────────────────────────────────────


def _classify(baseline: Dict[str, Any], cand: Dict[str, Any]) -> Optional[str]:
    """R-B1 ladder for one candidate arm against the baseline arm. Both arms
    already cleared MIN_ARM_N. Returns 'downgrade' | 'parity' | None."""
    scored = (
        baseline["comparability_key"] is not None
        and cand["comparability_key"] is not None
        and baseline["comparability_key"] == cand["comparability_key"]
        and not baseline["self_judged"]
        and not cand["self_judged"]
    )
    if scored:
        redraft_parity = (
            abs(cand["redraft_rate"] - baseline["redraft_rate"])
            <= REDRAFT_PARITY_TOLERANCE
        )
        if (
            redraft_parity
            and cand["score_mean"] >= baseline["score_mean"]
            and cand["success_rate"] >= baseline["success_rate"]
        ):
            return "downgrade"
    # Success-only ceiling (R-B1): parity, never downgrade, regardless of why
    # score comparability failed (no gate, mixed judge, self-judged, redraft
    # disparity).
    if cand["success_rate"] >= baseline["success_rate"]:
        return "parity"
    return None


def _annotations(arms: List[Dict[str, Any]]) -> List[str]:
    notes: List[str] = []
    for a in arms:
        if a["self_judged"]:
            notes.append(f"self_judged:{a['model']}")
        if a["family_judged"]:
            notes.append(f"family_judged:{a['model']}")
        if a["mixed_judge"]:
            notes.append(f"mixed_judge:{a['model']}")
    return notes


def _arm_row(a: Dict[str, Any]) -> Dict[str, Any]:
    """The evidence-block arm row — the reader's arm minus the per-key
    subgroup detail (which rides only when mixed, for honesty)."""
    row = {
        "model": a["model"],
        "n": a["n"],
        "success_rate": a["success_rate"],
        "scored_n": a["scored_n"],
        "score_mean": a["score_mean"],
        "score_variance": a["score_variance"],
        "redraft_rate": a["redraft_rate"],
        "comparability_key": a["comparability_key"],
        "self_judged": a["self_judged"],
        "family_judged": a["family_judged"],
        "mixed_judge": a["mixed_judge"],
    }
    if a["mixed_judge"]:
        row["judge_groups"] = a["judge_groups"]
    return row


def _attended_arm_row(a: Dict[str, Any]) -> Dict[str, Any]:
    """The INFORMATIONAL attended-evidence row (kaizen-exploration-proposals-v1
    P3) — success-rate-only, ``source``-tagged, and STRUCTURALLY separate from
    the fleet ``arms`` rows. It never enters candidate ranking; it is surfaced so
    the operator sees interactive signal alongside the fleet-observed evidence,
    honestly marked, never conflated."""
    return {
        "model": a.get("model"),
        "context": a.get("context"),
        "n": a.get("n"),
        "success_rate": a.get("success_rate"),
        "source": "attended",
    }


# ── producer ────────────────────────────────────────────────────────────


def build_binding_proposals(
    *,
    events_root: Optional[Path] = None,
    records: Optional[Dict[str, Any]] = None,
    window_days: int = WINDOW_DAYS,
    now: Optional[datetime] = None,
    tombstone_path: Optional[Path] = None,
    queue_path: Optional[Path] = None,
    attended_records_path: Optional[Path] = None,
) -> List[RoutingProposal]:
    """Aggregate → ladder → at most ONE proposal per skill per scan.

    Honest no-ops (each logged at INFO): <2 observed models, arms under
    MIN_ARM_N, unpinned baseline, baseline model unobserved at threshold,
    no candidate clears the ladder, tombstone-suppressed, or a model_binding
    proposal for the skill is already pending (evidence keeps accruing; the
    queue is not a changelog).
    """
    from grove.kaizen.binding_evidence import collect_arms

    if records is None:
        from grove.capability_registry import load_capabilities

        records = load_capabilities()

    res = collect_arms(events_root=events_root, window_days=window_days, now=now)
    by_skill: Dict[str, List[Dict[str, Any]]] = {}
    for arm in res["arms"]:
        by_skill.setdefault(arm["skill"], []).append(arm)

    tombstones = _load_tombstones(tombstone_path)
    pending_skills = {
        (p.payload or {}).get("skill")
        for p in read_all(path=queue_path)
        if getattr(p, "type", None) == PROPOSAL_TYPE_MODEL_BINDING
    }

    # kaizen-exploration-proposals-v1 P3 — the PARALLEL attended reader. OPT-IN:
    # only read when a store path is provided (production passes it via
    # run_binding_scan); ``None`` keeps fleet-only callers byte-identical (the
    # ``attended_arms`` key is never added, so evidence_hash / proposal_id are
    # unchanged). Resilient: a reader fault logs loud and degrades to no attended
    # context rather than WITHHOLDING the fleet proposal — attended evidence is
    # informational, never load-bearing for the model_binding decision.
    attended_arms: List[Dict[str, Any]] = []
    if attended_records_path is not None:
        try:
            from grove.eval.attended_evidence import collect_attended_arms

            attended_arms = collect_attended_arms(
                store_path=attended_records_path,
                window_days=window_days,
                now=now,
            )["arms"]
        except Exception as exc:  # noqa: BLE001 — informational leg, never withhold
            logger.warning(
                "[binding_scan] attended-evidence read failed (%r) — proceeding "
                "with fleet-only evidence", exc,
            )
            attended_arms = []

    proposals: List[RoutingProposal] = []
    for skill_id, arms in sorted(by_skill.items()):
        observed_models = sorted({a["model"] for a in arms})
        if len(observed_models) < 2:
            logger.info(
                "[binding_scan] %s: insufficient cross-model evidence "
                "(observed models: %s) — no proposal", skill_id, observed_models,
            )
            continue
        qualified = [a for a in arms if a["n"] >= MIN_ARM_N]
        if len(qualified) < 2:
            logger.info(
                "[binding_scan] %s: fewer than two arms at n>=%d — no proposal",
                skill_id, MIN_ARM_N,
            )
            continue

        cap = records.get(skill_id)
        if cap is None:
            logger.info(
                "[binding_scan] %s: no capability record loads — no proposal",
                skill_id,
            )
            continue
        mb = getattr(cap, "model_binding", None)
        if mb is None or mb.type != "model" or not mb.model:
            logger.info(
                "[binding_scan] %s: unpinned (tier-inherited) baseline is "
                "ambiguous — v1 proposes against pins only", skill_id,
            )
            continue
        baseline_model = mb.model
        baseline_arm = next(
            (a for a in qualified if a["model"] == baseline_model), None
        )
        if baseline_arm is None:
            logger.info(
                "[binding_scan] %s: current pin %s has no arm at n>=%d — no "
                "honest comparison", skill_id, baseline_model, MIN_ARM_N,
            )
            continue

        skill_name = skill_id.rsplit(".", 1)[-1]  # the canonical slug tail
        if skill_name in pending_skills or skill_id in pending_skills:
            logger.info(
                "[binding_scan] %s: a model_binding proposal is already "
                "pending — not stacking another", skill_id,
            )
            continue

        # Rank candidates: downgrade class first, then the stronger evidence.
        ranked = []
        for cand in qualified:
            if cand["model"] == baseline_model:
                continue
            cls = _classify(baseline_arm, cand)
            if cls is None:
                continue
            ranked.append((
                0 if cls == "downgrade" else 1,
                -(cand["score_mean"] if cand["score_mean"] is not None else -1.0),
                -cand["success_rate"],
                cand["model"],
                cls,
                cand,
            ))
        if not ranked:
            logger.info(
                "[binding_scan] %s: no candidate clears the evidence ladder — "
                "no proposal", skill_id,
            )
            continue
        ranked.sort()
        _, _, _, _, cls, cand = ranked[0]

        rubric_version = None
        gov = getattr(cap, "governance", None)
        if isinstance(gov, dict):
            gate = gov.get("quality_gate")
            if isinstance(gate, dict) and not gov.get("quality_gate_error"):
                rubric_version = gate.get("rubric_version")

        if _suppressed(
            tombstones,
            skill=skill_name,
            baseline_model=baseline_model,
            proposed_model=cand["model"],
            rubric_version=rubric_version,
            observed_models=observed_models,
        ):
            logger.info(
                "[binding_scan] %s: %s→%s suppressed by rejection tombstone "
                "(no material change) — no proposal",
                skill_id, baseline_model, cand["model"],
            )
            continue

        evidence_block = {
            "class": cls,
            "window_days": window_days,
            "baseline_model": baseline_model,
            "rubric_version": rubric_version,
            "observed_models": observed_models,
            "arms": [_arm_row(baseline_arm)]
            + [_arm_row(a) for a in qualified if a["model"] != baseline_model],
            "annotations": _annotations(qualified),
        }
        # P3 — attach INFORMATIONAL attended arms for the models in play, in a
        # STRUCTURALLY separate key (never merged into the fleet ``arms`` list,
        # never seen by _classify or the ranked candidate set above). Present-key
        # only: when there is no attended signal for these models the key is
        # absent, so a fleet-only evidence_block is byte-identical to pre-P3.
        attended_rows = [
            _attended_arm_row(a)
            for a in attended_arms
            if a.get("model") in observed_models
        ]
        if attended_rows:
            evidence_block["attended_arms"] = attended_rows
        evidence_hash = hashlib.sha256(
            json.dumps(evidence_block, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

        payload = {
            # The carrier's apply path resolves the SLUG TAIL through
            # resolve_skill_record (flywheel_cli._approve_model_binding).
            "skill": skill_name,
            "proposed_binding": {"type": "model", "model": cand["model"]},
            "previous_binding": {"type": "model", "model": baseline_model},
            "evidence_block": evidence_block,
        }
        evidence = (skill_name, evidence_hash)
        proposals.append(
            RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_MODEL_BINDING,
                    payload=payload,
                    evidence=evidence,
                ),
                type=PROPOSAL_TYPE_MODEL_BINDING,
                payload=payload,
                evidence=evidence,
                eval_hash="",
                created_at=datetime.now(timezone.utc).isoformat(),
                proposer="binding_telemetry",
            )
        )
    return proposals
