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
        "forced. When the operator approves a proposal you just surfaced "
        "(e.g. replies 'approve' to a shop-floor-note push), call this with NO "
        "proposal_id — it targets the most recently offered proposal. Only pass "
        "proposal_id when the operator names a specific OTHER proposal to approve "
        "(e.g. one of multiple pending, identified by part of its summary or id "
        "prefix)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "OPTIONAL. The proposal id (full or short prefix) to approve. "
                    "OMIT to act on the proposal most recently surfaced to the "
                    "operator (the shop-floor-note push they are replying to)."
                ),
            },
        },
    },
}

REJECT_PROPOSAL_SCHEMA = {
    "name": "reject_proposal",
    "description": (
        "REJECT a pending Flywheel proposal as WRONG — the proposed fact, "
        "routing change, or memory is incorrect or unwanted. This is permanent: "
        "it feeds the detector's rejection memory so the same insight is NOT "
        "re-proposed. Use ONLY when the operator says the content is wrong / "
        "bad / not true (e.g. 'no, that's incorrect', 'reject that'). For a mere "
        "'not now' / 'stop showing me this' / 'skip it', use dismiss_proposal "
        "instead — a soft dismiss must NOT poison the detector's memory. Call "
        "with NO proposal_id to act on the proposal most recently surfaced."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "OPTIONAL. The proposal id (full or short prefix) to reject. "
                    "OMIT to act on the proposal most recently surfaced to the "
                    "operator (the shop-floor-note push they are replying to)."
                ),
            },
            "reason": {
                "type": "string",
                "description": "OPTIONAL: why the operator declined.",
            },
        },
    },
}

DISMISS_PROPOSAL_SCHEMA = {
    "name": "dismiss_proposal",
    "description": (
        "Soft-DISMISS a surfaced crystallization (memory) proposal: 'stop "
        "bothering me with this' / 'not now' / 'no thanks' / 'skip it'. The "
        "proposal loses its proactive-push privilege and stays in the CLI "
        "backlog for manual review, but is NOT recorded as a rejection — the "
        "detector's rejection memory is untouched, so valid insights are not "
        "blinded. Use this for the common 'don't show me that again' reply to a "
        "shop-floor note. If the operator says the content is actually WRONG, "
        "use reject_proposal instead. Call with NO proposal_id to act on the "
        "proposal most recently surfaced."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": (
                    "OPTIONAL. The proposal id (full or short prefix) to dismiss. "
                    "OMIT to act on the proposal most recently surfaced to the "
                    "operator (the shop-floor-note push they are replying to)."
                ),
            },
        },
    },
}


# ── Last-offered handle (agent-ux-critical-fixes) ────────────────────────────
# The proactive push (run_agent._append_pending_offer) records the proposal it
# just surfaced here, so a later bare 'approve'/'dismiss' (no id) can target it.
# The push note deliberately shows no id, and review_proposals can return dozens
# of near-identical memory proposals — without this handle the model cannot
# resolve which proposal a one-word reply refers to. Best-effort; never raises.

def _last_offered_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / ".last_offered_proposal.json"


def _write_last_offered(short_id: str, *, type: str = "", session_id: str = "") -> None:
    """Record the proposal a push just surfaced. Best-effort; never raises."""
    try:
        from datetime import datetime, timezone
        path = _last_offered_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "short_id": short_id, "type": type, "session_id": session_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — convenience handle; never block the push
        pass


def _read_last_offered() -> Optional[str]:
    """The short_id of the most recently surfaced proposal, or None."""
    try:
        path = _last_offered_path()
        if not path.exists():
            return None
        rec = json.loads(path.read_text(encoding="utf-8"))
        sid = rec.get("short_id")
        return sid if isinstance(sid, str) and sid.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _clear_last_offered() -> None:
    try:
        _last_offered_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# kaizen-push-cadence-v1.1 — session-scoped persistence for proactive-push
# cadence. v1 stored the cooldown (_last_push_turn) and the dedup set
# (_surfaced_proposal_ids) as ephemeral AIAgent attributes; the gateway rebuilds
# the agent on nearly every turn (per-turn enabled_toolsets busts the agent
# cache signature), wiping both and making the cooldown AND the dedup inert.
# This file survives the rebuild — the same proven pattern as _last_offered.
# Single-slot, content-keyed by session_id: a record from another session reads
# as empty, so a session boundary resets cadence (the once-per-session semantics
# the in-memory sets had). ``surfaced_connectors`` is in the schema for the
# deferred connector-dedup fold; the proposal path preserves it untouched.
def _push_cadence_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / ".push_cadence.json"


def _read_push_cadence(session_id: str) -> dict:
    """Cooldown + dedup state for THIS session's proactive pushes.

    Returns ``{"last_push_turn": int|None, "surfaced_ids": set[str],
    "surfaced_connectors": set[str]}``. Missing file, parse error, or a record
    written under a different session_id all read as empty defaults (a session
    boundary resets cadence). Best-effort; never raises.
    """
    empty = {
        "last_push_turn": None,
        "surfaced_ids": set(),
        "surfaced_connectors": set(),
        "connector_active_map": {},
    }
    try:
        path = _push_cadence_path()
        if not path.exists():
            return empty
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rec, dict) or rec.get("session_id") != (session_id or ""):
            return empty
        lpt = rec.get("last_push_turn")
        return {
            "last_push_turn": lpt if isinstance(lpt, int) else None,
            "surfaced_ids": set(rec.get("surfaced_ids") or []),
            "surfaced_connectors": set(rec.get("surfaced_connectors") or []),
            "connector_active_map": {
                str(k): str(v)
                for k, v in (rec.get("connector_active_map") or {}).items()
            },
        }
    except Exception:  # noqa: BLE001
        return empty


def _write_push_cadence(
    session_id: str,
    *,
    last_push_turn: Optional[int],
    surfaced_ids: set,
    surfaced_connectors: set,
    connector_active_map: dict = None,
) -> None:
    """Atomically persist this session's push cadence. Best-effort; never raises.

    A write failure degrades the cooldown toward may-surface-again (never toward
    silence): the operator always still sees proposals — we fail toward
    visibility, the safe direction for this UX surface.
    """
    try:
        from datetime import datetime, timezone
        path = _push_cadence_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "session_id": session_id or "",
                "last_push_turn": last_push_turn,
                "surfaced_ids": sorted(surfaced_ids),
                "surfaced_connectors": sorted(surfaced_connectors),
                "connector_active_map": dict(connector_active_map or {}),
                "ts": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — never block the push on a persistence hiccup
        pass


# ── Global "ever-pushed" memory ledger (crystallization-cadence-v1, Gap 1) ───
# The push cadence above is session-scoped BY DESIGN (the cooldown resets on a
# session boundary). But a crystallization proposal born from a prior dormant
# session must NOT re-push in every NEW session — the session reset re-armed
# the dedup, leaking verbatim duplicates across conversations. This ledger is
# the cross-session dedup: a flat, GLOBAL set of memory short_ids ever pushed
# in ANY session. Once a memory proposal is pushed, it permanently loses its
# auto-push privilege (still reachable via the flywheel CLI for manual review).
# Separate file so the session-scoped cadence reset never touches it.
def _pushed_memory_ids_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / ".pushed_memory_ids.json"


def _read_pushed_memory_ids() -> set:
    """The GLOBAL set of memory short_ids ever proactively pushed. Missing file
    or parse error reads as empty. Best-effort; never raises."""
    try:
        path = _pushed_memory_ids_path()
        if not path.exists():
            return set()
        rec = json.loads(path.read_text(encoding="utf-8"))
        return set(rec.get("pushed_ids") or []) if isinstance(rec, dict) else set()
    except Exception:  # noqa: BLE001
        return set()


def _mark_pushed_memory_id(short_id: str) -> None:
    """Add ``short_id`` to the global ever-pushed ledger. Atomic, idempotent,
    best-effort. A write failure degrades toward may-push-again (visibility),
    never toward silence."""
    if not short_id:
        return
    try:
        from datetime import datetime, timezone
        path = _pushed_memory_ids_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _read_pushed_memory_ids()
        if short_id in current:
            return
        current.add(short_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "pushed_ids": sorted(current),
                "ts": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


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


def review_proposals(*, queue_path: Optional[Path] = None) -> str:
    """List ALL pending Kaizen proposals — routing AND memory — in one surface.

    kaizen-proposal-surface-unification-v1: ONE code path. Both queues read,
    wrapped as KaizenRenderables, merged, sorted by ``_PUSH_PRIORITY``, and
    rendered through the unified ``get_renderer`` — no separate per-type
    formatting. One Kaizen voice: the operator does not distinguish the source.
    """
    from grove import flywheel_cli
    from grove.eval.proposal_queue import default_queue_path, read_all
    from grove.kaizen.renderable import MemoryProposalRenderable

    renderables: list = []
    target = queue_path or default_queue_path()
    try:
        renderables.extend(read_all(path=target))
    except Exception as exc:  # noqa: BLE001 — fail loud into the tool result
        return json.dumps(
            {"success": False, "error": f"could not read the proposal queue: {exc!r}"},
            ensure_ascii=False,
        )
    try:
        from grove.memory.cli import _base, _pending
        for _full_id, record in _pending(_base(None)):
            renderables.append(MemoryProposalRenderable(record))
    except Exception as exc:  # noqa: BLE001 — fail loud, do not hide the gap
        return json.dumps(
            {"success": False, "error": f"could not read memory proposals: {exc!r}"},
            ensure_ascii=False,
        )

    if not renderables:
        return json.dumps(
            {"success": True, "pending_count": 0, "proposals": [],
             "message": "No pending proposals."},
            ensure_ascii=False,
        )

    renderables.sort(
        key=lambda r: (flywheel_cli._PUSH_PRIORITY.get(r.type, 99), r.sort_key)
    )
    lines = [
        {
            "ordinal": i + 1,
            "display": f"[{r.type}] {flywheel_cli.get_renderer(r.type)(r)}",
            "id": r.short_id,
        }
        for i, r in enumerate(renderables)
    ]
    return json.dumps(
        {"success": True, "pending_count": len(lines), "proposals": lines},
        ensure_ascii=False,
    )


def approve_proposal(
    proposal_id: str = "",
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

    # No id ⇒ act on the proposal the push most recently surfaced (the operator
    # is replying 'approve' to a shop-floor note). agent-ux-critical-fixes.
    pid = (proposal_id or "").strip() if isinstance(proposal_id, str) else ""
    used_last_offered = False
    if not pid:
        pid = _read_last_offered() or ""
        used_last_offered = bool(pid)
        if not pid:
            return json.dumps(
                {"success": False, "error": "No proposal_id given and no recently "
                 "offered proposal to act on. Call review_proposals to choose one."},
                ensure_ascii=False,
            )

    # 1. Routing queue first (existing path, unchanged).
    if flywheel_cli._resolve_proposal(pid, queue_path=queue_path) is not None:
        rc, message = _capture(
            lambda: flywheel_cli.cli_approve(
                pid, queue_path=queue_path, machine_path=machine_path,
            )
        )
        if rc == 0 and used_last_offered:
            _clear_last_offered()
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
        if rc == 0 and used_last_offered:
            _clear_last_offered()
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
    proposal_id: str = "",
    reason: Optional[str] = None,
    *,
    queue_path: Optional[Path] = None,
) -> str:
    """Route an operator rejection to the right store (Phase 3.2 store-aware).

    Probe-in-order, mirroring approve_proposal: routing queue first, then
    memory_proposals.jsonl (``cli_memory_reject``).
    """
    from grove import flywheel_cli

    # No id ⇒ act on the proposal the push most recently surfaced (the operator
    # is replying 'dismiss'/'skip' to a shop-floor note). agent-ux-critical-fixes.
    pid = (proposal_id or "").strip() if isinstance(proposal_id, str) else ""
    used_last_offered = False
    if not pid:
        pid = _read_last_offered() or ""
        used_last_offered = bool(pid)
        if not pid:
            return json.dumps(
                {"success": False, "error": "No proposal_id given and no recently "
                 "offered proposal to dismiss. Call review_proposals to choose one."},
                ensure_ascii=False,
            )

    # 1. Routing queue first (existing path).
    if flywheel_cli._resolve_proposal(pid, queue_path=queue_path) is not None:
        rc, message = _capture(
            lambda: flywheel_cli.cli_reject(pid, reason=reason, queue_path=queue_path)
        )
        if rc == 0 and used_last_offered:
            _clear_last_offered()
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
        if rc == 0 and used_last_offered:
            _clear_last_offered()
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


def dismiss_proposal(
    proposal_id: str = "",
) -> str:
    """Soft-dismiss a surfaced crystallization (memory) proposal
    (crystallization-cadence-v1, Gap 3).

    Distinct from reject_proposal: dismiss flips the memory proposal to
    ``status="dismissed"`` — it loses push eligibility and stays in the CLI
    backlog, but is NOT recorded as a rejection, so the detector's rejection
    memory (``_recently_rejected``) is untouched and valid insights are not
    blinded. Memory proposals only; a routing proposal has no soft-dismiss
    (reject_proposal owns those).
    """
    pid = (proposal_id or "").strip() if isinstance(proposal_id, str) else ""
    used_last_offered = False
    if not pid:
        pid = _read_last_offered() or ""
        used_last_offered = bool(pid)
        if not pid:
            return json.dumps(
                {"success": False, "error": "No proposal_id given and no recently "
                 "offered proposal to dismiss. Call review_proposals to choose one."},
                ensure_ascii=False,
            )

    from grove.memory import cli as memory_cli
    mem_full, _err = memory_cli._resolve(memory_cli._base(None), pid)
    if mem_full is not None:
        rc, message = _capture(lambda: memory_cli.cli_memory_dismiss(pid))
        if rc == 0 and used_last_offered:
            _clear_last_offered()
        return json.dumps(
            {"success": rc == 0, "proposal_id": pid, "kind": "memory",
             "message": message},
            ensure_ascii=False,
        )

    return json.dumps(
        {"success": False, "proposal_id": pid,
         "message": "dismiss applies to crystallization (memory) proposals; "
                    "for a routing proposal use reject_proposal."},
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
    reg.register(
        name="dismiss_proposal",
        toolset="flywheel",
        schema=DISMISS_PROPOSAL_SCHEMA,
        handler=lambda args, **kw: dismiss_proposal(args.get("proposal_id", "")),
        emoji="🤫",
    )
