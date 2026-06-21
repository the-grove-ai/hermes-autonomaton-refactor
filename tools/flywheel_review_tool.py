"""flywheel_review — the surface-agnostic operator review/approve loop (GRV-009 B3).

The ONE operator-facing path to review and act on Flywheel proposals, built as
registered tools that run through the shared agent/dispatcher loop — so Telegram,
webui, and CLI inherit the loop with ZERO per-surface code. This is deliberately
NOT a per-surface ``/flywheel`` slash handler (a graft Invariant 1 forbids) and
NOT a Sovereign-Prompt refactor (the Sovereign Prompt is an AndonHalt over live
tool intents; it cannot carry a system-proposed queued item without a large
cross-surface refactor against its operator-initiated-only design).

General over proposal types: these tools operate on the proposal QUEUE
abstraction via :mod:`grove.flywheel_cli` — routing_adjustment, zone_promotion,
skill_promotion, pattern_promotion/demotion, skill_synthesis — never a
Flywheel-only path. The forthcoming kaizen-offerings sprint layers voice and
proactive surfacing ON this loop; it does not rebuild it.

Governance: the tools ROUTE to ``grove.flywheel_cli``; they never bypass the B1
registry gate (including B2's no-cluster refusal on routing_adjustment).
``approve_proposal`` / ``reject_proposal`` are Yellow-zoned
(``config/zones.schema.yaml``) so the Sovereign Prompt governs the apply — the
agent cannot self-approve a self-modification without the operator's mechanical
tap. ``review_proposals`` is Green (read-only).
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)


REVIEW_PROPOSALS_SCHEMA = {
    "name": "review_proposals",
    "description": (
        "List the Autonomaton's pending self-improvement proposals (the Flywheel "
        "queue) for the operator to review — routing changes, zone/skill "
        "promotions, drafted skills, pattern promotions/demotions. Read-only. "
        "Use this when the operator asks what the system wants to change, what's "
        "pending, or before approving or rejecting a proposal."
    ),
    "parameters": {"type": "object", "properties": {}},
}

APPROVE_PROPOSAL_SCHEMA = {
    "name": "approve_proposal",
    "description": (
        "Approve a pending Flywheel proposal by id (the full id or the short "
        "prefix shown by review_proposals), applying the operator-approved "
        "change. Handles BOTH routing/zone/skill/pattern proposals and memory "
        "proposals — one surface for everything Kaizen has pending. This is a "
        "governed self-modification: it routes through the same gate the CLI "
        "uses, so a proposal that lacks its required evidence is refused, not "
        "forced. Use ONLY when the operator explicitly approves a specific "
        "proposal. If multiple proposals are pending, specify which one by "
        "including part of the proposal summary or its ID prefix; on a bare "
        "'yes' with more than one pending, call review_proposals and ask the "
        "operator which they mean."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "The proposal id (full or short prefix) to approve.",
            },
        },
        "required": ["proposal_id"],
    },
}

REJECT_PROPOSAL_SCHEMA = {
    "name": "reject_proposal",
    "description": (
        "Reject (dismiss) a pending Flywheel proposal by id, removing it from the "
        "queue with no change applied. Use when the operator declines a proposal."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "The proposal id (full or short prefix) to reject.",
            },
            "reason": {
                "type": "string",
                "description": "OPTIONAL: why the operator declined.",
            },
        },
        "required": ["proposal_id"],
    },
}


def _capture(fn: Callable[[], int]) -> Tuple[int, str]:
    """Run a flywheel_cli command, capturing its operator-facing stdout/stderr.

    The cli_* functions print the human-readable result (what was applied, or the
    loud refusal message) and return a UNIX rc. The tool relays both: rc → success,
    captured text → the message the operator sees. No re-implementation of the
    rendering — the CLI text IS the surface output, identical on every surface.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn()
    return rc, (out.getvalue() + err.getvalue()).strip()


def _memory_proposal_summaries() -> list:
    """Pending memory proposals as unified one-liners (Phase 3.2).

    Reuses the memory CLI reader + short-id + the Phase 3
    ``MemoryProposalHandler.summary_renderer`` — no new parser/renderer. Each
    line carries the ``[memory_context]`` type tag and the ``(ID: <short>)``
    the operator/model passes back to approve_proposal/reject_proposal.
    """
    from grove.memory.cli import _base, _pending, memory_proposal_short_id
    from grove.memory.digest import MemoryProposalHandler
    from grove.memory.store import MemoryStore

    base = _base(None)
    pending = _pending(base)
    if not pending:
        return []
    handler = MemoryProposalHandler(MemoryStore(base_dir=base))
    lines = []
    for _full_id, record in pending:
        proposal = record["proposal"]
        short_id = memory_proposal_short_id(proposal)
        summary = handler.summary_renderer(proposal)
        lines.append(f"[memory_context] {summary} (ID: {short_id})")
    return lines


def review_proposals(*, queue_path: Optional[Path] = None) -> str:
    """List ALL pending Kaizen proposals — routing AND memory — in one surface.

    REUSES ``flywheel_cli._format_summary`` for routing and
    ``MemoryProposalHandler.summary_renderer`` for memory; no new renderer is
    built. One Kaizen voice: the operator does not distinguish the source.
    """
    from grove import flywheel_cli
    from grove.eval.proposal_queue import default_queue_path, read_all

    target = queue_path or default_queue_path()
    try:
        routing = read_all(path=target)
    except Exception as exc:  # noqa: BLE001 — fail loud into the tool result
        return json.dumps(
            {"success": False, "error": f"could not read the proposal queue: {exc!r}"},
            ensure_ascii=False,
        )
    try:
        memory_lines = _memory_proposal_summaries()
    except Exception as exc:  # noqa: BLE001 — fail loud, do not hide the gap
        return json.dumps(
            {"success": False, "error": f"could not read memory proposals: {exc!r}"},
            ensure_ascii=False,
        )

    lines = [flywheel_cli._format_summary(p) for p in routing] + memory_lines
    if not lines:
        return json.dumps(
            {"success": True, "pending_count": 0, "proposals": [],
             "message": "No pending proposals."},
            ensure_ascii=False,
        )
    return json.dumps(
        {"success": True, "pending_count": len(lines), "proposals": lines},
        ensure_ascii=False,
    )


def approve_proposal(
    proposal_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> str:
    """Route an operator approval to ``flywheel_cli.cli_approve``.

    The tool ROUTES; it does not bypass governance. The B1 registry gate (and
    B2's no-cluster refusal on routing_adjustment) enforces at cli_approve, and
    the tool is itself Yellow-zoned so the apply only runs on the operator's
    Sovereign-Prompt confirmation.

    Phase 3.2 — store-aware. Probe-in-order: resolve against the routing queue
    first (unchanged path); if no routing proposal matches, resolve against
    memory_proposals.jsonl and route to the self-contained memory apply path
    (``cli_memory_approve``, which constructs its own MemoryStore). Routing and
    memory short-ids are both hex, so format detection is unreliable — probe
    each backend rather than guess.
    """
    from grove import flywheel_cli

    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return json.dumps(
            {"success": False, "error": "approve_proposal requires a non-empty 'proposal_id'."},
            ensure_ascii=False,
        )
    pid = proposal_id.strip()

    # 1. Routing queue first (existing path, unchanged).
    if flywheel_cli._resolve_proposal(pid, queue_path=queue_path) is not None:
        rc, message = _capture(
            lambda: flywheel_cli.cli_approve(
                pid, queue_path=queue_path, machine_path=machine_path,
            )
        )
        return json.dumps(
            {"success": rc == 0, "proposal_id": pid, "kind": "routing",
             "message": message},
            ensure_ascii=False,
        )

    # 2. Memory store fallback (Phase 3.2) — self-contained apply path.
    from grove.memory import cli as memory_cli
    mem_full, _err = memory_cli._resolve(memory_cli._base(None), pid)
    if mem_full is not None:
        rc, message = _capture(lambda: memory_cli.cli_memory_approve(pid))
        return json.dumps(
            {"success": rc == 0, "proposal_id": pid, "kind": "memory",
             "message": message},
            ensure_ascii=False,
        )

    # 3. Neither store owns it.
    return json.dumps(
        {"success": False, "proposal_id": pid,
         "message": f"No proposal matches {pid!r}."},
        ensure_ascii=False,
    )


def reject_proposal(
    proposal_id: str,
    reason: Optional[str] = None,
    *,
    queue_path: Optional[Path] = None,
) -> str:
    """Route an operator rejection to the right store (Phase 3.2 store-aware).

    Probe-in-order, mirroring approve_proposal: routing queue first, then
    memory_proposals.jsonl (``cli_memory_reject``).
    """
    from grove import flywheel_cli

    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return json.dumps(
            {"success": False, "error": "reject_proposal requires a non-empty 'proposal_id'."},
            ensure_ascii=False,
        )
    pid = proposal_id.strip()

    # 1. Routing queue first (existing path).
    if flywheel_cli._resolve_proposal(pid, queue_path=queue_path) is not None:
        rc, message = _capture(
            lambda: flywheel_cli.cli_reject(pid, reason=reason, queue_path=queue_path)
        )
        return json.dumps(
            {"success": rc == 0, "proposal_id": pid, "kind": "routing",
             "message": message},
            ensure_ascii=False,
        )

    # 2. Memory store fallback (Phase 3.2).
    from grove.memory import cli as memory_cli
    mem_full, _err = memory_cli._resolve(memory_cli._base(None), pid)
    if mem_full is not None:
        rc, message = _capture(
            lambda: memory_cli.cli_memory_reject(pid, reason=reason)
        )
        return json.dumps(
            {"success": rc == 0, "proposal_id": pid, "kind": "memory",
             "message": message},
            ensure_ascii=False,
        )

    # 3. Neither store owns it.
    return json.dumps(
        {"success": False, "proposal_id": pid,
         "message": f"No proposal matches {pid!r}."},
        ensure_ascii=False,
    )


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools — one registration,
    inherited by every surface through the shared agent/dispatcher loop."""
    reg.register(
        name="review_proposals",
        toolset="flywheel",
        schema=REVIEW_PROPOSALS_SCHEMA,
        handler=lambda args, **kw: review_proposals(),
        emoji="📋",
    )
    reg.register(
        name="approve_proposal",
        toolset="flywheel",
        schema=APPROVE_PROPOSAL_SCHEMA,
        handler=lambda args, **kw: approve_proposal(args.get("proposal_id", "")),
        emoji="✅",
    )
    reg.register(
        name="reject_proposal",
        toolset="flywheel",
        schema=REJECT_PROPOSAL_SCHEMA,
        handler=lambda args, **kw: reject_proposal(
            args.get("proposal_id", ""), reason=args.get("reason"),
        ),
        emoji="🚫",
    )
