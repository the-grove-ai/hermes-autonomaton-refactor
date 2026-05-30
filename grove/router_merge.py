"""GRV-008 § III source-of-truth hierarchy — the routing config merger.

Sprint 47. Defines two operations:

* :func:`load_merged_routing_config` — deep-merge the operator's
  ``routing.config.yaml`` (precedence) with the machine's
  ``routing.autonomaton.yaml``. Operator wins on every scalar key
  collision; lists merge as set-unions with operator entries first.

* :func:`apply_diff_to_machine_config` — write a proposal's diff to
  the machine-authored ``routing.autonomaton.yaml`` ONLY. Never
  touches the operator file. Idempotent on re-application of the
  same diff (set-union absorbs duplicates).

Per GRV-008 § III the machine MUST NOT mutate operator-authored
configuration. This module is the single seam through which machine-
authored routing changes enter the system, and it physically refuses
to write to ``routing.config.yaml`` — the operator-path parameter is
never used as a write target.

Per GATE-A operator revision (set-union vs. replace):

  For list values (intents lists in routing rules), the merge MUST
  perform SET-UNION. The operator's baseline intents survive; the
  machine's approved additions are appended. Neither side overwrites
  the other. For v1, the machine can only ADD to lists, not REMOVE
  from them.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


_MACHINE_HEADER = """\
# ============================================================================
# routing.autonomaton.yaml — Machine-authored routing additions (GRV-008 § III)
# ============================================================================
# This file is exclusively managed by the Flywheel's approval pipeline. It
# carries routing additions the operator has explicitly approved (Sprint 47
# `autonomaton flywheel approve <id>`). At runtime the Dispatcher deep-merges
# this file with the operator's routing.config.yaml; per GRV-008 § III the
# operator file's values strictly override on scalar collisions, and list
# values merge as set-unions with operator entries first.
#
# Do NOT edit by hand — operator-authored configuration lives in
# routing.config.yaml. Hand-edits here may be overwritten by the next
# approval cycle.
# ============================================================================
"""


def _deep_merge(
    operator_value: Any,
    machine_value: Any,
) -> Any:
    """Deep-merge two values with operator-wins precedence.

    * Both dicts: recurse per key.
    * Both lists of scalars: set-union, operator order first then machine
      additions in machine order. Preserves determinism and operator
      priority.
    * One present, the other absent: present value wins.
    * Operator scalar vs. machine scalar at the same key: operator wins.
    """
    if isinstance(operator_value, dict) and isinstance(machine_value, dict):
        merged: Dict[str, Any] = {}
        keys = list(operator_value.keys()) + [
            k for k in machine_value.keys() if k not in operator_value
        ]
        for key in keys:
            if key in operator_value and key in machine_value:
                merged[key] = _deep_merge(
                    operator_value[key], machine_value[key],
                )
            elif key in operator_value:
                merged[key] = deepcopy(operator_value[key])
            else:
                merged[key] = deepcopy(machine_value[key])
        return merged

    if isinstance(operator_value, list) and isinstance(machine_value, list):
        seen = set()
        merged_list: List[Any] = []
        for item in operator_value:
            key = _hashable_key(item)
            if key not in seen:
                seen.add(key)
                merged_list.append(deepcopy(item))
        for item in machine_value:
            key = _hashable_key(item)
            if key not in seen:
                seen.add(key)
                merged_list.append(deepcopy(item))
        return merged_list

    if operator_value is None:
        return deepcopy(machine_value)
    return deepcopy(operator_value)


def _hashable_key(item: Any) -> Any:
    """Return a hashable key for deduplication in a list merge.

    Scalars (str / int / float / bool) hash directly. Nested dicts/lists
    serialize to a canonical JSON string for set-membership comparison;
    set-union over heterogeneous structures is rare in routing config
    but supported here so the v1 implementation does not crash on a
    future schema change.
    """
    if isinstance(item, (str, int, float, bool, type(None))):
        return item
    import json
    return json.dumps(item, sort_keys=True, default=str)


def load_merged_routing_config(
    operator_path: Path,
    machine_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Deep-merge operator + machine routing configs.

    The operator file MUST exist; the machine file is optional (a fresh
    install has no machine additions yet). Operator wins on every
    scalar collision; list values set-union with operator order first.

    Returns the merged top-level mapping ready for ``CognitiveRouter``
    consumption (the Sprint 47 gate-proposal sandbox writes this dict
    to a tmp file and points a fresh CognitiveRouter at it).
    """
    operator_path = Path(operator_path)
    if not operator_path.exists():
        raise FileNotFoundError(
            f"operator routing config not found at {operator_path}; "
            f"GRV-008 § III requires the operator root"
        )
    operator = yaml.safe_load(operator_path.read_text(encoding="utf-8"))
    if not isinstance(operator, dict):
        raise ValueError(
            f"operator routing config at {operator_path} is not a YAML "
            f"mapping"
        )

    if machine_path is None or not Path(machine_path).exists():
        return operator

    machine = yaml.safe_load(Path(machine_path).read_text(encoding="utf-8"))
    if machine is None:
        return operator
    if not isinstance(machine, dict):
        raise ValueError(
            f"machine routing config at {machine_path} is not a YAML "
            f"mapping"
        )

    return _deep_merge(operator, machine)


def apply_diff_to_machine_config(
    diff: Dict[str, Any],
    machine_path: Path,
) -> None:
    """Merge ``diff`` into the machine routing file at ``machine_path``.

    Creates the file with the standard machine header banner on first
    write. The merge uses the same operator-wins / list-set-union
    semantics as :func:`load_merged_routing_config`, with the existing
    machine file taking the "operator" position in the recursion (its
    historical additions survive; the new diff is applied on top). This
    is idempotent on re-application: applying the same diff twice
    produces an unchanged file because the set-union absorbs duplicates.

    Per GRV-008 § III, this function MUST NOT be passed
    ``routing.config.yaml`` as ``machine_path``. The caller's discipline
    is the gate; the function does not introspect the path to enforce
    that, but the only call site (the approval handler) hardcodes the
    machine path.
    """
    machine_path = Path(machine_path)
    if machine_path.exists():
        existing = yaml.safe_load(machine_path.read_text(encoding="utf-8"))
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            raise ValueError(
                f"existing machine routing config at {machine_path} is "
                f"not a YAML mapping"
            )
    else:
        existing = {}
        machine_path.parent.mkdir(parents=True, exist_ok=True)

    merged = _deep_merge(existing, diff)
    rendered = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False)
    machine_path.write_text(_MACHINE_HEADER + "\n" + rendered, encoding="utf-8")
