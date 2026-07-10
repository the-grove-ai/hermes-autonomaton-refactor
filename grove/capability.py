"""Grove Autonomaton — the Capability primitive (GRV-009 E1, capability-record-v1).

The convergence target for the capability-layer refactor: one declarative
record that a verb, MCP server, skill, pattern, or contract section all resolve
into. This module is the record + its lifecycle state machine + its YAML
round-trip ONLY. It COEXISTS with the legacy capability layer (skills_tool,
tool_groups, manifest, mcp_tool) and changes no behavior — nothing imports it
yet.

Fail-loud discipline (Architectural Prime Directive): governance-bearing fields
have no defaults (omission raises); every value is validated at construction and
a violation raises ``ValueError`` naming the offending field. A unit without a
trigger would silently vanish from disclosure — we reject it loudly instead.

Defaults policy: id, kind, zone, lifecycle.state, telemetry.feed are
governance-bearing and required. All other fields instantiate with safe empty
defaults (empty list / empty dict / None) via ``field(default_factory=...)`` —
never a mutable literal, so state never bleeds across instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import yaml

__all__ = [
    "CapabilityKind",
    "Zone",
    "LifecycleState",
    "Provenance",
    "Disclosure",
    "TriggerDisclosure",
    "DockComposition",
    "ValidationStrategy",
    "FailureFallback",
    "Trigger",
    "Bindings",
    "TierValidation",
    "TierRule",
    "Telemetry",
    "Context",
    "SkillPresentation",
    "TransitionRecord",
    "Lifecycle",
    "Lineage",
    "CircuitBreaker",
    "Failure",
    "READ_SURFACE_VOCABULARY",
    "Capability",
    "IllegalTransitionError",
    "LEGAL_TRANSITIONS",
    "VALID_PLATFORMS",
]


# ── Platform surface registry ────────────────────────────────────────────────
# Public constant — downstream subsystems (gateway filter, tool-admission
# checks) enumerate valid surfaces from here rather than duplicating the list.
VALID_PLATFORMS: frozenset = frozenset({"telegram", "cli", "api", "web", "discord", "cron"})


# ── read_surfaces vocabulary (background-worker-runtime-v1) ───────────────────
# The canonical set of data-surface tokens a capability's skill may touch. A
# fleet background worker enforces this list generically: it may read only the
# surfaces its record declares. Structural / fail-loud — an unknown token would
# let a worker silently touch an unenforced surface, so validate() rejects it at
# LOAD (dry-run validation), never at runtime. Extend this set (not a record's
# ad-hoc string) before a new surface may be declared.
#
#   corpus_file  — plain files the skill reads (e.g. career-corpus.md).
#   cellar       — the living-cellar FTS index (grove/cellar.py).
#   wiki         — the wiki FTS index (grove/wiki/index.py).
#
# The index surfaces (cellar, wiki) require a collision-safe injected SQLite
# connection; both index opens are bare today (discovery: cellar.py:168,
# wiki/index.py:253), so the fleet runtime raises a loud Andon for any worker
# that declares them until that connection is wired. corpus_file is unaffected.
READ_SURFACE_VOCABULARY: frozenset = frozenset({"corpus_file", "cellar", "wiki"})


# ── Enums (str-valued; serialize as lowercase strings) ───────────────────────


class CapabilityKind(str, Enum):
    VERB = "verb"
    MCP = "mcp"
    SKILL = "skill"
    PATTERN = "pattern"
    CONTRACT = "contract"


class Zone(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class LifecycleState(str, Enum):
    PROPOSED = "proposed"  # GRV-009 A6 — the SOLE review/quarantine lock (.andon)
    APPROVED = "approved"  # vestigial post-A6 (no graph edges); retained for round-trip
    ACTIVE = "active"
    REFINED = "refined"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"  # GRV-009 A1/A6 — terminal; only from proposed
    MANAGED = "managed"  # GRV-009 A5 — installed skills; terminal, curator-exempt


class Provenance(str, Enum):
    OPERATOR_AUTHORED = "operator_authored"
    AGENT_PROPOSED = "agent_proposed"
    MIGRATED = "migrated"
    INSTALLED = "installed"  # GRV-009 A5 — minted by an install path, not authored


class Disclosure(str, Enum):
    EAGER = "eager"
    PULL = "pull"
    ALWAYS = "always"


class TriggerDisclosure(str, Enum):
    """GRV-009 E5 Amendment A4t — the per-record native-disclosure mode.

    The golden offer-parity snapshot shows native disclosure has three modes the
    ``always``/``intents`` trigger alone cannot express. This field declares the
    mode the resolver (C-RESOLVE) honors; it never re-narrows in code what a
    record declares open.

    * ``proactive`` — offered whenever tier-eligible on any intent/complexity
      (core control tools via ``always``; intent records via ``intents``).
    * ``complexity`` — offered only on complex/novel turns (the exploratory
      cohort), regardless of intent.
    * ``fallback`` — never offered proactively; reachable only via the
      maximal unknown-intent fallback (the never-grouped integration families).
    """

    PROACTIVE = "proactive"
    COMPLEXITY = "complexity"
    FALLBACK = "fallback"


class DockComposition(str, Enum):
    NONE = "none"
    GOAL_CONTEXT = "goal_context"
    FULL_DOCKET = "full_docket"


class ValidationStrategy(str, Enum):
    SHADOW_COMPARE = "shadow_compare"
    CANARY = "canary"
    OPERATOR_CONFIRM = "operator_confirm"


class FailureFallback(str, Enum):
    DEGRADE_TO_PULL = "degrade_to_pull"
    ESCALATE_TIER = "escalate_tier"
    HALT_AND_SURFACE = "halt_and_surface"


# ── Errors ───────────────────────────────────────────────────────────────────


class IllegalTransitionError(ValueError):
    """Raised when a lifecycle transition is not one of the legal edges."""


# ── State machine — the ONLY legal transitions ───────────────────────────────
# GRV-009 Amendment A6: ``quarantine`` is collapsed into ``proposed`` — the
# .andon review window IS state:proposed (the sole review/quarantine lock). The
# sovereignty governance edges are now single transitions:
#   * promote: proposed → active        * reject:  proposed → rejected (A1)
#   * revoke:  active   → proposed       * edit:    active   → refined
#   * delete:  active   → deprecated
# ``rejected`` (A1) is reachable only from ``proposed`` and is terminal.
# ``deprecated`` is the graceful exit from ``active`` and is terminal.

LEGAL_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.PROPOSED: frozenset({LifecycleState.ACTIVE, LifecycleState.REJECTED}),
    LifecycleState.ACTIVE: frozenset(
        {LifecycleState.REFINED, LifecycleState.DEPRECATED, LifecycleState.PROPOSED}
    ),
    LifecycleState.REFINED: frozenset({LifecycleState.ACTIVE}),
    LifecycleState.DEPRECATED: frozenset(),
    LifecycleState.REJECTED: frozenset(),
    LifecycleState.MANAGED: frozenset(),  # GRV-009 A5 — terminal; no legal exits
}


# GRV-009 E6b C2 — the executable lifecycle states. A skill record outside this
# set MUST NOT be offered in the <available_skills> index nor resolved into the
# model context: ``proposed``/``quarantine`` are under operator review (the
# proposed-window non-executable checkpoint); ``approved`` is a transient
# pre-activation state; ``deprecated``/``rejected`` are retired/dead. Only an
# active (incl. installed-managed and mid-refine) skill may run.
EXECUTABLE_STATES: frozenset[LifecycleState] = frozenset({
    LifecycleState.ACTIVE,
    LifecycleState.MANAGED,
    LifecycleState.REFINED,
})


# ── Nested records (composition mirrors the GRV-009 YAML) ────────────────────


@dataclass
class Trigger:
    intents: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    dock_affinity: list[str] = field(default_factory=list)
    # GRV-009 E5 Amendment A4 — bootstrap/ungated disclosure. A record with
    # ``always: true`` offers regardless of intent/keyword match (the control
    # tools and the D4 verb backfill ride this). The strict intent/keyword
    # trigger requirement in ``validate()`` is relaxed only when ``always`` is set.
    always: bool = False
    # GRV-009 E5 Amendment A4t — native-disclosure mode (see TriggerDisclosure).
    disclosure: "TriggerDisclosure" = field(default_factory=lambda: TriggerDisclosure.PROACTIVE)


@dataclass
class Bindings:
    """GRV-009 E5 Amendment A4 — the tool-ownership binding.

    ``tools`` is the strict set of tool names this record governs — 1:1 across
    the whole registry, enforced at load (``capability_registry`` post-load
    pass). ``credentials`` is a declarative credential handle (e.g. ``"google"``,
    ``"notion-oauth"``) — a pointer, not a secret. ``toolset_key`` is the
    ``CONFIGURABLE_TOOLSETS`` key the tools live under, or ``None`` for
    hosted-MCP records whose schema is pulled live from the server.
    """

    tools: list[str] = field(default_factory=list)
    credentials: str | None = None
    toolset_key: str | None = None


@dataclass
class TierValidation:
    strategy: ValidationStrategy = ValidationStrategy.SHADOW_COMPARE
    confidence_threshold: float = 0.0
    shadow_window: int = 0


@dataclass
class TierRule:
    # tier_rule.eligible: inert at admission after neuter-tier-eligible-gate —
    # NOT consulted when admitting tools (the cognitive router picks the tier and
    # the zone system governs mutation safety). Retained for record round-trip and
    # still STRUCTURALLY load-validated (see validate(): non-empty subset of {0,1,2,3}).
    eligible: list[int] = field(default_factory=list)
    preferred: int = -1
    promotion_criteria: dict = field(default_factory=dict)
    validation: TierValidation = field(default_factory=TierValidation)


@dataclass
class Telemetry:
    feed: str  # governance-bearing — no default
    track: list[str] = field(default_factory=list)


@dataclass
class Context:
    disclosure: Disclosure = Disclosure.PULL
    payload: str = ""
    dock_composition: DockComposition = DockComposition.NONE


@dataclass
class SkillPresentation:
    """GRV-009 E6a — skill-scoped presentation grouping (lock 2).

    ``category`` is the prompt-index grouping a skill appears under (today
    derived from the skill's directory under ``~/.grove/skills``). It exists for
    ONE reason: to reproduce the legacy index byte-for-byte. It is presentation,
    NEVER governance — the resolver does not read it for trigger/zone/tier, and
    overloading a governance field for taxonomy (the tool_groups anti-pattern)
    is exactly what this dedicated, explicit block exists to prevent. Category
    *descriptions* live in a separate side-record keyed by category name
    (``grove.skill_disclosure.load_skill_category_descriptions``), not here —
    one description serves many skills, so per-record storage would duplicate it.

    Present ONLY on kind=skill records (validate() rejects it elsewhere).
    """

    category: str = "general"


@dataclass
class TransitionRecord:
    actor: str
    timestamp: str
    from_state: str
    to_state: str
    reason: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Lifecycle:
    state: LifecycleState  # governance-bearing — no default
    provenance: Provenance = Provenance.OPERATOR_AUTHORED
    created_at: str = ""
    last_used: str | None = None
    use_count: int = 0
    flywheel_eligible: bool = False
    pinned: bool = False  # GRV-009 A5 — curator-exempt flag (backfilled from .usage.json)
    body_hash: str | None = None  # GRV-009 E6b C2 — sha256 of the skill body at mint (wake-match DEFERRED; populate only)


@dataclass
class Lineage:
    source_patterns: list[str] = field(default_factory=list)
    parent_id: str | None = None
    decision_log: list[TransitionRecord] = field(default_factory=list)


@dataclass
class CircuitBreaker:
    threshold: int = 0
    window_seconds: int = 0


@dataclass
class Failure:
    fallback: FailureFallback = FailureFallback.HALT_AND_SURFACE
    diagnostic_context: list[str] = field(default_factory=list)
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


# ── R5 (browser-read-surface-v1) — per-skill model binding ──────────────────
# Polymorphic: ``tier_override`` forces a skill's reasoning tier; ``specialty``
# is validated-but-honored-no-op (reserved for the Auxiliary Inference sprint,
# Andon A7 — the resolver falls through to the turn default). Precedence is
# operator routing.config.yaml > this binding > turn default; operator config is
# inviolate at the top. tier_override targets the inference tiers only (T0 is the
# Pattern Cache, Telemetry is the classifier — neither is a skill reasoning tier).
_MODEL_BINDING_TYPES: frozenset[str] = frozenset({"tier_override", "specialty"})
_MODEL_BINDING_TIERS: frozenset[str] = frozenset({"T1", "T2", "T3"})


@dataclass
class ModelBinding:
    type: str
    tier: str | None = None


# ── The Capability record ────────────────────────────────────────────────────
# kw_only so the governance-bearing fields (no default) may sit after fields
# that carry defaults without violating dataclass ordering.


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C1 — terminal_artifact.presentation validation.
#
# The OPTIONAL presentation declaration tells the portal how to render a
# worker's artifact as prose (headline / fact chips / collection preview /
# body / package order). It is ENHANCEMENT, never governance: a malformed
# block logs a LOUD warning naming the record + offending field and gains a
# machine-readable ``presentation_error`` sibling key — renderers treat
# error-present as declaration-absent and surface the notice inline. Loading
# never fails on presentation errors (F2).
# ---------------------------------------------------------------------------

import logging as _logging

_pres_logger = _logging.getLogger(__name__)


def _prose_entry_ok(entry) -> bool:
    """A prose entry is a field path: ``str`` or ``{path: str, md: bool?}``."""
    if isinstance(entry, str) and entry.strip():
        return True
    if isinstance(entry, dict):
        if not isinstance(entry.get("path"), str) or not entry["path"].strip():
            return False
        extra = set(entry) - {"path", "md"}
        return not extra and isinstance(entry.get("md", False), bool)
    return False


def _presentation_shape_error(pres) -> str | None:
    """Return the offending-field description for a malformed presentation
    block, or None when the shape is valid. All keys optional; unknown keys
    are malformed (strict — predictable degradation over silent tolerance)."""
    if not isinstance(pres, dict):
        return "presentation (must be a mapping)"
    allowed = {"headline", "facts", "collection", "body", "package"}
    unknown = set(pres) - allowed
    if unknown:
        return f"unknown key(s) {sorted(unknown)}"
    for key in ("headline", "body"):
        if key in pres and not _prose_entry_ok(pres[key]):
            return f"'{key}' (str or {{path, md}})"
    if "facts" in pres:
        facts = pres["facts"]
        if not isinstance(facts, list):
            return "'facts' (must be a list)"
        for f in facts:
            if (not isinstance(f, dict)
                    or not isinstance(f.get("path"), str) or not f["path"].strip()
                    or set(f) - {"path", "label"}
                    or ("label" in f and not isinstance(f["label"], str))):
                return "'facts' entry (needs {path, label?})"
    if "collection" in pres:
        c = pres["collection"]
        if not isinstance(c, dict):
            return "'collection' (must be a mapping)"
        if set(c) - {"path", "item_title", "item_prose", "preview_count"}:
            return "'collection' unknown key(s)"
        if not isinstance(c.get("path"), str) or not c["path"].strip():
            return "'collection.path' (required str)"
        if "item_title" in c and (not isinstance(c["item_title"], str)
                                  or not c["item_title"].strip()):
            return "'collection.item_title' (str)"
        if "item_prose" in c:
            if (not isinstance(c["item_prose"], list)
                    or not all(_prose_entry_ok(e) for e in c["item_prose"])):
                return "'collection.item_prose' (list of str or {path, md})"
        if "preview_count" in c and (
                not isinstance(c["preview_count"], int)
                or isinstance(c["preview_count"], bool)
                or c["preview_count"] < 1):
            return "'collection.preview_count' (int >= 1)"
    if "package" in pres:
        p = pres["package"]
        if not isinstance(p, dict) or set(p) - {"order", "title_from_meta"}:
            return "'package' (needs {order?, title_from_meta?})"
        for key in ("order", "title_from_meta"):
            if key in p and (not isinstance(p[key], list)
                             or not all(isinstance(x, str) for x in p[key])):
                return f"'package.{key}' (list of str)"
    return None


def _validate_presentation(governance, record_id: str) -> None:
    """Validate ``emission_preconditions.terminal_artifact.presentation`` IN
    PLACE. Malformed: warn LOUD (record + field) and set a machine-readable
    ``presentation_error`` string BESIDE the block (renderers treat
    error-present as declaration-absent and surface an inline notice). The
    operator's block itself is never deleted — a registry write-back must not
    destroy a fixable declaration."""
    if not isinstance(governance, dict):
        return
    ta = (governance.get("emission_preconditions") or {}).get("terminal_artifact")
    if not isinstance(ta, dict) or "presentation" not in ta:
        return
    err = _presentation_shape_error(ta["presentation"])
    if err is None:
        ta.pop("presentation_error", None)
        return
    _pres_logger.warning(
        "[capability] %s: terminal_artifact.presentation is malformed — %s; "
        "declaration treated as ABSENT (fallback card renders). Fix the "
        "presentation block in the capability record.", record_id, err,
    )
    ta["presentation_error"] = err


# ---------------------------------------------------------------------------
# wiki-writer-structured-output-v1 P1 — terminal_artifact.emit validation.
#
# The OPTIONAL emit declaration is the SOURCE OF TRUTH for a fleet producer's
# emit_package contract (GATE-B F5: record declares, harness derives). Shape:
#   emit:
#     transport: "tool" | "sentinel"          # required
#     files: {required: [name, ...]}          # optional (forge names its set;
#                                             #  declarative producers derive
#                                             #  from path_pattern + unit_id)
#     meta: {required_keys: [key, ...]}       # optional (forge identity keys)
# Rides the C1 presentation-block pattern exactly: a malformed block logs a
# LOUD warning naming the record + offending field and gains a non-destructive
# ``emit_error`` sibling — consumers treat error-present as declaration-absent
# (= sentinel transport, the migration default per GATE-B F6). Loading never
# fails on emit errors; the operator's block is never deleted.
# ---------------------------------------------------------------------------

_EMIT_TRANSPORTS: frozenset = frozenset({"tool", "sentinel"})


def _str_list_ok(value) -> bool:
    """A list of non-empty strings."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(x, str) and x.strip() for x in value)
    )


def _emit_shape_error(emit) -> str | None:
    """Return the offending-field description for a malformed emit block, or
    None when the shape is valid. ``transport`` is required; unknown keys are
    malformed (strict — predictable degradation over silent tolerance)."""
    if not isinstance(emit, dict):
        return "emit (must be a mapping)"
    unknown = set(emit) - {"transport", "files", "meta"}
    if unknown:
        return f"unknown key(s) {sorted(unknown)}"
    if emit.get("transport") not in _EMIT_TRANSPORTS:
        return "'transport' (required; \"tool\" or \"sentinel\")"
    if "files" in emit:
        f = emit["files"]
        if not isinstance(f, dict) or set(f) - {"required"}:
            return "'files' (needs {required})"
        if "required" in f and not _str_list_ok(f["required"]):
            return "'files.required' (non-empty list of non-empty str)"
    if "meta" in emit:
        m = emit["meta"]
        if not isinstance(m, dict) or set(m) - {"required_keys"}:
            return "'meta' (needs {required_keys})"
        if "required_keys" in m and not _str_list_ok(m["required_keys"]):
            return "'meta.required_keys' (non-empty list of non-empty str)"
    return None


def _validate_emit(governance, record_id: str) -> None:
    """Validate ``emission_preconditions.terminal_artifact.emit`` IN PLACE.
    Malformed: warn LOUD (record + field) and set a machine-readable
    ``emit_error`` string BESIDE the block (the harness treats error-present
    as declaration-absent → sentinel transport). The operator's block itself
    is never deleted — a registry write-back must not destroy a fixable
    declaration."""
    if not isinstance(governance, dict):
        return
    ta = (governance.get("emission_preconditions") or {}).get("terminal_artifact")
    if not isinstance(ta, dict) or "emit" not in ta:
        return
    err = _emit_shape_error(ta["emit"])
    if err is None:
        ta.pop("emit_error", None)
        return
    _pres_logger.warning(
        "[capability] %s: terminal_artifact.emit is malformed — %s; "
        "declaration treated as ABSENT (sentinel transport). Fix the emit "
        "block in the capability record.", record_id, err,
    )
    ta["emit_error"] = err


@dataclass(kw_only=True)
class Capability:
    id: str  # governance-bearing — no default
    kind: CapabilityKind  # governance-bearing — no default
    trigger: Trigger = field(default_factory=Trigger)
    bindings: Bindings = field(default_factory=Bindings)
    tier_rule: TierRule = field(default_factory=TierRule)
    zone: Zone  # governance-bearing — no default
    telemetry: Telemetry  # governance-bearing (contains feed) — no default
    context: Context = field(default_factory=Context)
    lifecycle: Lifecycle  # governance-bearing (contains state) — no default
    lineage: Lineage = field(default_factory=Lineage)
    failure: Failure = field(default_factory=Failure)
    # GRV-009 E6a — skill-scoped presentation grouping (lock 2). None for every
    # non-skill kind; required on kind=skill (see validate()). Governance-free.
    skill: "SkillPresentation | None" = None
    # tool-admission-unification: surface filter. "all" means every platform
    # receives this capability. A list restricts delivery to the named surfaces.
    # Valid values: telegram, cli, api, web, discord, cron.
    platform: list[str] | str = "all"
    # structural-review-gate-v1 — per-capability governance block (write-zone
    # confinement + emission preconditions + promotion policy). Opaque pass-through
    # dict: additive, None default so the 92 existing records load unchanged, and
    # NOT governance-bearing at construction (validate() does not read it — the
    # enforcement seams consume it, failing closed on a malformed block). Carried
    # through from_dict/to_dict so a lifecycle write (transition_record) never
    # erases it.
    governance: dict | None = None
    # R5 (browser-read-surface-v1) — per-skill model binding. None for the 92
    # existing records (present-key-only round-trip: serialization is
    # byte-identical when absent). Only valid on kind=skill records (validate()).
    model_binding: "ModelBinding | None" = None
    # background-worker-runtime-v1 — the data surfaces this capability's skill
    # may touch when run as a fleet background worker. Empty for every existing
    # record (present-key-only round-trip: serialization is byte-identical when
    # empty). Validated against READ_SURFACE_VOCABULARY at construction — an
    # unknown token fails loud. The fleet runtime enforces this list generically;
    # the record itself carries no runtime code, only the declaration.
    read_surfaces: list = field(default_factory=list)
    # NOTE: the fleet-pipeline-v1 P5 ``required_tools`` field was RETIRED in
    # fleet-corpus-only-offering-v1 P2. It had no runtime consumer once the L2
    # config-blind tool floor (Dispatcher, platform=='fleet' -> {read_file,
    # skill_view}) replaced the config-derived deny-complement — a dead-and-wrong
    # declaration (it named the walled Yellow invoke_skill). The corpus-only surface
    # is now a code-hardcoded floor (L2) plus a per-spawn allow-list on the
    # RuntimeContext config (L1), neither derived from the record.

    def __post_init__(self) -> None:
        self.validate()

    # ── Validation ───────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Fail loud, naming the offending field. Called at construction."""
        if not self.id:
            raise ValueError("id must be non-empty")
        if not isinstance(self.kind, CapabilityKind):
            raise ValueError("kind must be a valid CapabilityKind enum member")
        if not isinstance(self.zone, Zone):
            raise ValueError("zone must be a valid Zone enum member")
        if not isinstance(self.lifecycle.state, LifecycleState):
            raise ValueError(
                "lifecycle.state must be a valid LifecycleState enum member"
            )

        # Trigger discipline (A4 + A4t). A unit without a strict trigger silently
        # vanishes from disclosure — EXCEPT a ``disclosure: fallback`` record,
        # which is fallback-reachable by design (the maximal unknown-intent path),
        # so it legitimately carries no proactive trigger. The carve-out is tight:
        # ONLY fallback records may be empty, and a fallback record must carry NO
        # proactive trigger at all (else its declared mode contradicts itself).
        t = self.trigger
        if t.disclosure == TriggerDisclosure.FALLBACK:
            if t.always or t.intents or t.keywords:
                raise ValueError(
                    "disclosure: fallback is a fallback-only capability and must "
                    "carry no proactive trigger (always must be False; intents and "
                    "keywords must be empty)"
                )
        elif not (t.always or t.intents or t.keywords):
            raise ValueError(
                "trigger must declare at least one strict trigger: intents or "
                "keywords must be non-empty (or set trigger.always) — only a "
                "disclosure: fallback record may declare an empty trigger"
            )

        # A4 bindings — structural per-record checks (the strict 1:1 ownership
        # invariant is a collection-level post-load pass in capability_registry).
        b = self.bindings
        if b.tools:
            if not all(isinstance(t, str) and t for t in b.tools):
                raise ValueError("bindings.tools must all be non-empty strings")
            if len(set(b.tools)) != len(b.tools):
                raise ValueError(
                    "bindings.tools must not repeat a tool name within a record"
                )
        else:
            # A partial binding (credential/toolset handle without the tools it
            # governs) is malformed — fail loud rather than carry a dangling ref.
            if b.credentials is not None or b.toolset_key is not None:
                raise ValueError(
                    "bindings.credentials/toolset_key set without bindings.tools "
                    "— a binding must name the tools it governs"
                )
        if b.credentials is not None and not b.credentials:
            raise ValueError(
                "bindings.credentials, if set, must be a non-empty string"
            )
        if b.toolset_key is not None and not b.toolset_key:
            raise ValueError(
                "bindings.toolset_key, if set, must be a non-empty string"
            )

        # GRV-009 E6a (lock 2) — the skill presentation block is skill-scoped and
        # required for index parity. A kind=skill record with no block can't
        # reproduce the legacy category grouping; a non-skill carrying one is a
        # category leaking onto a kind that has none — both fail loud.
        if self.kind is CapabilityKind.SKILL:
            if self.skill is None:
                raise ValueError(
                    "a kind=skill record must carry a skill presentation block "
                    "(skill.category) — it is load-bearing for index parity"
                )
            if not self.skill.category:
                raise ValueError("skill.category must be non-empty")
        elif self.skill is not None:
            raise ValueError(
                f"skill presentation block is only valid on kind=skill records "
                f"(this record is kind={self.kind.value})"
            )

        if not self.telemetry.feed:
            raise ValueError("telemetry.feed must be non-empty")

        if not self.tier_rule.eligible:
            raise ValueError("tier_rule.eligible must be non-empty")
        if not set(self.tier_rule.eligible) <= {0, 1, 2, 3}:
            raise ValueError("tier_rule.eligible must be a subset of {0, 1, 2, 3}")
        if self.tier_rule.preferred not in self.tier_rule.eligible:
            raise ValueError("tier_rule.preferred must be in tier_rule.eligible")

        v = self.tier_rule.validation
        if not (0.0 < v.confidence_threshold <= 1.0):
            raise ValueError(
                "tier_rule.validation.confidence_threshold must be in (0.0, 1.0]"
            )
        if v.shadow_window <= 0:
            raise ValueError("tier_rule.validation.shadow_window must be > 0")

        cb = self.failure.circuit_breaker
        if cb.threshold <= 0:
            raise ValueError("failure.circuit_breaker.threshold must be > 0")
        if cb.window_seconds <= 0:
            raise ValueError("failure.circuit_breaker.window_seconds must be > 0")

        p = self.platform
        if isinstance(p, str):
            if p != "all":
                raise ValueError(
                    f"platform must be 'all' or a non-empty list of platform strings; "
                    f"got string {p!r}. Valid platform values: {sorted(VALID_PLATFORMS)}"
                )
        elif isinstance(p, list):
            if not p:
                raise ValueError("platform list must be non-empty; use 'all' for all surfaces")
            if len(set(p)) != len(p):
                raise ValueError("platform list must not repeat a surface name")
            invalid = set(p) - VALID_PLATFORMS
            if invalid:
                raise ValueError(
                    f"platform list contains unknown values {sorted(invalid)!r}. "
                    f"Valid: {sorted(VALID_PLATFORMS)}"
                )
        else:
            raise ValueError(f"platform must be 'all' or a list of strings; got {type(p)!r}")

        # R5 — per-skill model binding. Only meaningful on kind=skill (the
        # rebind fires on invoke_skill); a binding elsewhere would silently never
        # apply, so fail loud. Polymorphic type: tier_override live, specialty
        # validated-but-no-op. Unknown type / bad tier -> fail loud.
        mb = self.model_binding
        if mb is not None:
            if self.kind is not CapabilityKind.SKILL:
                raise ValueError(
                    f"model_binding is only valid on kind=skill records "
                    f"(this record is kind={self.kind.value}) — a binding on a "
                    f"non-skill capability would never apply"
                )
            if mb.type not in _MODEL_BINDING_TYPES:
                raise ValueError(
                    f"model_binding.type must be one of {sorted(_MODEL_BINDING_TYPES)}; "
                    f"got {mb.type!r}"
                )
            if mb.type == "tier_override":
                if mb.tier not in _MODEL_BINDING_TIERS:
                    raise ValueError(
                        f"model_binding.type=tier_override requires tier in "
                        f"{sorted(_MODEL_BINDING_TIERS)}; got {mb.tier!r}"
                    )
            else:  # specialty — reserved, honored-no-op; carries no tier
                if mb.tier is not None:
                    raise ValueError(
                        "model_binding.type=specialty carries no tier "
                        "(validated-but-no-op, reserved for Auxiliary Inference)"
                    )

        # background-worker-runtime-v1 — read_surfaces vocabulary. An unknown or
        # malformed token would let a fleet worker declare (and the runtime fail
        # to enforce) a surface that does not exist; reject at load. Empty is the
        # default and valid (a record that is never run as a worker declares no
        # surfaces). Duplicates are a copy-paste defect — fail loud, not dedupe.
        rs = self.read_surfaces
        if not isinstance(rs, list):
            raise ValueError(f"read_surfaces must be a list; got {type(rs)!r}")
        for token in rs:
            if not isinstance(token, str) or not token:
                raise ValueError("read_surfaces entries must be non-empty strings")
            if token not in READ_SURFACE_VOCABULARY:
                raise ValueError(
                    f"read_surfaces token {token!r} is not in the known "
                    f"vocabulary {sorted(READ_SURFACE_VOCABULARY)} — declare it "
                    f"in grove/capability.py READ_SURFACE_VOCABULARY before use"
                )
        if len(set(rs)) != len(rs):
            raise ValueError("read_surfaces must not repeat a token")

    # ── Lifecycle state machine ──────────────────────────────────────────────

    def transition(
        self,
        to_state: LifecycleState | str,
        actor: str,
        reason: str,
        evidence: list[str] | None = None,
    ) -> TransitionRecord:
        """Validate legality, append a TransitionRecord, update lifecycle.state.

        The only place lifecycle.state is mutated.
        """
        if not isinstance(to_state, LifecycleState):
            to_state = LifecycleState(to_state)

        current = self.lifecycle.state
        if to_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
            raise IllegalTransitionError(
                f"illegal transition {current.value} -> {to_state.value}"
            )

        record = TransitionRecord(
            actor=actor,
            timestamp=datetime.now(timezone.utc).isoformat(),
            from_state=current.value,
            to_state=to_state.value,
            reason=reason,
            evidence=list(evidence or []),
        )
        self.lineage.decision_log.append(record)
        self.lifecycle.state = to_state
        return record

    # ── YAML round-trip (the declarative-config contract) ────────────────────

    def to_dict(self) -> dict:
        """Plain dict; enums as lowercase strings, nested records as dicts.

        The ``skill`` block is emitted ONLY when present (kind=skill records),
        so every non-skill record's serialized shape is byte-identical to its
        pre-E6a form — zero migration churn on the 48 existing verb/mcp records.
        """
        d = {
            "id": self.id,
            "kind": self.kind.value,
            "trigger": {
                "intents": list(self.trigger.intents),
                "keywords": list(self.trigger.keywords),
                "dock_affinity": list(self.trigger.dock_affinity),
                "always": self.trigger.always,
                "disclosure": self.trigger.disclosure.value,
            },
            "bindings": {
                "tools": list(self.bindings.tools),
                "credentials": self.bindings.credentials,
                "toolset_key": self.bindings.toolset_key,
            },
            "tier_rule": {
                "eligible": list(self.tier_rule.eligible),
                "preferred": self.tier_rule.preferred,
                "promotion_criteria": dict(self.tier_rule.promotion_criteria),
                "validation": {
                    "strategy": self.tier_rule.validation.strategy.value,
                    "confidence_threshold": self.tier_rule.validation.confidence_threshold,
                    "shadow_window": self.tier_rule.validation.shadow_window,
                },
            },
            "zone": self.zone.value,
            "telemetry": {
                "feed": self.telemetry.feed,
                "track": list(self.telemetry.track),
            },
            "context": {
                "disclosure": self.context.disclosure.value,
                "payload": self.context.payload,
                "dock_composition": self.context.dock_composition.value,
            },
            "lifecycle": {
                "state": self.lifecycle.state.value,
                "provenance": self.lifecycle.provenance.value,
                "created_at": self.lifecycle.created_at,
                "last_used": self.lifecycle.last_used,
                "use_count": self.lifecycle.use_count,
                "flywheel_eligible": self.lifecycle.flywheel_eligible,
                "pinned": self.lifecycle.pinned,
                "body_hash": self.lifecycle.body_hash,
            },
            "lineage": {
                "source_patterns": list(self.lineage.source_patterns),
                "parent_id": self.lineage.parent_id,
                "decision_log": [
                    {
                        "actor": r.actor,
                        "timestamp": r.timestamp,
                        "from_state": r.from_state,
                        "to_state": r.to_state,
                        "reason": r.reason,
                        "evidence": list(r.evidence),
                    }
                    for r in self.lineage.decision_log
                ],
            },
            "failure": {
                "fallback": self.failure.fallback.value,
                "diagnostic_context": list(self.failure.diagnostic_context),
                "circuit_breaker": {
                    "threshold": self.failure.circuit_breaker.threshold,
                    "window_seconds": self.failure.circuit_breaker.window_seconds,
                },
            },
        }
        if self.skill is not None:
            d["skill"] = {"category": self.skill.category}
        if self.platform != "all":
            d["platform"] = list(self.platform) if isinstance(self.platform, list) else self.platform
        # structural-review-gate-v1 — emit the governance block only when present,
        # so every non-fleet record's serialized shape is unchanged (parity with
        # the skill/platform blocks) and the block survives a to_yaml round-trip.
        if self.governance is not None:
            d["governance"] = self.governance
        # R5 — emit only when present, so the 92 existing records are byte-identical.
        if self.model_binding is not None:
            mb: dict = {"type": self.model_binding.type}
            if self.model_binding.tier is not None:
                mb["tier"] = self.model_binding.tier
            d["model_binding"] = mb
        # background-worker-runtime-v1 — emit only when non-empty, so every
        # existing record's serialized shape is unchanged (parity with the
        # governance/model_binding blocks) and the list survives a to_yaml
        # round-trip.
        if self.read_surfaces:
            d["read_surfaces"] = list(self.read_surfaces)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Capability":
        """Rebuild from a plain dict, casting strings back into enums and
        reconstructing nested records (incl. decision_log) BEFORE instantiation.

        Present-key only: an absent non-governance block falls to its safe
        default; an absent governance-bearing field reaches the constructor
        missing and fails loud.
        """
        kwargs: dict = {}

        if "id" in d:
            kwargs["id"] = d["id"]
        if "kind" in d:
            kwargs["kind"] = CapabilityKind(d["kind"])
        if "zone" in d:
            kwargs["zone"] = Zone(d["zone"])

        if "trigger" in d:
            t = d["trigger"]
            kwargs["trigger"] = Trigger(
                intents=list(t.get("intents", [])),
                keywords=list(t.get("keywords", [])),
                dock_affinity=list(t.get("dock_affinity", [])),
                always=bool(t.get("always", False)),
                disclosure=TriggerDisclosure(
                    t.get("disclosure", TriggerDisclosure.PROACTIVE.value)
                ),
            )

        if "bindings" in d:
            bd = d["bindings"]
            kwargs["bindings"] = Bindings(
                tools=list(bd.get("tools", [])),
                credentials=bd.get("credentials"),
                toolset_key=bd.get("toolset_key"),
            )

        if "tier_rule" in d:
            tr = d["tier_rule"]
            v = tr.get("validation", {})
            kwargs["tier_rule"] = TierRule(
                eligible=list(tr.get("eligible", [])),
                preferred=tr.get("preferred", -1),
                promotion_criteria=dict(tr.get("promotion_criteria", {})),
                validation=TierValidation(
                    strategy=ValidationStrategy(
                        v.get("strategy", ValidationStrategy.SHADOW_COMPARE.value)
                    ),
                    confidence_threshold=v.get("confidence_threshold", 0.0),
                    shadow_window=v.get("shadow_window", 0),
                ),
            )

        if "telemetry" in d:
            tm = d["telemetry"]
            kwargs["telemetry"] = Telemetry(
                feed=tm["feed"], track=list(tm.get("track", []))
            )

        if "context" in d:
            c = d["context"]
            kwargs["context"] = Context(
                disclosure=Disclosure(c.get("disclosure", Disclosure.PULL.value)),
                payload=c.get("payload", ""),
                dock_composition=DockComposition(
                    c.get("dock_composition", DockComposition.NONE.value)
                ),
            )

        if "lifecycle" in d:
            lc = d["lifecycle"]
            kwargs["lifecycle"] = Lifecycle(
                state=LifecycleState(lc["state"]),
                provenance=Provenance(
                    lc.get("provenance", Provenance.OPERATOR_AUTHORED.value)
                ),
                created_at=lc.get("created_at", ""),
                last_used=lc.get("last_used"),
                use_count=lc.get("use_count", 0),
                flywheel_eligible=lc.get("flywheel_eligible", False),
                pinned=lc.get("pinned", False),
                body_hash=lc.get("body_hash"),
            )

        if "lineage" in d:
            ln = d["lineage"]
            kwargs["lineage"] = Lineage(
                source_patterns=list(ln.get("source_patterns", [])),
                parent_id=ln.get("parent_id"),
                decision_log=[
                    TransitionRecord(
                        actor=r["actor"],
                        timestamp=r["timestamp"],
                        from_state=r["from_state"],
                        to_state=r["to_state"],
                        reason=r["reason"],
                        evidence=list(r.get("evidence", [])),
                    )
                    for r in ln.get("decision_log", [])
                ],
            )

        if "skill" in d and d["skill"] is not None:
            sk = d["skill"]
            kwargs["skill"] = SkillPresentation(category=sk.get("category", "general"))

        if "platform" in d:
            kwargs["platform"] = d["platform"]

        # structural-review-gate-v1 — carry the governance block through verbatim
        # (present-key only; absent -> None default). Opaque dict, not cast into a
        # nested record: the enforcement seams read it and fail closed on malformed
        # shape, so the loader stays a pass-through and validate() never touches it.
        # fleet-artifact-legibility-v1 C1 — the ONE exception to pass-through: the
        # OPTIONAL terminal_artifact.presentation block is shape-validated here
        # (loud warn + treated absent on malformed; loading NEVER fails on it —
        # presentation is enhancement, disposition is governance).
        if "governance" in d:
            _validate_presentation(d["governance"], d.get("id", "<no id>"))
            # wiki-writer-structured-output-v1 P1 — the SECOND validated-optional
            # terminal_artifact sibling (emit), same non-destructive discipline.
            _validate_emit(d["governance"], d.get("id", "<no id>"))
            kwargs["governance"] = d["governance"]

        # R5 — per-skill model binding (present-key only; absent -> None default).
        if "model_binding" in d and d["model_binding"] is not None:
            mb = d["model_binding"]
            kwargs["model_binding"] = ModelBinding(type=mb.get("type"), tier=mb.get("tier"))

        # background-worker-runtime-v1 — read_surfaces (present-key only; absent
        # -> empty-list default). Carried verbatim; validate() checks the
        # vocabulary so a round-trip never silently drops or mangles a token.
        if "read_surfaces" in d:
            kwargs["read_surfaces"] = list(d["read_surfaces"])

        if "failure" in d:
            f = d["failure"]
            cb = f.get("circuit_breaker", {})
            kwargs["failure"] = Failure(
                fallback=FailureFallback(
                    f.get("fallback", FailureFallback.HALT_AND_SURFACE.value)
                ),
                diagnostic_context=list(f.get("diagnostic_context", [])),
                circuit_breaker=CircuitBreaker(
                    threshold=cb.get("threshold", 0),
                    window_seconds=cb.get("window_seconds", 0),
                ),
            )

        return cls(**kwargs)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)

    @classmethod
    def from_yaml(cls, text: str) -> "Capability":
        return cls.from_dict(yaml.safe_load(text))
