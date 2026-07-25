"""grove/fleet/rubric_registry.py — the versioned quality-gate rubric
registry loader (artifact-review-v1 P1).

A fleet capability record's ``governance.quality_gate`` names a rubric by
``rubric_ref`` (e.g. ``"resume-package@1"``); this module loads
``config/rubrics.yaml`` and resolves that reference to its criteria and
calibrated ``default_threshold``. The rubric is keyed on the ARTIFACT CLASS,
not on the producer — no producer name appears here or in the registry.

Loader shape mirrors the house declarative-config idiom EXACTLY
(:func:`grove.eval.producer_recurrence.load_producer_resilience_thresholds`,
:func:`grove.eval.fault_triage.load_fault_triage_thresholds`,
:func:`grove.ledger_retention.load_retention_config`):

  * absent file / absent ``rubrics`` block  -> frozen-dataclass default
    (an EMPTY registry), silent;
  * a present block is validated key-by-key, and any malformed value raises
    a bare ``ValueError``, LOUD;
  * hand-rolled ``isinstance`` validation, no schema library;
  * uncached — read on demand, so a git-authored edit is live on next call.

Path resolution differs from those witnesses BY DESIGN: they read the
operator's GROVE_HOME ``flywheel.config.yaml``; this registry is a REPO
config surface, resolved like ``config/zones.schema.yaml``
(``Path(__file__).resolve().parents[2] / "config" / "rubrics.yaml"``;
cf. grove/red_policy.py, grove/zones.py, grove/capability_registry.py). The
idiom governs the loader's SHAPE, not its path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "Criterion",
    "Rubric",
    "RubricRegistry",
    "compute_content_hash",
    "load_rubric_registry",
]

# The only criterion type Phase 1 supports. A criterion declares HOW it is
# evaluated; today that is always a model judgment. Later phases may add a
# deterministic type — the typed-object shape is what leaves that door open.
_SUPPORTED_CRITERION_TYPES = frozenset({"llm"})


@dataclass(frozen=True)
class Criterion:
    """One rubric criterion: a stable id, a type, and its definition text.

    ``id`` is an identity the verdict record cites, not a display label.
    ``type`` is ``"llm"`` in Phase 1 (a model judgment).
    """

    id: str
    type: str
    definition: str


@dataclass(frozen=True)
class Rubric:
    """A resolved rubric: its ``class@version`` key, calibrated default
    threshold, ordered criteria, and the verified content hash. A record MAY
    override the threshold; the default here is the rubric's own calibration."""

    key: str
    default_threshold: float
    criteria: List[Criterion]
    content_hash: str


def compute_content_hash(default_threshold: float, criteria: List[Criterion]) -> str:
    """artifact-review-v1 R-14 — the content hash over a rubric's MEANING:
    its ``default_threshold`` and its ordered criteria (id/type/definition).

    Canonical serialization is deterministic and explicit so a published
    version's hash is reproducible forever: a compact JSON object with sorted
    keys, criteria in declared order (order is meaning — ids are cited), and
    UTF-8 encoding. Any change to a criterion's text, id, type, order, or the
    default threshold changes the hash. This is what makes ``@1`` immutable:
    the loader verifies the declared hash against this recomputation and fails
    loud on mismatch, so the only path to different criteria is minting ``@2``
    — mint, never mutate (R-6 stands; versions still change, meanings do not).
    """
    payload = json.dumps(
        {
            "default_threshold": float(default_threshold),
            "criteria": [
                {"id": c.id, "type": c.type, "definition": c.definition}
                for c in criteria
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RubricRegistry:
    """The loaded registry: ``class@version`` -> :class:`Rubric`. The default
    is EMPTY — an absent file or absent block yields a registry that resolves
    no reference, so a declared ``rubric_ref`` fails loud at its consumer
    rather than silently degrading (the fail-closed floor)."""

    rubrics: Dict[str, Rubric] = field(default_factory=dict)

    def get(self, key: str) -> Optional[Rubric]:
        """Return the rubric for *key*, or ``None`` if unknown."""
        return self.rubrics.get(key)

    def resolve(self, key: str) -> Rubric:
        """Return the rubric for *key*, or raise ``KeyError`` LOUD naming the
        known keys — the unknown-tier discipline
        (``CognitiveRouter.get_tier_config``), no fallback rubric."""
        try:
            return self.rubrics[key]
        except KeyError:
            known = ", ".join(sorted(self.rubrics)) or "(none)"
            raise KeyError(
                f"unknown rubric_ref {key!r}; config/rubrics.yaml declares: {known}"
            ) from None


def _default_registry_path() -> Path:
    """Resolve the repo ``config/rubrics.yaml`` (RULING 2: repo surface, not
    GROVE_HOME). This module is ``grove/fleet/rubric_registry.py``, so
    ``parents[2]`` is the repo root."""
    return Path(__file__).resolve().parents[2] / "config" / "rubrics.yaml"


def _require_threshold(entry: Dict[str, object], key: str) -> float:
    """Validate a required ``default_threshold`` in [0.0, 1.0]. Mirrors the
    capability-record threshold rule (grove/capability.py); a bool is not a
    number here."""
    if key not in entry:
        raise ValueError(
            f"config/rubrics.yaml rubric {entry.get('_key')!r} is missing "
            f"required {key!r}."
        )
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"config/rubrics.yaml rubric {entry.get('_key')!r} {key} must be "
            f"a number, got {value!r} ({type(value).__name__})."
        )
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(
            f"config/rubrics.yaml rubric {entry.get('_key')!r} {key} must be "
            f"in [0.0, 1.0], got {value}."
        )
    return float(value)


def _require_nonempty_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"config/rubrics.yaml {where} must be a non-empty string, got "
            f"{value!r} ({type(value).__name__})."
        )
    return value


def _build_criterion(raw: object, rubric_key: str, index: int) -> Criterion:
    where = f"rubric {rubric_key!r} criteria[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(
            f"config/rubrics.yaml {where} must be a mapping, got "
            f"{type(raw).__name__}."
        )
    cid = _require_nonempty_str(raw.get("id"), f"{where}.id")
    ctype = _require_nonempty_str(raw.get("type"), f"{where}.type")
    if ctype not in _SUPPORTED_CRITERION_TYPES:
        raise ValueError(
            f"config/rubrics.yaml {where}.type {ctype!r} is unsupported; "
            f"Phase 1 supports {sorted(_SUPPORTED_CRITERION_TYPES)}."
        )
    definition = _require_nonempty_str(
        raw.get("definition"), f"{where}.definition"
    )
    return Criterion(id=cid, type=ctype, definition=definition)


def _build_rubric(key: str, raw: object) -> Rubric:
    if not isinstance(raw, dict):
        raise ValueError(
            f"config/rubrics.yaml rubric {key!r} must be a mapping, got "
            f"{type(raw).__name__}."
        )
    entry = dict(raw)
    entry["_key"] = key  # carried only for error messages
    default_threshold = _require_threshold(entry, "default_threshold")
    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not criteria_raw:
        raise ValueError(
            f"config/rubrics.yaml rubric {key!r} criteria must be a non-empty "
            f"list, got {criteria_raw!r}."
        )
    criteria: List[Criterion] = []
    seen_ids: set = set()
    for index, raw_criterion in enumerate(criteria_raw):
        criterion = _build_criterion(raw_criterion, key, index)
        if criterion.id in seen_ids:
            raise ValueError(
                f"config/rubrics.yaml rubric {key!r} has duplicate criterion "
                f"id {criterion.id!r}; ids must be unique within a rubric."
            )
        seen_ids.add(criterion.id)
        criteria.append(criterion)
    # artifact-review-v1 R-14 — a published version's MEANING is immutable.
    # The declared content_hash must match a recomputation over the criteria +
    # default_threshold; a mismatch is an in-place edit of a published version
    # and fails LOUD. Minting @2 (a new key) is the only way to change meaning.
    declared_hash = _require_nonempty_str(
        raw.get("content_hash"), f"rubric {key!r}.content_hash"
    )
    expected_hash = compute_content_hash(default_threshold, criteria)
    if declared_hash != expected_hash:
        raise ValueError(
            f"config/rubrics.yaml rubric {key!r} content_hash mismatch: "
            f"declared {declared_hash!r} but criteria + default_threshold hash "
            f"to {expected_hash!r}. A published version is immutable — mint a "
            f"new version (e.g. a new class@N key) instead of editing {key!r} "
            f"in place, or update content_hash to {expected_hash!r} if this is "
            f"a deliberate new mint."
        )
    return Rubric(
        key=key,
        default_threshold=default_threshold,
        criteria=criteria,
        content_hash=expected_hash,
    )


def load_rubric_registry(
    config_path: Optional[Path] = None,
) -> RubricRegistry:
    """Load ``config/rubrics.yaml`` into a :class:`RubricRegistry`.

    Absent file / empty file / absent ``rubrics`` block -> empty registry
    (silent). A present ``rubrics`` block is validated rubric-by-rubric and
    any malformed value raises a bare ``ValueError`` LOUD. Malformed YAML
    propagates from the parser. Uncached — one file read per call.
    """
    if config_path is None:
        config_path = _default_registry_path()
    if not config_path.exists():
        return RubricRegistry()

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return RubricRegistry()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    block = raw.get("rubrics")
    if block is None:
        return RubricRegistry()
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} rubrics must be a mapping, got "
            f"{type(block).__name__}."
        )
    rubrics: Dict[str, Rubric] = {}
    for key, raw_rubric in block.items():
        rubrics[str(key)] = _build_rubric(str(key), raw_rubric)
    return RubricRegistry(rubrics=rubrics)
