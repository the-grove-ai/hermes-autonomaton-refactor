"""propose_governance_change — the sole Stage-04 door for governance-config writes.

GRV-010 C1b Phase 3 (Option A, sole-path). Post secrets-only-wall-v1, generic
file tools can write non-secret ``~/.grove`` paths through Yellow-zone approval.
This tool is the ONLY authorized writer of governance configuration.

It is a *target-classified intent*: the Dispatcher classifies a
``propose_governance_change`` call by its ``target_file`` at Stage 04 —
``.env`` → RED (operator-only, GRV-001 §V); the YAML configs
(``zones.schema.yaml`` / ``routing.config.yaml`` / ``routing.autonomaton.yaml`` /
routing profiles) and the Dock tree (``~/.grove/dock/``) → YELLOW
(store-and-resume). The handler runs ONLY after an
approved disposition (the Green-path executor runs it post-Stage-04), at which
point it writes the change AND appends a ``governance_change`` Kaizen-ledger
entry (rationale + diff hashes + disposition).

Realization note (ANDON-LOOP avoided): the spec's "GovernanceChangeIntent" is
realized as this target-classified ``ToolIntent`` flowing the EXISTING dispatch
loop — not a new yield type — so no base-AIAgent-loop refactor is required. The
zone branch lives in ``grove.dispatcher._classify_one_intent``; the ledger entry
is keyed by ``GROVE_SESSION_ID`` (set by the Dispatcher per turn).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The YAML governance configs writable through this door (operator copies under
# ~/.grove). Adding a config here is the declarative way to bring it under the
# governance-write gate.
GOVERNANCE_YAML_NAMES = frozenset({
    "zones.schema.yaml",
    "routing.config.yaml",
    "routing.autonomaton.yaml",
})

# The Dock (``~/.grove/dock/``) is the operator's strategic command center —
# governed config the agent legitimately edits (goal manifests + context).
# Admit ONLY the suffixes the Dock loader actually reads (grove/dock.py: the
# ``dock.yaml`` manifest + goal ``context_sources``, declared as
# ``goals/*.{yaml,md}``). Derived from the loader's read set, not invented —
# widening beyond it would over-admit (ANDON-OVER-ADMIT).
DOCK_LOADABLE_SUFFIXES = frozenset({".yaml", ".md"})


def classify_governance_target(target_file: object) -> Optional[str]:
    """Return ``"red"`` for ``.env``, ``"yellow"`` for a recognized governance
    YAML config (or routing profile) under ``~/.grove``, or ``None`` if
    *target_file* is not a recognized governance-config path.

    The Dispatcher consults this to set the Stage-04 zone; the handler consults
    it to refuse non-governance targets (this tool is not a write-anywhere
    bypass of the file-tool lock).
    """
    if not target_file or not isinstance(target_file, str):
        return None
    from hermes_constants import get_hermes_home

    try:
        grove_home = Path(os.path.realpath(get_hermes_home()))
        target = Path(os.path.realpath(os.path.expanduser(target_file)))
    except (OSError, ValueError):
        return None

    # .env is sovereign regardless of location (operator-only secrets).
    if target.name == ".env":
        return "red"

    inside_grove = (target == grove_home) or (grove_home in target.parents)
    if not inside_grove:
        return None
    if target.name in GOVERNANCE_YAML_NAMES:
        return "yellow"
    if "routing-profiles" in target.parts and target.suffix in (".yaml", ".yml"):
        return "yellow"
    # GRV-010 GOV-WRITE — admit the Dock tree (``~/.grove/dock/``) at YELLOW.
    # The Dock is operator-governed strategic config the agent edits through
    # this door, which target-classifies and logs the change. Containment
    # is anchored to the RESOLVED Dock root via ``is_relative_to`` — NOT
    # ``str.startswith`` or parts-membership, either of which a sibling
    # ``dock-evil/`` or a stray ``dock`` path component would defeat;
    # ``is_relative_to`` immunizes both the substring collision and the ``..``
    # escape (``target`` is already realpath-resolved above). Evaluated AFTER
    # the ``.env``→RED and YAML-name / routing-profile→YELLOW checks, and below
    # the ``inside_grove`` gate, so a stricter zone always wins the waterfall (a
    # ``.env`` or governance YAML that ever sits under dock/ keeps its zone) and
    # a Dock symlinked outside ``~/.grove`` is rejected before reaching here.
    dock_root = Path(os.path.realpath(grove_home / "dock"))
    if target.is_relative_to(dock_root) and target.suffix in DOCK_LOADABLE_SUFFIXES:
        return "yellow"
    return None


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _record_governance_ledger(
    *, target_file: str, zone: str, rationale: str,
    content: str, prior: Optional[str],
) -> None:
    """Append a ``governance_change`` ledger entry. Hashes (not bodies) are
    stored so a ``.env`` secret never lands in the audit trail. Non-fatal."""
    try:
        from grove.kaizen_ledger import KaizenLedger

        session_id = os.environ.get("GROVE_SESSION_ID") or "governance"
        ledger = KaizenLedger(session_id)
        ledger.record(
            "governance_change",
            target_file=target_file,
            zone=zone,
            rationale=rationale,
            # The handler runs only after an approved Stage-04 disposition; the
            # precise once/session/always is carried by the Dispatcher's paired
            # andon_disposition entry.
            disposition="approved",
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
    """Write a governance-config change. Sanctioned post-Stage-04 effect.

    By the time this handler runs, the Dispatcher has classified the intent
    (``.env`` → RED, YAML → YELLOW) and the operator has approved — the write is
    the approved effect, not a request. ``content`` (alias ``diff_or_content``)
    is the FULL new file content (write-replace). Refuses any target that is not
    a recognized governance config.

    ``approved_content_sha256`` (propose-approve-deadlock-v1 Phase 1a, Step 6) —
    the TOCTOU integrity anchor. When set (the RED store-and-approve path passes
    the hash captured at propose time), the payload about to be written is
    re-hashed and MUST match, or the write is refused fail-loud. This closes the
    propose-time→execute-time window the durable store introduces.
    """
    body = content if content is not None else diff_or_content
    zone = classify_governance_target(target_file)
    if zone is None:
        return _err(
            "propose_governance_change writes only recognized governance config "
            "(.env → operator-only; zones.schema.yaml / routing.config.yaml / "
            "routing.autonomaton.yaml / routing profiles; or a .yaml/.md file in "
            "the Dock tree ~/.grove/dock/). Unrecognized target: "
            f"{target_file!r}."
        )
    if body is None:
        return _err("content is required — the full new file content (write-replace).")
    if not rationale or not str(rationale).strip():
        return _err("rationale is required — the governance change is logged with its reason.")

    # TOCTOU integrity gate (Phase 1a Step 6): verify the payload matches the one
    # approved at propose time, BEFORE it reaches the write. Fail-loud on drift.
    if approved_content_sha256 is not None:
        live = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if live != approved_content_sha256:
            return _err(
                "governance write refused — approved payload integrity check "
                f"failed (expected {approved_content_sha256[:12]}…, got "
                f"{live[:12]}…). The content changed after approval; not written."
            )

    target = Path(os.path.realpath(os.path.expanduser(target_file)))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        prior = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        return _err(f"governance write failed: {exc!r}")

    _record_governance_ledger(
        target_file=str(target), zone=zone,
        rationale=str(rationale), content=body, prior=prior,
    )
    return json.dumps(
        {
            "success": True,
            "target_file": str(target),
            "zone": zone,
            "message": (
                f"Governance change written to {target.name} (zone={zone}) and "
                f"recorded in the Kaizen ledger."
            ),
        },
        ensure_ascii=False,
    )


GOVERNANCE_CHANGE_SCHEMA = {
    "name": "propose_governance_change",
    "description": (
        "Propose a change to a GOVERNANCE config file — the ONLY way to modify "
        "~/.grove governance configuration. Targets: .env (operator-only — "
        "sovereign/RED), or the YAML "
        "configs zones.schema.yaml / routing.config.yaml / "
        "routing.autonomaton.yaml / routing profiles, or a .yaml/.md file in "
        "the Dock tree ~/.grove/dock/ (e.g. dock.yaml or goals/*.md) "
        "(operator-approved — YELLOW). Use THIS tool — not write_file/patch — "
        "to edit the Dock. The change is classified by target at Stage 04 and applied "
        "only after the operator approves; the write and its rationale are "
        "logged. Provide the FULL new file content. For fleet output directories "
        "(~/.grove/scout/, ~/.grove/drafter/, etc.) use write_file directly — "
        "those are not governance config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": (
                    "Path to the governance config to change (e.g. "
                    "~/.grove/zones.schema.yaml)."
                ),
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
