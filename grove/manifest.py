"""Grove Disclosure Manifest — Sprint 74 context-jit-disclosure-v1 (Phase 1).

The manifest is an INDEX of disclosable units: one always-loaded line per unit
({id, kind, trigger, oneline, payload-pointer, tiers}) standing in front of the
heavy payload it points at (a tool schema, an MCP server's schemas, a goal
record). The dispatcher reads the index on the match-pass (Phase 2) and the
agent pulls a payload on demand (Phase 3); this module is the parse-and-validate
surface ONLY — import-only, no wiring, no enforcement until Phase 2.

ADDITIVE to Sprint 29 (D-GATE-B): native intent→tool selection STAYS in
``tool_groups.yaml``. The manifest does not restate it — a ``tool`` unit carries
NO trigger (its ``intents``/``keywords`` are empty); the manifest adds only the
one-liner + payload pointer for tools. The NEW trigger map is for ``mcp`` units
(MCPs flip from allow-by-default to disclose-on-match) and ``goal`` units
(``dock_goal`` pointer).

Fail-loud discipline (Architectural Prime Directive, mirroring
``grove.tier_budget``): a cap violation or malformed entry raises ``ValueError``
at load. The always-loaded index is prefill — an uncapped ``oneline`` or a
payload that smuggles an inlined schema would devour the very budget the
manifest exists to protect, so both are rejected at the door.

Hard caps (D5):
    oneline      <= 120 chars, non-empty
    keywords     <= 8 per trigger
    tiers        >= 1 eligible tier
    payload      a POINTER string ("<namespace>:<key>") — never inlined schema
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import yaml

__all__ = [
    "ONELINE_CAP",
    "MAX_KEYWORDS",
    "PAYLOAD_CAP",
    "VALID_KINDS",
    "UnitTrigger",
    "DisclosableUnit",
    "load_manifest",
]

# ── Hard caps (D5) ───────────────────────────────────────────────────────
ONELINE_CAP = 120
MAX_KEYWORDS = 8
PAYLOAD_CAP = 120

VALID_KINDS: frozenset = frozenset({"tool", "mcp", "goal", "contract_section"})


@dataclass(frozen=True)
class UnitTrigger:
    """The match-pass inputs for one disclosable unit.

    ``intents`` — ClassificationResult.intent_class values that disclose the
    unit (empty for ``tool`` units: native selection stays in tool_groups.yaml).
    ``keywords`` — surface-form keywords for the Dock-style matcher (<= 8).
    ``dock_goal`` — the goal id a ``goal`` unit's record belongs to, else None.
    """

    intents: Tuple[str, ...]
    keywords: Tuple[str, ...]
    dock_goal: Optional[str]

    def __post_init__(self) -> None:
        if len(self.keywords) > MAX_KEYWORDS:
            raise ValueError(
                f"UnitTrigger has {len(self.keywords)} keywords; the cap is "
                f"{MAX_KEYWORDS} (the index is always-loaded prefill — keep "
                f"triggers terse)"
            )


@dataclass(frozen=True)
class DisclosableUnit:
    """One always-loaded index entry standing in front of a heavy payload.

    ``payload`` is a POINTER ("tool_schema:terminal", "mcp_schema:notion",
    "goal_record:<id>") — the lookup key the disclosure path resolves, NEVER
    the inlined schema/record. That separation is the whole point: the index is
    cheap and always loaded; the payload discloses only on match or pull.
    """

    id: str
    kind: str
    oneline: str
    payload: str
    tiers: Tuple[str, ...]
    trigger: UnitTrigger

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f"DisclosableUnit {self.id!r} has unknown kind {self.kind!r}; "
                f"expected one of {sorted(VALID_KINDS)}"
            )
        oneline = (self.oneline or "").strip()
        if not oneline:
            raise ValueError(
                f"DisclosableUnit {self.id!r} has an empty oneline; the index "
                f"entry must describe the unit"
            )
        if len(self.oneline) > ONELINE_CAP:
            raise ValueError(
                f"DisclosableUnit {self.id!r} oneline is {len(self.oneline)} "
                f"chars; the cap is {ONELINE_CAP} (always-loaded prefill)"
            )
        if not self.tiers:
            raise ValueError(
                f"DisclosableUnit {self.id!r} has no eligible tiers; a unit "
                f"that no tier can disclose is dead weight — list >= 1 tier"
            )
        self._validate_payload()
        self._validate_mcp_trigger()

    def _validate_mcp_trigger(self) -> None:
        """Untriggered-MCP policy (Phase 2, D-GATE-B item 2): an ``mcp`` unit
        MUST declare at least one trigger signal (intents OR keywords OR
        dock_goal). Under disclose-on-match an mcp unit with no trigger could
        never match — it would silently vanish from every turn, the exact
        allow-by-default-to-nothing failure the flip must not introduce.
        Declarative discipline: adding a connector = a manifest entry WITH its
        trigger. Tool units are exempt — native selection (tool_groups.yaml)
        owns them, so they carry no trigger by design.
        """
        if self.kind != "mcp":
            return
        t = self.trigger
        if not (t.intents or t.keywords or t.dock_goal):
            raise ValueError(
                f"DisclosableUnit {self.id!r} is an mcp unit with no trigger "
                f"(no intents, no keywords, no dock_goal). Under disclose-on-"
                f"match it could never disclose — a silent vanish. Every "
                f"disclosable MCP must declare a trigger."
            )

    def _validate_payload(self) -> None:
        payload = self.payload or ""
        if not payload.strip():
            raise ValueError(
                f"DisclosableUnit {self.id!r} has an empty payload pointer"
            )
        if len(payload) > PAYLOAD_CAP:
            raise ValueError(
                f"DisclosableUnit {self.id!r} payload is {len(payload)} chars; "
                f"the cap is {PAYLOAD_CAP}. payload is a POINTER, not the "
                f"schema/record it points at"
            )
        if ":" not in payload:
            raise ValueError(
                f"DisclosableUnit {self.id!r} payload {payload!r} is not a "
                f"pointer; expected '<namespace>:<key>' "
                f"(e.g. 'tool_schema:terminal')"
            )
        if any(ch in payload for ch in ("{", "}", "\n")):
            raise ValueError(
                f"DisclosableUnit {self.id!r} payload looks like an inlined "
                f"schema, not a pointer. The index must NEVER carry the heavy "
                f"payload it points at — use a '<namespace>:<key>' pointer"
            )


# ── Loader + validator ───────────────────────────────────────────────────


def _resolve_manifest_path() -> Path:
    """Runtime sovereign copy then repo template.

    Mirrors the tier_budget / taxonomy resolution: operator copy at
    ``$GROVE_HOME/manifest.yaml`` first, else the repo template at
    ``config/manifest.yaml`` (``grove/`` is one level under the repo root).
    """
    from hermes_constants import get_hermes_home

    runtime = Path(get_hermes_home()) / "manifest.yaml"
    if runtime.exists():
        return runtime
    return Path(__file__).resolve().parents[1] / "config" / "manifest.yaml"


def _str_tuple(value, unit_id: str, field: str, target: Path) -> Tuple[str, ...]:
    """Validate a list-of-strings manifest field; default [] to empty tuple."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(
            f"manifest at {target}: unit {unit_id!r} {field} must be a list "
            f"(got {type(value).__name__})"
        )
    out = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"manifest at {target}: unit {unit_id!r} {field} entries must "
                f"be strings (got {item!r})"
            )
        out.append(item)
    return tuple(out)


def _parse_unit(spec, idx: int, target: Path) -> DisclosableUnit:
    """Validate one ``units[]`` entry and build a DisclosableUnit. Fail-loud."""
    if not isinstance(spec, dict):
        raise ValueError(
            f"manifest at {target}: units[{idx}] must be a mapping "
            f"(got {type(spec).__name__})"
        )
    unit_id = spec.get("id")
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise ValueError(
            f"manifest at {target}: units[{idx}] has a missing or non-string id"
        )
    for required in ("kind", "oneline", "payload", "tiers"):
        if required not in spec:
            raise ValueError(
                f"manifest at {target}: unit {unit_id!r} missing required key "
                f"{required!r}"
            )

    trig_raw = spec.get("trigger") or {}
    if not isinstance(trig_raw, dict):
        raise ValueError(
            f"manifest at {target}: unit {unit_id!r} trigger must be a mapping "
            f"(got {type(trig_raw).__name__})"
        )
    dock_goal = trig_raw.get("dock_goal")
    if dock_goal is not None and not isinstance(dock_goal, str):
        raise ValueError(
            f"manifest at {target}: unit {unit_id!r} trigger.dock_goal must be "
            f"a string or null (got {dock_goal!r})"
        )
    trigger = UnitTrigger(
        intents=_str_tuple(trig_raw.get("intents"), unit_id, "trigger.intents", target),
        keywords=_str_tuple(trig_raw.get("keywords"), unit_id, "trigger.keywords", target),
        dock_goal=dock_goal,
    )

    return DisclosableUnit(
        id=unit_id,
        kind=str(spec["kind"]),
        oneline=str(spec["oneline"]),
        payload=str(spec["payload"]),
        tiers=_str_tuple(spec["tiers"], unit_id, "tiers", target),
        trigger=trigger,
    )


def load_manifest(path: Optional[Path] = None) -> Tuple[DisclosableUnit, ...]:
    """Load + validate the disclosure manifest.

    Args:
        path: explicit ``manifest.yaml`` path (tests pass this). When ``None``,
            resolves the runtime sovereign copy then the repo template.

    Returns:
        A tuple of validated :class:`DisclosableUnit`, in declared order.

    Raises:
        ValueError: the manifest is malformed, names an unknown kind, or
            violates a hard cap (oneline / keywords / tiers / payload). Fail-loud
            — no silent drop of a malformed entry.
        FileNotFoundError: neither the runtime copy nor the repo template exists.
    """
    target = Path(path) if path is not None else _resolve_manifest_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(
            f"manifest at {target} is not a mapping (got {type(raw).__name__})"
        )
    if raw.get("version") != 1:
        raise ValueError(
            f"manifest at {target} unsupported version {raw.get('version')!r} "
            f"(expected 1)"
        )
    units_raw = raw.get("units")
    if not isinstance(units_raw, list):
        raise ValueError(f"manifest at {target}: units must be a list")

    units = []
    seen_ids: set = set()
    for i, spec in enumerate(units_raw):
        unit = _parse_unit(spec, i, target)
        if unit.id in seen_ids:
            raise ValueError(
                f"manifest at {target}: duplicate unit id {unit.id!r}"
            )
        seen_ids.add(unit.id)
        units.append(unit)
    return tuple(units)


# ── build_manifest (Phase 3): derive tool units + merge declarative ──────


def _oneline_from_description(desc: str) -> str:
    """The first line of a tool/MCP description, truncated to the cap.

    The derived index entry is always-loaded prefill; the registry description
    can be paragraphs. Take the first line and hard-cap it at ``ONELINE_CAP``.
    """
    first = (desc or "").strip().splitlines()[0].strip() if desc else ""
    if not first:
        first = "(no description)"
    if len(first) > ONELINE_CAP:
        first = first[: ONELINE_CAP - 3].rstrip() + "..."
    return first
