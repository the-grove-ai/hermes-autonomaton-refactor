"""Session compactor — dormant session transcripts → canonical wiki pages.

Sprint K5 (session-compaction-v1). The fifth producer for the living cellar:
where K1 compacts fleet sink documents, K2 projects Dock goals, and K3
graduates memory records, this module compacts a dormant session's filtered
transcript into one canonical wiki page via the existing
Writer→Evaluator→Editor pipeline (:func:`grove.wiki.pipeline.compact`).

A session is not file-based, so there is NO ``Adapter`` subclass (D2): these
standalone functions build a :class:`grove.wiki.adapters.NormalizedDoc`
directly from a transcript and hand it to :func:`compact`. The synthetic
source path ``session#<session_id>`` follows the K2/K3 prefix pattern — opaque
to ``_write_page`` (hashed, never path-normalized), so the ``#`` is safe.

Fail loud: there are no ambient error-swallowing guards here. The single
commanded graceful path is goal-derivation failure (D6 + A2), which the SPEC
explicitly authorizes to return ``[]``. Per-session compaction failure
isolation (A1) lives in the Dispatcher's best-effort wrapper, not this module.

Imports of the heavy collaborators (``NormalizedDoc``, ``compact``,
``_session_worth_extracting``, ``get_wiki_path``) are function-level (lazy) to
avoid circular dependencies with the pipeline and memory packages.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from grove.wiki.adapters import NormalizedDoc
    from grove.wiki.pipeline import CanonicalPage


# Source-stable synthetic path prefix (D2), peer of K2's ``dock.yaml#`` and
# K3's ``memory#``. Opaque to ``_write_page`` — hashed, never path-normalized.
_SESSION_SOURCE_PREFIX = "session#"
_SESSION_SOURCE_TYPE = "session_compacted"

# Ends-middle truncation budget (D4). 50K chars is well within Haiku's 200K
# context window. Named constant, config-promotable later.
SESSION_RAW_BUDGET = 50000
_HEAD_TURNS = 3

# Short source hash length — MUST match ``grove.wiki.pipeline._HASH_LEN`` so the
# idempotency pre-check and ``_write_page``'s filename hash agree (D5).
_HASH_LEN = 8


# ── serialization (D3) ──────────────────────────────────────────────────


def serialize_transcript(filtered: List[Dict[str, Any]]) -> str:
    """Serialize a filtered transcript to role-labeled, human-readable text (D3).

    Rules:

    * ``role == "user"`` → ``[operator]``; ``role == "assistant"`` →
      ``[assistant]``; ``role == "system"`` (and anything else) is skipped —
      system messages are scaffolding, not session substance.
    * A message carrying ``tool_calls`` appends one ``[tool: <name>]`` line per
      call AFTER the assistant content.
    * List content (multimodal) joins its text parts; non-text parts drop.
    * Each message's content is stripped; a message empty after stripping (and
      carrying no tool calls) is omitted.
    """
    lines: List[str] = []
    for msg in filtered:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            label = "[operator]"
        elif role == "assistant":
            label = "[assistant]"
        else:
            continue  # system / unknown — scaffolding, not session substance

        content = _content_text(msg.get("content", "")).strip()
        tool_lines = _tool_lines(msg.get("tool_calls")) if role == "assistant" else []

        if not content and not tool_lines:
            continue  # nothing extractable from this message

        if content:
            lines.append(f"{label} {content}")
        lines.extend(tool_lines)
    return "\n".join(lines)


def _content_text(content: Any) -> str:
    """Reduce a message content field to plain text. A string returns as-is; a
    list (multimodal) joins its text parts and drops non-text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _tool_lines(tool_calls: Any) -> List[str]:
    """One ``[tool: <name>]`` line per tool call. Handles both the nested
    ``{"function": {"name": ...}}`` shape that
    :func:`grove.memory.transcript_filter.filter_transcript_for_extraction`
    emits and a flat ``{"name": ...}`` shape."""
    if not isinstance(tool_calls, list):
        return []
    out: List[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        name = fn.get("name") if isinstance(fn, dict) else tc.get("name")
        out.append(f"[tool: {name}]")
    return out


# ── ends-middle truncation (D4) ─────────────────────────────────────────


def ends_middle_cap(
    text: str, budget: int = SESSION_RAW_BUDGET, head_turns: int = _HEAD_TURNS
) -> str:
    """Ends-middle truncation (D4).

    If ``text`` is within ``budget``, return it unchanged. Otherwise keep the
    first ``head_turns`` turn blocks (the operator's intent and context-setting)
    plus as many trailing turn blocks as fit, eliding the middle with a one-line
    ``[... <N> turns elided ...]`` marker.

    A turn boundary is a line beginning with ``[operator]`` or ``[assistant]``;
    standalone ``[tool: ...]`` and continuation lines attach to the current
    turn block. Degenerate case (step 5): when the head alone exceeds budget,
    return the head truncated to budget.
    """
    if len(text) <= budget:
        return text

    blocks = _split_turn_blocks(text)
    if len(blocks) <= head_turns:
        # Can't separate a head from a tail — hard-truncate.
        return text[:budget]

    head_text = "\n".join(blocks[:head_turns])
    if len(head_text) >= budget:
        # D4 step 5 — head alone exceeds budget; return truncated head.
        return head_text[:budget]

    remaining = blocks[head_turns:]
    # Grow the tail from the END while head + marker + tail fits the budget.
    tail: List[str] = []
    for block in reversed(remaining):
        trial_tail = [block] + tail
        marker = _elision_marker(len(remaining) - len(trial_tail))
        if len(head_text) + len(marker) + len("\n".join(trial_tail)) > budget:
            break
        tail = trial_tail

    marker = _elision_marker(len(remaining) - len(tail))
    return head_text + marker + "\n".join(tail)


def _split_turn_blocks(text: str) -> List[str]:
    """Split serialized transcript text into turn blocks. A new block starts at
    each line beginning with ``[operator]`` or ``[assistant]``; every other line
    (``[tool: ...]``, multi-line content) attaches to the current block."""
    blocks: List[str] = []
    current: List[str] = []
    for line in text.split("\n"):
        if line.startswith("[operator]") or line.startswith("[assistant]"):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _elision_marker(n: int) -> str:
    return f"\n\n[... {n} turns elided ...]\n\n"


# ── dominant dock-goal (D6) ─────────────────────────────────────────────


def dominant_dock_goal(intent_store: Any, session_id: str) -> List[str]:
    """Return the SINGLE most-frequent dock-goal ref for ``session_id`` (D6).

    Queries the intent store for the session's records, collects non-empty
    ``goal_alignment`` strings, and returns ``[most_frequent]`` — a single-
    element list keeps BM25 boosting sharp (D6). Returns ``[]`` when no goals
    exist.

    On ANY exception this returns ``[]`` (A2 — goal derivation failure is not
    fatal; the page is still valuable without goal linking). This is the one
    SPEC-commanded graceful path in the module, not an ambient guard.
    """
    try:
        records = intent_store.filter(session_id=session_id)
        goals = [
            g
            for r in records
            if isinstance((g := getattr(r, "goal_alignment", None)), str) and g.strip()
        ]
        if not goals:
            return []
        return [Counter(goals).most_common(1)[0][0]]
    except Exception:  # noqa: BLE001 — A2-commanded graceful path
        return []


# ── document build + compaction entry point ─────────────────────────────


def build_session_doc(
    session_id: str,
    filtered_transcript: List[Dict[str, Any]],
    intent_store: Any,
    source_mtime: float,
) -> "NormalizedDoc":
    """Orchestrate serialize → ends-middle cap → goal-derive → NormalizedDoc.

    The ``raw_content`` carries a two-line preamble (D9) so the Writer system
    prompt — which compacts a source document regardless of source type — has
    explicit context that this is a session transcript.
    """
    from grove.wiki.adapters import NormalizedDoc

    capped = ends_middle_cap(serialize_transcript(filtered_transcript))
    return NormalizedDoc(
        source_type=_SESSION_SOURCE_TYPE,
        source_path=f"{_SESSION_SOURCE_PREFIX}{session_id}",
        source_mtime=source_mtime,
        dock_goal_refs=dominant_dock_goal(intent_store, session_id),
        raw_content=(
            "Source type: session transcript\n"
            f"Session ID: {session_id}\n\n"
            f"{capped}"
        ),
    )


def compact_session(
    session_id: str,
    filtered_transcript: List[Dict[str, Any]],
    intent_store: Any,
    source_mtime: float,
    *,
    wiki_root: Optional[Path] = None,
) -> Optional["CanonicalPage"]:
    """Compact one dormant session into a canonical wiki page. Returns the
    :class:`CanonicalPage`, or ``None`` on a skip (below complexity gate, or
    already compacted).

    Steps:

    1. **Complexity gate (D8):** reuse the detector's ``_session_worth_extracting``
       (≥3 operator messages OR tool use). Below threshold → ``None``, no T1
       calls.
    2. **Idempotency (D5):** the source hash is source-stable and identical to
       what ``_write_page`` computes for this source, so an existing
       ``pages/session_compacted/*-<hash>.md`` short-circuits the three T1 calls.
    3. **Build** the NormalizedDoc and **4. compact** via the existing pipeline.
    """
    from grove.memory.detector import _session_worth_extracting
    from grove.wiki.pipeline import compact

    # 1. Complexity gate (D8).
    if not _session_worth_extracting(filtered_transcript):
        return None

    # 2. Idempotency check (D5) — filesystem hash presence.
    short_hash = hashlib.sha256(
        f"{_SESSION_SOURCE_PREFIX}{session_id}".encode()
    ).hexdigest()[:_HASH_LEN]
    root = Path(wiki_root) if wiki_root is not None else _default_wiki_root()
    out_dir = root / "pages" / _SESSION_SOURCE_TYPE
    if any(out_dir.glob(f"*-{short_hash}.md")):
        return None

    # 3. Build + 4. compact (wiki_root threaded through unchanged — compact
    #    resolves None to get_wiki_path() itself, matching the glob above).
    doc = build_session_doc(
        session_id, filtered_transcript, intent_store, source_mtime
    )
    return compact(doc, wiki_root=wiki_root)


def _default_wiki_root() -> Path:
    from hermes_constants import get_wiki_path

    return get_wiki_path()
