"""Grove Dock — Sprint 68 the-dock-v1.

The Dock is the operator's strategic command center: long-running goals
that let the Autonomaton understand WHY an intent is being asked, and
silently collapse an ambiguous request into the operator's local reality
(Superposition Collapse). Local-first, Obsidian-compatible — plain
markdown under ``~/.grove/dock/goals/`` with a ``dock.yaml`` manifest.

This module is the pure-function loader surface:

* :func:`load_dock` reads + validates ``dock.yaml``.
* :func:`active_goals` filters to ``accelerating`` / ``cruising``.
* :func:`build_classifier_goals_block` renders the OPERATOR GOALS text
  the classifier scores ``goal_alignment`` against.
* :func:`_safe_read` is the Obsidian-race-tolerant file reader
  (Component 4 / Component 5 consume it).

Sovereignty note — the Dock reads the RUNTIME copy at
``$GROVE_HOME/dock/dock.yaml`` ONLY. Unlike the Sprint 29
``tool_groups.yaml`` loader, the repo template at ``config/dock/`` is a
SEED source, not a live fallback: Dock goals are operator-owned
strategic data and must never silently activate off example seeds. A
missing runtime ``dock.yaml`` means "Dock not installed" — graceful, the
legacy ``goals.md`` classifier path is unaffected (GATE-B DECISION 2).

Failure register (the dual-register discipline, GATE-A finding 3):

* runtime ``dock.yaml`` ABSENT       → :func:`load_dock` returns None
                                        (graceful; Dock opt-in overlay).
* runtime ``dock.yaml`` MALFORMED    → ``ValueError`` (fail-loud, like
                                        the Sprint 29 taxonomy loader).
* a ``context_sources`` file present
  but unreadable                     → :func:`_safe_read` retries
                                        100/200/400ms, then fail-loud.

The classifier's ``_read_goals_content`` (grove/classify.py) is the one
sanctioned graceful consumer: it wraps the Dock call and degrades to the
legacy ``goals.md`` text on ANY Dock failure. The per-turn injection path
(Component 3) does NOT swallow — a missing promised context file Andons
the turn.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "Goal",
    "Dock",
    "TurnGoalContext",
    "DockBudgetAndon",
    "VECTOR_RANK",
    "ACTIVE_STATUSES",
    "load_dock",
    "active_goals",
    "build_classifier_goals_block",
    "resolve_goal",
    "load_goal_context",
    "build_turn_goal_context",
]

# Vector priority for conflict resolution (Component 5): higher wins.
# Sprint 69.2 added ``operational`` and ``product`` between strategic and
# personal, preserving the apex > strategic > personal ordering the
# conflict-resolution tests rely on.
VECTOR_RANK: Dict[str, int] = {
    "apex_strategic": 5,
    "strategic": 4,
    "operational": 3,
    "product": 2,
    "personal": 1,
}
_VALID_VECTORS = frozenset(VECTOR_RANK)

# Status taxonomy. Active goals are surfaced to the classifier and
# eligible for context loading; the rest are dormant. Sprint 69.2 admitted
# ``staging`` / ``blocked`` / ``parked`` to the VALID set (the operator's
# expanded Dock uses them) WITHOUT making them active — only accelerating
# and cruising surface.
ACTIVE_STATUSES = frozenset({"accelerating", "cruising"})
_VALID_STATUSES = frozenset({
    "accelerating", "cruising", "staging", "blocked", "parked",
    "paused", "complete",
})

# Accepted ``version`` values. The Sprint 68 seed used the int ``1``; the
# operator's expanded dock.yaml declares the string ``"1.0"``. Both (and
# the bare string ``"1"``) name schema version 1; anything else fails loud.
_SUPPORTED_VERSIONS = frozenset({1, "1", "1.0"})

# Default per-turn goal-context char budget (GATE-B DECISION 4; raised
# 4000 → 5000 in Sprint 69.2 for the 9-goal Dock). Overridable via the
# ``context_char_budget`` top-level key in dock.yaml.
_DEFAULT_CONTEXT_CHAR_BUDGET = 5000

_REQUIRED_GOAL_KEYS = frozenset({
    "id", "name", "vector", "status", "definition_of_done",
    "context_sources", "keywords", "unlocked_skills",
})


@dataclass(frozen=True)
class Goal:
    """One Dock goal, parsed from a ``dock.yaml`` ``goals[]`` entry.

    ``context_sources`` paths are stored relative to the Dock root
    (the directory holding ``dock.yaml``); resolve via
    :meth:`resolved_sources`.
    """

    id: str
    name: str
    vector: str
    status: str
    definition_of_done: str
    context_sources: Tuple[str, ...]
    keywords: Tuple[str, ...]
    unlocked_skills: Tuple[str, ...]
    root: Path
    # Sprint 69.2: unknown per-goal keys (deadline, why_this_matters,
    # milestones, targets, content_zones, tracking, ...) pass through here
    # — accessible, not consumed. compare=False keeps the frozen dataclass
    # hashable/eq-safe despite the dict.
    extra: Dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def rank(self) -> int:
        return VECTOR_RANK[self.vector]

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def resolved_sources(self) -> List[Path]:
        """Resolve each ``context_sources`` entry to an absolute path.

        Sprint 68 stored sources relative to the Dock root. Sprint 69.2's
        operator dock.yaml declares absolute ``~/.grove/dock/goals/*.md``
        paths, so expand ``~`` and honor already-absolute paths; relative
        entries still resolve against the root (back-compatible).
        """
        out: List[Path] = []
        for src in self.context_sources:
            p = Path(src).expanduser()
            out.append(p if p.is_absolute() else self.root / p)
        return out


@dataclass(frozen=True)
class Dock:
    """A parsed, validated Dock manifest."""

    goals: Tuple[Goal, ...]
    context_char_budget: int
    root: Path
    # Sprint 69.2: the full top-level mapping (routing_hints,
    # operator_preferences, notion_sources, design_system, ...) passes
    # through here — accessible, not consumed. compare=False keeps the
    # frozen dataclass hashable/eq-safe despite the dict.
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)


def _resolve_dock_path() -> Path:
    """Runtime sovereign path: ``$GROVE_HOME/dock/dock.yaml``.

    No template fallback — see the module docstring's sovereignty note.
    """
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "dock" / "dock.yaml"


def load_dock(path: Optional[Path] = None) -> Optional[Dock]:
    """Load + validate the Dock manifest.

    Args:
        path: explicit ``dock.yaml`` path (tests pass this). When None,
            resolves the runtime sovereign path.

    Returns:
        A validated :class:`Dock`, or ``None`` when the manifest is
        absent (graceful "Dock not installed", GATE-B DECISION 2).

    Raises:
        ValueError: the manifest exists but is malformed — fail-loud per
            the Architectural Prime Directive.
    """
    target = Path(path) if path is not None else _resolve_dock_path()
    if not target.exists():
        logger.debug("[dock] no manifest at %s — Dock not installed", target)
        return None

    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(
            f"dock.yaml at {target} is not a mapping (got "
            f"{type(raw).__name__})"
        )
    if raw.get("version") not in _SUPPORTED_VERSIONS:
        raise ValueError(
            f"dock.yaml at {target} unsupported version "
            f"{raw.get('version')!r} (expected 1)"
        )
    goals_raw = raw.get("goals")
    if not isinstance(goals_raw, list):
        raise ValueError(f"dock.yaml at {target}: goals must be a list")

    budget = raw.get("context_char_budget", _DEFAULT_CONTEXT_CHAR_BUDGET)
    if not isinstance(budget, int) or budget <= 0:
        raise ValueError(
            f"dock.yaml at {target}: context_char_budget must be a "
            f"positive int (got {budget!r})"
        )

    root = target.parent
    goals: List[Goal] = []
    seen_ids: set = set()
    for i, g in enumerate(goals_raw):
        goals.append(_parse_goal(g, i, target, root))
        gid = goals[-1].id
        if gid in seen_ids:
            raise ValueError(
                f"dock.yaml at {target}: duplicate goal id {gid!r}"
            )
        seen_ids.add(gid)

    return Dock(
        goals=tuple(goals),
        context_char_budget=budget,
        root=root,
        raw=raw,
    )


def _parse_goal(g: Any, idx: int, target: Path, root: Path) -> Goal:
    """Validate one goals[] entry and return a :class:`Goal`. Fail-loud."""
    if not isinstance(g, dict):
        raise ValueError(
            f"dock.yaml at {target}: goals[{idx}] must be a mapping"
        )
    missing = _REQUIRED_GOAL_KEYS - set(g.keys())
    if missing:
        raise ValueError(
            f"dock.yaml at {target}: goals[{idx}] missing keys "
            f"{sorted(missing)}"
        )
    vector = g["vector"]
    if vector not in _VALID_VECTORS:
        raise ValueError(
            f"dock.yaml at {target}: goals[{idx}] (id={g['id']!r}) vector "
            f"{vector!r} invalid; expected one of {sorted(_VALID_VECTORS)}"
        )
    status = g["status"]
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"dock.yaml at {target}: goals[{idx}] (id={g['id']!r}) status "
            f"{status!r} invalid; expected one of {sorted(_VALID_STATUSES)}"
        )
    for list_key in ("context_sources", "keywords", "unlocked_skills"):
        if not isinstance(g[list_key], list):
            raise ValueError(
                f"dock.yaml at {target}: goals[{idx}] (id={g['id']!r}) "
                f"{list_key} must be a list"
            )
    extra = {k: v for k, v in g.items() if k not in _REQUIRED_GOAL_KEYS}
    return Goal(
        id=str(g["id"]),
        name=str(g["name"]),
        vector=vector,
        status=status,
        definition_of_done=str(g["definition_of_done"]),
        context_sources=tuple(str(s) for s in g["context_sources"]),
        keywords=tuple(str(k) for k in g["keywords"]),
        unlocked_skills=tuple(str(s) for s in g["unlocked_skills"]),
        root=root,
        extra=extra,
    )


def active_goals(dock: Dock) -> List[Goal]:
    """The goals eligible for surfacing — status accelerating / cruising."""
    return [g for g in dock.goals if g.is_active]


# ── Classifier-facing OPERATOR GOALS block ───────────────────────────
#
# Two distinct goal blocks exist (GATE-B implementation refinement):
#   * THIS block feeds the CLASSIFIER's OPERATOR GOALS slot so it can
#     score goal_alignment. Built from the manifest ONLY (name / vector /
#     definition_of_done / keywords) — no per-goal file reads, so the
#     classifier's sanctioned-graceful path never depends on an Obsidian
#     race. Carries the CLASSIFICATION DIRECTIVE.
#   * The per-turn MAIN-AGENT injection block (Component 3) carries the
#     Superposition Collapse partner framing + the matched goal's loaded
#     context. That one reads files and is budget-guarded.

_CLASSIFIER_DIRECTIVE = (
    "CLASSIFICATION DIRECTIVE: when scoring goal_alignment, judge the "
    "request against the ACTIVE GOALS above. `direct` = it advances one "
    "of these goals. `indirect` = it supports something that helps one. "
    "`orthogonal` / `distracting` as defined. With goals present, do not "
    "return no_goals_set — use `orthogonal` when nothing applies."
)


def build_classifier_goals_block(dock: Dock) -> str:
    """Render the OPERATOR GOALS text for the classifier prompt.

    Built from the manifest alone (no file reads). Returns "" when no
    goals are active — the caller then falls back to the legacy
    ``goals.md`` text.
    """
    goals = active_goals(dock)
    if not goals:
        return ""
    lines = ["ACTIVE GOALS:"]
    for g in goals:
        lines.append(f"  • {g.name} [{g.vector}] — done when: {g.definition_of_done}")
        if g.keywords:
            lines.append(f"    touches: {', '.join(g.keywords)}")
    lines.append("")
    lines.append(_CLASSIFIER_DIRECTIVE)
    return "\n".join(lines)


# ── Per-turn goal-context injection (Component 3, Path A′) ───────────
#
# The per-turn block is injected into the CURRENT TURN's user message at
# the ephemeral seam in run_agent.py (the same mechanism memory-prefetch
# uses) — never into the cached system prompt, and never persisted to
# session history. It is rebuilt every turn from that turn's
# classification, so a goal shift purges the previous context rather than
# stacking it. Gate: only when goal_alignment == "direct" AND the keyword
# matcher resolves a goal.

# Terse Superposition Collapse framing carried inside the per-turn fence.
# Concise on purpose — it rides every direct-aligned turn.
_TURN_FRAMING = (
    "You hold long-running context on the operator's goal below. Use it "
    "silently to sharpen this answer — already know the constraints, do "
    "not ask the operator to restate what they have told you, do not "
    "announce that you are using goal context, do not recite it back. "
    "Do NOT be overbearing; the operator should feel the precision, not "
    "see the machinery."
)


@dataclass(frozen=True)
class TurnGoalContext:
    """The resolved per-turn goal-context injection.

    ``goal_id`` feeds the rolling history window (Component 5); ``block``
    is the fenced text appended to the user message at the seam.
    """

    goal_id: str
    block: str


def resolve_goal(
    dock: Dock,
    message: str,
    history: Optional[List[str]] = None,
) -> Optional[Goal]:
    """Identify which active goal a prompt touches (Ghost Active Goal Overlap).

    Keyword match first. On a single match, return it. On overlap (the
    prompt touches two goals at once), resolve deterministically:

      1. Highest vector priority wins (apex_strategic > strategic >
         personal).
      2. Tie at the top vector → rolling 3-intent history momentum: the
         most recently resolved of the tied leaders wins.
      3. Still tied (no history) → manifest order (the first declared).

    Example (the seeded Dock): "Draft an email to Doctorow about the
    GRV-001 spec" hits both ``influencer-outreach`` (email, doctorow) and
    ``grv-001-humanity-ai`` (grv-001, doctorow). Vector priority resolves
    to ``grv-001-humanity-ai`` (apex_strategic > strategic).
    """
    lowered = message.lower()
    matches = [
        g for g in active_goals(dock)
        if any(kw.lower() in lowered for kw in g.keywords)
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # 1) highest vector priority
    top_rank = max(g.rank for g in matches)
    leaders = [g for g in matches if g.rank == top_rank]
    if len(leaders) == 1:
        return leaders[0]

    # 2) tie → rolling 3-intent history momentum (most recent first)
    leader_by_id = {g.id: g for g in leaders}
    for gid in reversed(list(history or [])[-3:]):
        if gid in leader_by_id:
            return leader_by_id[gid]

    # 3) deterministic floor — first in manifest order
    return leaders[0]


class DockBudgetAndon(RuntimeError):
    """Even the frontmatter digest exceeds the context budget — fail-loud.

    Truncating full content to the frontmatter digest is a DESIGNED
    fallback, not an Andon. Only when the digest ITSELF will not fit does
    the Dock halt the turn: the operator must raise ``context_char_budget``
    or trim the goal file. Silent truncation past this floor is the
    antipattern the Architectural Prime Directive forbids.
    """


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a markdown file into (frontmatter mapping, body).

    Frontmatter is a leading ``---`` fenced YAML block. Returns ``({}, text)``
    when it is absent, unterminated, unparseable, or not a mapping — the
    caller treats "no frontmatter" as "nothing to fall back to".
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            return meta, "\n".join(lines[i + 1:]).strip()
    return {}, text


def _frontmatter_digest(meta: Dict[str, Any]) -> str:
    """The summary + latest_update fallback rendering (Component 4 step 3)."""
    out: List[str] = []
    if meta.get("summary"):
        out.append(str(meta["summary"]))
    if meta.get("latest_update"):
        out.append(f"Now: {meta['latest_update']}")
    return "\n".join(out)


def load_goal_context(goal: Goal, char_budget: int) -> str:
    """Load a goal's ``context_sources`` in order, fitting ``char_budget``.

    Sprint 69.2 made loading incremental ACROSS sources (the operator's
    multi-source goals — e.g. hermes lists a 4 KB context file plus a 20 KB
    README — must not Andon just because a *later* source is too large):

      * Sources load in declared order. Each is appended only while the
        running total stays within budget; the first source that would
        overflow is skipped (logged warning) and the rest are dropped. The
        leading sources carry the critical context, so partial load is the
        designed behavior, not a failure.
      * The FIRST source is special — it must fit alone. The single-source
        truncation pipeline (Component 4) is preserved for it:
          1. fits the budget         → full content
          2. over budget             → frontmatter digest (summary +
                                        latest_update)
          3. digest over (or absent) → :class:`DockBudgetAndon`
      * A declared source that cannot be read fails loud (``_safe_read``)
        — a missing promised file is never silently skipped.
    """
    parts: List[str] = []
    total = 0
    for i, path in enumerate(goal.resolved_sources()):
        text = _safe_read(path).strip()
        if i == 0:
            if len(text) <= char_budget:
                parts.append(text)
                total = len(text)
                continue
            # First source alone over budget → frontmatter digest fallback.
            meta, _ = _parse_frontmatter(text)
            digest = _frontmatter_digest(meta)
            if digest and len(digest) <= char_budget:
                logger.warning(
                    "[dock] goal %r first context source %s (%d chars) over "
                    "budget %d — truncated to frontmatter digest (%d chars)",
                    goal.id, path, len(text), char_budget, len(digest),
                )
                return digest
            raise DockBudgetAndon(
                f"[dock] goal {goal.id!r} first context source cannot fit the "
                f"{char_budget}-char budget: source={len(text)} chars, "
                f"frontmatter digest={len(digest)} chars. Raise "
                f"context_char_budget in dock.yaml or trim "
                f"{goal.context_sources[0]}."
            )
        # Subsequent sources: append only if the cumulative total still fits
        # (account for the "\n\n" join separator); otherwise stop.
        addition = len(text) + 2
        if total + addition <= char_budget:
            parts.append(text)
            total += addition
        else:
            logger.warning(
                "[dock] goal %r: context source %s skipped — %d-char budget "
                "exhausted at %d chars (source adds %d). Remaining sources "
                "skipped.",
                goal.id, path, char_budget, total, len(text),
            )
            break
    return "\n\n".join(parts).strip()


def build_turn_goal_context(
    dock: Dock,
    *,
    message: str,
    history: Optional[List[str]] = None,
) -> Optional[TurnGoalContext]:
    """Resolve + load the per-turn goal-context block, or None.

    Caller gates on ``goal_alignment == "direct"`` before calling this.
    Returns None when no active goal matches the prompt — the turn then
    injects nothing. Fail-loud: a missing promised context file or a
    budget ANDON propagates (the turn path does NOT swallow).
    """
    goal = resolve_goal(dock, message, history)
    if goal is None:
        return None
    context = load_goal_context(goal, dock.context_char_budget)
    block = (
        f"<grove-dock goal=\"{goal.id}\">\n"
        f"{_TURN_FRAMING}\n\n"
        f"GOAL: {goal.name}\n"
        f"{context}\n"
        f"</grove-dock>"
    )
    return TurnGoalContext(goal_id=goal.id, block=block)


# ── Obsidian-race-tolerant file reader (Component 4 / 5 consume it) ───


def _safe_read(
    path: Path,
    *,
    retries: int = 3,
    base_delay: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Read ``path``, tolerating the Obsidian mid-write race.

    Obsidian (and other editors) can briefly unlink-then-rewrite a note,
    so a read can transiently hit FileNotFoundError / PermissionError.
    Retry with exponential backoff (100ms / 200ms / 400ms by default),
    then fail-loud — no silent empty-string fallback on a file the
    manifest promised exists.

    Args:
        path: file to read.
        retries: attempts after the first read before giving up.
        base_delay: first backoff in seconds; doubles each retry.
        sleep: injected for deterministic tests (no wall clock).

    Raises:
        OSError: the final attempt failed.
    """
    attempt = 0
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError) as exc:
            if attempt >= retries:
                raise OSError(
                    f"[dock] could not read {path} after {retries + 1} "
                    f"attempts (Obsidian race or missing file): {exc!r}"
                ) from exc
            delay = base_delay * (2 ** attempt)
            logger.debug(
                "[dock] read retry %d/%d for %s in %.3fs (%r)",
                attempt + 1, retries, path, delay, exc,
            )
            sleep(delay)
            attempt += 1
