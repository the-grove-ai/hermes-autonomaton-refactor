"""The compaction pipeline — NormalizedDoc → CanonicalPage.

Sprint K1 (living-cellar-v1) Phase 4. :func:`compact` turns one source
document into a canonical, index-ready wiki page via at most THREE T1 calls
(:func:`grove.t1_call.call_t1`):

1. **Writer** (plain text) — produces the canonical page: a YAML-frontmatter
   header carrying ONLY the semantic fields it owns (``title``, ``topics``,
   ``key_entities``) plus a vocabulary-canonicalized body (summary, key
   findings, relationships to prior knowledge).
2. **Evaluator** (forced tool_use) — a structured verdict against the SOURCE:
   ``{complete, accurate, quality_score, issues}``. Pass/fail on
   :data:`QUALITY_THRESHOLD` (a named constant, config-promotable later).
3. **Editor** (plain text, CONDITIONAL) — runs ONLY on an Evaluator fail, at
   most once, with NO re-evaluation loop. Its output is the last word.

The DETERMINISTIC fields are pipeline-injected and never LLM-authored:
``source`` / ``source_type`` from the NormalizedDoc; ``created_at`` /
``updated_at`` derived from ``source_mtime`` (updated_at stamped now);
``dock_goal_refs`` from the adapter; ``confidence`` set from the Evaluator's
``quality_score`` (a compaction-faithfulness signal, not a Writer free-guess).

Fail loud: the Writer's (or Editor's) emitted frontmatter is parsed and
validated; unparseable/invalid output raises :class:`MalformedWriterOutput` —
no silent default, no retry-into-the-void (A6-adjacent).

A6: the pipeline reads ``NormalizedDoc.raw_content`` and writes the cellar page
only — it NEVER writes a source file.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

from hermes_constants import get_wiki_path

from grove.t1_call import T1TruncationError, call_t1
from grove.wiki.adapters import NormalizedDoc

if TYPE_CHECKING:
    from grove.dock import Goal
    from grove.memory.record import MemoryRecord

logger = logging.getLogger(__name__)

# Pass/fail line for the Evaluator's quality_score (0-1). A named constant for
# K1; promotable to config later. Pass requires complete AND accurate AND
# quality_score >= this.
QUALITY_THRESHOLD = 0.7

# Output ceilings per call (Writer/Editor need room for a full page).
_WRITER_MAX_TOKENS = 4096
_EDITOR_MAX_TOKENS = 4096
_EVAL_MAX_TOKENS = 1024

# Short source hash length — the source-stable component of the filename.
_HASH_LEN = 8

# Dock projection (Sprint K2 dock-cellar-projection-v1). A Dock goal projects to
# a canonical page with this source_type and a synthetic, source-stable
# ``source`` string ``dock.yaml#<goal id>`` — opaque to _write_page (hashed,
# never path-normalized), so the "#" is safe.
_DOCK_GOAL_SOURCE_TYPE = "dock_goal"
_DOCK_GOAL_SOURCE_PREFIX = "dock.yaml#"
_DOCK_MANIFEST_FILENAME = "dock.yaml"

# Memory graduation (memory-cellar-graduation-v1). A graduated MemoryRecord
# projects to a canonical page with this source_type and a synthetic,
# source-stable ``source`` string ``memory#<record id>`` — opaque to
# _write_page (hashed, never path-normalized), so the "#" is safe.
_MEMORY_SOURCE_TYPE = "memory_graduated"
_MEMORY_SOURCE_PREFIX = "memory#"
# Title is the record content capped at this length (or full when shorter).
_MEMORY_TITLE_MAX = 80

_EVAL_TOOL: Dict[str, Any] = {
    "name": "wiki_evaluation",
    "description": (
        "Record a structured verdict on whether the canonical page faithfully "
        "and completely compacts the source."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "complete": {
                "type": "boolean",
                "description": "Does the page capture the source's substance?",
            },
            "accurate": {
                "type": "boolean",
                "description": "Is the page free of claims the source does not support?",
            },
            "quality_score": {
                "type": "number",
                "description": "Overall compaction faithfulness, 0.0-1.0.",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific problems for the Editor to fix.",
            },
        },
        "required": ["complete", "accurate", "quality_score", "issues"],
    },
}


class MalformedWriterOutput(ValueError):
    """The Writer (or Editor) produced output that is not a valid page."""


@dataclass(frozen=True)
class CanonicalPage:
    """The compacted, index-ready page and where it was written."""

    source: str
    source_type: str
    title: str
    topics: List[str]
    key_entities: List[str]
    dock_goal_refs: List[str]
    confidence: float
    created_at: str
    updated_at: str
    body: str
    path: Path
    markdown: str
    editor_ran: bool
    evaluator_verdict: Dict[str, Any]
    # Mesh-primitive lineage seam (Sprint R1): the recurrence/slug key carried
    # from the NormalizedDoc, or None. Drives the Writer's supersede; emitted
    # to frontmatter only when present.
    lineage_key: Optional[str] = None


@dataclass(frozen=True)
class _SemanticPage:
    """The LLM-owned slice parsed from a Writer/Editor response."""

    title: str
    topics: List[str]
    key_entities: List[str]
    body: str


# ── prompts ─────────────────────────────────────────────────────────────

# P2 (wiki-writer-structured-output-v1): Writer and Editor emit the page as a
# FORCED wiki_page tool call — validated args, no prose parse, no frontmatter-
# format instructions anywhere in these prompts. The Evaluator's _EVAL_TOOL
# below is the in-file precedent (same tier, same transport).

_WRITER_SYSTEM = (
    "You compact a source document into one canonical wiki page for the Grove "
    "Autonomaton's living cellar. Call the wiki_page tool exactly once with: "
    "title (string), topics (list of strings), key_entities (list of "
    "strings), and body — the page body in Markdown: a short summary, the key "
    "findings, and relationships to prior knowledge. Canonicalize vocabulary. "
    "Source, timestamps, and confidence are set by the system, not by you."
)

_EDITOR_SYSTEM = (
    "You revise a canonical wiki page to fix the listed issues. Call the "
    "wiki_page tool exactly once with all four revised fields (title, topics, "
    "key_entities, body). Your revision is final."
)

# The forced Writer/Editor tool: the page's four semantic fields as validated
# args. Anthropic-style shape; call_t1 reshapes generically for the
# chat_completions arm (_to_openai_tool), so this constant is transport-blind.
_WIKI_PAGE_TOOL: Dict[str, Any] = {
    "name": "wiki_page",
    "description": (
        "Emit the canonical wiki page as structured fields. body is the full "
        "Markdown page body — no frontmatter, no delimiters; the system "
        "renders the page file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "canonical page title"},
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "canonicalized topic tags",
            },
            "key_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "named entities central to the page",
            },
            "body": {
                "type": "string",
                "description": (
                    "the complete page body in Markdown: short summary, key "
                    "findings, relationships to prior knowledge"
                ),
            },
        },
        "required": ["title", "topics", "key_entities", "body"],
    },
}


def _writer_prompt(doc: NormalizedDoc) -> str:
    return (
        f"Source type: {doc.source_type}\n"
        f"Source content:\n\n{doc.raw_content}\n"
    )


def _eval_prompt(doc: NormalizedDoc, page_body: str) -> str:
    return (
        "Evaluate the canonical page against the source. Call wiki_evaluation "
        "with your verdict.\n\n"
        f"=== SOURCE ===\n{doc.raw_content}\n\n"
        f"=== CANONICAL PAGE ===\n{page_body}\n"
    )


def _editor_prompt(page_body: str, issues: List[str]) -> str:
    bullet = "\n".join(f"- {i}" for i in issues) or "- (no specific issues listed)"
    return (
        f"Issues to fix:\n{bullet}\n\n"
        f"=== PAGE TO REVISE ===\n{page_body}\n"
    )


# ── public API ──────────────────────────────────────────────────────────


def compact(
    normalized: NormalizedDoc,
    *,
    wiki_root: Optional[Path] = None,
) -> CanonicalPage:
    """Compact one NormalizedDoc into a CanonicalPage and write it to the cellar.

    At most three T1 calls (Writer always, Evaluator always, Editor 0-or-1).
    Returns the written page. Raises :class:`MalformedWriterOutput` if the
    Writer or Editor emits output that is not a valid page.
    """
    # 1. Writer (always) — forced wiki_page tool (P2); args validated by
    # _parse_semantic_page, the retained Andon.
    semantic = _call_page_tool(
        _writer_prompt(normalized),
        system=_WRITER_SYSTEM,
        max_tokens=_WRITER_MAX_TOKENS,
        role="writer",
    )

    # 2. Evaluator (always).
    verdict = _validate_verdict(
        call_t1(
            _eval_prompt(normalized, semantic.body),
            tool=_EVAL_TOOL,
            max_tokens=_EVAL_MAX_TOKENS,
        )
    )

    # 3. Editor (only on fail; at most once; no re-evaluation).
    editor_ran = False
    if not _passed(verdict):
        logger.info(
            "[wiki] evaluator failed (score=%.3f, issues=%s); running one Editor "
            "pass.",
            verdict["quality_score"],
            verdict["issues"],
        )
        semantic = _call_page_tool(
            _editor_prompt(semantic.body, verdict["issues"]),
            system=_EDITOR_SYSTEM,
            max_tokens=_EDITOR_MAX_TOKENS,
            role="editor",
        )
        editor_ran = True

    # Deterministic, pipeline-owned fields.
    created_at = _iso_from_mtime(normalized.source_mtime)
    updated_at = _now_iso()
    confidence = _clamp01(float(verdict["quality_score"]))

    page = CanonicalPage(
        source=normalized.source_path,
        source_type=normalized.source_type,
        title=semantic.title,
        topics=semantic.topics,
        key_entities=semantic.key_entities,
        dock_goal_refs=list(normalized.dock_goal_refs),
        confidence=confidence,
        created_at=created_at,
        updated_at=updated_at,
        body=semantic.body,
        path=Path("/dev/null"),  # replaced below
        markdown="",             # replaced below
        editor_ran=editor_ran,
        evaluator_verdict=verdict,
        lineage_key=normalized.lineage_key,
    )
    markdown = _render(page)
    path = _write_page(page, markdown, wiki_root)
    # CanonicalPage is frozen — rebuild with the resolved path + markdown.
    return _with_output(page, path=path, markdown=markdown)


def project(
    goal: "Goal",
    *,
    wiki_root: Optional[Path] = None,
    source_mtime: Optional[float] = None,
) -> CanonicalPage:
    """Project one Dock :class:`grove.dock.Goal` into a canonical wiki page.

    The DETERMINISTIC sibling of :func:`compact`: it NEVER calls the LLM
    (``call_t1`` / Writer / Evaluator / Editor). The goal's own fields map
    straight to a canonical page, so the same retrieval surface that serves
    fleet briefs also serves the operator's strategic goals.

    Mapping (Sprint K2): ``title`` ← name; ``source`` ← ``dock.yaml#<id>``
    (source-stable, opaque to :func:`_write_page`); ``source_type`` ←
    ``dock_goal``; ``dock_goal_refs`` ← ``[id]``; ``topics`` / ``key_entities``
    ← keywords; ``confidence`` ← 1.0. ``status`` and ``vector`` are rendered
    into the frontmatter (the index ignores them, but Obsidian/the operator
    see them). Both timestamps derive from the ``dock.yaml`` mtime (RULING 3),
    rendered UTC so a page is byte-identical whether built on the Mac (tests)
    or the VM (runtime).

    Reuses :func:`_write_page` for the hash/glob idempotency but renders its
    OWN markdown (``_render`` omits status/vector and stamps wall-clock), so
    the fleet path through ``_render`` is untouched.

    ``source_mtime`` is the authoritative ``dock.yaml`` mtime. When None it is
    re-derived from ``goal.root/dock.yaml`` (the standalone case); :func:`
    project_dock` THREADS it so every page in one reconcile shares the trigger
    file's stamp (GUARD P2-a — the trigger source and the stamp source are one
    file).
    """
    if source_mtime is None:
        manifest = goal.root / _DOCK_MANIFEST_FILENAME
        # Fail loud: the manifest must exist to stamp timestamps.
        source_mtime = manifest.stat().st_mtime
    stamp = _iso_from_mtime(source_mtime)
    keywords = list(goal.keywords)

    page = CanonicalPage(
        source=_DOCK_GOAL_SOURCE_PREFIX + goal.id,
        source_type=_DOCK_GOAL_SOURCE_TYPE,
        title=goal.name,
        topics=keywords,
        key_entities=keywords,
        dock_goal_refs=[goal.id],
        confidence=1.0,
        created_at=stamp,
        updated_at=stamp,
        body=_render_dock_body(goal),
        path=Path("/dev/null"),  # replaced below
        markdown="",             # replaced below
        editor_ran=False,
        evaluator_verdict={},
    )
    markdown = _render_dock_page(page, goal)
    path = _write_page(page, markdown, wiki_root)
    return _with_output(page, path=path, markdown=markdown)


def project_dock(
    wiki_root: Optional[Path] = None,
    *,
    dock_path: Optional[Path] = None,
) -> List[CanonicalPage]:
    """Reconcile the ``dock_goal`` cellar against the live Dock manifest.

    Pure projection: the set of ``dock_goal`` pages mirrors the live goals.

    1. Load the manifest (``dock_path`` injected by the watcher; runtime path
       when None). An ABSENT manifest (:func:`grove.dock.load_dock` returns
       None) is a NO-OP — nothing is touched, nothing reaped (GUARD P2-b).
    2. Project EVERY goal (not just active — a ``complete`` goal still gets a
       page). :func:`project`'s own glob-clear removes a title-drifted goal's
       stale same-hash slug (GUARD P2-c, axis 1).
    3. Set-difference reap (GUARD P2-c, axis 2): delete ``dock_goal`` pages
       whose trailing hash is ABSENT from the expected set — i.e. goals that no
       longer exist. A present-but-EMPTY manifest yields an empty expected set
       and reaps ALL dock_goal pages — correct-by-model and intentional.

    Returns the pages projected this pass (``[]`` on the no-op path or an empty
    manifest).
    """
    from grove.dock import load_dock

    dock = load_dock(dock_path)
    if dock is None:
        logger.debug("[wiki] no Dock manifest — projection no-op")
        return []

    # Single authoritative stamp source (GUARD P2-a): the manifest load_dock
    # just read. dock.root is <manifest dir>; the file is dock.yaml.
    manifest = dock.root / _DOCK_MANIFEST_FILENAME
    source_mtime = manifest.stat().st_mtime

    root = Path(wiki_root) if wiki_root else get_wiki_path()
    out_dir = root / "pages" / _DOCK_GOAL_SOURCE_TYPE

    pages: List[CanonicalPage] = []
    expected: set[str] = set()
    for goal in dock.goals:
        pages.append(project(goal, wiki_root=root, source_mtime=source_mtime))
        expected.add(_dock_source_hash(goal.id))

    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.md")):
            h = _trailing_hash(path)
            if h is not None and h not in expected:
                path.unlink()
                logger.info("[wiki] reaped orphaned dock page %s", path.name)

    return pages


def project_memory(
    record: "MemoryRecord",
    *,
    wiki_root: Optional[Path] = None,
) -> CanonicalPage:
    """Project one graduated :class:`grove.memory.record.MemoryRecord` into a
    canonical wiki page — the DETERMINISTIC sibling of :func:`compact`.

    Like :func:`project`, it NEVER calls the LLM (``call_t1`` / Writer /
    Evaluator / Editor): a graduated record's own fields map straight to a
    page, so the same retrieval surface that serves the fleet cellar and the
    Dock goals also serves crystallized operator memory.

    Mapping (Sprint K3): ``title`` ← ``content`` capped at
    :data:`_MEMORY_TITLE_MAX`; ``source`` ← ``memory#<id>`` (source-stable,
    opaque to :func:`_write_page`); ``source_type`` ← ``memory_graduated``;
    ``dock_goal_refs`` ← ``[dock_goal_ref]`` or ``[]``; ``topics`` ←
    ``[entity_type]``; ``key_entities`` ← ``[]``; ``confidence`` ←
    ``record.confidence``. ``status`` is rendered into the frontmatter (the
    index ignores it; the operator/Obsidian see it).

    Both frontmatter timestamps derive from ``record.created_at`` — already an
    ISO string fixed at the record's creation (K3 ruling), so the page is
    byte-stable and no ``stat``/wall-clock conversion is needed (unlike K2's
    mtime path). Reuses :func:`_write_page` for the source-hash/glob
    idempotency but renders its OWN markdown (``_render_memory_page`` carries
    ``status``), so the fleet path through :func:`_render` is untouched.
    """
    title = record.content[:_MEMORY_TITLE_MAX]
    dock_refs = [record.dock_goal_ref] if record.dock_goal_ref else []

    page = CanonicalPage(
        source=_MEMORY_SOURCE_PREFIX + record.id,
        source_type=_MEMORY_SOURCE_TYPE,
        title=title,
        topics=[record.entity_type],
        key_entities=[],
        dock_goal_refs=dock_refs,
        confidence=record.confidence,
        created_at=record.created_at,
        updated_at=record.created_at,
        body=_render_memory_body(record),
        path=Path("/dev/null"),  # replaced below
        markdown="",             # replaced below
        editor_ran=False,
        evaluator_verdict={},
    )
    markdown = _render_memory_page(page, record)
    path = _write_page(page, markdown, wiki_root)
    return _with_output(page, path=path, markdown=markdown)


# ── parsing / validation (fail loud) ────────────────────────────────────


def _call_page_tool(
    prompt: str, *, system: str, max_tokens: int, role: str
) -> _SemanticPage:
    """One forced wiki_page call, guarded by the P0/P1 truncation ladder.

    Cap-hit (T1TruncationError — the router's native_finish_reason truth
    signal, surfaced by call_t1's chat_completions arm) → ONE raised-cap
    retry (P0: identical-at-cap retry is deterministic 0/6; raised-cap 2/2)
    → on a second cap-hit, MalformedWriterOutput. Validated args build the
    SemanticPage directly; :func:`_parse_semantic_page` is the Andon.
    """
    try:
        args = call_t1(
            prompt, system=system, tool=_WIKI_PAGE_TOOL, max_tokens=max_tokens
        )
    except T1TruncationError:
        raised = 2 * max_tokens
        logger.warning(
            "[wiki] %s wiki_page call truncated at max_tokens=%d; retrying "
            "once at %d.", role, max_tokens, raised,
        )
        try:
            args = call_t1(
                prompt, system=system, tool=_WIKI_PAGE_TOOL, max_tokens=raised
            )
        except T1TruncationError as exc:
            raise MalformedWriterOutput(
                f"{role} output truncated even after the raised-cap retry "
                f"({raised} tokens): {exc}"
            ) from exc
    return _parse_semantic_page(args)


def _parse_semantic_page(args: Any) -> _SemanticPage:
    """Single chokepoint for BOTH Writer and Editor output — the Andon (P2).

    Shrunk from the prose/frontmatter parser to ARG VALIDATION: the page's
    four semantic fields arrive as forced wiki_page tool args, so no parse of
    prose remains anywhere in the pipeline. The failure contract is
    unchanged — empty, missing, or mistyped fields raise
    :class:`MalformedWriterOutput` exactly as the prose parser did.
    """
    if not isinstance(args, dict):
        raise MalformedWriterOutput(
            f"wiki_page args are not an object: {type(args).__name__}"
        )
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MalformedWriterOutput("missing or empty 'title'")
    body = args.get("body")
    if not isinstance(body, str) or not body.strip():
        raise MalformedWriterOutput("page body is empty")

    return _SemanticPage(
        title=title.strip(),
        topics=_as_str_list(args.get("topics"), "topics"),
        key_entities=_as_str_list(args.get("key_entities"), "key_entities"),
        body=body,
    )


# Retained for the canonical page FILE format (an untouched invariant):
# _read_lineage_key anchors on lines that are exactly ``---`` when reading
# EXISTING pages from disk. Model output no longer carries frontmatter.
_FRONTMATTER_DELIM = re.compile(r"^---\s*$")


def _as_str_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise MalformedWriterOutput(f"'{field}' must be a list, got {type(value).__name__}")


def _validate_verdict(verdict: Any) -> Dict[str, Any]:
    if not isinstance(verdict, dict):
        raise MalformedWriterOutput(f"evaluator verdict is not an object: {verdict!r}")
    for key in ("complete", "accurate", "quality_score", "issues"):
        if key not in verdict:
            raise MalformedWriterOutput(f"evaluator verdict missing '{key}'")
    try:
        float(verdict["quality_score"])
    except (TypeError, ValueError) as exc:
        raise MalformedWriterOutput(
            f"evaluator quality_score is not a number: {verdict['quality_score']!r}"
        ) from exc
    if not isinstance(verdict["issues"], list):
        raise MalformedWriterOutput("evaluator 'issues' must be a list")
    return verdict


def _passed(verdict: Dict[str, Any]) -> bool:
    return bool(
        verdict["complete"]
        and verdict["accurate"]
        and float(verdict["quality_score"]) >= QUALITY_THRESHOLD
    )


# ── rendering / writing ─────────────────────────────────────────────────


def _render(page: CanonicalPage) -> str:
    """Render the canonical page to Markdown. Field order is stable and the
    frontmatter carries exactly what WikiIndex (Phase 2) reads."""
    fm = {
        "title": page.title,
        "source_type": page.source_type,
        "source": page.source,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "confidence": page.confidence,
        "dock_goal_refs": list(page.dock_goal_refs),
        "topics": list(page.topics),
        "key_entities": list(page.key_entities),
    }
    # Mesh-primitive lineage: emit the recurrence/slug key ONLY when the doc
    # carries one. Adapters without a defined lineage key leave it None, so
    # their pages stay clean — no null field, no supersede.
    if page.lineage_key is not None:
        fm["lineage_key"] = page.lineage_key
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + page.body.rstrip("\n")
        + "\n"
    )


def _render_dock_page(page: CanonicalPage, goal: "Goal") -> str:
    """Render a projected Dock goal to Markdown (Sprint K2, own-render).

    Distinct from :func:`_render`: the frontmatter additionally carries
    ``status`` and ``vector`` (pulled from the goal, not the page), and every
    field is deterministic — no wall-clock, so output is byte-stable. The key
    set the index reads (title/source_type/confidence/dock_goal_refs/topics/
    key_entities) is a superset-compatible subset here.
    """
    fm = {
        "title": page.title,
        "source_type": page.source_type,
        "source": page.source,
        "status": goal.status,
        "vector": goal.vector,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "confidence": page.confidence,
        "dock_goal_refs": list(page.dock_goal_refs),
        "topics": list(page.topics),
        "key_entities": list(page.key_entities),
    }
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + page.body.rstrip("\n")
        + "\n"
    )


def _render_dock_body(goal: "Goal") -> str:
    """Deterministic body for a projected Dock goal — name, vector, status,
    definition of done, keywords, and context sources, in a fixed layout."""
    keywords = ", ".join(goal.keywords) if goal.keywords else "(none)"
    lines = [
        f"# {goal.name}",
        "",
        f"- **Vector:** {goal.vector}",
        f"- **Status:** {goal.status}",
        "",
        "## Definition of Done",
        "",
        goal.definition_of_done,
        "",
        "## Keywords",
        "",
        keywords,
        "",
        "## Context Sources",
        "",
    ]
    if goal.context_sources:
        lines.extend(f"- {src}" for src in goal.context_sources)
    else:
        lines.append("(none)")
    return "\n".join(lines)


def _render_memory_page(page: CanonicalPage, record: "MemoryRecord") -> str:
    """Render a projected graduated MemoryRecord to Markdown (Sprint K3).

    Distinct from :func:`_render`: the frontmatter additionally carries
    ``status`` (pulled from the record, not the page) and every field is
    deterministic — no wall-clock, so output is byte-stable. The key set the
    index reads (title/source_type/confidence/dock_goal_refs/topics/
    key_entities) is a superset-compatible subset here.
    """
    fm = {
        "title": page.title,
        "source_type": page.source_type,
        "source": page.source,
        "status": record.status,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "confidence": page.confidence,
        "dock_goal_refs": list(page.dock_goal_refs),
        "topics": list(page.topics),
        "key_entities": list(page.key_entities),
    }
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + page.body.rstrip("\n")
        + "\n"
    )


def _render_memory_body(record: "MemoryRecord") -> str:
    """Deterministic body for a graduated MemoryRecord — the content VERBATIM
    and prominent on the first line, then entity type, the source list, and the
    access telemetry, in a fixed layout."""
    last_accessed = record.last_accessed or "(never)"
    lines = [
        record.content,
        "",
        f"- **Entity type:** {record.entity_type}",
        f"- **Created:** {record.created_at}",
        f"- **Last accessed:** {last_accessed}",
        f"- **Access count:** {record.access_count}",
        "",
        "## Sources",
        "",
    ]
    if record.sources:
        lines.extend(f"- {src}" for src in record.sources)
    else:
        lines.append("(none)")
    return "\n".join(lines)


def _write_page(page: CanonicalPage, markdown: str, wiki_root: Optional[Path]) -> Path:
    root = Path(wiki_root) if wiki_root else get_wiki_path()
    out_dir = root / "pages" / page.source_type
    out_dir.mkdir(parents=True, exist_ok=True)

    short_hash = hashlib.sha256(page.source.encode("utf-8")).hexdigest()[:_HASH_LEN]
    # Idempotency: one page per source. Clear any prior page for this source
    # (the hash is source-stable even if the Writer's title — hence the slug —
    # drifts between ingests), then write the current slug+hash file.
    for stale in out_dir.glob(f"*-{short_hash}.md"):
        stale.unlink()

    path = out_dir / f"{_slug(page.title)}-{short_hash}.md"
    path.write_text(markdown, encoding="utf-8")

    # Lineage supersede (Sprint R1) — ADDITIONAL to the same-source glob-clear
    # above, never a replacement. Tombstone prior pages in this source_type dir
    # that share this page's lineage_key. Generic: matches the frontmatter
    # FIELD, never a skill name or source_type branch.
    _supersede_prior(page, path, out_dir, root)
    return path


def _supersede_prior(
    page: CanonicalPage, new_path: Path, out_dir: Path, wiki_root: Path
) -> None:
    """Tombstone prior pages in ``out_dir`` (the doc's OWN source_type dir)
    that share this page's ``lineage_key``. No-op when the page carries none —
    the default for any adapter without a defined lineage key. The match is purely
    on the emitted frontmatter FIELD; this never reads a skill name, and the
    source_type dir comes from the doc itself (generic structural behavior).

    The atomic file+FTS retirement is owned by :meth:`WikiIndex.tombstone` —
    the Writer asks, the index executes. No DB access is duplicated here."""
    if page.lineage_key is None:
        return
    from grove.wiki.index import WikiIndex

    pages_root = wiki_root / "pages"
    index = WikiIndex(wiki_root=wiki_root)
    for prior in sorted(out_dir.glob("*.md")):
        if prior == new_path:
            continue
        if _read_lineage_key(prior) == page.lineage_key:
            rel = str(prior.relative_to(pages_root))
            index.tombstone(rel)
            logger.info(
                "[wiki] superseded %s (lineage_key=%s)", rel, page.lineage_key
            )


def _read_lineage_key(path: Path) -> Optional[str]:
    """The ``lineage_key`` frontmatter field of an existing page, or None when
    the file has no frontmatter or no such field. Fail loud on a present-but-
    corrupt frontmatter block (the WikiIndex is the broader malformed-page
    authority; a non-canonical neighbour simply doesn't match here)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_DELIM.match(lines[0]):
        return None
    close = next(
        (i for i in range(1, len(lines)) if _FRONTMATTER_DELIM.match(lines[i])),
        None,
    )
    if close is None:
        raise MalformedWriterOutput(
            f"prior page {path.name}: frontmatter block is not terminated"
        )
    try:
        meta = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError as exc:
        raise MalformedWriterOutput(
            f"prior page {path.name}: unparseable frontmatter: {exc}"
        ) from exc
    if not isinstance(meta, dict):
        return None
    key = meta.get("lineage_key")
    return key if isinstance(key, str) else None


_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _dock_source_hash(goal_id: str) -> str:
    """Expected filename hash for a Dock goal id. Shares the source PREFIX
    constant, sha256, and ``_HASH_LEN`` with :func:`_write_page` (GUARD P2-d) —
    never a re-spelled ``dock.yaml#`` literal — so the reaper's expected set and
    the writer's filenames can't silently desync."""
    source = _DOCK_GOAL_SOURCE_PREFIX + goal_id
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _trailing_hash(path: Path) -> Optional[str]:
    """The source hash = the LAST ``-``-delimited segment of the filename stem
    (RULING 4). Slugs contain ``-``, so split on the last one. Returns the
    segment only when it is exactly ``_HASH_LEN`` lowercase-hex chars; otherwise
    None — a non-conforming file is not a projection artifact and is left alone
    (the reaper never deletes a hand-authored note)."""
    seg = path.stem.rsplit("-", 1)[-1]
    if len(seg) == _HASH_LEN and _HEX_RE.match(seg):
        return seg
    return None


def _slug(title: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    s = s[:max_len].strip("-")
    return s or "untitled"


# ── small deterministic helpers ─────────────────────────────────────────


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _with_output(page: CanonicalPage, *, path: Path, markdown: str) -> CanonicalPage:
    return CanonicalPage(
        source=page.source,
        source_type=page.source_type,
        title=page.title,
        topics=page.topics,
        key_entities=page.key_entities,
        dock_goal_refs=page.dock_goal_refs,
        confidence=page.confidence,
        created_at=page.created_at,
        updated_at=page.updated_at,
        body=page.body,
        path=path,
        markdown=markdown,
        editor_ran=page.editor_ran,
        evaluator_verdict=page.evaluator_verdict,
        lineage_key=page.lineage_key,
    )
