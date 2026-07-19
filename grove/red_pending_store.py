"""Durable (SQLite WAL) store for pending RED governance proposals.

durable-red-store-v1 Move A (GOVERNANCE CORE). Supersedes the propose-approve-
deadlock-v1 Phase 1a/1b in-memory ``_by_id`` map: the store is now the SOLE
source of truth, backed by a SQLite WAL database under ``$GROVE_HOME``.

A RED action (a ``.env`` ``propose_governance_change`` credential write, or a
privileged / secret-bearing / opaque shell command) has no store-and-resume path
— RED hard-cancels. This module is the CORE half of the fix: a pending-RED store
that a later operator approval claims and executes (see ``approve_red_proposal``).

Confinement (deliberate):
  * DURABLE, but OPERATOR-CONFINED. Persisted to ``~/.grove/red_pending.db`` so a
    pending proposal survives the gateway rebuilding the Dispatcher per turn AND a
    full gateway restart (the durability the 1a volatile bridge deferred). The DB
    holds the raw (possibly secret-bearing) ``arguments``, so it is created
    owner-only (mode 0600) inside the already owner-only ``~/.grove`` — never an
    agent-readable surface.
  * NOT A TOOL. Never registered on any registry; no model-invoked tool reaches
    it. The secret payload therefore never crosses an agent-readable surface.

The claim is a single ``DELETE … RETURNING`` (whole-row atomic, exactly-once:
SQLite serializes writers). Keyed by ``proposal_id`` (content-addressed
``sha256`` of the effect signature) so the same proposed effect is idempotent and
the id doubles as the integrity anchor re-checked at approve.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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


def red_action_title(
    tool_name: str, pattern_key: Optional[str] = None, is_opaque: bool = False
) -> str:
    """The per-action-type portal card title (red-action-store-pending-v1 Phase B).
    Derived from the effect: governance write / privileged shell / secret access /
    opaque command / a generic tool fallback."""
    pk = str(pattern_key or "")
    if is_opaque or pk.startswith("opacity:"):
        return "RED — opaque command"
    if tool_name == "propose_governance_change":
        return "RED — governance write"
    if pk.startswith("priv:") or pk.startswith("rm:") or pk.startswith("govwrite:"):
        return "RED — privileged shell"
    if pk.startswith("secret:"):
        return "RED — secret access"
    if tool_name in ("terminal", "execute_code"):
        return "RED — shell command"
    return f"RED — {tool_name}"


def red_action_reason(
    tool_name: str, pattern_key: Optional[str] = None, is_opaque: bool = False
) -> str:
    """A short named reason WHY the action is RED — the ZoneResult reason, restated
    for the pending-RED card (unresolved-writer-execution-path-v1 Fix 3). Resolved
    from ``tool_name`` / ``pattern_key`` (the ZoneResult.reason text is not
    persisted); mirrors the :func:`red_action_title` classification so the card's
    title and reason agree."""
    pk = str(pattern_key or "")
    if is_opaque or pk.startswith("opacity:"):
        return ("Effect not statically resolved — approving authorizes the intent "
                "to run this string, not a guaranteed outcome.")
    if tool_name == "propose_governance_change":
        return "Writes credentials to a governed configuration file."
    if pk.startswith("priv:"):
        return "Needs privileges that stay with you — sudo / su / doas."
    if pk.startswith("UNRESOLVED_WRITER"):
        return ("Writes to a target that can't be resolved before it runs; "
                "treated as scope-defining.")
    if pk.startswith("secret:"):
        return "Reads a secret-bearing path."
    if pk.startswith("rm:") or pk.startswith("govwrite:"):
        return "A destructive or scope-defining shell effect."
    return "A command effect that requires your approval."


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
    if tool_name == "write_file":
        # kaizen-queue-hygiene-v1 K-5 — write_file args ARE in the store column; the
        # generic "arguments hidden" fallback below was a RENDER gap, not a data gap.
        # A write_file RED is scope/target-driven (a governed / meta-surface path), so
        # its path + content are legible-by-design — show the target and a bounded
        # content preview so the operator approves an effect they can actually see (an
        # approval surface that cannot show what it approves is no approval).
        path = str(arguments.get("path") or "?")
        content = str(arguments.get("content") or "")
        shown = content if len(content) <= 200 else content[:197] + "..."
        summary = f"Write file {path}"
        if content:
            summary += f" ({len(content)} chars): {shown}"
        return (summary, False)
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
    pattern_key: Optional[str] = None  # AST effect signature — drives the card title (Phase B)
    zone: str = "red"
    origin: str = "operator"  # durable-red-store-v1: the proposing surface. Move A
    # is the operator path (always "operator"); fleet-red-durable-v2 sets "fleet".


def default_red_pending_path() -> Path:
    """``~/.grove/red_pending.db`` — sibling of ``pattern_cache.db`` (top-level
    under ``$GROVE_HOME``). Resolved live so a redirected home (tests) is honored,
    mirroring :func:`grove.pattern_cache.default_pattern_cache_path`."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "red_pending.db"


# The durable schema — durable-red-store-v1 Move A (9 columns, LOCKED). No
# ``claim_state`` (the claim is the atomic ``DELETE … RETURNING`` — no UPDATE
# path) and NO nonce columns (nonces live only on the portal request surface,
# ``grove/api/red_nonce.py``). ``is_opaque``/``pattern_key`` are immutable
# proposal-time classification metadata, serialized so the portal OPAQUE banner +
# per-type card title resolve from SQLite now that the in-memory map is retired.
_COLUMNS = (
    "proposal_id", "tool_name", "arguments", "effect_signature",
    "masked_description", "origin", "created_at", "is_opaque", "pattern_key",
)

# Owner-only (0600). The DB persists raw, possibly secret-bearing ``arguments``
# (a ``secret:operand`` RED can carry a credential), so it is created strictly
# tighter than the 0644 sibling stores — which hold no secrets and are walled only
# by the 0700 ``~/.grove`` parent. Applied to the DB and its WAL/SHM sidecars.
_DB_MODE = 0o600


class RedPendingStore:
    """Durable (SQLite WAL) store of pending RED proposals — sole source of truth.

    durable-red-store-v1 Move A — replaces the Phase 1a/1b in-memory ``_by_id``
    map. The claim is a single ``DELETE … RETURNING`` (exactly-once across threads
    AND processes: SQLite serializes writers). Not a tool; never registered on any
    registry, so no agent surface reaches it. Held as a process-level thin handle
    (:func:`get_red_pending_store`) whose state is the on-disk DB, so a pending
    proposal survives both a per-turn Dispatcher rebuild and a gateway restart.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._path = Path(db_path) if db_path is not None else default_red_pending_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-create owner-only so the file is NEVER briefly world-readable between
        # creation and chmod (the payload is secret-bearing). No-op if it exists.
        if not self._path.exists():
            os.close(os.open(str(self._path), os.O_CREAT | os.O_RDWR, _DB_MODE))
        self._ensure_schema()
        self._harden_perms()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._path), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS red_pending (
                    proposal_id        TEXT PRIMARY KEY,
                    tool_name          TEXT NOT NULL,
                    arguments          TEXT NOT NULL,
                    effect_signature   TEXT NOT NULL,
                    masked_description TEXT NOT NULL,
                    origin             TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    is_opaque          INTEGER NOT NULL DEFAULT 0,
                    pattern_key        TEXT
                )
                """
            )

    def _harden_perms(self) -> None:
        """Force owner-only (0600) on the DB and its WAL/SHM sidecars. The sidecars
        are created by SQLite honoring the process umask (typically 0644), so a
        secret in the WAL would otherwise be group/other-readable."""
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self._path) + suffix)
            if p.exists():
                os.chmod(p, _DB_MODE)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> "PendingRedProposal":
        """Rebuild a PendingRedProposal from a durable row. ``rationale`` and
        ``zone`` are NOT persisted (unread after store — approve keys on
        tool_name/arguments/effect_signature; render keys on the others) and rebuild
        with their defaults; ``is_opaque`` coerces INTEGER 0/1 ↔ bool."""
        return PendingRedProposal(
            proposal_id=row["proposal_id"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments"]),
            effect_signature=row["effect_signature"],
            description=row["masked_description"],
            rationale="",
            created_at=row["created_at"],
            is_opaque=bool(row["is_opaque"]),
            pattern_key=row["pattern_key"],
            origin=row["origin"],
        )

    def put(self, entry: PendingRedProposal) -> None:
        """Durably persist a pending RED proposal — write + commit before returning.

        FAIL-CLOSED ORDERING (durable-red-store-v1 Move A / Gemini Q2): the caller
        commits THIS row BEFORE appending the proposal-queue ``governance_env_pending``
        row, so a crash between the two leaves an invisible payload orphan (no card)
        — never a card without a payload. ``INSERT OR REPLACE`` keeps ``put``
        idempotent on the content-addressed ``proposal_id`` (same effect → identical
        row). The ``with`` block commits on exit (WAL); the row is flushed to the WAL
        before this returns."""
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO red_pending "
                "(proposal_id, tool_name, arguments, effect_signature, "
                " masked_description, origin, created_at, is_opaque, pattern_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.proposal_id,
                    entry.tool_name,
                    json.dumps(entry.arguments, ensure_ascii=False),
                    entry.effect_signature,
                    entry.description,
                    getattr(entry, "origin", "operator"),
                    entry.created_at,
                    1 if entry.is_opaque else 0,
                    entry.pattern_key,
                ),
            )
        self._harden_perms()  # a first-write may have just created the WAL/SHM

    def get(self, proposal_id: str) -> Optional[PendingRedProposal]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM red_pending WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def pop(self, proposal_id: str) -> Optional[PendingRedProposal]:
        """Atomic claim — ``DELETE … RETURNING``. Exactly one caller receives the
        row; a concurrent second caller receives zero rows (SQLite serializes
        writers), which is the exactly-once approve gate that replaces the 1a dict
        ``.pop``. Used on successful execute / abort."""
        with self._connect() as con:
            row = con.execute(
                "DELETE FROM red_pending WHERE proposal_id=? RETURNING *",
                (proposal_id,),
            ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def masked_description(self, proposal_id: str) -> Optional[str]:
        """The masked operator-facing description for a proposal, or None."""
        with self._connect() as con:
            row = con.execute(
                "SELECT masked_description FROM red_pending WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return row["masked_description"] if row is not None else None

    def is_opaque(self, proposal_id: str) -> bool:
        """True iff the pending action is an OPAQUE dynamic effect (the classifier
        could not statically resolve it). red-action-store-pending-v1 Phase B — the
        portal card reads this to render the OPAQUE_DYNAMIC_EFFECT warning. Resolved
        from the persisted ``is_opaque`` flag."""
        with self._connect() as con:
            row = con.execute(
                "SELECT is_opaque FROM red_pending WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return bool(row["is_opaque"]) if row is not None else False

    def card_title(self, proposal_id: str) -> str:
        """Per-action-type portal card title (Phase B). Defaults to a generic RED
        title for a missing entry. Resolved from the persisted ``tool_name`` /
        ``pattern_key`` / ``is_opaque``."""
        entry = self.get(proposal_id)
        if entry is None:
            return "RED — action"
        return red_action_title(entry.tool_name, entry.pattern_key, entry.is_opaque)

    def is_credential_write(self, proposal_id: str) -> bool:
        """True iff the pending action is a credential (``.env`` governance) write —
        the ONLY card kind that renders the masked-value ``.env`` template
        (unresolved-writer-execution-path-v1 Fix 3). Every other RED (shell / generic)
        renders the command/effect card. Resolved from the persisted ``tool_name``;
        a missing entry is not a credential write."""
        entry = self.get(proposal_id)
        return entry is not None and entry.tool_name == "propose_governance_change"

    def card_reason(self, proposal_id: str) -> Optional[str]:
        """The named RED reason for the card (Fix 3), or None for a missing entry.
        Resolved from the persisted ``tool_name`` / ``pattern_key`` / ``is_opaque``."""
        entry = self.get(proposal_id)
        if entry is None:
            return None
        return red_action_reason(entry.tool_name, entry.pattern_key, entry.is_opaque)

    def has(self, proposal_id: str) -> bool:
        """True iff a durable payload row exists for *proposal_id*.

        The portal render calls this (via ``request.app["red_pending_store"]``) to
        distinguish a live pending proposal from an ORPHAN: the durable queue row can
        outlive its payload row (payload claimed/cleared, or a legacy row), so a
        metadata row whose ``has()`` is False renders EXPIRED (1b-ii, retained as
        defense-in-depth) rather than a dead approve."""
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM red_pending WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return row is not None

    def __len__(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COUNT(*) AS n FROM red_pending").fetchone()
        return int(row["n"])


# ── Process-level singleton (durable-red-store-v1 Move A; thin handle) ─────────
# Retained for the reason 1b-i introduced it — the gateway rebuilds the Dispatcher
# per turn (ThreadPoolExecutor) and the portal approve is a SEPARATE request, so a
# per-Dispatcher instance would be GC'd before approve. This shared handle (mirrors
# grove.grants.get_grant_store) is the ONE store the store-on-propose (any
# Dispatcher) and the portal approve handler use. Its state is now the on-disk DB,
# so it ALSO survives a gateway restart. Still not a tool; no agent surface reaches
# it. Concurrency is handled by SQLite (per-call connections + writer
# serialization), not an in-process lock.
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
                "No pending proposal with that id — it was already approved or "
                "dismissed. The durable payload store survives restarts, so a "
                "missing payload means it was disposed, not lost to a restart."
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
    # unresolved-writer-execution-path-v1 Fix 1 — the approved-effect ContextVar is
    # now published by ``registry.dispatch`` itself (the EXACT gate-CONSUMED
    # signature), unifying what the gate consumed with what the tool guard honors.
    # No separate setter here; the gate mint above is claimed inside dispatch.
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
        # operator-red-correctness-v1 Move 2 — additive per-effect identity so the
        # portal confirm card reflects the ACTUAL executed effect rather than a
        # hardcoded governance-write mislabel. tool_name/pattern_key are non-secret
        # classification metadata; target_path is the governance-write PATH ONLY
        # (from target_file) — never the credential value (which stays in arguments,
        # not surfaced here). Absent for non-governance effects (terminal → None).
        "tool_name": entry.tool_name,
        "pattern_key": entry.pattern_key,
        "target_path": (entry.arguments or {}).get("target_file"),
        # artifact-continuation-v1 P2 (1e ruling) — the approved action's
        # filesystem write targets, derived by the SEAM'S OWN extraction
        # machinery (never a parallel derivation). PATHS only — argument
        # values (which may bear secrets) are never surfaced. Confirm-time
        # identity emission consumes this to file artifact_written for the
        # approved write.
        "write_targets": _extract_write_targets_safe(
            entry.tool_name, entry.arguments
        ),
    }


def _extract_write_targets_safe(tool_name: str, arguments: dict) -> list:
    """``tools.file_tools.extract_write_targets`` behind a loud-resilient
    guard: identity metadata must never fail an approval that already
    executed. Non-write tools return [] by the extractor's own contract."""
    try:
        from tools.file_tools import extract_write_targets

        return list(extract_write_targets(tool_name, arguments or {}))
    except Exception as exc:  # noqa: BLE001 — telemetry-only
        logger.warning(
            "[red-pending] write-target extraction failed (identity metadata "
            "only — the approved execution stands): %r", exc,
        )
        return []


def reap_orphaned_red_pending(
    *,
    store: Optional[RedPendingStore] = None,
    queue_path: Optional[Path] = None,
    ledger_dir: Optional[Path] = None,
) -> List[str]:
    """Startup reaper (kaizen-queue-hygiene-v1 K-2a) — dispose orphaned RED bridge rows.

    A RED action commits its payload to ``red_pending.db`` FIRST, then appends a
    ``governance_env_pending`` bridge row to the proposal queue (:func:`put` docstring).
    Disposal is the reverse: the portal claim pops the payload (:func:`RedPendingStore.pop`)
    and, in a SEPARATE non-transactional write, removes the queue row. A partial
    disposition — payload popped, queue row not removed — strands a bridge row whose
    ``store.has()`` is False; the portal then renders it as a dead EXPIRED card that
    only a manual dismiss clears. This sweep, run once at gateway startup, disposes
    every such orphan through the sanctioned ledger-recording writer, making the
    non-transactional pair safe going forward: an orphan is transient, one restart deep.

    ONLY ``governance_env_pending`` rows whose payload is gone are touched — a bridge
    row WITH a live payload (a real pending RED) is never swept. Disposal routes
    through :func:`grove.eval.proposal_queue.finalize_proposal_state` so each reap
    writes a ``kaizen_disposition`` ledger row (provenance per reap); there is NO raw
    store rewrite and no new writer surface. A RED bridge row has no apply handler, so
    finalize records the disposition and dequeues WITHOUT invoking one.

    Returns the reaped proposal_ids. Always emits ONE loud summary log line — a
    zero-reap run logs too, so the sweep is never a silent pass.
    """
    from grove.eval.proposal_queue import (
        finalize_proposal_state,
        read_all,
    )

    st = store if store is not None else get_red_pending_store()
    reaped: List[str] = []
    for proposal in read_all(path=queue_path):
        # governance_env_pending is the ONLY RED type that bridges to a payload store
        # (dispatcher.py / continuation.py append it for EVERY RED action kind —
        # governance write / privileged shell / secret / opaque — under this one
        # bridge type). It is therefore the only type for which a missing payload means
        # ORPHAN rather than a by-design payload-less proposal. Extend this filter if a
        # new payload-bridged RED type is ever added.
        if proposal.type != RED_PENDING_PROPOSAL_TYPE:
            continue
        # The queue row's id is PREFIXED ("governance_env_pending:<hash>"); the store
        # keys on the bare <hash>. Strip the prefix for the has() lookup EXACTLY as the
        # portal does (actions.py / fragments.py: split(":", 1)[1]) — the queue id stays
        # prefixed for disposal below.
        bare = (
            proposal.proposal_id.split(":", 1)[1]
            if ":" in proposal.proposal_id
            else proposal.proposal_id
        )
        if st.has(bare):
            continue  # live payload — a real pending RED, never swept
        if finalize_proposal_state(
            proposal.proposal_id,
            "reaped",
            reason=(
                "Orphaned RED bridge row — the red_pending.db payload is absent "
                "(store.has()==False), so this card can never approve. The payload "
                "was disposed without the paired queue-row removal; startup reaper "
                "clears the stranded row."
            ),
            path=queue_path,
            ledger_dir=ledger_dir,
        ):
            reaped.append(proposal.proposal_id)

    logger.info(
        "[red-reaper] startup sweep: reaped %d orphaned %s row(s)%s",
        len(reaped),
        RED_PENDING_PROPOSAL_TYPE,
        (": " + ", ".join(reaped)) if reaped else "",
    )
    return reaped
