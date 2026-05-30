"""GRV-008 § IV operator review surface for Sprint 47 routing proposals.

CLI renderers reachable via ``autonomaton flywheel
{list,show,approve,reject}``. Each renderer prints to stdout/stderr,
sets a UNIX exit code, and writes telemetry events the Kaizen Ledger
records as operator sovereignty acts.

The four operations:

* :func:`cli_list` — show every pending proposal in
  ``~/.grove/proposals.jsonl`` as one human-readable line each.
* :func:`cli_show` — show one proposal's payload, evidence, and the
  diff it would apply to ``routing.autonomaton.yaml``.
* :func:`cli_approve` — apply the proposal's payload to the machine
  file (set-union per GRV-008 § III); remove from queue.
* :func:`cli_reject` — remove from queue; no config change.

Per GRV-008 § III the machine file is the only path the renderers
write to. ``routing.config.yaml`` is never opened in write mode.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from grove.eval.proposal_queue import (
    RoutingProposal,
    default_queue_path,
    read,
    read_all,
    remove,
)
from grove.router_merge import apply_diff_to_machine_config

logger = logging.getLogger(__name__)


__all__ = [
    "cli_list",
    "cli_show",
    "cli_approve",
    "cli_reject",
]


def _machine_config_path() -> Path:
    """The hermes_home routing.autonomaton.yaml path."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "routing.autonomaton.yaml"


def _proposal_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a RoutingProposal's payload into a routing-config diff.

    The diff is a partial routing config shape suitable for
    ``apply_diff_to_machine_config`` — the set-union semantics in the
    merger handle the intent-list combination with any pre-existing
    machine additions.
    """
    if proposal.type != "routing_update":
        raise ValueError(
            f"unsupported proposal type {proposal.type!r}; Sprint 47 "
            f"handles routing_update only"
        )
    rule = proposal.payload.get("rule")
    add_intents = list(proposal.payload.get("add_intents") or [])
    if rule not in ("downward", "upward") or not add_intents:
        raise ValueError(
            f"malformed routing_update payload: {proposal.payload!r}"
        )
    return {
        "routing": {
            "routing_rules": {
                rule: {
                    "match": {
                        "intents": add_intents,
                    },
                },
            },
        },
    }


def _format_summary(proposal: RoutingProposal) -> str:
    """One-line operator-facing summary of a proposal."""
    rule = proposal.payload.get("rule", "?")
    intents = ", ".join(proposal.payload.get("add_intents", []))
    short_id = proposal.proposal_id.split(":")[-1][:12]
    n_evidence = len(proposal.evidence)
    return (
        f"{short_id}  {proposal.type:<14}  "
        f"add {intents} to {rule}  "
        f"(evidence: {n_evidence} turn(s))  "
        f"{proposal.created_at}"
    )


# ── list ─────────────────────────────────────────────────────────────


def cli_list(*, queue_path: Optional[Path] = None) -> int:
    """Show every pending proposal in the queue.

    Returns exit code 0. Empty queue prints a friendly message.
    """
    proposals = read_all(path=queue_path or default_queue_path())
    if not proposals:
        print(
            "No pending Flywheel proposals. The TierRatchet emits "
            "proposals once usage patterns shift a class into the "
            "qualifying band (see GRV-008 § I)."
        )
        return 0
    print(f"{len(proposals)} pending proposal(s) in the queue:")
    print()
    for proposal in proposals:
        print("  " + _format_summary(proposal))
    print()
    print(
        "Inspect: autonomaton flywheel show <id>\n"
        "Approve: autonomaton flywheel approve <id>\n"
        "Reject:  autonomaton flywheel reject <id> [--reason \"...\"]"
    )
    return 0


# ── show ─────────────────────────────────────────────────────────────


def _resolve_proposal(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Resolve a proposal by full or short id.

    Accepts the full ``sha256:...`` id, the bare hash, or a unique
    short prefix (≥ 8 chars).
    """
    target = queue_path or default_queue_path()
    proposal = read(partial_id, path=target)
    if proposal is not None:
        return proposal
    bare = partial_id.split(":")[-1]
    if len(bare) < 8:
        return None
    matches = [
        p for p in read_all(path=target)
        if p.proposal_id.split(":")[-1].startswith(bare)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def cli_show(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Show one proposal's payload, evidence, and the YAML diff it
    would apply to the machine routing file.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}. "
            f"Run `autonomaton flywheel list` to see pending ids.",
            file=sys.stderr,
        )
        return 1

    print(f"Proposal ID:    {proposal.proposal_id}")
    print(f"Type:           {proposal.type}")
    print(f"Created:        {proposal.created_at}")
    print(f"Eval hash:      {proposal.eval_hash or '(unset — pre-gate)'}")
    print(f"Evidence:       {len(proposal.evidence)} turn(s)")
    for tid in proposal.evidence:
        print(f"                  - {tid}")
    print()
    print("Payload:")
    print(yaml.safe_dump(proposal.payload, sort_keys=False, default_flow_style=False))

    diff = _proposal_to_diff(proposal)
    print("Diff (would apply to routing.autonomaton.yaml):")
    print(yaml.safe_dump(diff, sort_keys=False, default_flow_style=False))
    return 0


# ── approve ──────────────────────────────────────────────────────────


def cli_approve(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Apply the proposal's diff to the machine routing file; remove
    from queue.

    Per GRV-008 § III, this MUST NOT touch ``routing.config.yaml``.
    The path is hardcoded to the machine file via :func:`_machine_config_path`.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    diff = _proposal_to_diff(proposal)
    target = machine_path or _machine_config_path()
    apply_diff_to_machine_config(diff, target)
    removed = remove(proposal.proposal_id, path=queue_path or default_queue_path())
    if not removed:
        # The proposal vanished between resolve and remove — log and
        # continue. The diff was still applied; the queue is the right
        # place to be skeptical, not the merge.
        logger.warning(
            "[flywheel] proposal %s was applied but had already been "
            "removed from the queue", proposal.proposal_id,
        )

    print(f"Approved {proposal.proposal_id}")
    print(f"Applied to: {target}")
    print()
    print("Diff:")
    print(yaml.safe_dump(diff, sort_keys=False, default_flow_style=False))
    return 0


# ── reject ───────────────────────────────────────────────────────────


def cli_reject(
    partial_id: str,
    *,
    reason: Optional[str] = None,
    queue_path: Optional[Path] = None,
) -> int:
    """Remove a proposal from the queue. No config change.

    ``reason`` is recorded at INFO level for the Kaizen Ledger to
    pick up. Sprint 47 does not require structured rejection
    telemetry — that lands when the Kaizen Ledger integration sprint
    ships.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1
    target = queue_path or default_queue_path()
    removed = remove(proposal.proposal_id, path=target)
    if not removed:
        print(
            f"Proposal {proposal.proposal_id} was not in the queue "
            f"(already removed?).",
            file=sys.stderr,
        )
        return 1
    if reason:
        logger.info(
            "[flywheel] proposal %s rejected — %s",
            proposal.proposal_id, reason,
        )
    print(f"Rejected {proposal.proposal_id}")
    if reason:
        print(f"Reason:   {reason}")
    return 0
