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
from typing import Any, Dict, List, Optional

import yaml

from hermes_constants import get_wiki_path

from grove.t1_call import call_t1
from grove.wiki.adapters import NormalizedDoc

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


@dataclass(frozen=True)
class _SemanticPage:
    """The LLM-owned slice parsed from a Writer/Editor response."""

    title: str
    topics: List[str]
    key_entities: List[str]
    body: str


# ── prompts ─────────────────────────────────────────────────────────────

_WRITER_SYSTEM = (
    "You compact a source document into one canonical wiki page for the Grove "
    "Autonomaton's living cellar. Output a Markdown document that begins with a "
    "YAML frontmatter block containing EXACTLY these keys: title (string), "
    "topics (list of strings), key_entities (list of strings). After the "
    "closing '---', write the page body: a short summary, the key findings, and "
    "relationships to prior knowledge. Canonicalize vocabulary. Do NOT include "
    "any other frontmatter keys — source, timestamps, and confidence are set by "
    "the system, not by you."
)

_EDITOR_SYSTEM = (
    "You revise a canonical wiki page to fix the listed issues. Output the same "
    "Markdown+frontmatter format (title, topics, key_entities, then body). Your "
    "revision is final."
)


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
    # 1. Writer (always).
    writer_text = call_t1(
        _writer_prompt(normalized),
        system=_WRITER_SYSTEM,
        max_tokens=_WRITER_MAX_TOKENS,
    )
    semantic = _parse_semantic_page(writer_text)

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
        editor_text = call_t1(
            _editor_prompt(semantic.body, verdict["issues"]),
            system=_EDITOR_SYSTEM,
            max_tokens=_EDITOR_MAX_TOKENS,
        )
        semantic = _parse_semantic_page(editor_text)
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
    )
    markdown = _render(page)
    path = _write_page(page, markdown, wiki_root)
    # CanonicalPage is frozen — rebuild with the resolved path + markdown.
    return _with_output(page, path=path, markdown=markdown)


# ── parsing / validation (fail loud) ────────────────────────────────────


def _parse_semantic_page(text: str) -> _SemanticPage:
    if not isinstance(text, str) or not text.strip():
        raise MalformedWriterOutput("writer/editor returned empty output")
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise MalformedWriterOutput("output has no YAML frontmatter block")
    end = stripped.find("\n---", 3)
    if end == -1:
        raise MalformedWriterOutput("output frontmatter block is not terminated")
    fm_str = stripped[3:end]
    body = stripped[end + 4:].lstrip("\n")
    try:
        meta = yaml.safe_load(fm_str)
    except yaml.YAMLError as exc:
        raise MalformedWriterOutput(f"unparseable frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise MalformedWriterOutput("frontmatter is not a mapping")

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MalformedWriterOutput("missing or empty 'title'")
    if not body.strip():
        raise MalformedWriterOutput("page body is empty")

    return _SemanticPage(
        title=title.strip(),
        topics=_as_str_list(meta.get("topics"), "topics"),
        key_entities=_as_str_list(meta.get("key_entities"), "key_entities"),
        body=body,
    )


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
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + page.body.rstrip("\n")
        + "\n"
    )


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
    return path


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
    )
