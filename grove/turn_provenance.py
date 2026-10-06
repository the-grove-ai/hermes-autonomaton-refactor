"""What the current turn has read, for a tool that must check before it writes.

A ``ContextVar`` the agent's per-invocation chokepoint (``AIAgent._invoke_tool``)
sets from Dispatcher-owned state just before a tool runs. The Dispatcher owns
the facts; a tool only reads them. Pipeline stage: Approval — a governed tool
uses this to refuse an action whose turn drew on context it is not allowed to
use (see ``grove.decision_work``).

``ContextVar`` rather than a module global so concurrent sessions in the
gateway never see each other's turn (the ``tools/skill_provenance.py``
precedent; the executor copies the context into its worker threads).
"""

from __future__ import annotations

import contextvars
from typing import Any, Dict, Optional

_current: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "grove_turn_provenance", default=None,
)


def set_current(snapshot: Optional[Dict[str, Any]]) -> "contextvars.Token":
    return _current.set(snapshot)


def reset(token: "contextvars.Token") -> None:
    _current.reset(token)


def current() -> Optional[Dict[str, Any]]:
    """The running turn's provenance snapshot, or None outside a governed
    tool invocation. Keys: ``session_id``, ``turn_id``, ``turn_uid``, ``tier``,
    ``model``, ``cellar_hits``, ``sections`` (the composed prompt's section
    names), ``tools_yielded`` and ``isolation_goal`` (the goal this session is
    isolated to, else None)."""
    return _current.get()
