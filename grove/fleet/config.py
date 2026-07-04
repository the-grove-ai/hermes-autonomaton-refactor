"""fleet_workers.yaml loader — the OPERATIONAL registry (background-worker-runtime-v1).

Parses ``config/fleet_workers.yaml`` into ``WorkerConfig`` objects: WHEN and
HOW-MUCH a worker runs (id, skill, cadence, input_state, budget, limits,
quiet_hours, enabled). It carries NO structural fields (zone / read_surfaces /
sink) — those live in the capability record. The two layers do not cross.

Silent-degradation guard (mandatory, Phase-0 gate condition 1): a copy-paste
duplicate worker id must fail LOUD at load, not silently drop. PyYAML/ruamel
duplicate-KEY rejection only catches dup keys within one mapping; two list
entries with the same ``id`` are distinct mappings, so worker-id uniqueness is
asserted explicitly. Both guards are applied.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.paths import validate_worker_id


def default_fleet_workers_path() -> Path:
    """The repo-default registry: ``<repo>/config/fleet_workers.yaml``."""
    return Path(__file__).resolve().parents[2] / "config" / "fleet_workers.yaml"


@dataclass
class WorkerConfig:
    """One worker's operational contract. Structural fields live in its record."""

    id: str
    skill: str  # capability-record id, e.g. skill.fleet.forge-jobsearch
    enabled: bool
    cadence: Optional[str] = None
    input_state: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)
    quiet_hours: Optional[Dict[str, Any]] = None

    def validate(self) -> None:
        """Fail loud, naming the offending field. Called at load."""
        # id is the isolation key (a path component) — validate as a strict slug.
        validate_worker_id(self.id)
        if not isinstance(self.skill, str) or not self.skill.strip():
            raise FleetWorkerAndon(
                f"worker {self.id!r}: 'skill' must be a non-empty capability-record id",
                worker_id=self.id,
                check="missing_skill",
            )
        if not isinstance(self.enabled, bool):
            raise FleetWorkerAndon(
                f"worker {self.id!r}: 'enabled' must be a boolean (got "
                f"{type(self.enabled).__name__})",
                worker_id=self.id,
                check="bad_enabled",
            )
        if not isinstance(self.input_state, dict):
            raise FleetWorkerAndon(
                f"worker {self.id!r}: 'input_state' must be a mapping",
                worker_id=self.id,
                check="bad_input_state",
            )
        for name, val in (("budget", self.budget), ("limits", self.limits)):
            if not isinstance(val, dict):
                raise FleetWorkerAndon(
                    f"worker {self.id!r}: '{name}' must be a mapping",
                    worker_id=self.id,
                    check=f"bad_{name}",
                )
        if self.quiet_hours is not None and not isinstance(self.quiet_hours, dict):
            raise FleetWorkerAndon(
                f"worker {self.id!r}: 'quiet_hours' must be a mapping or omitted",
                worker_id=self.id,
                check="bad_quiet_hours",
            )


def _from_dict(d: Dict[str, Any]) -> WorkerConfig:
    if not isinstance(d, dict):
        raise FleetWorkerAndon(
            f"fleet_workers.yaml: each worker entry must be a mapping; got "
            f"{type(d).__name__}",
            check="malformed_entry",
        )
    if "id" not in d:
        raise FleetWorkerAndon(
            "fleet_workers.yaml: a worker entry is missing the required 'id' field",
            check="missing_id",
        )
    if "skill" not in d:
        raise FleetWorkerAndon(
            f"fleet_workers.yaml: worker {d.get('id')!r} is missing the required "
            f"'skill' field",
            worker_id=str(d.get("id")),
            check="missing_skill",
        )
    if "enabled" not in d:
        raise FleetWorkerAndon(
            f"fleet_workers.yaml: worker {d.get('id')!r} is missing the required "
            f"'enabled' field (must be explicit — no implicit default)",
            worker_id=str(d.get("id")),
            check="missing_enabled",
        )
    cfg = WorkerConfig(
        id=d["id"],
        skill=d["skill"],
        enabled=d["enabled"],
        cadence=d.get("cadence"),
        input_state=d.get("input_state") or {},
        budget=d.get("budget") or {},
        limits=d.get("limits") or {},
        quiet_hours=d.get("quiet_hours"),
    )
    cfg.validate()
    return cfg


def load_fleet_workers(path: Optional[Path] = None) -> Dict[str, WorkerConfig]:
    """Load the fleet-worker registry, keyed by worker id.

    Fail-loud on: unreadable/malformed file, duplicate YAML keys within any
    mapping (ruamel ``allow_duplicate_keys=False``), a missing/mistyped
    ``workers`` key, or a duplicate worker id across list entries. An empty
    registry (``workers: []``) is valid and returns ``{}``.
    """
    target = Path(path) if path is not None else default_fleet_workers_path()
    if not target.exists():
        raise FleetWorkerAndon(
            f"fleet_workers.yaml not found at {target} — the fleet-worker "
            f"registry must exist (ship it empty: 'workers: []')",
            check="registry_missing",
        )

    # ruamel safe loader with duplicate-KEY rejection (catches a dup key WITHIN a
    # single mapping — e.g. two 'cadence:' lines on one worker).
    from ruamel.yaml import YAML
    from ruamel.yaml.constructor import DuplicateKeyError

    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        raw = target.read_text(encoding="utf-8")
        data = yaml.load(io.StringIO(raw))
    except DuplicateKeyError as exc:
        raise FleetWorkerAndon(
            f"fleet_workers.yaml has a duplicate key: {exc}",
            check="duplicate_key",
        ) from exc
    except Exception as exc:  # malformed YAML — fail loud, do not guess
        raise FleetWorkerAndon(
            f"fleet_workers.yaml at {target} could not be parsed: {exc}",
            check="unparseable",
        ) from exc

    data = data or {}
    if "workers" not in data:
        raise FleetWorkerAndon(
            f"fleet_workers.yaml at {target} has no top-level 'workers' key "
            f"(ship it empty as 'workers: []')",
            check="no_workers_key",
        )
    workers = data["workers"]
    if workers is None:
        workers = []
    if not isinstance(workers, list):
        raise FleetWorkerAndon(
            f"fleet_workers.yaml 'workers' must be a list; got "
            f"{type(workers).__name__}",
            check="workers_not_list",
        )

    result: Dict[str, WorkerConfig] = {}
    for entry in workers:
        cfg = _from_dict(entry)
        # The real copy-paste guard: two list entries with the same id are NOT a
        # YAML duplicate key, so ruamel above will not catch them. Fail loud here.
        if cfg.id in result:
            raise FleetWorkerAndon(
                f"fleet_workers.yaml declares worker id {cfg.id!r} more than once "
                f"— worker ids must be unique (each is an isolation key)",
                worker_id=cfg.id,
                check="duplicate_worker_id",
            )
        result[cfg.id] = cfg
    return result
