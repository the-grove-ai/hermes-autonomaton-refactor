"""Gate-consumed effect-signature execution context — unresolved-writer-execution
-path-v1 (supersedes red-action-store-pending-v1 Phase C's approved_effect_var).

A ``contextvars.ContextVar`` carrying the EXACT effect signature the dispatch
primitive CONSUMED for the tool call currently executing under an active
``ApprovalGate``. Set by ``tools.registry.ToolRegistry.dispatch`` immediately
after a successful ``_gate.consume(...)``, and reset (token) in a ``finally`` so
it never leaks past the single dispatch. Read by execution-time tool guards
(``tools.approval.check_all_command_guards`` for shell; ``tools.file_tools`` for
the scope wall) to honor an operator-approved re-dispatch — a matching effect →
execute; else fail-closed.

Unified signature: there is now ONE signature — the one the gate consumed over the
FULL dispatched args (``canonical_effect_signature(name, args)``). BOTH guards
honor by byte-exact EQUALITY against it: they recompute
``canonical_effect_signature`` over the exact dispatched args (the shell guard over
the terminal args threaded through the handler; the file guard over its tool args)
and require it to equal the consumed signature. This eliminates the
command-only-vs-full-args divergence that refused approved shell re-dispatches — a
different command OR a different non-command arg (e.g. workdir) yields a different
signature and is refused.

``ContextVar`` (NOT ``threading.local``) — native isolation across the gateway's
asyncio loop AND its ThreadPoolExecutor turn threads; ``reset(token)`` in a
``finally`` is exception-safe. Standalone module (no imports of registry /
approval / red_pending_store) so setter and readers import it without a cycle.
"""
from __future__ import annotations

import contextvars
from typing import Optional

# The EXACT gate-consumed effect signature of the tool call currently executing
# under an active ApprovalGate, or None when no gate-consumed dispatch is in flight.
consumed_signature_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "grove_consumed_signature", default=None
)
