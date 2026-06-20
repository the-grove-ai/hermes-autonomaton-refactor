"""Deterministic transcript pre-filter for memory extraction.

No LLM. Strips the noise the Context Persistence Detector should never see
(tool outputs, reasoning traces, base64 blobs, system scaffolding) so the
T1 Haiku call works from clean operator/assistant signal only. This is a
cost and signal-quality gate ahead of the model call, not a governance
boundary.
"""

from __future__ import annotations

import string
from typing import Any, Dict, List, Optional

__all__ = ["filter_transcript_for_extraction"]


# Multimodal content stored by hermes_state is prefixed with this marker
# (``_CONTENT_JSON_PREFIX`` in hermes_state.py). A prefixed string carrying
# a long base64 run is an embedded image/asset blob — useless for memory
# extraction and ruinous for the prompt budget.
_CONTENT_JSON_PREFIX = "\x00json:"
_BASE64_ALPHABET = frozenset(string.ascii_letters + string.digits + "+/=")
_BASE64_RUN_THRESHOLD = 500

# Assistant-message keys carrying reasoning/codex traces — stripped so only
# the operator-visible text and tool intent survive.
_REASONING_KEYS = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)


def _has_long_base64_run(text: str, threshold: int = _BASE64_RUN_THRESHOLD) -> bool:
    """True if ``text`` contains a contiguous base64-alphabet run > threshold."""
    run = 0
    for ch in text:
        if ch in _BASE64_ALPHABET:
            run += 1
            if run > threshold:
                return True
        else:
            run = 0
    return False


def _is_base64_blob(content: Any) -> bool:
    """True for a ``\\x00json:``-prefixed string carrying a long base64 run."""
    return (
        isinstance(content, str)
        and content.startswith(_CONTENT_JSON_PREFIX)
        and _has_long_base64_run(content)
    )


def _clean_content(content: Any) -> Optional[str]:
    """Reduce a message content field to plain text, dropping base64 blobs.

    Returns ``None`` when nothing extractable remains (e.g. the content was
    a base64 blob, or a list of non-text parts only).
    """
    if isinstance(content, str):
        return None if _is_base64_blob(content) else content
    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                # image / tool_result / other parts dropped
            elif isinstance(part, str) and not _is_base64_blob(part):
                texts.append(part)
        return "\n".join(texts) if texts else None
    return None


def _strip_tool_call(tool_call: Any) -> Dict[str, Any]:
    """Keep only the tool name + arguments; drop id/type and any result."""
    if not isinstance(tool_call, dict):
        return {"function": {"name": None, "arguments": None}}
    fn = tool_call.get("function")
    if isinstance(fn, dict):
        return {"function": {"name": fn.get("name"), "arguments": fn.get("arguments")}}
    # Flat shape: {"name": ..., "arguments": ...}
    return {"function": {"name": tool_call.get("name"),
                         "arguments": tool_call.get("arguments")}}


def filter_transcript_for_extraction(
    messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Return a new, filtered copy of ``messages`` for memory extraction.

    Rules (SPEC Phase 2):

    * KEEP ``user`` messages (full text); drop ones that are base64 blobs.
    * KEEP ``assistant`` text + tool-call name/arguments; STRIP reasoning,
      codex traces, tool-call ids/types, and tool output.
    * STRIP ``tool`` and ``system`` messages entirely.

    Never mutates the input.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "user":
            cleaned = _clean_content(msg.get("content"))
            if cleaned is None:
                continue  # base64 blob or empty — nothing to extract
            out.append({"role": "user", "content": cleaned})

        elif role == "assistant":
            new_msg: Dict[str, Any] = {"role": "assistant"}
            new_msg["content"] = _clean_content(msg.get("content"))
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                new_msg["tool_calls"] = [_strip_tool_call(tc) for tc in tool_calls]
            # reasoning/codex keys (_REASONING_KEYS) are never copied across
            out.append(new_msg)

        # role == "tool" / "system" / "session_meta" / unknown → dropped

    return out
