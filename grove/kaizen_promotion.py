"""Sprint 32 Phase 2 — Kaizen zone-promotion proposal generator.

When the operator answers "Always allow this" at the Kaizen Sovereign
Prompt, the Dispatcher builds a :class:`grove.eval.proposal_queue.
RoutingProposal` with ``type=PROPOSAL_TYPE_ZONE_PROMOTION`` and
appends it to ``~/.grove/proposals.jsonl``. The operator approves the
proposal later via ``autonomaton flywheel approve`` and the green
rule is written to ``zones.schema.yaml`` through
:func:`grove.zone_rules.save_zone_rule`.

Pattern normalization (v0.1 per GATE-A):

* When the halted command is a terminal invocation of a script under
  ``~/.grove/skills/<name>/``, the regex normalizes to
  ``.*\\.grove/skills/<name>/.*`` — operator's home prefix elided so
  the same rule fires for any operator on any host.
* When the halted command is a non-skill terminal command, the regex
  escapes the literal command and anchors it (``^<escape(cmd)>$``).
  This is conservative — broader patterns require manual editing of
  ``zones.schema.yaml``. Documented as a v0.1 limitation per GATE-A A7.
* When the halted action is a non-terminal tool, the regex is the
  tool name itself (the bare-string zone form), encoded into the
  green rule for normalization symmetry.

Sprint 32.2 — shell-variable expansion (``$HOME``, ``${HOME}``,
leading ``~/``) is now handled before the substring match.  The
A7 limitation that punted this to v0.2 is closed for the three
common forms; arbitrary shell evaluation remains out of scope.
The normalization itself lives in
``grove.action_facts.normalize_command`` (the shared action-fact layer)
so the template matcher and this generator see the same expanded path.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Tuple

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    _now_iso,
    compute_proposal_id,
)
from grove.action_facts import (
    _extract_skill_name,
    normalize_command,
)

logger = logging.getLogger(__name__)


__all__ = [
    "build_zone_promotion_proposal",
    "normalize_pattern",
    "PromotionPayloadShape",
]


# Type alias for clarity at call sites — the dict shape carried in
# ``proposal.payload`` for zone_promotion proposals.
PromotionPayloadShape = Dict[str, Any]


def normalize_pattern(tool_name: str, command_string: str) -> str:
    """Generate the regex pattern for a zone-promotion green rule.

    v0.1 strategy per GATE-A:

    * Terminal + ``.grove/skills/<name>/`` path → ``.*\\.grove/skills/
      <name>/.*``. The operator's home prefix is elided so the rule
      fires for any operator on any host.
    * Terminal + non-skill command → ``^<re.escape(command)>$``. Safe
      and conservative; manual edits broaden later.
    * Non-terminal tool → the tool name as a literal (the bare-string
      zone form encoded into a green rule for normalization symmetry).

    Sprint 32.2 — ``$HOME`` / ``${HOME}`` / leading ``~/`` are
    expanded before the substring match via the shared
    :func:`grove.sovereign_prompt_handlers.normalize_command`
    helper, closing the GATE-A A7 v0.2 limitation for the three
    common forms.  Arbitrary shell evaluation (other variables,
    command substitution, nested expansions) remains out of scope.
    """
    if tool_name == "terminal":
        normalized = normalize_command(command_string or "")
        skill_name = _extract_skill_name(normalized)
        if skill_name != "unknown":
            return r".*\.grove/skills/" + re.escape(skill_name) + r"/.*"
        if normalized:
            return "^" + re.escape(normalized) + "$"
        return "^.*$"  # degenerate; will fail safety check, surfacing the issue
    return re.escape(tool_name)


def _kaizen_reason(tool_name: str, command_string: str) -> str:
    """The operator-facing rationale string written to the green rule.

    Matches the brief's example: "Operator approved: allow
    SKILL_NAME to execute via terminal" for skill paths; generic
    fallback otherwise.

    Sprint 32.2 — same normalization as :func:`normalize_pattern`
    so a ``${HOME}/.grove/skills/...`` halt produces the skill-
    specific rationale string instead of the generic terminal one.
    """
    if tool_name == "terminal":
        normalized = normalize_command(command_string or "")
        skill_name = _extract_skill_name(normalized)
        if skill_name != "unknown":
            return (
                f"Operator approved: allow {skill_name} to execute via "
                f"terminal."
            )
        return "Operator approved: allow this terminal command pattern."
    return f"Operator approved: allow {tool_name} actions."


def build_zone_promotion_proposal(
    *,
    tool_name: str,
    command_string: str,
    evidence_turn_id: str,
    eval_hash: str = "",
) -> Tuple[RoutingProposal, PromotionPayloadShape]:
    """Build a ZonePromotionProposal from a halted action.

    Returns a ``(proposal, payload)`` tuple. The payload is also
    embedded in ``proposal.payload``; surfaced separately so the
    caller can log or inspect it without re-parsing.

    Arguments:

    * ``tool_name``: the halted tool (``intent.tool_name``).
    * ``command_string``: for terminal halts, the full command line
      that classify_command_string evaluated against. For non-terminal
      halts, an empty string or arbitrary representation of the
      arguments dict — the regex generator falls through to the
      tool-name encoding regardless.
    * ``evidence_turn_id``: the ``turn_id`` of the halt that
      triggered the "always" disposition. Used as the sole evidence
      element for now.
    * ``eval_hash``: optional GRV-008 § II eval_hash. Sprint 32 leaves
      the field empty for zone_promotion proposals; the hero-suite
      gate doesn't currently model zone config, so the field is
      reserved for a future sprint that extends the gate.
    """
    pattern = normalize_pattern(tool_name, command_string)
    reason = _kaizen_reason(tool_name, command_string)
    payload: PromotionPayloadShape = {
        "tool": tool_name,
        "pattern": pattern,
        "zone": "green",
        "reason": reason,
    }
    evidence = (evidence_turn_id,) if evidence_turn_id else ()
    proposal = RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_ZONE_PROMOTION,
            payload=payload,
            evidence=evidence,
        ),
        type=PROPOSAL_TYPE_ZONE_PROMOTION,
        payload=payload,
        evidence=evidence,
        eval_hash=eval_hash,
        created_at=_now_iso(),
    )
    return proposal, payload
