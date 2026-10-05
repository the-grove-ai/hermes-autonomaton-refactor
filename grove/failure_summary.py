"""failure-summary-v1 — a SAFE description of why something failed.

Pipeline stage: Telemetry. Failure reasons are written to records that are kept
permanently, hashed, and may be shown on screen. Raw exception text and API
error strings can carry request bodies, headers or credentials, so they are
NEVER stored. A failure is reduced to:

* a ``kind`` from the closed set :data:`FAILURE_KINDS`, and
* a short ``summary`` assembled only from safe parts — the exception class
  name and an HTTP status code when one is present.

Callers that know more safe context (a tier name, a model slug from config)
append it themselves; nothing here reads message text into the output.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Tuple

__all__ = [
    "FAILURE_KINDS",
    "summarize_exception",
    "summarize_turn_result",
]

FAILURE_KINDS = frozenset({
    "auth_error",                 # credentials rejected (401 / 403)
    "rate_limited",               # 429
    "provider_error",             # 5xx or connection failure
    "timeout",
    "api_error",                  # other non-retryable API failure
    "no_tool_call",               # a forced tool call came back without one
    "malformed_output",           # model output could not be parsed
    "retries_exhausted",
    "truncated_output",
    "context_overflow",           # compression / context length exhausted
    "invalid_tool_calls",
    "empty_response",
    "interrupted",
    "exception",                  # an unexpected exception ended the turn
    "unrecorded_exit",            # the turn ended and wrote no record itself
})

_STATUS_RE = re.compile(r"(?<!\d)([45]\d\d)(?!\d)")
_SUMMARY_MAX = 120


def _status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 400 <= value <= 599:
            return value
    return None


def _kind_for_status(status: int) -> str:
    if status in (401, 403):
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if status in (408, 504):
        return "timeout"
    if status >= 500:
        return "provider_error"
    return "api_error"


def summarize_exception(exc: BaseException) -> Tuple[str, str]:
    """``(kind, summary)`` for an exception. The summary contains the exception
    class name and an HTTP status when the exception carries one — never
    ``str(exc)``."""
    name = type(exc).__name__
    status = _status_of(exc)
    lowered = name.lower()
    if status is not None:
        kind = _kind_for_status(status)
    elif "timeout" in lowered:
        kind = "timeout"
    elif "authentication" in lowered or "permissiondenied" in lowered:
        kind = "auth_error"
    elif "ratelimit" in lowered:
        kind = "rate_limited"
    elif "connection" in lowered:
        kind = "provider_error"
    elif "jsondecode" in lowered or "validation" in lowered:
        kind = "malformed_output"
    elif isinstance(exc, (KeyboardInterrupt,)) or "interrupt" in lowered:
        kind = "interrupted"
    else:
        # Message TEXT is consulted only to pick a kind from the closed set —
        # it is never copied into the summary.
        text = ""
        try:
            text = str(exc).lower()
        except Exception:
            text = ""
        if "no tool_calls" in text or "forced tool" in text:
            kind = "no_tool_call"
        elif "truncat" in text or "cap-cut" in text:
            kind = "truncated_output"
        else:
            kind = "exception"
    summary = name if status is None else f"{name} (HTTP {status})"
    return kind, summary[:_SUMMARY_MAX]


def summarize_turn_result(result: Any) -> Optional[Tuple[str, str, str]]:
    """Decide whether a turn's returned result describes a failure.

    Returns ``(outcome, kind, summary)`` — outcome is ``"error"`` or
    ``"interrupted"`` — or None when the result looks like a normal completion.
    The agent loop's early exits return a result dict instead of yielding a
    final response; this reads only its flags, and uses the error TEXT solely to
    choose a kind (and to lift an HTTP status code), never as the summary.
    """
    if not isinstance(result, Mapping):
        return "error", "unrecorded_exit", "turn returned no result"
    if result.get("interrupted"):
        return "interrupted", "interrupted", "turn interrupted before completion"
    error = result.get("error")
    failed = bool(result.get("failed")) or bool(result.get("partial"))
    incomplete = result.get("completed") is False
    if not (error or failed or incomplete):
        return None
    text = str(error or "").lower()
    status_match = _STATUS_RE.search(text)
    if "interrupt" in text:
        return "interrupted", "interrupted", "turn interrupted before completion"
    if "retries" in text or "retry" in text:
        kind, summary = "retries_exhausted", "retries exhausted"
    elif "truncat" in text or "max_tokens" in text or "length" in text:
        kind, summary = "truncated_output", "response truncated"
    elif "compress" in text or "context" in text or "too large" in text:
        kind, summary = "context_overflow", "context could not be reduced"
    elif "invalid tool" in text or "tool call" in text:
        kind, summary = "invalid_tool_calls", "model produced invalid tool calls"
    elif "empty" in text:
        kind, summary = "empty_response", "model returned no content"
    elif status_match:
        status = int(status_match.group(1))
        kind, summary = _kind_for_status(status), "API request failed"
    else:
        kind, summary = "unrecorded_exit", "turn ended without completing"
    if status_match:
        summary = f"{summary} (HTTP {status_match.group(1)})"
    return "error", kind, summary[:_SUMMARY_MAX]
