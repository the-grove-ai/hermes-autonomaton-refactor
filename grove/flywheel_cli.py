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
from typing import Any, Dict, Optional, Tuple

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


def _routing_adjustment_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a routing_adjustment proposal into a routing-config diff.

    The diff is a partial routing config shape suitable for
    ``apply_diff_to_machine_config`` — the set-union semantics in the
    merger handle the intent-list combination with any pre-existing
    machine additions.
    """
    rule = proposal.payload.get("rule")
    add_intents = list(proposal.payload.get("add_intents") or [])
    if rule not in ("downward", "upward") or not add_intents:
        raise ValueError(
            f"malformed routing_adjustment payload: {proposal.payload!r}"
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


def _proposal_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a proposal payload into a routing-config diff.

    Sprint 32 — dispatch by ``proposal.type``. The Sprint 47 v0.1
    ``routing_update`` literal is honored as an alias for
    ``routing_adjustment`` so legacy queue entries continue to render
    in ``cli_show``.
    """
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        PROPOSAL_TYPE_ZONE_PROMOTION,
    )
    if proposal.type in (PROPOSAL_TYPE_ROUTING_ADJUSTMENT, "routing_update"):
        return _routing_adjustment_to_diff(proposal)
    if proposal.type == PROPOSAL_TYPE_ZONE_PROMOTION:
        # Zone promotions don't translate to a routing-config diff —
        # they write directly to zones.schema.yaml via save_zone_rule.
        # The "diff" displayed to the operator is the YAML-shaped
        # rule that would be appended.
        return {
            "tool_zones": {
                proposal.payload.get("tool", "?"): {
                    "rules": [
                        {
                            "match_pattern": proposal.payload.get("pattern", ""),
                            "zone": proposal.payload.get("zone", "?"),
                            "reason": proposal.payload.get("reason", ""),
                        },
                    ],
                },
            },
        }
    raise ValueError(
        f"unsupported proposal type {proposal.type!r}; recognised: "
        f"routing_adjustment, zone_promotion"
    )


def _format_summary(proposal: RoutingProposal) -> str:
    """One-line operator-facing summary of a proposal.

    Sprint 32 — renders both routing_adjustment and zone_promotion
    shapes generically. Unknown types fall through to a payload
    preview so the operator still sees something actionable.
    """
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        PROPOSAL_TYPE_ZONE_PROMOTION,
    )
    short_id = proposal.proposal_id.split(":")[-1][:12]
    n_evidence = len(proposal.evidence)
    if proposal.type in (PROPOSAL_TYPE_ROUTING_ADJUSTMENT, "routing_update"):
        rule = proposal.payload.get("rule", "?")
        intents = ", ".join(proposal.payload.get("add_intents", []))
        body = f"add {intents} to routing.{rule}"
    elif proposal.type == PROPOSAL_TYPE_ZONE_PROMOTION:
        tool = proposal.payload.get("tool", "?")
        pattern = proposal.payload.get("pattern", "?")
        body = f"greenlight {tool} pattern={pattern!r}"
    else:
        body = f"payload={proposal.payload!r}"
    return (
        f"{short_id}  {proposal.type:<22}  "
        f"{body}  "
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


def _approve_routing_adjustment(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Apply a routing_adjustment proposal to routing.autonomaton.yaml.

    Returns the (target_path, applied_diff) pair so the caller can
    print the result. Per GRV-008 § III this NEVER touches
    ``routing.config.yaml``.
    """
    diff = _routing_adjustment_to_diff(proposal)
    target = machine_path or _machine_config_path()
    apply_diff_to_machine_config(diff, target)
    return target, diff


def _approve_zone_promotion(
    proposal: RoutingProposal,
) -> Tuple[str, Dict[str, Any]]:
    """Apply a zone_promotion proposal to zones.schema.yaml.

    Sprint 32 Phase 2c — delegates to
    :func:`grove.zone_rules.save_zone_rule` which already exists from
    Sprint 22 and writes through ruamel.yaml (preserving comments)
    with a synchronous ``zones.reload()`` at the tail. Returns the
    (rendered-rule-summary, applied-rule-dict) pair so the caller can
    print the result.
    """
    from grove.zone_rules import save_zone_rule

    tool = proposal.payload.get("tool")
    pattern = proposal.payload.get("pattern")
    zone = proposal.payload.get("zone", "green")
    reason = proposal.payload.get("reason", "")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(
            f"zone_promotion payload missing 'tool': {proposal.payload!r}"
        )
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(
            f"zone_promotion payload missing 'pattern': {proposal.payload!r}"
        )
    save_zone_rule(
        tool_id=tool, pattern=pattern, zone=zone, reason=reason,
    )
    applied = {
        "match_pattern": pattern,
        "zone": zone,
        "reason": reason,
    }
    return f"tool_zones.{tool}.rules", applied


def cli_approve(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Apply the proposal; remove from queue.

    Sprint 32 — dispatch by ``proposal.type``:

    * ``routing_adjustment`` (and the Sprint 47 legacy
      ``routing_update``) → :func:`_approve_routing_adjustment`,
      which writes to ``routing.autonomaton.yaml``.
    * ``zone_promotion`` → :func:`_approve_zone_promotion`, which
      writes to ``zones.schema.yaml`` via
      :func:`grove.zone_rules.save_zone_rule`.

    Per GRV-008 § III, neither path EVER opens
    ``routing.config.yaml`` for writing — operator-authored
    configuration is inviolate.
    """
    from grove.eval.proposal_queue import (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
        PROPOSAL_TYPE_ZONE_PROMOTION,
    )

    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    if proposal.type in (PROPOSAL_TYPE_ROUTING_ADJUSTMENT, "routing_update"):
        target, applied = _approve_routing_adjustment(
            proposal, machine_path=machine_path,
        )
        applied_label = f"Applied to: {target}"
    elif proposal.type == PROPOSAL_TYPE_ZONE_PROMOTION:
        target_label, applied = _approve_zone_promotion(proposal)
        applied_label = f"Applied to: {target_label}"
    else:
        print(
            f"Cannot approve proposal type {proposal.type!r}. "
            f"Supported: routing_adjustment, zone_promotion.",
            file=sys.stderr,
        )
        return 1

    removed = remove(
        proposal.proposal_id,
        path=queue_path or default_queue_path(),
    )
    if not removed:
        logger.warning(
            "[flywheel] proposal %s was applied but had already been "
            "removed from the queue", proposal.proposal_id,
        )

    print(f"Approved {proposal.proposal_id}")
    print(applied_label)
    print()
    print("Applied:")
    print(yaml.safe_dump(applied, sort_keys=False, default_flow_style=False))
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
