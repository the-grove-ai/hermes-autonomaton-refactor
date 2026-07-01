"""Tool-class tags for the emission-precondition gate (structural-review-gate-v1).

The WHETHER dimension of capability governance asks: *did the model actually do
the work a capability's terminal artifact requires, or did it emit a hollow
artifact and skip the tool calls?* Answering that structurally needs a stable
mapping from concrete tool names to abstract **tool classes** (retrieval,
skill_invocation, …). A capability record's ``emission_preconditions`` declare
minimums per class; the gate counts a turn's actual invocations by class and
refuses a terminal write that falls short.

This map is the SOLE source of truth for that name→class tagging. It is
deliberately declarative and small: a tool absent from the map carries no class
and is never counted (it neither satisfies nor blocks a precondition).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

__all__ = ["TOOL_CLASS_MAP", "classify_tool", "count_tool_classes"]


# name → abstract class. Unmapped tools are class-less (ignored by the counter).
TOOL_CLASS_MAP: Dict[str, str] = {
    "web_search": "retrieval",
    "x_search": "retrieval",
    "cellar_search": "retrieval",
    "web_extract": "retrieval",
    "invoke_skill": "skill_invocation",
    "write_file": "file_write",
    "patch": "file_write",
    "terminal": "terminal",
}


def classify_tool(name: object) -> Optional[str]:
    """Return the tool-class tag for *name*, or ``None`` if the tool is unmapped
    (carries no class and is never counted)."""
    if not isinstance(name, str):
        return None
    return TOOL_CLASS_MAP.get(name)


def count_tool_classes(invocations: Iterable[Any]) -> Dict[str, int]:
    """Tally a turn's tool invocations by class.

    *invocations* is the dispatcher's ``_current_turn_tool_invocations`` ledger —
    an iterable of ``{"tool": <name>, "args": ...}`` dicts. Only mapped tools
    contribute; an unmapped or malformed entry is skipped. Returns a
    ``{class: count}`` dict (absent classes are simply not present → treated as 0
    by the precondition check)."""
    counts: Dict[str, int] = {}
    for inv in invocations:
        name = inv.get("tool") if isinstance(inv, dict) else None
        cls = classify_tool(name)
        if cls is not None:
            counts[cls] = counts.get(cls, 0) + 1
    return counts
