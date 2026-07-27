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


# ═════════════════════════════════════════════════════════════════════════════
# GRV-001 v2.0 — split-config loaders (routing-v2-migration-v1, Phase 1)
# ═════════════════════════════════════════════════════════════════════════════
# ADDITIVE ONLY. The v1 loaders above (load_merged_routing_config /
# apply_diff_to_machine_config) and all 12 call sites keep working against the
# single v1 file untouched. These loaders serve the v2 split form and are not
# wired into any call site this phase.


class ConfigurationError(ValueError):
    """A v2 routing config (operational or authority) violated the GRV-001 v2.0
    contract — wrong schema_version, a v1-shaped file, or (operational only) an
    authority-reserved key bleeding in.

    Deliberately a plain ``ValueError`` subclass with NO grove-internal import:
    router_merge is grove-import-free by construction (the property that lets all
    12 call sites import it at module scope without a cycle — routing-loader-
    unification-v1). Subclassing ``grove.errors.GroveError`` would pull a grove
    import into this module and forfeit that guarantee.
    """


# GRV-001 v2.0 §V confused-deputy protection. These keys are reserved for
# routing.authority.yaml and are config-blind by design: their presence in ANY
# operational payload — including the operator's own file — is fatal, no
# fallback. The set is exactly the authority-destined keys the migration tool
# routes out of the operational surface.
_AUTHORITY_RESERVED_KEYS = frozenset(
    {"escalation_threshold", "tier_budgets", "escalation_policy"}
)

_V2_SCHEMA_VERSION = "2.0"
_MIGRATION_COMMAND = "python -m grove.config.routing_migrate <v1-routing.config.yaml>"


def _require_v2_shape(data: Any, path: Path) -> None:
    """Shared version gate for both v2 loaders.

    Rejects, in order: a non-mapping; a v1-shaped file (a top-level ``routing:``
    mapping) with an error naming the migration command; a missing
    schema_version; an integer schema_version (v1 used the int ``1``); and any
    schema_version other than the string ``"2.0"``.
    """
    if not isinstance(data, dict):
        raise ConfigurationError(f"routing config at {path} is not a YAML mapping")
    if isinstance(data.get("routing"), dict):
        raise ConfigurationError(
            f"routing config at {path} is v1-shaped (a top-level 'routing:' "
            f"mapping); GRV-001 v2.0 requires the split form. Migrate with: "
            f"{_MIGRATION_COMMAND}"
        )
    version = data.get("schema_version")
    if version is None:
        raise ConfigurationError(
            f"routing config at {path} has no schema_version; GRV-001 v2.0 "
            f'requires schema_version: "2.0". Migrate with: {_MIGRATION_COMMAND}'
        )
    if not isinstance(version, str):
        raise ConfigurationError(
            f"routing config at {path} has a non-string schema_version "
            f"{version!r} (v1 used the integer 1); GRV-001 v2.0 requires the "
            f'string "2.0". Migrate with: {_MIGRATION_COMMAND}'
        )
    if version != _V2_SCHEMA_VERSION:
        raise ConfigurationError(
            f"unsupported schema_version {version!r} in {path} "
            f'(expected "{_V2_SCHEMA_VERSION}")'
        )


def load_operational_routing_config(
    operational_path: Path,
    machine_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the v2 ``routing.operational`` config, overlay the machine additions,
    and enforce the GRV-001 v2.0 operational contract.

    The operational file MUST exist; the machine overlay is optional and merges
    with the same operator-wins / list-set-union semantics as the v1 loader
    (:func:`load_merged_routing_config`). After the overlay:

    * the v2 version gate applies (:func:`_require_v2_shape`); then
    * §V confused-deputy: if any :data:`_AUTHORITY_RESERVED_KEYS` member is
      present post-overlay, raise :class:`ConfigurationError` naming the key, the
      offending source (operator file vs machine overlay — attributed from the
      pre-overlay dicts), and ``routing.authority.yaml`` as its home. Fatal.
    """
    operational_path = Path(operational_path)
    if not operational_path.exists():
        raise FileNotFoundError(
            f"operational routing config not found at {operational_path}"
        )
    operator = yaml.safe_load(operational_path.read_text(encoding="utf-8"))
    if not isinstance(operator, dict):
        raise ConfigurationError(
            f"operational routing config at {operational_path} is not a YAML mapping"
        )

    machine: Optional[Dict[str, Any]] = None
    if machine_path is not None and Path(machine_path).exists():
        loaded = yaml.safe_load(Path(machine_path).read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigurationError(
                f"machine routing overlay at {machine_path} is not a YAML mapping"
            )
        machine = loaded if isinstance(loaded, dict) else None

    merged = _deep_merge(operator, machine) if machine is not None else operator

    _require_v2_shape(merged, operational_path)

    # §V confused-deputy — reserved authority keys must not appear post-overlay,
    # from ANY source. A valid v2 operational file is flat, so a top-level
    # membership test both matches the shape and closes the bleed.
    for key in _AUTHORITY_RESERVED_KEYS:
        if key in merged:
            if key in operator:
                source = f"the operator file ({operational_path})"
            elif machine is not None and key in machine:
                source = f"the machine overlay ({machine_path})"
            else:
                source = "the merged operational payload"
            raise ConfigurationError(
                f"authority-reserved key {key!r} is present in the operational "
                f"payload via {source}; GRV-001 v2.0 §V forbids it — its home is "
                f"routing.authority.yaml. This surface is config-blind by design: "
                f"no operational source may carry an authority key."
            )
    return merged


def load_authority_routing_config(authority_path: Path) -> Dict[str, Any]:
    """Load the v2 ``routing.authority`` config.

    There is NO overlay parameter — structurally absent, not defaulted-off: R1,
    no machine-writable file can reach this loader, by construction. The version
    gate applies; the authority-reserved keys legitimately live here, so no
    confused-deputy check runs.
    """
    authority_path = Path(authority_path)
    if not authority_path.exists():
        raise FileNotFoundError(
            f"authority routing config not found at {authority_path}"
        )
    data = yaml.safe_load(authority_path.read_text(encoding="utf-8"))
    _require_v2_shape(data, authority_path)
    return data
