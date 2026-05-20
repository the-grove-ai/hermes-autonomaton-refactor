"""Zone classifier for the Grove Autonomaton.

Reads ``~/.grove/zones.schema.yaml`` (or the repo default at
``config/zones.schema.yaml``) and exposes a pure
``classify(action) -> ZoneResult`` query. No enforcement, no prompts, no
blocking. Sprint 06a turns this output into the Sovereignty Gate.

Action identifiers are opaque pure-dot-notation strings. The tool dispatch
layer (Sprint 06a) is responsible for mapping filesystem paths, command
lines, and other tool inputs into action identifiers before calling
``classify()``. The classifier never inspects paths or commands.

Precedence (per Sprint 03 design, corrected in Sprint 04):
    1. ``tool_zones`` exact match (highest)
    2. Zone rules: ``sovereign`` > ``proposes`` > ``auto_approve``
    3. Red > Yellow > Green (most restrictive wins on conflict)
    4. Unmatched action -> yellow with source ``"default"``

Pattern matching: exact match, or trailing ``.*`` (recursive — matches the
prefix exactly AND every descendant). Mid-pattern wildcards raise
``ValueError`` at load time to fail loud.

``reload()`` is the one SPEC-commanded graceful degradation: on parse or
validation failure, the classifier retains the last known good map and
logs the error loudly. Signal-based reload triggers (SIGHUP/SIGUSR1) are
not wired in this sprint — a later integration sprint negotiates signal
ownership with the existing handlers in ``cli.py``, ``hermes_cli/main.py``,
``tui_gateway/entry.py``, and others.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoneResult:
    """Classification result for an action.

    Attributes:
        zone: one of ``"green"``, ``"yellow"``, ``"red"``.
        matched_rule: the literal pattern that matched, or ``"default"``.
        source: which list/map produced the result —
            ``"tool_zones" | "sovereign" | "proposes" | "auto_approve" | "default"``.
    """

    zone: str
    matched_rule: str
    source: str


class ZoneClassifier:
    """Loads and queries a zones.schema.yaml file."""

    def __init__(self, schema_path: Path):
        self._schema_path = Path(schema_path)
        self._tool_zones: dict[str, str] = {}
        self._sovereign: list[str] = []
        self._proposes: list[str] = []
        self._auto_approve: list[str] = []
        self._load_into_self()

    # ----- public query API ---------------------------------------------------

    def classify(self, action: str) -> ZoneResult:
        if action in self._tool_zones:
            return ZoneResult(
                zone=self._tool_zones[action],
                matched_rule=action,
                source="tool_zones",
            )
        for pattern in self._sovereign:
            if self._pattern_matches(pattern, action):
                return ZoneResult(zone="red", matched_rule=pattern, source="sovereign")
        for pattern in self._proposes:
            if self._pattern_matches(pattern, action):
                return ZoneResult(zone="yellow", matched_rule=pattern, source="proposes")
        for pattern in self._auto_approve:
            if self._pattern_matches(pattern, action):
                return ZoneResult(zone="green", matched_rule=pattern, source="auto_approve")
        return ZoneResult(zone="yellow", matched_rule="default", source="default")

    def reload(self) -> None:
        """Reload schema from disk; on failure, keep last known good map and log loudly."""
        snapshot = (
            dict(self._tool_zones),
            list(self._sovereign),
            list(self._proposes),
            list(self._auto_approve),
        )
        try:
            self._load_into_self()
        except Exception as exc:
            # SPEC-commanded graceful degradation. Loud log; previous state retained.
            logger.error(
                "[zones] reload failed; keeping last known good config: %r", exc
            )
            (
                self._tool_zones,
                self._sovereign,
                self._proposes,
                self._auto_approve,
            ) = snapshot

    # ----- internals ----------------------------------------------------------

    def _load_into_self(self) -> None:
        """Read, parse, validate; mutate self atomically on success."""
        with open(self._schema_path) as fh:
            raw = yaml.safe_load(fh)

        if not isinstance(raw, dict):
            raise ValueError(
                f"zones schema at {self._schema_path} did not parse to a mapping"
            )

        version = raw.get("schema_version")
        if version != 1:
            raise ValueError(
                f"unsupported schema_version {version!r} in {self._schema_path}"
                f" (expected 1)"
            )

        zones = raw.get("zones", {}) or {}
        tool_zones = raw.get("tool_zones", {}) or {}

        sovereign = list((zones.get("red") or {}).get("sovereign", []) or [])
        proposes = list((zones.get("yellow") or {}).get("proposes", []) or [])
        auto_approve = list((zones.get("green") or {}).get("auto_approve", []) or [])

        for pattern in sovereign + proposes + auto_approve:
            self._validate_pattern(pattern)

        # All-or-nothing swap (mutation only after validation succeeds).
        self._tool_zones = dict(tool_zones)
        self._sovereign = sovereign
        self._proposes = proposes
        self._auto_approve = auto_approve

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        if "*" not in pattern:
            return
        if pattern == "*":
            return
        if not pattern.endswith(".*"):
            raise ValueError(
                f"only trailing '.*' wildcards are supported; got: {pattern!r}"
            )
        prefix = pattern[:-2]
        if "*" in prefix:
            raise ValueError(
                f"mid-pattern wildcards are not supported; got: {pattern!r}"
            )

    @staticmethod
    def _pattern_matches(pattern: str, action: str) -> bool:
        if pattern == "*":
            return True
        if "*" not in pattern:
            return pattern == action
        prefix = pattern[:-2]  # strip trailing ".*"
        return action == prefix or action.startswith(prefix + ".")


# ----- module-level singleton + helpers ---------------------------------------

_singleton: Optional[ZoneClassifier] = None


def initialize(schema_path: Optional[Path] = None) -> ZoneClassifier:
    """Initialize (or re-initialize) the module-level singleton.

    Resolution order for ``schema_path``:
        1. Explicit argument, if given.
        2. ``~/.grove/zones.schema.yaml`` (operator copy).
        3. Repo default at ``<grove-package-parent>/config/zones.schema.yaml``,
           copied to the operator location on first run.

    Raises FileNotFoundError if neither the operator copy nor the repo
    default exists.
    """
    global _singleton
    _singleton = ZoneClassifier(_resolve_schema_path(schema_path))
    return _singleton


def classify(action: str) -> ZoneResult:
    """Module-level convenience that delegates to the singleton."""
    if _singleton is None:
        raise RuntimeError(
            "grove.zones is not initialized; call grove.zones.initialize() first."
        )
    return _singleton.classify(action)


def reload() -> None:
    """Reload the singleton's schema. Raises if not yet initialized."""
    if _singleton is None:
        raise RuntimeError(
            "grove.zones is not initialized; call grove.zones.initialize() first."
        )
    _singleton.reload()


def _resolve_schema_path(explicit: Optional[Path]) -> Path:
    if explicit is not None:
        return Path(explicit)

    operator_copy = Path.home() / ".grove" / "zones.schema.yaml"
    if operator_copy.exists():
        return operator_copy

    repo_default = (
        Path(__file__).resolve().parent.parent / "config" / "zones.schema.yaml"
    )
    if not repo_default.exists():
        raise FileNotFoundError(
            f"no zones schema found at {operator_copy} or {repo_default}"
        )

    operator_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_default, operator_copy)
    return operator_copy
