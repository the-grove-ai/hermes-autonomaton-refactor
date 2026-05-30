"""Zone classifier for the Grove Autonomaton.

Reads ``~/.grove/zones.schema.yaml`` (or the repo default at
``config/zones.schema.yaml``) and exposes a pure
``classify(action) -> ZoneResult`` query. No enforcement, no prompts, no
blocking. Sprint 06a turns this output into the Sovereignty Gate.

Action identifiers are opaque pure-dot-notation strings. The tool dispatch
layer (Sprint 06a) is responsible for mapping filesystem paths, command
lines, and other tool inputs into action identifiers before calling
``classify()``. The classifier never inspects paths or commands when
called through ``classify(action)``.

Sprint 22 adds ``classify_command_string(command, action, *, tool_id)``
— a hierarchical-first classification path that lets a tool opt into
argument-level rules via the dict form of its ``tool_zones`` entry. The
existing ``classify(action)`` path is untouched; bare-string
``tool_zones`` entries behave identically to pre-Sprint-22.

Precedence (per Sprint 03 design, corrected in Sprint 04, extended in
Sprint 22):
    1. Hierarchical ``tool_zones`` rules (Sprint 22): if the tool's entry
       is a dict with ``rules``, evaluate top-to-bottom against the
       command string and return the first match. If no rule matches,
       return ``default_zone`` for that tool. Only fired when the caller
       uses ``classify_command_string``; ``classify(action)`` ignores
       the rules list entirely (the action identifier is not the
       command string).
    2. ``tool_zones`` exact match (bare-string form)
    3. Zone rules: ``sovereign`` > ``proposes`` > ``auto_approve``
    4. Red > Yellow > Green (most restrictive wins on conflict)
    5. Unmatched action -> yellow with source ``"default"``

Pattern matching for the dot-notation path: exact match, or trailing
``.*`` (recursive — matches the prefix exactly AND every descendant).
Mid-pattern wildcards raise ``ValueError`` at load time to fail loud.

Pattern matching for the Sprint 22 hierarchical path: full Python
regex via ``re.fullmatch``. Patterns are validated at load time for
ReDoS-vulnerable shapes (nested quantifiers, excessive alternation)
and rejected per-rule with a loud log; the rest of the schema still
loads (graceful per rule, not per schema).

``reload()`` is the one SPEC-commanded graceful degradation: on parse or
validation failure, the classifier retains the last known good map and
logs the error loudly. Signal-based reload triggers (SIGHUP/SIGUSR1) are
not wired in this sprint — a later integration sprint negotiates signal
ownership with the existing handlers in ``cli.py``, ``hermes_cli/main.py``,
``tui_gateway/entry.py``, and others.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# ── Sprint 22 — Tool-id derivation ────────────────────────────────────────────
#
# Hierarchical rules live under the tool's entry in ``tool_zones``. When a
# caller asks "classify this command" without specifying which tool it
# came from, we derive the tool id from the dot-notation action prefix.
# v0.1 ships a single explicit mapping (the only tool that benefits from
# argument-level rules today is the terminal); future tools that want
# hierarchical rules should pass ``tool_id`` explicitly into
# ``classify_command_string`` rather than expand this map. Generalising
# action→tool derivation is deferred until the second tool actually
# requests it.
_ACTION_PREFIX_TO_TOOL: dict[str, str] = {
    "command.execute": "terminal",
}


def _derive_tool_id_from_action(action: str) -> Optional[str]:
    """Look up the tool that produced this dot-notation action identifier.

    Returns the registered tool name or ``None`` if the action prefix
    is not in the v0.1 map (no hierarchical-rule lookup happens then;
    the caller falls through to the existing dot-notation classify).
    """
    if not action:
        return None
    parts = action.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in _ACTION_PREFIX_TO_TOOL:
            return _ACTION_PREFIX_TO_TOOL[prefix]
    return None


# ── Sprint 22 — ReDoS / pattern safety ────────────────────────────────────────
#
# Python's stdlib ``re`` has no runtime timeout, so the only mitigation
# we can offer is structural — reject vulnerable shapes at load time.
# These constants set the conservative envelope; operators editing
# ``zones.schema.yaml`` who hit these limits will see a loud per-rule
# rejection in the logs (rest of the schema still loads).

_MAX_PATTERN_LENGTH = 200
_MAX_ALTERNATION_BRANCHES = 10
# Nested-quantifier ReDoS shape: a group containing a `+` or `*` inside,
# immediately followed by a `+` or `*` outside (with optional `?` for
# lazy quantifiers — `(.+)+?` is just as vulnerable). Catches classic
# catastrophic-backtracking patterns like ``(a+)+``, ``(.*)*``,
# ``(.+)+``. Not perfect — a determined attacker can craft a payload
# this heuristic misses — but covers the well-known cases and the
# synthesis-bug class.
_REDOS_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^)]*[+*][^)]*\)[+*]\??"
)
# Bare anything-matches patterns. Operators can write these manually
# if they really mean it, but synthesis must never emit them and we
# reject them on load so a typo doesn't accidentally green-list the
# entire universe.
_FORBIDDEN_BARE_PATTERNS = frozenset({".*", "^.*", ".*$", "^.*$", ".*/.*", ".+"})


def check_pattern_safety(pattern: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` — reject ReDoS-prone or universe-matching shapes.

    Called by the loader before ``re.compile`` and by ``save_zone_rule``
    before write. Both call sites must short-circuit on a False return
    so a dangerous pattern never reaches the matcher.
    """
    if not isinstance(pattern, str) or not pattern:
        return False, "pattern must be a non-empty string"
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return False, (
            f"pattern length {len(pattern)} exceeds limit {_MAX_PATTERN_LENGTH}"
        )
    if pattern.strip() in _FORBIDDEN_BARE_PATTERNS:
        return False, (
            f"pattern {pattern!r} matches everything — refuse to load. "
            f"Use a more specific shape, e.g. anchored prefix + /.* for "
            f"directory scope."
        )
    if _REDOS_NESTED_QUANTIFIER_RE.search(pattern):
        return False, (
            f"pattern {pattern!r} contains a nested-quantifier shape "
            f"vulnerable to catastrophic backtracking; rewrite without "
            f"`(a+)+` / `(.*)*` style nesting."
        )
    # Alternation count: only check inside groups (top-level `|` is
    # fine, the regex engine handles it well). Heuristic: count `|`
    # characters between matching parens.
    depth = 0
    branches = 0
    for ch in pattern:
        if ch == "(":
            depth += 1
            branches = 0
        elif ch == ")":
            if branches > _MAX_ALTERNATION_BRANCHES:
                return False, (
                    f"pattern {pattern!r} has a group with "
                    f"{branches + 1} alternation branches "
                    f"(max {_MAX_ALTERNATION_BRANCHES}); split into "
                    f"multiple rules."
                )
            depth = max(0, depth - 1)
            branches = 0
        elif ch == "|" and depth > 0:
            branches += 1
    # Defense in depth: confirm Python can compile the pattern at all.
    try:
        re.compile(pattern)
    except re.error as exc:
        return False, f"pattern {pattern!r} is not a valid regex: {exc}"
    return True, "ok"


@dataclass(frozen=True)
class ZoneRule:
    """One Sprint-22 argument-level rule for a tool's command string.

    Attributes:
        match_pattern: the literal regex string from the schema.
        zone: ``"green" | "yellow" | "red"`` returned on a match.
        reason: human-readable explanation surfaced to operators.
        compiled: the pre-compiled ``re.Pattern`` used for matching.
    """

    match_pattern: str
    zone: str
    reason: str
    compiled: "re.Pattern"


@dataclass(frozen=True)
class ToolZoneEntry:
    """A tool's evolved ``tool_zones`` entry: default + ordered rules.

    Bare-string entries are normalised to ``ToolZoneEntry(default_zone=<value>,
    rules=())`` so the rule-evaluation code path is uniform.
    """

    default_zone: str
    rules: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ZoneResult:
    """Classification result for an action.

    Attributes:
        zone: one of ``"green"``, ``"yellow"``, ``"red"``.
        matched_rule: the literal pattern that matched, or ``"default"``.
        source: which list/map produced the result —
            ``"tool_zones" | "sovereign" | "proposes" | "auto_approve" |
            "default" | "tool_zones.<tool>.rules" |
            "tool_zones.<tool>.default"``.
        reason: (Sprint 22) human-readable explanation when the result
            came from a hierarchical rule; ``None`` for the legacy
            dot-notation path.
        pattern_key: (Sprint 22) the matched regex pattern when the
            result came from a hierarchical rule; ``None`` otherwise.
            Used by ``approve_session`` / ``approve_permanent`` to key
            allowlist entries on the specific pattern rather than the
            rule-level category.
    """

    zone: str
    matched_rule: str
    source: str
    reason: Optional[str] = None
    pattern_key: Optional[str] = None


class ZoneClassifier:
    """Loads and queries a zones.schema.yaml file."""

    def __init__(self, schema_path: Path):
        self._schema_path = Path(schema_path)
        self._tool_zones: dict[str, str] = {}
        self._tool_zones_rich: dict[str, ToolZoneEntry] = {}
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

    def classify_command_string(
        self,
        command: str,
        action: str,
        *,
        tool_id: Optional[str] = None,
    ) -> ZoneResult:
        """Sprint 22 — hierarchical-first classification for command strings.

        Args:
            command: the full command line ("rm -rf /tmp/cache", "sudo apt
                install", etc.) used for regex matching against the tool's
                rule list.
            action: the dot-notation action identifier
                (``command.execute.rm``) used for the existing
                ``classify(action)`` fall-through.
            tool_id: which tool's rules to consult. When ``None``, derive
                from the action prefix via ``_ACTION_PREFIX_TO_TOOL``;
                pass explicitly for tools not in the v0.1 map.

        Returns:
            A ``ZoneResult``. If a hierarchical rule matched, the result
            carries ``reason`` and ``pattern_key`` populated; if no rule
            matched and the tool has a hierarchical entry, the default_zone
            is returned with ``source="tool_zones.<tool>.default"``; if
            the tool's entry is bare-string or absent, the call falls
            through to ``classify(action)`` unchanged.

            **First-match-wins** within the rule list — order matters in
            the schema. Operators write the most specific rules first.
        """
        resolved_tool = tool_id if tool_id is not None else _derive_tool_id_from_action(action)
        if resolved_tool is None:
            return self.classify(action)
        entry = self._tool_zones_rich.get(resolved_tool)
        if entry is None:
            # Bare-string ``tool_zones`` entry (or no entry at all) for the
            # resolved tool. Sovereign patterns on the action still apply
            # so ``command.execute.sudo`` etc. land RED even when the tool
            # has a bare-string entry. After sovereign, honor the bare-
            # string tool entry per the schema contract:
            #
            #   Every command flowing through this tool is classified
            #   `yellow`. The classifier never inspects the command's
            #   arguments.
            #
            # Previously this branch fell straight through to
            # ``classify(action)``, which keyed on ``command.execute.<verb>``
            # — a string the schema doesn't carry — so the bare-string
            # entry was silently ignored and every command landed on
            # default-yellow with ``source="default"``. That violated
            # the documented contract.
            for pattern in self._sovereign:
                if self._pattern_matches(pattern, action):
                    return ZoneResult(
                        zone="red", matched_rule=pattern, source="sovereign",
                    )
            if resolved_tool in self._tool_zones:
                return ZoneResult(
                    zone=self._tool_zones[resolved_tool],
                    matched_rule=resolved_tool,
                    source="tool_zones",
                )
            return self.classify(action)
        for rule in entry.rules:
            if rule.compiled.fullmatch(command):
                return ZoneResult(
                    zone=rule.zone,
                    matched_rule=rule.match_pattern,
                    source=f"tool_zones.{resolved_tool}.rules",
                    reason=rule.reason,
                    pattern_key=rule.match_pattern,
                )
        return ZoneResult(
            zone=entry.default_zone,
            matched_rule=resolved_tool,
            source=f"tool_zones.{resolved_tool}.default",
            reason=None,
            pattern_key=None,
        )

    def reload(self) -> None:
        """Reload schema from disk; on failure, keep last known good map and log loudly."""
        snapshot = (
            dict(self._tool_zones),
            dict(self._tool_zones_rich),
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
                self._tool_zones_rich,
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
        tool_zones_raw = raw.get("tool_zones", {}) or {}

        sovereign = list((zones.get("red") or {}).get("sovereign", []) or [])
        proposes = list((zones.get("yellow") or {}).get("proposes", []) or [])
        auto_approve = list((zones.get("green") or {}).get("auto_approve", []) or [])

        for pattern in sovereign + proposes + auto_approve:
            self._validate_pattern(pattern)

        # Sprint 22 — split tool_zones into bare-string (legacy) and
        # rich (hierarchical) maps. Bare entries continue to drive the
        # existing ``classify(action)`` path; rich entries are
        # consulted by ``classify_command_string`` when the caller
        # passes a command line.
        tool_zones_bare: dict[str, str] = {}
        tool_zones_rich: dict[str, ToolZoneEntry] = {}
        for tool_id, value in tool_zones_raw.items():
            if isinstance(value, str):
                tool_zones_bare[tool_id] = value
            elif isinstance(value, dict):
                entry = self._build_tool_entry(tool_id, value)
                tool_zones_rich[tool_id] = entry
                # The default_zone also seeds the bare map so any
                # caller that still uses ``classify(action)`` on the
                # tool's bare action identifier (e.g.
                # ``classify("terminal")``) gets the same default the
                # hierarchical path uses. Tool *commands* (with args)
                # only flow through ``classify_command_string``.
                tool_zones_bare[tool_id] = entry.default_zone
            else:
                raise ValueError(
                    f"tool_zones[{tool_id!r}] must be a string or a "
                    f"mapping with default_zone+rules; got {type(value).__name__}"
                )

        # All-or-nothing swap (mutation only after validation succeeds).
        self._tool_zones = tool_zones_bare
        self._tool_zones_rich = tool_zones_rich
        self._sovereign = sovereign
        self._proposes = proposes
        self._auto_approve = auto_approve

    @staticmethod
    def _build_tool_entry(tool_id: str, value: dict) -> ToolZoneEntry:
        """Parse one hierarchical ``tool_zones`` entry; reject malformed.

        Per-rule ReDoS / safety failures drop the offending rule with a
        loud log but keep the rest of the entry — graceful per rule,
        per the Sprint 22 spec.
        """
        default_zone = value.get("default_zone")
        if default_zone not in ("green", "yellow", "red"):
            raise ValueError(
                f"tool_zones[{tool_id!r}].default_zone must be one of "
                f"green/yellow/red; got {default_zone!r}"
            )
        rules_raw = value.get("rules") or []
        if not isinstance(rules_raw, list):
            raise ValueError(
                f"tool_zones[{tool_id!r}].rules must be a list; got "
                f"{type(rules_raw).__name__}"
            )
        compiled_rules = []
        for idx, rule_raw in enumerate(rules_raw):
            # Sprint 32 Phase 3b — schema faults raise at load time.
            # The agent does NOT start with malformed governance.
            # Replaces the v1.0 graceful "drop the bad rule + continue
            # loading" pattern that silently degraded the policy
            # surface. Error messages name the tool, the rule index,
            # the failed check, and where in the file to look.
            from grove.errors import SchemaConfigurationError
            if not isinstance(rule_raw, dict):
                raise SchemaConfigurationError(
                    f"zones.schema.yaml: tool_zones[{tool_id!r}]."
                    f"rules[{idx}] must be a mapping; got {rule_raw!r}. "
                    f"Fix the rule entry or remove it from the file."
                )
            pattern = rule_raw.get("match_pattern")
            zone = rule_raw.get("zone")
            reason = str(rule_raw.get("reason") or "").strip()
            if zone not in ("green", "yellow", "red"):
                raise SchemaConfigurationError(
                    f"zones.schema.yaml: tool_zones[{tool_id!r}]."
                    f"rules[{idx}].zone must be one of green/yellow/red; "
                    f"got {zone!r}. Fix the rule entry."
                )
            ok, why = check_pattern_safety(pattern)
            if not ok:
                raise SchemaConfigurationError(
                    f"zones.schema.yaml: tool_zones[{tool_id!r}]."
                    f"rules[{idx}].match_pattern rejected by safety check: "
                    f"{why}. pattern={pattern!r}. "
                    f"Tighten the pattern or remove the rule."
                )
            compiled_rules.append(
                ZoneRule(
                    match_pattern=pattern,
                    zone=zone,
                    reason=reason,
                    compiled=re.compile(pattern),
                )
            )
        return ToolZoneEntry(default_zone=default_zone, rules=tuple(compiled_rules))

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
