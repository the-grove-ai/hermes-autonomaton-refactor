"""Approved-effect execution context — red-action-store-pending-v1 Phase C.

A ``contextvars.ContextVar`` carrying the effect signature of a governed RED
re-dispatch that the operator has APPROVED. Set by
``grove.red_pending_store.approve_red_proposal`` around ``registry.dispatch``;
read by the terminal guard (``tools.approval.check_all_command_guards``) to honor
an approved execution — RED shell + a matching approved-effect → execute; else the
existing block stands.

``ContextVar`` (NOT ``threading.local``) — Gemini GATE-B mandate: native isolation
across the gateway's asyncio loop AND its ThreadPoolExecutor turn threads, and
``reset(token)`` in a ``finally`` is exception-safe so an approved-effect can never
leak past its single dispatch into another turn/task.

Standalone module (no imports of red_pending_store / approval) so both the setter
and the reader import it without a cycle.
"""
from __future__ import annotations

import contextvars
from typing import Optional

# The exact minted effect signature of the RED action currently being executed
# under operator approval, or None when no approved re-dispatch is in flight.
approved_effect_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "grove_approved_effect", default=None
)
