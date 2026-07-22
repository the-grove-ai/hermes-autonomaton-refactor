"""propose_governance_change — the THIN PROPOSER for governed-config changes.

capability-mutation-surface-v1 P5 (M5): Pipeline-A's write authority is
RETIRED. This module no longer classifies targets and no longer writes —
governed-config changes are SEALED against the governed-writer registry
(``grove.red_pending_store._GOVERNED_WRITERS``) at Stage-04 store-pend time
and executed only by the approved-claim executor. The Stage-04 zone comes
from the declarative scope wall (the scope-membership file consumed by
``grove.utils.fs_utils.is_scope_defining``) plus the universal ``.env``→RED
rule in ``grove.dispatcher._classify_one_intent``.

What survives here: argument validation, the viability refusal (M6 — repo
definition surfaces point at the git commit + deploy SOP; governed surfaces
with no registered writer name the registry miss), and the
``governance_change`` Kaizen-ledger channel (``_record_governance_ledger``),
which the seal site and the executor adapters both emit through — keyed by
``GROVE_SESSION_ID`` (set by the Dispatcher per turn).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _record_governance_ledger(
    *, target_file: str, zone: str, rationale: str,
    content: str, prior: Optional[str],
    disposition: str = "approved",
    approval_id: Optional[str] = None,
) -> None:
    """Append a ``governance_change`` ledger entry. Hashes (not bodies) are
    stored so a ``.env`` secret never lands in the audit trail. Non-fatal.

    capability-mutation-surface-v1 P5 (item 5) — the R2 census channel
    survives Pipeline-A's retirement: emission now happens at CLAIM-SEAL
    (``disposition="sealed"``, from the store-pending seam) and at EXECUTOR
    SUCCESS (``disposition="written"``, from the governed-writer adapters),
    each carrying the claim's ``approval_id``."""
    try:
        from grove.kaizen_ledger import KaizenLedger

        session_id = os.environ.get("GROVE_SESSION_ID") or "governance"
        ledger = KaizenLedger(session_id)
        ledger.record(
            "governance_change",
            target_file=target_file,
            zone=zone,
            rationale=rationale,
            disposition=disposition,
            approval_id=approval_id,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            prior_sha256=(
                hashlib.sha256(prior.encode("utf-8")).hexdigest()
                if prior is not None else None
            ),
            bytes=len(content.encode("utf-8")),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "governance_change ledger write failed (non-fatal): %r", exc,
        )


def propose_governance_change(
    target_file: str,
    content: Optional[str] = None,
    rationale: Optional[str] = None,
    diff_or_content: Optional[str] = None,
    task_id: str = "default",
    approved_content_sha256: Optional[str] = None,
) -> str:
    """THIN PROPOSER — capability-mutation-surface-v1 P5 (M5). NEVER writes.

    The write authority died with Pipeline-A: governed-config changes execute
    ONLY through the approved-claim executor's writer registry
    (``grove.red_pending_store._GOVERNED_WRITERS``), reached via the Stage-04
    RED store-pend path — which SEALS the claim (writer_name / writer_payload
    / target / expected_hash) before anything is queued and BYPASSES this
    handler entirely. What remains here:

    * argument validation (target / content / rationale), fail-loud;
    * the viability refusal (M6): a repo-definition target names the git
      commit + deploy SOP; a governed surface with no registered writer names
      the registry miss;
    * a proposer-not-writer refusal for any direct execution — a viable
      governance write that reaches THIS handler was not routed through
      store-pending, and nothing is written.

    ``task_id`` / ``approved_content_sha256`` are retained for call-shape
    compatibility; the TOCTOU anchor now lives on the sealed claim
    (target_sha256 CAS + byte-parity by construction).
    """
    _ = (task_id, approved_content_sha256)  # call-shape compat; unused
    body = content if content is not None else diff_or_content
    if not target_file or not isinstance(target_file, str) or not target_file.strip():
        return _err("target_file is required — the governance config to change.")
    if body is None:
        return _err("content is required — the full new file content (write-replace).")
    if not rationale or not str(rationale).strip():
        return _err("rationale is required — the governance change is logged with its reason.")

    from grove.red_pending_store import is_viable_red_target

    viable, reason = is_viable_red_target(
        "propose_governance_change",
        {"target_file": target_file, "content": body, "rationale": rationale},
    )
    if not viable:
        return _err(reason)

    # Viable target, direct execution: the proposer is not a writer. The
    # governed write lands only through Stage-04 store-pending → operator
    # approval → the executor's registered writer.
    return _err(
        "propose_governance_change is a thin proposer of governance config "
        "changes — it never writes. The change executes only through the "
        "operator-approved claim executor (writer registry), reached via the "
        "Stage-04 store-pending path. Nothing was written by this call; if "
        "you are seeing this after an approval, the claim shape was legacy — "
        "re-propose."
    )


GOVERNANCE_CHANGE_SCHEMA = {
    "name": "propose_governance_change",
    "description": (
        "Propose a change to GOVERNED configuration — the sanctioned door for "
        "~/.grove governance surfaces (.env is operator-only/RED; governed "
        "config is RED and operator-approved). This tool PROPOSES: the change "
        "is sealed against a registered governed writer at Stage 04, queued "
        "for operator approval, and executed by the approval executor — never "
        "written directly. A surface with no registered writer, or a repo "
        "definition file (change those via git commit + deploy), is refused "
        "loudly. Provide the FULL new file content and a rationale; the seal "
        "and the write are both logged. For fleet output directories "
        "(~/.grove/scout/, ~/.grove/drafter/, etc.) use write_file directly — "
        "those are not governed config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "Path to the governed config file to change.",
            },
            "content": {
                "type": "string",
                "description": "The full new file content (write-replace).",
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Why this change is being made — logged in the audit ledger. "
                    "Required."
                ),
            },
        },
        "required": ["target_file", "content", "rationale"],
    },
}


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools. Registered under
    the ``file`` toolset — the sanctioned governance-write counterpart to the
    now-locked write_file/patch."""
    reg.register(
        name="propose_governance_change",
        toolset="file",
        schema=GOVERNANCE_CHANGE_SCHEMA,
        handler=lambda args, **kw: propose_governance_change(
            target_file=args.get("target_file", ""),
            content=args.get("content"),
            rationale=args.get("rationale"),
            diff_or_content=args.get("diff_or_content"),
            task_id=kw.get("task_id", "default"),
        ),
        emoji="🏛️",
    )
