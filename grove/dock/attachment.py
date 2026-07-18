"""goal-spine-v1 P1 — the goal-attachment detector (dry-run only).

Off-path: reads the Kaizen ledger's ``artifact_written`` events, prefilters
artifact content against the Dock goals' K2 projection pages (Stage 1,
recall-oriented), and asks the config-declared adjudicator tier whether each
candidate artifact ADVANCES the matched goal (Stage 2, direction-explicit).

P1 is inert by ruling (PM scope amendment): a MANUAL entry point
(``python -m grove.dock.attachment``) that prints a dry-run report. It emits
NO proposal, writes NO ledger event, and is NOT wired into the Dispatcher
detector sweep — wiring lands in P3 alongside emission.

Design rulings baked in (P1 gate):

* G1 — the eventual home is the Dispatcher detector sweep
  (``_extract_memory_from_dormant_sessions``), whose dormancy gating
  satisfies R-6 (terminal-gated adjudication) structurally.
* G2 — Stage 1 scores via ``WikiIndex.query(text, source_type="dock_goal")``
  over the K2 goal-projection pages. The scorer is corpus-bound, so
  :func:`verify_goal_projection_coverage` fires LOUD on any active goal
  without a projection page — a gap is a permanent silent miss, forbidden.
* G5 — artifact content is read through the ONE containment implementation
  (``grove.api.artifacts.resolve_contained_path`` over the shared
  ``resolve_recorded_path`` core). No second containment copy.
* R-5 — ``goal_alignment`` (joined via ``turn_id`` →
  ``IntentStore.latest_by_turn``) may only PREFILTER; it never asserts
  attachment.
* R-7 — an artifact whose turn has no IntentRecord (or a record without
  ``goal_alignment``) stays ELIGIBLE on content evidence; the unknown is
  recorded explicitly in the report, never treated as orthogonal.

Cursor: a timestamp watermark under ``$GROVE_HOME/state/`` so a run does not
re-adjudicate the whole ledger. Events are compared by their ISO-8601 UTC
``timestamp``; an event back-dated below an already-saved watermark (clock
skew across sessions) is not revisited — the cursor is monotonic by design.
Delete the cursor file to re-scan from the beginning.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ── config (the load_pattern_cache_config section-loader precedent) ─────────


_DEFAULTS: Dict[str, Any] = {
    "adjudicator_tier": "T-GA",
    "stage2_candidate_cap": 5,
    "prefilter_top_k": 3,
    # P2 — bound on the adjudication excerpt stored in the
    # artifact_goal_attached event (attachment_store.mint_attachment).
    "excerpt_cap_chars": 600,
}

# Character bound on artifact content used for BOTH the Stage-1 retrieval
# query and the Stage-2 adjudication prompt. A bound, not a policy knob —
# the config-valued knobs are the cap and the retrieval depth above.
_CONTENT_CHARS = 4000

# Stage-2 verdict output ceiling — matches the T-GA tier's declared
# max_tokens (the quality-gate _EVAL_MAX_TOKENS precedent: a reasoning
# binding spends budget thinking before the forced tool call materializes).
_ADJ_MAX_TOKENS = 4096

# R-5 prefilter set: a turn whose latest IntentRecord carries one of these
# goal_alignment values is skipped in Stage 1. DELIBERATELY narrow —
# "no_goals_set" stays eligible (goals may exist NOW that did not at
# classification time) and absent/None alignment stays eligible (R-7:
# unknown is not orthogonal).
_ALIGNMENT_PREFILTER_EXCLUDES = frozenset({"orthogonal", "distracting"})

_VALID_VERDICTS = ("advances", "neutral", "counter")


def load_goal_attachment_config() -> Dict[str, Any]:
    """Read the ``goal_attachment`` section from routing.config.yaml.

    Operator copy (``~/.grove/routing.config.yaml``) wins over the repo
    default (``config/routing.config.yaml``) — the
    ``load_pattern_cache_config`` precedent. A missing section falls back to
    :data:`_DEFAULTS`; a malformed YAML file raises (fail loud, never a
    silent default over a broken config).
    """
    import yaml

    cfg = dict(_DEFAULTS)
    candidates = (
        Path.home() / ".grove" / "routing.config.yaml",
        Path(__file__).resolve().parents[2] / "config" / "routing.config.yaml",
    )
    for path in candidates:
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = data.get("goal_attachment")
        if isinstance(section, dict):
            for key in _DEFAULTS:
                if key in section:
                    cfg[key] = section[key]
        break
    return cfg


# ── P3 exclusion seams (explicit, empty-set-returning by design) ────────────


def attached_artifact_ids() -> Set[str]:
    """Artifact ids already attached to a live goal — SKIPPED by the detector.

    FILLED in P2: reads the attachment projection
    (``grove.dock.attachment_store.attached_artifact_ids``, read-time
    collapse over artifact_goal_attached/detached events). Scoped to LIVE
    Dock goals (R-9): an attachment whose goal was since pruned contributes
    nothing, so that artifact is eligible for re-adjudication. Dock absent →
    no live goals → no exclusions (the detector has already failed loud on
    a missing Dock before this runs).
    """
    from grove.dock import load_dock
    from grove.dock.attachment_store import (
        attached_artifact_ids as _store_attached,
    )

    dock = load_dock()
    live = {g.id for g in dock.goals} if dock is not None else set()
    return _store_attached(live_goal_ids=live)


def suppressed_artifact_ids() -> Set[str]:
    """Artifact ids carrying a rejection tombstone — SKIPPED by the detector.

    STILL the empty-set seam in P2 (only the attached seam filled): the
    suppression store does not exist yet. P3's reject callback fills this
    from operator-rejection tombstones (the binding-telemetry
    ``record_tombstone`` precedent), so a rejected attachment is never
    re-proposed.
    """
    return set()


# ── projection coverage (G2 hard condition) ─────────────────────────────────


class GoalProjectionGapError(RuntimeError):
    """An active Dock goal has no K2 projection page — the Stage-1 scorer is
    corpus-bound on those pages, so the goal could never surface a candidate.
    A silent permanent miss is forbidden (G2 hard condition); this fires
    instead."""


def verify_goal_projection_coverage(
    goals: List[Any], wiki_root: Optional[Path] = None
) -> None:
    """FIRE LOUD when any active goal lacks its ``dock_goal`` projection page.

    Uses the pipeline's own expected-hash helper (``_dock_source_hash``,
    GUARD P2-d: shares the prefix constant, sha256, and hash length with the
    page writer — never a re-spelled literal), so this check and the writer's
    filenames cannot silently desync.
    """
    from hermes_constants import get_wiki_path

    from grove.wiki.pipeline import _DOCK_GOAL_SOURCE_TYPE, _dock_source_hash

    root = Path(wiki_root) if wiki_root else get_wiki_path()
    pages_dir = root / "pages" / _DOCK_GOAL_SOURCE_TYPE
    missing: List[str] = []
    for goal in goals:
        expected = _dock_source_hash(goal.id)
        if not any(pages_dir.glob(f"*-{expected}.md")):
            missing.append(goal.id)
    if missing:
        raise GoalProjectionGapError(
            f"Active Dock goal(s) with NO dock_goal projection page under "
            f"{pages_dir}: {sorted(missing)}. The Stage-1 scorer is "
            f"corpus-bound on projection pages, so these goals can never "
            f"surface a candidate. Run the Dock projection "
            f"(grove.wiki.pipeline.project_dock) and re-run the detector."
        )


# ── cursor watermark (GROVE_HOME state, never the repo tree) ────────────────


def _cursor_path(home: Path) -> Path:
    return home / "state" / "goal_attachment.cursor.json"


def _load_cursor(home: Path) -> Optional[str]:
    """The saved ISO-8601 watermark, or None on first run."""
    path = _cursor_path(home)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    watermark = data.get("watermark")
    if watermark is not None and not isinstance(watermark, str):
        raise ValueError(
            f"goal_attachment cursor at {path} has a non-string watermark: "
            f"{watermark!r} — delete the file to reset."
        )
    return watermark


def _save_cursor(home: Path, watermark: str) -> None:
    """Atomic write (tmp + os.replace, the dock-writer precedent)."""
    path = _cursor_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"watermark": watermark}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ── report shapes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttachmentCandidate:
    """One Stage-1 hit: an artifact scored against a goal's projection page."""

    artifact_id: str
    path: str
    goal_id: str
    relevance_score: float
    turn_id: Optional[str]
    goal_alignment: Optional[str]
    content: str  # bounded to _CONTENT_CHARS; feeds Stage 2


@dataclass(frozen=True)
class AdjudicatedCandidate:
    """A Stage-2 ruling. Excerpt + rationale are REQUIRED — they are the
    operator's sub-second verdict surface in P3."""

    candidate: AttachmentCandidate
    verdict: str  # advances | neutral | counter
    excerpt: str
    rationale: str


@dataclass
class AttachmentDryRunReport:
    """Everything a run saw and decided — printed, never persisted (P1)."""

    watermark_before: Optional[str]
    watermark_after: Optional[str]
    events_scanned: int
    events_new: int
    excluded_attached: int
    excluded_suppressed: int
    alignment_filtered: List[Tuple[str, str]] = field(default_factory=list)
    alignment_unknown: List[str] = field(default_factory=list)  # R-7 record
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)  # no goal page surfaced
    cap_dropped: int = 0
    adjudicated: List[AdjudicatedCandidate] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "goal-attachment dry-run report (P1 — no proposal, no ledger event)",
            "=" * 68,
            f"watermark: {self.watermark_before!r} -> {self.watermark_after!r}",
            f"events scanned: {self.events_scanned} "
            f"(new since watermark: {self.events_new})",
            f"excluded — already attached: {self.excluded_attached}, "
            f"suppressed: {self.excluded_suppressed}",
        ]
        if self.alignment_filtered:
            lines.append("alignment-prefiltered (R-5, prefilter only):")
            for aid, alignment in self.alignment_filtered:
                lines.append(f"  - {aid}  goal_alignment={alignment}")
        if self.alignment_unknown:
            lines.append(
                "alignment UNKNOWN — eligible on content evidence (R-7):"
            )
            for aid in self.alignment_unknown:
                lines.append(f"  - {aid}")
        if self.unreadable:
            lines.append("UNREADABLE (containment refusal or read failure):")
            for aid, reason in self.unreadable:
                lines.append(f"  - {aid}  {reason}")
        if self.unmatched:
            lines.append("no goal surfaced by Stage-1 retrieval:")
            for aid in self.unmatched:
                lines.append(f"  - {aid}")
        if self.cap_dropped:
            lines.append(
                f"CAP: {self.cap_dropped} candidate(s) NOT adjudicated this "
                f"run (stage2_candidate_cap). The cursor has advanced past "
                f"them — raise the cap and delete the cursor file to "
                f"revisit. Nothing was dropped silently."
            )
        lines.append(f"adjudicated: {len(self.adjudicated)}")
        for adj in self.adjudicated:
            c = adj.candidate
            lines.extend(
                [
                    "-" * 68,
                    f"artifact {c.artifact_id}  ({c.path})",
                    f"goal     {c.goal_id}  "
                    f"(stage-1 score {c.relevance_score:.3f}, "
                    f"turn {c.turn_id!r}, alignment {c.goal_alignment!r})",
                    f"verdict  {adj.verdict}",
                    f"excerpt  {adj.excerpt}",
                    f"rationale {adj.rationale}",
                ]
            )
        return "\n".join(lines)


# ── Stage-2 adjudication (direction-explicit, forced tool) ──────────────────


class MalformedAdjudication(ValueError):
    """The adjudicator returned a structurally invalid verdict — loud."""


_ADJ_SYSTEM = (
    "You adjudicate whether an artifact ADVANCES an operator goal. "
    "Topical adjacency is NOT alignment: an artifact can mention a goal's "
    "topic while doing nothing for it, and an artifact arguing AGAINST the "
    "goal's premise is topically close but counter-aligned. Rule on "
    "DIRECTION: does this artifact move the goal toward its definition of "
    "done? Respond only via the verdict tool."
)

_ADJ_TOOL: Dict[str, Any] = {
    "name": "goal_attachment_verdict",
    "description": (
        "Record a structured directional ruling on whether the artifact "
        "advances the goal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(_VALID_VERDICTS),
                "description": (
                    "advances: the artifact moves the goal toward its "
                    "definition of done. neutral: topically related or "
                    "unrelated, but does not advance it. counter: works "
                    "against the goal's premise or direction."
                ),
            },
            "excerpt": {
                "type": "string",
                "description": (
                    "A short verbatim quote from the artifact that best "
                    "evidences the verdict."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "One to three sentences: why the verdict holds, in "
                    "terms of the goal's definition of done."
                ),
            },
        },
        "required": ["verdict", "excerpt", "rationale"],
    },
}


def _validate_adjudication(raw: Any) -> Dict[str, str]:
    """Structural validation of the forced-tool verdict — loud on mismatch
    (the quality-gate MalformedVerdict precedent)."""
    if not isinstance(raw, dict):
        raise MalformedAdjudication(
            f"adjudicator returned {type(raw).__name__}, expected dict"
        )
    verdict = raw.get("verdict")
    excerpt = raw.get("excerpt")
    rationale = raw.get("rationale")
    if verdict not in _VALID_VERDICTS:
        raise MalformedAdjudication(
            f"verdict {verdict!r} not in {_VALID_VERDICTS}"
        )
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise MalformedAdjudication("excerpt missing or empty — required")
    if not isinstance(rationale, str) or not rationale.strip():
        raise MalformedAdjudication("rationale missing or empty — required")
    return {
        "verdict": verdict,
        "excerpt": excerpt.strip(),
        "rationale": rationale.strip(),
    }


# ── the detector ────────────────────────────────────────────────────────────


class GoalAttachmentDetector:
    """Two-stage, off-path, dry-run-only (P1) goal-attachment detector.

    Detection is separable from emission by construction (the
    DockMutationDetector shape): :meth:`detect` returns a report and stages
    NOTHING. There is no staging method in P1 — P3 adds it alongside the
    Kaizen proposal type.

    Every collaborator is injectable for tests; production defaults resolve
    lazily inside :meth:`detect`.
    """

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None,
        dock: Optional[Any] = None,
        wiki_index: Optional[Any] = None,
        intent_store: Optional[Any] = None,
        adjudicate: Optional[Callable[..., Dict[str, str]]] = None,
        artifact_roots: Optional[List[Path]] = None,
    ) -> None:
        self._home = Path(home) if home is not None else Path(get_hermes_home())
        self._config = config if config is not None else load_goal_attachment_config()
        self._dock = dock
        self._wiki_index = wiki_index
        self._intent_store = intent_store
        self._adjudicate = adjudicate or self._adjudicate_via_tier
        self._artifact_roots = artifact_roots

    # -- production collaborators (lazy) ------------------------------------

    def _resolve_dock(self) -> Any:
        if self._dock is not None:
            return self._dock
        from grove.dock import load_dock

        dock = load_dock()
        if dock is None:
            raise RuntimeError(
                "Dock not installed (no dock.yaml) — the goal-attachment "
                "detector has nothing to attach to. Install the Dock or "
                "pass dock= explicitly."
            )
        return dock

    def _resolve_wiki_index(self) -> Any:
        if self._wiki_index is not None:
            return self._wiki_index
        from grove.wiki.index import WikiIndex

        return WikiIndex()

    def _resolve_intent_store(self) -> Any:
        if self._intent_store is not None:
            return self._intent_store
        from grove.intent_store import IntentStore

        return IntentStore()

    def _resolve_roots(self) -> List[Path]:
        if self._artifact_roots is not None:
            return list(self._artifact_roots)
        from grove.api.artifacts import resolve_artifact_roots

        return resolve_artifact_roots()

    # -- Stage 2 default adjudicator (config tier, no hardcoded model) ------

    def _adjudicate_via_tier(
        self, *, artifact_text: str, goal: Any
    ) -> Dict[str, str]:
        """One forced-tool call on the config-declared adjudicator tier.

        Tier resolves BY NAME through ``call_t1(tier=...)`` (the T-QA
        quality-gate pattern); an unknown tier raises KeyError — never a
        fallback model (G3 / A2)."""
        from grove.t1_call import call_t1

        tier = self._config["adjudicator_tier"]
        dod = " ".join(str(goal.definition_of_done or "").split())
        prompt = (
            f"GOAL: {goal.name}\n"
            f"GOAL ID: {goal.id}\n"
            f"DEFINITION OF DONE: {dod or '(none declared)'}\n"
            f"KEYWORDS: {', '.join(goal.keywords) or '(none)'}\n\n"
            f"ARTIFACT CONTENT (bounded excerpt):\n{artifact_text}\n\n"
            "Does this artifact ADVANCE the goal toward its definition of "
            "done — not merely mention its topic? Record your directional "
            "ruling via the tool."
        )
        raw = call_t1(
            prompt,
            system=_ADJ_SYSTEM,
            tool=_ADJ_TOOL,
            max_tokens=_ADJ_MAX_TOKENS,
            tier=tier,
        )
        return _validate_adjudication(raw)

    # -- the dry run --------------------------------------------------------

    def detect(self) -> AttachmentDryRunReport:
        """Run both stages and return the dry-run report.

        Emits NO proposal and writes NO ledger event (P1 invariant, pinned
        by tests). Advances the cursor watermark on completion.
        """
        from grove.api.artifacts import (
            _scan_artifact_events,
            _scan_ledger_index,
            resolve_contained_path,
        )
        from grove.dock import active_goals
        from grove.wiki.pipeline import _DOCK_GOAL_SOURCE_TYPE

        dock = self._resolve_dock()
        goals = active_goals(dock)
        if not goals:
            raise RuntimeError(
                "Dock has no active goals — nothing to attach. "
                "(Staging/parked goals are not adjudication targets.)"
            )
        goals_by_id = {g.id: g for g in goals}

        # G2 hard condition — every active goal must have a projection page
        # or the corpus-bound scorer silently never surfaces it. Fires loud.
        wiki = self._resolve_wiki_index()
        wiki_root = getattr(wiki, "_wiki_root", None)
        verify_goal_projection_coverage(goals, wiki_root=wiki_root)

        watermark = _load_cursor(self._home)
        events = _scan_artifact_events()
        new_events = [
            e
            for e in events
            if isinstance(e.get("timestamp"), str)
            and (watermark is None or e["timestamp"] > watermark)
        ]

        # Latest event per artifact wins (the _recent_artifacts precedent).
        by_artifact: Dict[str, dict] = {}
        for event in new_events:
            aid = event.get("artifact_id")
            if isinstance(aid, str) and aid:
                by_artifact[aid] = event

        report = AttachmentDryRunReport(
            watermark_before=watermark,
            watermark_after=watermark,
            events_scanned=len(events),
            events_new=len(new_events),
            excluded_attached=0,
            excluded_suppressed=0,
        )

        # Exclusion seams (P3 fills both; empty sets in P1 by design).
        attached = attached_artifact_ids()
        suppressed = suppressed_artifact_ids()

        # R-5 join surface: turn_id -> latest IntentRecord.
        latest_by_turn = {
            r.turn_id: r for r in self._resolve_intent_store().latest_by_turn()
        }

        index = _scan_ledger_index()
        roots = self._resolve_roots()
        top_k = int(self._config["prefilter_top_k"])

        candidates: List[AttachmentCandidate] = []
        for aid, event in by_artifact.items():
            if aid in attached:
                report.excluded_attached += 1
                continue
            if aid in suppressed:
                report.excluded_suppressed += 1
                continue

            turn_id = event.get("turn_id")
            record = (
                latest_by_turn.get(turn_id)
                if isinstance(turn_id, str)
                else None
            )
            alignment = getattr(record, "goal_alignment", None)
            if alignment in _ALIGNMENT_PREFILTER_EXCLUDES:
                # R-5: prefilter ONLY — the alignment never asserts anything.
                report.alignment_filtered.append((aid, alignment))
                continue
            if record is None or alignment is None:
                # R-7: unknown stays eligible; record the unknown, loudly.
                report.alignment_unknown.append(aid)

            # G5 — the ONE containment implementation, app-free form.
            path = resolve_contained_path(aid, index=index, roots=roots)
            if path is None:
                report.unreadable.append(
                    (aid, "containment refusal (unknown id / escape / vanished)")
                )
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                report.unreadable.append((aid, f"read failed: {exc!r}"))
                continue
            content = content[:_CONTENT_CHARS]
            if not content.strip():
                report.unreadable.append((aid, "empty content"))
                continue

            # Stage 1 — corpus-bound relevance against dock_goal projection
            # pages only (G2 ruling; resolve_goal rejected as the FL2 second
            # resolver).
            results = wiki.query(
                content, k=top_k, source_type=_DOCK_GOAL_SOURCE_TYPE
            )
            hit = next(
                (
                    r
                    for r in results
                    if r.dock_goal_refs and r.dock_goal_refs[0] in goals_by_id
                ),
                None,
            )
            if hit is None:
                report.unmatched.append(aid)
                continue

            candidates.append(
                AttachmentCandidate(
                    artifact_id=aid,
                    path=str(path),
                    goal_id=hit.dock_goal_refs[0],
                    relevance_score=float(hit.relevance_score),
                    turn_id=turn_id if isinstance(turn_id, str) else None,
                    goal_alignment=alignment,
                    content=content,
                )
            )

        # CAP — config-valued, never a literal. Dropped candidates are
        # reported loudly (no silent caps).
        cap = int(self._config["stage2_candidate_cap"])
        candidates.sort(key=lambda c: c.relevance_score, reverse=True)
        promoted, dropped = candidates[:cap], candidates[cap:]
        report.cap_dropped = len(dropped)

        for candidate in promoted:
            ruling = self._adjudicate(
                artifact_text=candidate.content,
                goal=goals_by_id[candidate.goal_id],
            )
            report.adjudicated.append(
                AdjudicatedCandidate(
                    candidate=candidate,
                    verdict=ruling["verdict"],
                    excerpt=ruling["excerpt"],
                    rationale=ruling["rationale"],
                )
            )

        # Advance the watermark over everything this run SAW (new events),
        # adjudicated or not — monotonic cursor; delete the file to re-scan.
        if new_events:
            new_watermark = max(e["timestamp"] for e in new_events)
            _save_cursor(self._home, new_watermark)
            report.watermark_after = new_watermark

        return report


def main() -> None:
    """Manual dry-run entry point (P1). Prints the report; changes nothing
    but the cursor watermark."""
    logging.basicConfig(level=logging.INFO)
    report = GoalAttachmentDetector().detect()
    print(report.render())


if __name__ == "__main__":
    main()
