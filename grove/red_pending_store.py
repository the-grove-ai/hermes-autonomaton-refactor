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
    if tool_name == "add_catalog_entry":
        # model-catalog-v1 P4 — the mint tool's Yellow approval card shows the
        # resolved merged view (per-slug SHADOWS markers) built from the tool
        # args, so the operator approves the effective catalog, not raw fields.
        try:
            from grove.config.model_catalog import describe_catalog_entry_addition

            desc = describe_catalog_entry_addition(dict(arguments or {}))
        except Exception:  # noqa: BLE001 — describer must never break the card
            desc = None
        if desc is not None:
            return (desc, False)
        slug = str((arguments or {}).get("slug") or "?")
        return (f"Add model {slug} to the catalog.", False)
    if tool_name == "write_file":
        # kaizen-queue-hygiene-v1 K-5 — write_file args ARE in the store column; the
        # generic "arguments hidden" fallback below was a RENDER gap, not a data gap.
        # A write_file RED is scope/target-driven (a governed / meta-surface path), so
        # its path + content are legible-by-design — show the target and a bounded
        # content preview so the operator approves an effect they can actually see (an
        # approval surface that cannot show what it approves is no approval).
        path = str(arguments.get("path") or "?")
        content = str(arguments.get("content") or "")
        # model-catalog-v1 M-5/G-4 — a write to the model catalog renders the
        # fully-resolved merged view (per-slug SHADOWS markers) instead of the
        # raw file blob, so the operator approves the effective catalog, not a
        # diff they must merge in their head. Falls back to the generic render
        # if the path is not a catalog file or the content does not parse.
        try:
            from grove.config.model_catalog import describe_catalog_write

            catalog_desc = describe_catalog_write(path, content)
        except Exception:  # noqa: BLE001 — render helper must never break the card
            catalog_desc = None
        if catalog_desc is not None:
            return (catalog_desc, False)
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
    # ── capability-mutation-surface-v1 P4 (M3) — SEALED CLAIM fields ──
    # target_sha256: propose-time content hash of the write target (None for
    # non-file effects) — the approve-time CAS anchor. A drifted target
    # withdraws the claim loudly (reason="target_drift"), never writes.
    target_sha256: Optional[str] = None
    # writer_name/writer_payload/sealed_target: the WRITER-REGISTRY TRANSLATION
    # for governed-config targets ONLY — sealed at propose-time BEFORE put(),
    # dispatched through the named sanctioned writer on approval. Non-config
    # RED claims (terminal etc.) keep writer_name=None and re-dispatch
    # semantics under the same lifecycle (LIFECYCLE is tool-agnostic; the
    # TRANSLATION is config-only — P4 scope split).
    writer_name: Optional[str] = None
    writer_payload: Optional[dict] = None
    sealed_target: Optional[str] = None


def default_red_pending_path() -> Path:
    """``~/.grove/red_pending.db`` — sibling of ``pattern_cache.db`` (top-level
    under ``$GROVE_HOME``). Resolved live so a redirected home (tests) is honored,
    mirroring :func:`grove.pattern_cache.default_pattern_cache_path`."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "red_pending.db"


# The durable schema — durable-red-store-v1 Move A (9 base columns) extended by
# capability-mutation-surface-v1 P4 (M3): sealed-claim columns (target_sha256 /
# writer_name / writer_payload / sealed_target), the transactional-claim marker
# (claimed_at — NULL = unclaimed; the claim is now UPDATE-claim -> dispatch ->
# DELETE-on-success-only, superseding Move A's DELETE…RETURNING-as-claim), and
# last_error (the surfaced writer failure a retained claim carries). NO nonce
# columns (nonces live only on the portal request surface,
# ``grove/api/red_nonce.py``). Legacy ROWS get no migration shim (P4 item 5 —
# R1 confirmed 0 live rows): DDL converges via ALTER ADD COLUMN, and an
# old-shape governed-config row fails LOUD at approve (legacy_shape), never
# silently re-dispatches.
_COLUMNS = (
    "proposal_id", "tool_name", "arguments", "effect_signature",
    "masked_description", "origin", "created_at", "is_opaque", "pattern_key",
    "target_sha256", "writer_name", "writer_payload", "sealed_target",
    "claimed_at", "last_error",
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
                    pattern_key        TEXT,
                    target_sha256      TEXT,
                    writer_name        TEXT,
                    writer_payload     TEXT,
                    sealed_target      TEXT,
                    claimed_at         TEXT,
                    last_error         TEXT
                )
                """
            )
            # DDL convergence for a pre-P4 DB (NOT a row-migration shim —
            # P4 item 5): nullable columns added in place; legacy rows read
            # with NULL sealed fields and are refused LOUD at approve when
            # their shape demands sealing (legacy_shape).
            have = {
                r["name"]
                for r in con.execute("PRAGMA table_info(red_pending)")
            }
            for col in (
                "target_sha256", "writer_name", "writer_payload",
                "sealed_target", "claimed_at", "last_error",
            ):
                if col not in have:
                    con.execute(
                        f"ALTER TABLE red_pending ADD COLUMN {col} TEXT"
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
        keys = row.keys()

        def _col(name):
            return row[name] if name in keys else None

        wp = _col("writer_payload")
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
            target_sha256=_col("target_sha256"),
            writer_name=_col("writer_name"),
            writer_payload=json.loads(wp) if wp else None,
            sealed_target=_col("sealed_target"),
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
                " masked_description, origin, created_at, is_opaque, "
                " pattern_key, target_sha256, writer_name, writer_payload, "
                " sealed_target, claimed_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
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
                    getattr(entry, "target_sha256", None),
                    getattr(entry, "writer_name", None),
                    (
                        json.dumps(entry.writer_payload, ensure_ascii=False)
                        if getattr(entry, "writer_payload", None) is not None
                        else None
                    ),
                    getattr(entry, "sealed_target", None),
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
        """Atomic consume — ``DELETE … RETURNING``. capability-mutation-
        surface-v1 P4 (M4): no longer the approve CLAIM (that is
        :meth:`claim`); ``pop`` fires ON SUCCESS ONLY (and on operator
        dismiss/withdraw). Exactly one caller receives the row (SQLite
        serializes writers)."""
        with self._connect() as con:
            row = con.execute(
                "DELETE FROM red_pending WHERE proposal_id=? RETURNING *",
                (proposal_id,),
            ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    # ── capability-mutation-surface-v1 P4 (M4) — transactional claim ────────
    # Claim state machine (tool-agnostic; the store's lifecycle):
    #
    #   PENDING (row, claimed_at NULL)
    #     --claim()-->            IN_FLIGHT (claimed_at set; exactly-once via
    #                             UPDATE…WHERE claimed_at IS NULL RETURNING)
    #   IN_FLIGHT
    #     --success-->            CONSUMED   (pop: DELETE — success only)
    #     --writer failure-->     PENDING    (release: claimed_at NULL,
    #                             last_error surfaced; retry/withdraw offered)
    #     --CAS drift/integrity-> WITHDRAWN  (withdraw: DELETE, reason loud)
    #   crash while IN_FLIGHT --> release_stale_claims() at startup reap
    #                             (single-gateway: nothing is genuinely in
    #                             flight across a restart).

    def claim(self, proposal_id: str):
        """Atomically claim a PENDING row for execution. Returns
        ``(entry, "claimed")``, ``(None, "absent")``, or ``(entry,
        "in_flight")`` when a concurrent approve already holds the claim."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            row = con.execute(
                "UPDATE red_pending SET claimed_at=? "
                "WHERE proposal_id=? AND claimed_at IS NULL RETURNING *",
                (now, proposal_id),
            ).fetchone()
            if row is not None:
                return self._row_to_entry(row), "claimed"
            row = con.execute(
                "SELECT * FROM red_pending WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            return None, "absent"
        return self._row_to_entry(row), "in_flight"

    def release(self, proposal_id: str, *, error: Optional[str] = None) -> None:
        """Return an IN_FLIGHT claim to PENDING after a writer failure — the
        claim SURVIVES with the error surfaced (last_error) so the portal card
        offers retry (re-approve) or withdraw (dismiss)."""
        with self._connect() as con:
            con.execute(
                "UPDATE red_pending SET claimed_at=NULL, last_error=? "
                "WHERE proposal_id=?",
                (error, proposal_id),
            )

    def withdraw(self, proposal_id: str) -> Optional[PendingRedProposal]:
        """Terminal removal WITH reason semantics (CAS drift / integrity /
        operator withdraw) — same DELETE as pop; named separately so call
        sites read as the state transition they are."""
        return self.pop(proposal_id)

    def last_error(self, proposal_id: str) -> Optional[str]:
        """The surfaced error of a retained (failed-and-released) claim."""
        with self._connect() as con:
            row = con.execute(
                "SELECT last_error FROM red_pending WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        return row["last_error"] if row is not None else None

    def release_stale_claims(self) -> int:
        """Startup crash-recovery: clear every IN_FLIGHT marker. The gateway
        is single-instance, so a claim surviving a restart is stale by
        definition — the row returns to PENDING (an entry that failed
        execution is NOT an orphan; its payload row is intact)."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE red_pending SET claimed_at=NULL "
                "WHERE claimed_at IS NOT NULL"
            )
            return cur.rowcount

    def rendered_payload(self, proposal_id: str) -> Optional[str]:
        """The EXACT payload the executor dispatches, for portal review —
        capability-mutation-surface-v1 P4 (M3): render and dispatch read the
        same durable row, so they can never diverge (T3d byte-parity).

        Sealed claims render the canonical writer-payload serialization;
        unsealed claims render the content-bearing argument the re-dispatch
        carries (write content / governance body / shell command)."""
        entry = self.get(proposal_id)
        if entry is None:
            return None
        if entry.writer_name is not None:
            return json.dumps(
                entry.writer_payload or {}, sort_keys=True, ensure_ascii=False
            )
        args = entry.arguments or {}
        if entry.tool_name == "propose_governance_change":
            body = args.get("content")
            if body is None:
                body = args.get("diff_or_content")
            return body if isinstance(body, str) else None
        if "content" in args and isinstance(args.get("content"), str):
            return args["content"]
        if "command" in args and isinstance(args.get("command"), str):
            return args["command"]
        return json.dumps(args, sort_keys=True, ensure_ascii=False)

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


# ── capability-mutation-surface-v1 P4 (M3) — claim sealing + writer registry ──


def _hash_target_bytes(target: str) -> str:
    """sha256 of the target file's current bytes; an ABSENT target hashes as
    empty bytes (the creation anchor — a file appearing between propose and
    approve is drift)."""
    import hashlib

    p = Path(os.path.realpath(os.path.expanduser(target)))
    try:
        data = p.read_bytes() if p.exists() else b""
    except OSError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def _first_write_target(tool_name: str, arguments: dict):
    """(target, body) for a claim — propose carries its own target arg; the
    write-family tools go through the shared extraction seam."""
    args = arguments or {}
    if tool_name == "propose_governance_change":
        target = args.get("target_file")
        body = args.get("content")
        if body is None:
            body = args.get("diff_or_content")
        return (
            target if isinstance(target, str) and target.strip() else None,
            body if isinstance(body, str) else None,
        )
    targets = _extract_write_targets_safe(tool_name, args)
    body = args.get("content")
    return (
        targets[0] if targets else None,
        body if isinstance(body, str) else None,
    )


def _dock_status_changes(body: str):
    """Map a proposed dock.yaml body to goal-status mutations, or None.

    The Dock's ONLY sanctioned mutator is ``update_dock_goal_status`` (status
    field per goal) — P5 ruling: dock targets seal against the existing dock
    writers, no new writer is minted. A proposed body qualifies iff it is the
    CURRENT dock.yaml with nothing but ``goals[*].status`` changed; anything
    else (structure edits, new goals, non-status fields) is a registry miss.
    """
    import copy

    import yaml as _yaml

    from hermes_constants import get_hermes_home

    dock_path = Path(get_hermes_home()) / "dock" / "dock.yaml"
    try:
        current = _yaml.safe_load(dock_path.read_text(encoding="utf-8"))
        proposed = _yaml.safe_load(body)
    except Exception:  # noqa: BLE001 — unreadable/unparseable → no mapping
        return None
    if not isinstance(current, dict) or not isinstance(proposed, dict):
        return None

    def _strip_status(doc):
        c = copy.deepcopy(doc)
        for g in c.get("goals") or []:
            if isinstance(g, dict):
                g.pop("status", None)
        return c

    if _strip_status(current) != _strip_status(proposed):
        return None  # more than a status mutation — not expressible
    cur_by_id = {
        g.get("id"): g
        for g in (current.get("goals") or [])
        if isinstance(g, dict) and g.get("id")
    }
    from grove.dock import _VALID_STATUSES

    changes = []
    for g in proposed.get("goals") or []:
        if not isinstance(g, dict):
            return None
        gid = g.get("id")
        if gid in cur_by_id and g.get("status") != cur_by_id[gid].get("status"):
            # An off-set status is a registry miss at SEAL time — never a
            # queued claim that the dock writer will refuse post-approval.
            if g.get("status") not in _VALID_STATUSES:
                return None
            changes.append({"goal_id": gid, "status": g["status"]})
    return changes or None


def _resolve_governed_writer(resolved_target: str, body):
    """capability-mutation-surface-v1 P5 (M5) — the WRITER REGISTRY resolution.

    Maps a governed-config target to its registered sanctioned writer:
    ``(writer_name, writer_payload, "")`` or ``(None, None, miss_reason)``.
    The registry is the SOLE write authority — a governed surface with no
    registration (e.g. the retired ``~/.grove/zones.schema.yaml`` dead door)
    is a registry miss, and its claims are refused at the viability seam
    before anything is queued."""
    from hermes_constants import get_hermes_home

    gh = Path(os.path.realpath(get_hermes_home()))
    p = Path(resolved_target)

    # .env — operator-only credential store; anywhere on disk (the universal
    # rule classify_governance_target carried, preserved here).
    if p.name == ".env":
        if not isinstance(body, str):
            return None, None, ".env write carries no content body"
        return "env_write", {"target_file": str(p), "content": body}, ""

    if p == gh / "routing.config.yaml":
        if not isinstance(body, str):
            return None, None, "routing config write carries no content body"
        # D3 unification — sealed against RoutingConfigWriter.apply_mutation
        # (backup → sandbox-validate → atomic replace → hot-reload); the
        # governance door's raw write path is dead. deploy.sh's cp remains
        # the deliberate out-of-band writer.
        return "routing_config_replace", {"content": body}, ""

    if p == gh / "dock" / "dock.yaml":
        if not isinstance(body, str):
            return None, None, "dock manifest write carries no content body"
        changes = _dock_status_changes(body)
        if changes is None:
            return None, None, (
                "dock.yaml admits only goal-status mutation "
                "(update_dock_goal_status is the sole registered dock writer); "
                "the proposed body changes more than goals[*].status"
            )
        return "dock_goal_status", {"changes": changes}, ""

    if p.parent == gh / "capabilities" / "state" and p.suffix == ".yaml":
        if not isinstance(body, str):
            return None, None, "admission state write carries no content body"
        try:
            import yaml as _yaml

            doc = _yaml.safe_load(body)
        except Exception:  # noqa: BLE001
            doc = None
        if (
            isinstance(doc, dict)
            and isinstance(doc.get("id"), str)
            and set(doc) <= {"id", "intents", "tiers", "provenance"}
            and (doc.get("intents") is not None or doc.get("tiers") is not None)
        ):
            return "write_admission_state", {
                "record_id": doc["id"],
                "intents": doc.get("intents"),
                "tiers": doc.get("tiers"),
            }, ""
        return None, None, (
            "capability state writes admit only canonical admission docs "
            "({id, intents, tiers}) through write_admission_state"
        )

    return None, None, "no governed writer is registered for this surface"


def is_viable_red_target(tool_name: str, arguments: dict):
    """capability-mutation-surface-v1 P5 (M6) — the VIABILITY SEAM (ruling
    A-1). Consulted by ``_store_pending_red_proposal`` BEFORE ``put()`` and
    before the queue-row append, so an approvable-but-unappliable card can
    never be minted (the 2026-07-21 browser_read churn class).

    Returns ``(True, "")`` or ``(False, reason)``:

    * REPO-DEFINITION targets (``<module_root>/config/**``, including
      ``config/capabilities/**``): refused — approval could never apply them
      (kernel read-only on the deployed VM); the reason names the git commit
      + deploy SOP.
    * GOVERNED-CONFIG targets with no registered writer: refused — the
      writer registry is the sole write authority (this retires the
      ``~/.grove/zones.schema.yaml`` dead door: nothing registers for it).
    * Non-config RED claims (terminal etc.): pass through — lifecycle-only,
      no translation, behavior unchanged.
    """
    target, body = _first_write_target(tool_name, arguments)
    if target is None:
        return True, ""  # target-less RED (shell etc.) — lifecycle-only
    resolved = os.path.realpath(os.path.expanduser(target))

    from grove.utils.fs_utils import _MODULE_CONFIG_ROOT, is_scope_defining

    if resolved == _MODULE_CONFIG_ROOT or resolved.startswith(
        _MODULE_CONFIG_ROOT + os.sep
    ):
        reason = (
            f"Nonviable target {resolved}: a repo DEFINITION surface "
            "(<module_root>/config/**) — a runtime write can never apply "
            "(config/ is kernel read-only on the deployed VM), so approving "
            "it would only mint a dead card. Definition surfaces change "
            "through a git commit and deploy (scripts/deploy.sh) — that is "
            "the sanctioned path."
        )
        # capability-mutation-surface-v1 P7 micro-arc item 2 — SIGNPOST: a
        # capability-definition target usually means the caller wants an
        # ADMISSION change, which IS operator-mutable at runtime. Redirect
        # to the governed door instead of dead-ending at git+deploy.
        rel = os.path.relpath(resolved, _MODULE_CONFIG_ROOT)
        if (
            rel.startswith("capabilities" + os.sep)
            and rel.endswith((".yaml", ".yml"))
        ):
            reason += (
                " NOTE: capability admission (intents/tiers) is "
                "operator-mutable via a governed admission-state proposal "
                "(write_admission_state) — no deploy required; definitions "
                "stay git+deploy."
            )
        return False, reason
    if Path(resolved).name == ".env" or is_scope_defining(resolved):
        writer, _payload, miss = _resolve_governed_writer(resolved, body)
        if writer is None:
            return False, (
                f"Nonviable governed target {resolved}: no registered writer "
                f"({miss}). The governed-writer registry is the sole write "
                "authority — a claim with no writer could never execute."
            )
    return True, ""


def seal_red_claim(tool_name: str, arguments: dict) -> Dict[str, object]:
    """Propose-time SEALING — capability-mutation-surface-v1 M3/M5.

    Returns the sealed-claim fields to stamp onto :class:`PendingRedProposal`
    BEFORE ``put()``:

    * ``target_sha256`` — for ANY claim with a resolvable write target
      (lifecycle CAS anchor; tool-agnostic).
    * ``writer_name``/``writer_payload``/``sealed_target`` — the writer-
      registry translation for GOVERNED-CONFIG targets, resolved through
      :func:`_resolve_governed_writer` (P5: per-surface registrations; the
      transitional ``governance_write`` pass-through is retired). Non-config
      claims keep re-dispatch semantics under the same lifecycle.
    """
    sealed: Dict[str, object] = {
        "target_sha256": None,
        "writer_name": None,
        "writer_payload": None,
        "sealed_target": None,
    }
    target, body = _first_write_target(tool_name, arguments)
    if target is None:
        return sealed
    resolved = os.path.realpath(os.path.expanduser(target))
    sealed["sealed_target"] = resolved
    sealed["target_sha256"] = _hash_target_bytes(target)

    from grove.utils.fs_utils import is_scope_defining

    if Path(resolved).name == ".env" or is_scope_defining(resolved):
        writer, payload, _miss = _resolve_governed_writer(resolved, body)
        if writer is not None:
            if writer == "env_write":
                payload["rationale"] = (
                    (arguments or {}).get("rationale") or ""
                )
            sealed["writer_name"] = writer
            sealed["writer_payload"] = payload
    return sealed


def _emit_governed_write_ledger(
    *, target_file: str, rationale: str, content: str,
    prior, disposition: str, approval_id: str,
) -> None:
    """R2-census continuity (P5 item 5) — the ``governance_change`` ledger
    channel survives Pipeline-A's retirement: the executor emits at write
    time (the seal site emits its own entry at claim time)."""
    from tools.governance_tool import _record_governance_ledger

    _record_governance_ledger(
        target_file=target_file, zone="red", rationale=rationale,
        content=content, prior=prior,
        disposition=disposition, approval_id=approval_id,
    )


def _dispatch_env_write(entry: "PendingRedProposal", approval_id: str) -> str:
    """Sealed ``.env`` write — operator-only credentials land ONLY through an
    approved claim; the proposer never writes."""
    payload = entry.writer_payload or {}
    target = payload.get("target_file")
    body = payload.get("content")
    if not isinstance(target, str) or not isinstance(body, str):
        return json.dumps(
            {"success": False, "error": "sealed env payload malformed"}
        )
    p = Path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    prior = p.read_text(encoding="utf-8") if p.exists() else None
    p.write_text(body, encoding="utf-8")
    _emit_governed_write_ledger(
        target_file=str(p),
        rationale=str(payload.get("rationale") or f"approved:{approval_id}"),
        content=body, prior=prior,
        disposition="written", approval_id=approval_id,
    )
    return json.dumps({
        "success": True, "target_file": str(p),
        "message": f"Governance change written to {p.name}.",
    })


def _dispatch_routing_config_replace(
    entry: "PendingRedProposal", approval_id: str
) -> str:
    """Sealed routing-config replacement through the ONE sanctioned routing
    writer (D3 unification): RoutingConfigWriter.apply_mutation — backup →
    sandbox-validate → atomic replace → hot-reload. A body the sandbox router
    rejects raises ConfigValidationError → the claim survives for retry."""
    from hermes_constants import get_hermes_home
    from grove.config.routing_writer import (
        ConfigValidationError,
        RoutingConfigWriter,
        _ruamel,
    )

    payload = entry.writer_payload or {}
    body = payload.get("content")
    if not isinstance(body, str):
        return json.dumps(
            {"success": False, "error": "sealed routing payload malformed"}
        )
    cfg_path = Path(get_hermes_home()) / "routing.config.yaml"
    writer = RoutingConfigWriter(cfg_path)

    def _mutate(data):
        new = _ruamel().load(body)
        if not isinstance(new, dict):
            raise ConfigValidationError(
                "proposed routing config body must be a mapping"
            )
        data.clear()
        for k, v in new.items():
            data[k] = v

    prior = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    writer.apply_mutation(_mutate, label=f"governance approve {approval_id}")
    _emit_governed_write_ledger(
        target_file=str(cfg_path),
        rationale=f"approved:{approval_id}",
        content=body, prior=prior,
        disposition="written", approval_id=approval_id,
    )
    return json.dumps({
        "success": True, "target_file": str(cfg_path),
        "message": "Routing config replaced through RoutingConfigWriter.",
    })


def _dispatch_dock_goal_status(
    entry: "PendingRedProposal", approval_id: str
) -> str:
    """Sealed dock mutation through the existing sanctioned dock writer —
    goal-status changes only (the resolution seam admits nothing else)."""
    from hermes_constants import get_hermes_home
    from grove.dock.writer import update_dock_goal_status

    changes = (entry.writer_payload or {}).get("changes") or []
    applied = []
    for ch in changes:
        gid = str(ch.get("goal_id") or "")
        status = str(ch.get("status") or "")
        if not update_dock_goal_status(gid, status):
            return json.dumps({
                "success": False,
                "error": f"dock goal {gid!r} not found — nothing applied "
                         f"beyond {applied!r}",
            })
        applied.append({"goal_id": gid, "status": status})
    dock_path = Path(get_hermes_home()) / "dock" / "dock.yaml"
    _emit_governed_write_ledger(
        target_file=str(dock_path),
        rationale=f"approved:{approval_id}",
        content=json.dumps(applied, sort_keys=True), prior=None,
        disposition="written", approval_id=approval_id,
    )
    return json.dumps({
        "success": True, "target_file": str(dock_path), "changes": applied,
    })


def _dispatch_admission_write(entry: "PendingRedProposal", approval_id: str) -> str:
    """Sealed admission write — the executor is the ONLY source of approval
    ids: the provenance stamp write_admission_state REQUIRES is minted here,
    carrying the approval id, never supplied by the proposing agent."""
    from datetime import datetime, timezone

    from grove.capability_registry import write_admission_state

    payload = entry.writer_payload or {}
    path = write_admission_state(
        str(payload.get("record_id") or ""),
        intents=payload.get("intents"),
        tiers=payload.get("tiers"),
        provenance={
            "approval_id": approval_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "surface": "red_approval",
            "write_class": "capability_admission",
        },
    )
    # P7 live-verify fix — item-5 ledger continuity applies to EVERY governed
    # writer: the admission adapter was the one missing the "written" emit
    # (found when the live chain showed sealed with no written).
    _emit_governed_write_ledger(
        target_file=str(path),
        rationale=f"approved:{approval_id}",
        content=json.dumps(payload, sort_keys=True),
        prior=None,
        disposition="written", approval_id=approval_id,
    )
    return json.dumps({"success": True, "state_file": str(path)})


# capability-mutation-surface-v1 P5 (M5) — the WRITER REGISTRY: per-surface
# registrations, each dispatching a SANCTIONED writer. This registry is the
# acceptance test (R-1): nothing outside it retains an independent
# classify-or-write authority for governed config. The transitional P4
# ``governance_write`` pass-through is retired. Registration is a reviewed
# diff. (~/.grove/zones.schema.yaml deliberately has NO registration — the
# dead door is retired; routing.autonomaton.yaml / routing-profiles are
# machine-flywheel surfaces, likewise unregistered through this door.)
_GOVERNED_WRITERS = {
    "env_write": _dispatch_env_write,
    "routing_config_replace": _dispatch_routing_config_replace,
    "dock_goal_status": _dispatch_dock_goal_status,
    "write_admission_state": _dispatch_admission_write,
}


def approve_red_proposal(
    proposal_id: str, store: Optional[RedPendingStore] = None
) -> Dict[str, object]:
    """Approve-and-execute a pending RED claim — capability-mutation-surface-v1
    P4 (M4) lifecycle rewrite. Operator-only: the portal /confirm handler and
    ``Dispatcher.approve_pending_red_proposal``. NEVER model-reachable.

    LIFECYCLE (tool-agnostic — see the state machine at
    :meth:`RedPendingStore.claim`): transactional claim → CAS check →
    integrity check → dispatch → **pop ON SUCCESS ONLY**. A writer failure
    RELEASES the claim (it survives, error surfaced — the portal card offers
    retry via re-approve or withdraw via dismiss). A drifted target WITHDRAWS
    the claim loudly (``reason="target_drift"``). Replay of a consumed claim
    is ``not_found`` — honest "already resolved", never an expiry lie.

    DISPATCH is split (P4 scope): sealed governed-config claims route through
    the ``_GOVERNED_WRITERS`` registry (the executor mints the approval-id-
    bearing provenance); everything else keeps mint-and-re-dispatch through
    the tool registry under the same lifecycle.

    Returns ``{"success": bool, "reason": one of "written"/"not_found"/
    "in_flight"/"target_drift"/"integrity"/"legacy_shape"/"unknown_tool"/
    "execute_error", ...}``; ``claim_retained`` is True when the claim
    survives for retry.
    """
    import json as _json

    from grove.effect_signature import ApprovalGate, canonical_effect_signature

    st = store if store is not None else get_red_pending_store()

    entry, claim_state = st.claim(proposal_id)
    if claim_state == "absent":
        return {
            "success": False,
            "reason": "not_found",
            "error": (
                "No pending proposal with that id — it was already resolved "
                "(approved, withdrawn, or dismissed). The durable payload "
                "store survives restarts, so an absent claim means it was "
                "resolved, not lost. Nothing further was written."
            ),
        }
    if claim_state == "in_flight":
        return {
            "success": False,
            "reason": "in_flight",
            "error": (
                "This claim is already being executed by a concurrent "
                "approval — no second execution was started."
            ),
        }

    def _identity(extra: Dict[str, object]) -> Dict[str, object]:
        # operator-red-correctness-v1 Move 2 — per-effect identity for the
        # confirm card; paths only, never argument values.
        base: Dict[str, object] = {
            "proposal_id": proposal_id,
            "tool_name": entry.tool_name,
            "pattern_key": entry.pattern_key,
            "target_path": (entry.arguments or {}).get("target_file"),
            "write_targets": _extract_write_targets_safe(
                entry.tool_name, entry.arguments
            ),
        }
        base.update(extra)
        return base

    # CAS — approve-time compare-and-swap on the propose-time target hash
    # (M3). Drift → the claim is WITHDRAWN with its reason; nothing executes.
    if entry.target_sha256 is not None:
        cas_target = entry.sealed_target
        if not cas_target:
            wts = _extract_write_targets_safe(entry.tool_name, entry.arguments)
            cas_target = wts[0] if wts else None
        if cas_target is not None:
            live_hash = _hash_target_bytes(str(cas_target))
            if live_hash != entry.target_sha256:
                st.withdraw(proposal_id)
                return _identity({
                    "success": False,
                    "reason": "target_drift",
                    "error": (
                        "Withdrawn — target content drift: the file changed "
                        "after this action was proposed (CAS anchor "
                        f"{entry.target_sha256[:12]}… no longer matches). "
                        "Nothing was written; review the target and "
                        "re-propose."
                    ),
                })

    # Integrity: the claimed args must still recompute to the stored
    # effect_signature (realpath-canonical → symlink swap caught). A mismatch
    # withdraws — the stored effect is no longer the approved effect.
    live_sig = canonical_effect_signature(entry.tool_name, entry.arguments)
    if live_sig != entry.effect_signature:
        st.withdraw(proposal_id)
        return {
            "success": False,
            "reason": "integrity",
            "error": (
                "Approval aborted — stored action integrity check failed (the "
                "effect changed since it was proposed). Nothing was executed."
            ),
        }

    # P4 item 5 — legacy-shape refusal (no migration shim; R1 counted 0 live
    # rows): a governance claim minted before sealing existed carries no
    # writer_name. Refuse LOUD and withdraw; a silent raw re-dispatch of a
    # governed-config write is exactly the defect this sprint closes.
    if entry.tool_name == "propose_governance_change" and not entry.writer_name:
        st.withdraw(proposal_id)
        return _identity({
            "success": False,
            "reason": "legacy_shape",
            "error": (
                "Withdrawn — this governance claim predates sealed claims "
                "(no writer translation stored) and cannot be executed "
                "safely. Re-propose the change."
            ),
        })

    # ── DISPATCH ──
    if entry.writer_name is not None:
        # Sealed governed-config claim → the writer registry. The approval id
        # (the claim id) is minted into provenance by the adapter — the
        # executor is the only source of approval ids.
        adapter = _GOVERNED_WRITERS.get(entry.writer_name)
        if adapter is None:
            st.release(
                proposal_id,
                error=f"no registered governed writer {entry.writer_name!r}",
            )
            return _identity({
                "success": False,
                "reason": "execute_error",
                "claim_retained": True,
                "error": (
                    f"No registered governed writer {entry.writer_name!r} — "
                    "the claim is retained; retry after the writer registry "
                    "is corrected, or dismiss to withdraw."
                ),
            })
        try:
            result = adapter(entry, proposal_id)
        except Exception as exc:  # noqa: BLE001 — loud, claim survives
            st.release(proposal_id, error=repr(exc))
            return _identity({
                "success": False,
                "reason": "execute_error",
                "claim_retained": True,
                "error": (
                    f"Writer {entry.writer_name!r} raised {exc!r} — the claim "
                    "is retained; retry from the pending card, or dismiss to "
                    "withdraw."
                ),
            })
    else:
        # Unsealed claim → mint-and-re-dispatch (unchanged seam; the gate is
        # consumed inside registry.dispatch on signature match).
        from tools.registry import ToolRegistry, register_builtin_tools

        registry = ToolRegistry()
        register_builtin_tools(registry)
        if registry.get_entry(entry.tool_name) is None:
            st.release(
                proposal_id,
                error=f"tool {entry.tool_name!r} not on approval registry",
            )
            return _identity({
                "success": False,
                "reason": "unknown_tool",
                "claim_retained": True,
                "error": (
                    f"tool {entry.tool_name!r} is not registered on the "
                    "approval registry; cannot re-dispatch (Phase B: registry "
                    "completeness). The claim is retained."
                ),
            })
        gate = ApprovalGate()
        gate.activate()
        registry._approval_gate = gate
        gate.mint(entry.effect_signature)
        try:
            result = registry.dispatch(entry.tool_name, entry.arguments)
        except Exception as exc:  # noqa: BLE001 — loud, claim survives
            st.release(proposal_id, error=repr(exc))
            return _identity({
                "success": False,
                "reason": "execute_error",
                "claim_retained": True,
                "error": (
                    f"Dispatch raised {exc!r} — the claim is retained; retry "
                    "from the pending card, or dismiss to withdraw."
                ),
            })
        finally:
            gate.flush()  # single-use mint per attempt; a retry re-mints

    # A handler error / GovernanceError surfaces as {"error": ...} or
    # success:false in the JSON result. LOUD execute failure — the claim is
    # RELEASED (survives with the surfaced error), never silently consumed.
    ok = True
    try:
        parsed = _json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, dict) and (
            parsed.get("error") or parsed.get("success") is False
        ):
            ok = False
    except Exception:  # noqa: BLE001 — non-JSON handler output treated as success
        pass

    if not ok:
        _detail = ""
        try:
            _p = _json.loads(result) if isinstance(result, str) else result
            _detail = str((_p or {}).get("error") or "")[:500]
        except Exception:  # noqa: BLE001
            _detail = str(result)[:500]
        st.release(proposal_id, error=_detail or "writer reported failure")
        return _identity({
            "success": False,
            "reason": "execute_error",
            "claim_retained": True,
            "result": result,
            "error": _detail or "writer reported failure",
        })

    # SUCCESS — pop ON SUCCESS ONLY (M4): the claim is consumed exactly once,
    # after the write landed.
    st.pop(proposal_id)
    return _identity({
        "success": True,
        "reason": "written",
        "result": result,
    })


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
    # capability-mutation-surface-v1 P4 (M4) — crash recovery FIRST: release
    # stale IN_FLIGHT claims (a claim surviving a restart is stale by
    # definition on a single-instance gateway). NOTE the lifecycle rule: an
    # entry whose execution FAILED is NOT an orphan — its payload row is
    # intact (release, not pop), so ``st.has()`` below stays True and the
    # sweep never touches it. Only a queue row whose payload is GONE
    # (consumed on success, withdrawn on drift, or dismissed) is orphaned.
    stale = st.release_stale_claims()
    if stale:
        logger.info(
            "[red-reaper] released %d stale in-flight claim(s) at startup",
            stale,
        )
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
