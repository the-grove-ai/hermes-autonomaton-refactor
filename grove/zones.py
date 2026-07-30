"""Zone classifier for the Grove Autonomaton (v2 · scope-keyed).

Reads the repo policy schema at ``config/zones.schema.yaml`` and exposes a pure
``classify(action) -> ZoneResult`` query. No enforcement, no prompts, no
blocking — the Sovereignty Gate turns this output into a posture.

zones-v2-scope-keying: the schema is ``schema_version: 2`` — tools declare an
EFFECT CLASS (read_only / contained_write / workspace_write / external_effect /
governance / operator_only) and the zone is DERIVED at load from the schema's
declared derivation rules (``effect_classes[<class>].derives``). The v1
category-keyed machinery (action-type pattern lists, hierarchical
``tool_zones.<tool>.rules`` / ``classify_command_string``, the terminal.rules
writer) is RETIRED in P2 — shell/execute_code commands are classified by EFFECT
via the bashlex-AST classifier (``grove/shell_effects.py``), not this module.

Classification precedence:
    1. ``tool_effects`` exact match on the action (the tool name) → the derived
       zone for that tool's declared effect class.
    2. Unmatched action → the declared ``default_unmatched`` posture (yellow),
       with ``source="default"``.

Operator overlay (``~/.grove/zones.autonomaton.yaml``): a v2 overlay may carry
ONLY ``red_denied_by_policy`` (read by grove/red_policy.py) plus
``schema_version``. Any other (retired, category-keyed) key is REFUSED LOUD at
load — never silently merged (A3). See :func:`_load_and_validate_overlay`.

``reload()`` is the one SPEC-commanded graceful degradation: on parse or
validation failure, the classifier retains the last known good map and logs the
error loudly.
"""

from __future__ import annotations

import logging
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
        matched_rule: the action that matched a ``tool_effects`` entry, or
            ``"default"`` for the unmatched posture.
        source: ``"tool_zones"`` (a derived tool-effect classification) or
            ``"default"`` (the unmatched posture). The name ``tool_zones`` is
            retained for telemetry parity across the v1→v2 rekey.
        reason / pattern_key: retained on the dataclass for callers that read
            them (shell-effect classifications populate ``pattern_key``); the
            tool-effect path leaves them ``None``.
        is_promotable: whether an operator "Always" may promote this
            classification to a standing store. The shell classifier sets it
            False for a bucket-3 UNRESOLVED_WRITER (and any RED chain).
    """

    zone: str
    matched_rule: str
    source: str
    reason: Optional[str] = None
    pattern_key: Optional[str] = None
    is_promotable: bool = True


class ZoneClassifier:
    """Loads and queries a v2 zones.schema.yaml file."""

    # The SIX closed effect classes. The loader rejects a schema that adds,
    # removes, or renames a class, and rejects a tool declaring a class not here.
    _EFFECT_CLASSES: frozenset = frozenset({
        "read_only", "contained_write", "workspace_write",
        "external_effect", "governance", "operator_only",
    })

    def __init__(self, schema_path: Path):
        self._schema_path = Path(schema_path)
        self._tool_zones: dict[str, str] = {}
        # The declared effect-class → zone derivation and the tool → class map.
        # ``_default_unmatched`` is the DECLARED posture returned for an unmatched
        # action; the loader enforces it against the schema.
        self._effect_derivations: dict[str, str] = {}
        self._tool_effects: dict[str, str] = {}
        self._default_unmatched: str = "yellow"
        self._load_into_self()

    # ----- public query API ---------------------------------------------------

    def classify(self, action: str) -> ZoneResult:
        if action in self._tool_zones:
            return ZoneResult(
                zone=self._tool_zones[action],
                matched_rule=action,
                source="tool_zones",
            )
        # Unmatched → the DECLARED default posture. source="default" for telemetry.
        return ZoneResult(
            zone=self._default_unmatched, matched_rule="default", source="default",
        )

    def reload(self) -> None:
        """Reload schema from disk; on failure, keep last known good map and log loudly."""
        snapshot = (
            dict(self._tool_zones),
            dict(self._effect_derivations),
            dict(self._tool_effects),
            self._default_unmatched,
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
                self._effect_derivations,
                self._tool_effects,
                self._default_unmatched,
            ) = snapshot

    # ----- internals ----------------------------------------------------------

    def _load_merged(self) -> None:
        """Load repo policy, validate the overlay if present, apply to self.

        The overlay (``~/.grove/zones.autonomaton.yaml``) is only consulted when
        ``self._schema_path`` is the canonical repo policy path. A v2 overlay
        contributes nothing to classification (its sole valid content,
        ``red_denied_by_policy``, is read independently by grove/red_policy.py) —
        but it is still loaded and VALIDATED so a malformed or retired-key overlay
        is refused loud (A2/A3) rather than silently ignored.
        """
        with open(self._schema_path, encoding="utf-8") as fh:
            repo_data = yaml.safe_load(fh)
        if not isinstance(repo_data, dict):
            raise ValueError(
                f"zones schema at {self._schema_path} did not parse to a mapping"
            )

        # Only consult the overlay when using the canonical repo policy path.
        repo_default = (
            Path(__file__).resolve().parent.parent / "config" / "zones.schema.yaml"
        )
        overlay_data = None
        if self._schema_path.resolve() == repo_default.resolve():
            overlay_path = _resolve_overlay_path()
            if overlay_path is not None:
                overlay_data = _load_and_validate_overlay(overlay_path)

        merged = merge_zone_schemas(repo_data, overlay_data)
        self._load_from_dict(merged)

    def _load_into_self(self) -> None:
        self._load_merged()

    def _load_from_dict(self, raw: dict) -> None:
        """Validate and atomically apply a v2 schema dict to self.

        zones-v2-scope-keying P2 (D2): the loader now requires
        ``schema_version: 2``. The v1 (category-keyed) parse path is retired;
        any other version is a hard reject.
        """
        version = raw.get("schema_version")
        if version != 2:
            raise ValueError(
                f"unsupported schema_version {version!r} in {self._schema_path}"
                f" (expected 2 — v1 category-keyed schemas are retired,"
                f" zones-v2-scope-keying P2)"
            )
        self._load_v2(raw)

    def _load_v2(self, raw: dict) -> None:
        """Parse the v2 schema: effect classes + derivation rules + tool_effects.

        The zone is DERIVED at load from each tool's declared class into
        ``_tool_zones``, so ``classify()`` is a plain map lookup.
        """
        # 1) Derivation rules — the SIX closed classes, each with a `derives`.
        effect_classes_raw = raw.get("effect_classes") or {}
        if not isinstance(effect_classes_raw, dict):
            raise ValueError(
                f"v2 schema at {self._schema_path}: `effect_classes` must be a "
                f"mapping; got {type(effect_classes_raw).__name__}"
            )
        derivations: dict[str, str] = {}
        for cls, spec in effect_classes_raw.items():
            derives = (spec or {}).get("derives") if isinstance(spec, dict) else None
            if derives not in ("green", "yellow", "red"):
                raise ValueError(
                    f"v2 schema: effect_classes[{cls!r}].derives must be one of "
                    f"green/yellow/red; got {derives!r}"
                )
            derivations[cls] = derives
        declared = set(derivations)
        if declared != self._EFFECT_CLASSES:
            missing = self._EFFECT_CLASSES - declared
            extra = declared - self._EFFECT_CLASSES
            raise ValueError(
                f"v2 schema: effect_classes must declare EXACTLY the six closed "
                f"classes {sorted(self._EFFECT_CLASSES)}. "
                f"missing={sorted(missing)} unexpected={sorted(extra)}"
            )

        # 2) Declared default posture for an unmatched action (enforced).
        default_unmatched = raw.get("default_unmatched", "yellow")
        if default_unmatched not in ("green", "yellow", "red"):
            raise ValueError(
                f"v2 schema: default_unmatched must be green/yellow/red; got "
                f"{default_unmatched!r}"
            )

        # 3) tool_effects → derive each tool's zone. A contained_write entry
        #    MUST cite a containment primitive (A1: no citation, no class).
        tool_effects_raw = raw.get("tool_effects") or {}
        if not isinstance(tool_effects_raw, dict):
            raise ValueError(
                f"v2 schema: `tool_effects` must be a mapping; got "
                f"{type(tool_effects_raw).__name__}"
            )
        tool_zones: dict[str, str] = {}
        tool_effects: dict[str, str] = {}
        for tool_id, decl in tool_effects_raw.items():
            containment = None
            if isinstance(decl, str):
                cls = decl
            elif isinstance(decl, dict):
                cls = decl.get("class")
                containment = decl.get("containment")
            else:
                raise ValueError(
                    f"v2 schema: tool_effects[{tool_id!r}] must be a class string "
                    f"or a mapping with class(+containment); got "
                    f"{type(decl).__name__}"
                )
            if cls not in derivations:
                raise ValueError(
                    f"v2 schema: tool_effects[{tool_id!r}] declares effect class "
                    f"{cls!r} which is not one of the six declared classes "
                    f"{sorted(self._EFFECT_CLASSES)}"
                )
            if cls == "contained_write" and not (
                isinstance(containment, str) and containment.strip()
            ):
                raise ValueError(
                    f"v2 schema: tool_effects[{tool_id!r}] is contained_write but "
                    f"cites no `containment` primitive — REFUSING (A1: a jailed "
                    f"write derives GREEN only with a cited containment path)."
                )
            tool_zones[tool_id] = derivations[cls]
            tool_effects[tool_id] = cls

        # All-or-nothing swap (mutation only after validation succeeds).
        self._tool_zones = tool_zones
        self._effect_derivations = derivations
        self._tool_effects = tool_effects
        self._default_unmatched = default_unmatched


# ----- module-level singleton + helpers ---------------------------------------

_singleton: Optional[ZoneClassifier] = None


def initialize(schema_path: Optional[Path] = None) -> ZoneClassifier:
    """Initialize (or re-initialize) the module-level singleton.

    Resolution order for ``schema_path``:
        1. Explicit argument, if given.
        2. Repo default at ``<grove-package-parent>/config/zones.schema.yaml``.

    If ``~/.grove/zones.autonomaton.yaml`` exists it is loaded and VALIDATED
    against the v2 overlay contract (A2/A3) — a malformed or retired-key overlay
    refuses loud.

    Raises FileNotFoundError if the repo policy schema does not exist.
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

    repo_default = (
        Path(__file__).resolve().parent.parent / "config" / "zones.schema.yaml"
    )
    if not repo_default.exists():
        raise FileNotFoundError(
            f"repo policy schema not found at {repo_default} — "
            f"ANDON A1: policy source absent."
        )
    return repo_default


def _resolve_overlay_path() -> Optional[Path]:
    """Return the operator overlay path if it exists, else None."""
    # instance-cold-start-parity-v1 D3 — resolve via get_hermes_home() so the
    # overlay honors GROVE_HOME (was a hardcoded Path.home()/".grove").
    from hermes_constants import get_hermes_home

    overlay = Path(get_hermes_home()) / "zones.autonomaton.yaml"
    if overlay.exists():
        return overlay
    return None


# ── v2 overlay contract (zones-v2-scope-keying A2/A3) ─────────────────────────
# A v2 operator overlay may carry ONLY the deny-list structure red_policy.py
# parses (``red_denied_by_policy`` — grove/red_policy.py:38,64) plus the
# loader-required ``schema_version``. Every other key is a retired category-keyed
# door and is REFUSED LOUD at load — never silently dropped (a silently-dropped
# key is the hollow-artifact class the merge contract must not admit; GATE-B G7a).
_OVERLAY_VALID_KEYS: frozenset = frozenset({"schema_version", "red_denied_by_policy"})
_OVERLAY_RETIRED_KEYS: dict = {
    # tool_zones carried bare tool-name zone assignments, per-tool default_zone
    # overrides, AND terminal.rules. All three are retired: tool zones are now
    # DERIVED from repo effect classes, and the terminal.rules writer/reader
    # machinery is deleted (zones-v2-scope-keying P2).
    "tool_zones": (
        "bare tool-name zone assignments, default_zone overrides, and "
        "terminal.rules — all retired (tool zones derive from repo effect "
        "classes; the terminal.rules writer/reader machinery is deleted in P2). "
        "P2 migration prunes it."
    ),
    # The action-type pattern blocks (auto_approve/proposes/sovereign).
    "zones": (
        "action-type category patterns (auto_approve/proposes/sovereign) — "
        "retired; the enforced model is scope-keyed effect classes."
    ),
}


def _load_and_validate_overlay(overlay_path: Path) -> Optional[dict]:
    """Load the operator overlay, enforcing the v2 contract. FAIL LOUD.

    A2 — malformed overlay (unparseable YAML or non-mapping) RAISES, names the
    file and the repair path, and performs zero fallback (malformed ≠ absent;
    an absent overlay loads repo-policy-only, unchanged). An empty file parses
    to ``None`` and is treated as absent.

    A3 — the overlay's valid key surface is :data:`_OVERLAY_VALID_KEYS`. Any
    retired/unrecognized key RAISES and is NAMED in the refusal.
    """
    try:
        with open(overlay_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"[zones] operator overlay at {overlay_path} is not valid YAML "
            f"({exc}) — REFUSING to load (A2: malformed ≠ absent; zero "
            f"fallback). Repair the YAML, or remove the file to fall back to "
            f"repo policy."
        ) from exc
    if data is None:
        return None  # empty file == absent → repo-policy-only
    if not isinstance(data, dict):
        raise ValueError(
            f"[zones] operator overlay at {overlay_path} did not parse to a "
            f"mapping — REFUSING to load (A2). Repair the file, or remove it to "
            f"fall back to repo policy."
        )
    for key in data:
        if key in _OVERLAY_RETIRED_KEYS:
            raise ValueError(
                f"[zones] operator overlay at {overlay_path} carries RETIRED key "
                f"{key!r}: {_OVERLAY_RETIRED_KEYS[key]} REFUSING to load (A3; a "
                f"retired key is never silently dropped). Remove it."
            )
        if key not in _OVERLAY_VALID_KEYS:
            raise ValueError(
                f"[zones] operator overlay at {overlay_path} carries "
                f"unrecognized key {key!r}. A v2 overlay may carry only "
                f"{sorted(_OVERLAY_VALID_KEYS)} — REFUSING to load (A3)."
            )
    return data


def merge_zone_schemas(
    repo_data: dict,
    overlay_data: Optional[dict],
) -> dict:
    """Combine repo policy and the (validated) operator overlay.

    zones-v2-scope-keying P2: a v2 overlay carries no classification content
    (only ``red_denied_by_policy`` + ``schema_version``, both loader-irrelevant),
    so this is effectively a passthrough that returns the repo policy. The
    overlay has already been validated by :func:`_load_and_validate_overlay`
    (retired category-keyed keys refused loud), so there is nothing to merge into
    the derived tool-effect map.
    """
    import copy

    return copy.deepcopy(repo_data)
