"""Session-volatile store for pending RED governance proposals.

propose-approve-deadlock-v1 Phase 1a (GOVERNANCE CORE).

A ``.env`` (RED) ``propose_governance_change`` is the operator's request to
persist a secret. The dispatcher classifies ``.env`` RED, but RED has no
store-and-resume path — it hard-cancels (Phase 0 verdict: STRUCTURAL bug). This
module is the CORE half of the fix: a **dispatcher-owned, in-memory** map of
pending RED proposals that a later operator approval mints and executes.

Confinement (deliberate):
  * IN-MEMORY ONLY. Never written to disk. A gateway restart drops every pending
    entry — a durable, restart-surviving store is NET-NEW infrastructure that
    Phase 0 (Task 2) flagged as scope-material and DEFERRED to a later gate. 1a
    is the volatile, same-session bridge.
  * NOT A TOOL. This object is held by the Dispatcher and is unreachable by any
    model-invoked tool (write_file/patch/shell only ever cross the registry;
    this class is never registered). The secret ``.env`` payload therefore never
    passes through an agent-readable surface.

The map is keyed by ``proposal_id`` = ``sha256(canonical .env content)`` so the
same proposed body is idempotent and the id doubles as the integrity anchor.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# The proposal ``type`` written (opaque) to the agent-reachable queue as the
# 1b portal-render bridge. Namespaced so existing flywheel/portal consumers
# render it generically and never mis-route it to a non-RED approve handler.
RED_PENDING_PROPOSAL_TYPE = "governance_env_pending"

# Match ``KEY=...`` env lines to name the affected keys (names are NOT secret;
# values are) for the masked operator-facing description.
_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def content_proposal_id(content: str) -> str:
    """``sha256`` of the canonical ``.env`` content — the map key + integrity anchor."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_env_keys(content: str) -> List[str]:
    """Key NAMES present in a proposed ``.env`` body (values excluded).

    Names are safe to surface (the operator authored them); values never are.
    Order-preserving, de-duplicated.
    """
    seen: Dict[str, None] = {}
    for m in _ENV_KEY_RE.finditer(content or ""):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def masked_env_description(target_file: str, key_names: List[str]) -> str:
    """Operator-facing description — names the target + keys, MASKS every value."""
    where = target_file or ".env"
    if key_names:
        shown = ", ".join(key_names[:6])
        more = "" if len(key_names) <= 6 else f" (+{len(key_names) - 6} more)"
        return f"Persist credential(s) to {where}: {shown}{more} — values hidden."
    return f"Persist credential(s) to {where} — values hidden."


def action_proposal_id(effect_signature: str) -> str:
    """Content-addressed id for a pending RED action — ``sha256`` of its effect
    signature (which binds tool + realpath-canonical args). Idempotent (the same
    action proposed twice collapses to one entry) and the id doubles as the
    integrity anchor re-checked at approve."""
    return hashlib.sha256(("red_action:" + effect_signature).encode("utf-8")).hexdigest()


def prepare_execute_arguments(tool_name: str, arguments: dict) -> dict:
    """The exact args to re-dispatch on approval — a COPY; the original is never
    mutated. ``propose_governance_change`` gets its TOCTOU anchor
    (``approved_content_sha256``) folded in so the shipped ``.env`` write path is
    byte-identical to 1a; every other tool re-dispatches its args verbatim."""
    args = dict(arguments or {})
    if tool_name == "propose_governance_change" and "approved_content_sha256" not in args:
        body = args.get("content")
        if body is None:
            body = args.get("diff_or_content")
        if isinstance(body, str) and body:
            args["approved_content_sha256"] = content_proposal_id(body)
    return args


def describe_red_action(
    tool_name: str, arguments: dict, pattern_key: Optional[str] = None
) -> Tuple[str, bool]:
    """``(operator-facing description, is_opaque)`` for the pending-RED card.

    Per-type: governance-write → masked env keys; legible shell → the command
    string; opaque dynamic (``pattern_key`` startswith ``opacity:``) → a
    non-committal "effect not statically resolved" line (the OPAQUE render
    affordance is Phase B). Generic tools name the tool + arg keys, values hidden.
    """
    is_opaque = bool(pattern_key and str(pattern_key).startswith("opacity:"))
    if is_opaque:
        return ("Opaque dynamic command — effect not statically resolved.", True)
    if tool_name == "propose_governance_change":
        body = arguments.get("content") or arguments.get("diff_or_content") or ""
        return (
            masked_env_description(arguments.get("target_file") or "", extract_env_keys(body)),
            False,
        )
    if tool_name in ("terminal", "execute_code"):
        raw = str(arguments.get("command") or arguments.get("code") or "").strip()
        shown = raw if len(raw) <= 200 else raw[:197] + "..."
        verb = "Run command" if tool_name == "terminal" else "Execute code"
        return (f"{verb}: {shown}", False)
    keys = ", ".join(sorted(str(k) for k in (arguments or {}).keys()))
    return (f"Run {tool_name}({keys}) — arguments hidden.", False)


@dataclass
class PendingRedProposal:
    """One pending RED action, held IN-MEMORY only.

    red-action-store-pending-v1 Phase A — generalized from the 1a ``.env``-only
    record to ANY RED action. The action is the frozen ``(tool_name, arguments)``
    ToolIntent; ``effect_signature`` is the ``canonical_effect_signature`` mint
    anchor + integrity gate; ``description`` is the per-type operator-facing copy.
    Value-bearing arguments (a ``.env`` body, a secret-read path) live only here,
    never on an agent tool surface or the durable queue. ``propose_governance_
    change`` is now ONE instance (``tool_name == "propose_governance_change"``).
    """

    proposal_id: str          # action_proposal_id(effect_signature) — key + anchor
    tool_name: str            # the RED tool to re-dispatch on approval
    arguments: dict           # the exact execute args (SECRET-bearing; in-memory only)
    effect_signature: str     # canonical_effect_signature(tool, args) — mint + integrity anchor
    description: str          # per-type operator-facing copy (masked/legible)
    rationale: str
    created_at: str
    is_opaque: bool = False   # ZoneResult.pattern_key startswith "opacity:" (Phase B render)
    zone: str = "red"


class RedPendingStore:
    """Dispatcher-owned, session-volatile map of pending RED proposals.

    Not a tool; no agent surface reaches it. Thread-safe (the gateway may touch
    it from the turn thread and an approval thread).
    """

    def __init__(self) -> None:
        self._by_id: Dict[str, PendingRedProposal] = {}
        self._lock = threading.Lock()

    def put(self, entry: PendingRedProposal) -> None:
        with self._lock:
            self._by_id[entry.proposal_id] = entry

    def get(self, proposal_id: str) -> Optional[PendingRedProposal]:
        with self._lock:
            return self._by_id.get(proposal_id)

    def pop(self, proposal_id: str) -> Optional[PendingRedProposal]:
        """Remove and return an entry (used on successful execute / abort)."""
        with self._lock:
            return self._by_id.pop(proposal_id, None)

    def masked_description(self, proposal_id: str) -> Optional[str]:
        """The masked operator-facing description for a proposal, or None."""
        entry = self.get(proposal_id)
        return entry.description if entry else None

    def has(self, proposal_id: str) -> bool:
        """True iff a live payload is held for *proposal_id*.

        propose-approve-deadlock-v1 Phase 1b-i (Step 3) — the portal render calls
        this (via ``request.app["red_pending_store"]``) to distinguish a live
        pending proposal from an ORPHAN: the durable queue row persists across a
        restart, but this in-memory payload does not, so a metadata row whose
        ``has()`` is False renders EXPIRED (1b-ii) rather than a dead approve.
        """
        with self._lock:
            return proposal_id in self._by_id

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)


# ── Process-level singleton (propose-approve-deadlock-v1 Phase 1b-i, Step 1) ──
# The store MUST survive across turns and requests: the gateway rebuilds the
# Dispatcher per turn (ThreadPoolExecutor) and the portal approve is a SEPARATE
# request, so the 1a per-Dispatcher instance was GC'd before approve — the
# proposal was unreachable. This shared process singleton (mirrors
# grove.grants.get_grant_store) is the ONE reachable, lifetime-scoped store both
# the store-on-propose (any Dispatcher) and the portal approve handler use. Still
# in-memory (restart drops it), still not a tool, still no agent surface reaches
# it. Thread-safe: the gateway touches it from executor threads AND the loop.
_STORE: Optional[RedPendingStore] = None


def get_red_pending_store() -> RedPendingStore:
    """Return the process-level RedPendingStore singleton (creates on first call)."""
    global _STORE
    if _STORE is None:
        _STORE = RedPendingStore()
    return _STORE


def approve_red_proposal(
    proposal_id: str, store: Optional[RedPendingStore] = None
) -> Dict[str, object]:
    """Mint-on-approve — claim-then-execute a pending RED action.

    red-action-store-pending-v1 Phase A — generalized from the 1a ``.env``-only
    write to ANY stored RED ToolIntent. Operator-only: the portal /confirm handler
    (holds the singleton store) and ``Dispatcher.approve_pending_red_proposal``
    (delegates here). NEVER model-reachable — no tool exposes it.

    CLAIM: atomic ``pop`` — exactly one claimant; a concurrent second approve pops
    ``None`` → ``not_found``. Integrity: the claimed args must still recompute to
    the stored ``effect_signature`` (realpath-canonical → a symlink swap is caught).
    Then mint that signature into an ISOLATED ApprovalGate and RE-DISPATCH the
    action through ``registry.dispatch`` — which consumes the token (signature
    match) and invokes the tool handler by name, the SAME seam every governed
    execution uses. ``propose_governance_change`` is one instance; the ``.env``
    write routes through here too (its TOCTOU anchor rode in via
    ``prepare_execute_arguments``, so the handler's second gate still fires).

    Returns ``{"success": bool, "reason": one of
    "written"/"not_found"/"integrity"/"unknown_tool"/"execute_error", ...}``.
    """
    import json as _json

    from grove.effect_signature import ApprovalGate, canonical_effect_signature

    st = store if store is not None else get_red_pending_store()

    entry = st.pop(proposal_id)  # CLAIM — atomic single-writer gate
    if entry is None:
        return {
            "success": False,
            "reason": "not_found",
            "error": (
                "No pending proposal with that id — it was already approved, or "
                "the gateway restarted (pending proposals are session-scoped and "
                "do not survive a restart)."
            ),
        }

    # Integrity: recompute the effect signature over the claimed args; it must
    # still match the stored anchor (realpath-canonical → symlink-swap caught).
    live_sig = canonical_effect_signature(entry.tool_name, entry.arguments)
    if live_sig != entry.effect_signature:
        return {  # entry already popped — nothing dispatched, fail-safe
            "success": False,
            "reason": "integrity",
            "error": (
                "Approval aborted — stored action integrity check failed (the "
                "effect changed since it was proposed). Nothing was executed."
            ),
        }

    # Isolated registry + gate: mint the bound signature, then RE-DISPATCH. The
    # gate is consumed inside registry.dispatch (canonical_effect_signature match).
    # A bare registry carries no MCP/plugin surface — a stored MCP action returns
    # unknown_tool (Phase B: ExecutionIdentity / registry completeness).
    from tools.registry import ToolRegistry, register_builtin_tools

    registry = ToolRegistry()
    register_builtin_tools(registry)
    if registry.get_entry(entry.tool_name) is None:
        return {
            "success": False,
            "reason": "unknown_tool",
            "error": (
                f"tool {entry.tool_name!r} is not registered on the approval "
                f"registry; cannot re-dispatch (Phase B: registry completeness)."
            ),
        }

    gate = ApprovalGate()
    gate.activate()
    registry._approval_gate = gate
    gate.mint(entry.effect_signature)
    try:
        result = registry.dispatch(entry.tool_name, entry.arguments)
    finally:
        gate.flush()  # single-use; the entry is already popped and cannot re-fire

    # registry.dispatch returns a JSON string; a handler error / GovernanceError
    # surfaces as {"error": ...}. Treat that as a LOUD execute failure, never a
    # silent success.
    ok, reason = True, "written"
    try:
        parsed = _json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("success") is False):
            ok, reason = False, "execute_error"
    except Exception:  # noqa: BLE001 — non-JSON handler output is treated as success
        pass
    return {
        "success": ok,
        "reason": reason,
        "proposal_id": proposal_id,
        "result": result,
    }
