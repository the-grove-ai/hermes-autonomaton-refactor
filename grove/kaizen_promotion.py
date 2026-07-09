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
from urllib.parse import urlparse

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


# terminal-rule-generalization-v1 — verb taxonomy for terminal zone-promotion
# broadening. Read verbs broaden inside a granted workspace; read network verbs
# broaden to the target domain; mutating verbs broaden inside a workspace but
# stay path-constrained (no leading-slash absolute paths, no `..` traversal).
# `echo` is deliberately NOT a read verb — `echo x > file` is a write — so it
# falls to the conservative exact-match default.
SAFE_NET_VERBS = frozenset({"curl", "wget", "ping"})
SAFE_READ_VERBS = frozenset({"ls", "cat", "grep", "which", "pwd", "head", "tail"})
MUTATING_VERBS = frozenset(
    {"rm", "mv", "cp", "chmod", "chown", "mkdir", "touch", "kill"}
)


def normalize_pattern(
    tool_name: str, command_string: str = "", cwd: str = None,
) -> str:
    """Generate the regex pattern for a zone-promotion green rule.

    The matcher (:mod:`grove.zones`) uses ``re.fullmatch``, so every pattern is
    implicitly anchored end-to-end; the explicit ``^...$`` below are
    belt-and-suspenders.

    terminal-rule-generalization-v1 — instead of pinning the exact command,
    generalize by verb class so a previously-approved verb does not re-prompt on
    a varied argument:

    * skill-path command → any command touching that skill dir (unchanged).
    * read network verb (curl/wget/ping) → any invocation targeting the same
      domain.
    * read local verb (ls/cat/grep/…) → any invocation inside a granted
      workspace; outside, any non-redirecting/non-chaining invocation.
    * mutating verb (rm/mv/mkdir/…) → any invocation inside a granted workspace
      with a relative, non-traversing path; else exact match.
    * anything else → exact match (conservative default).

    Non-terminal tool → the tool name (tool-level: every invocation matches).

    Sprint 32.2 — ``$HOME`` / ``${HOME}`` / leading ``~/`` are expanded via
    :func:`grove.action_facts.normalize_command` before matching (preserved
    from the prior version, applied to every terminal branch).
    """
    if tool_name != "terminal":
        return re.escape(tool_name)

    # Preserve Sprint 32.2 $HOME/~ normalization for ALL terminal branches.
    command_string = normalize_command((command_string or "").strip())
    if not command_string:
        return "^.*$"  # degenerate; fails the loader safety check, surfacing it

    # 1. Skill-path branch (unchanged) — any command under that skill dir.
    skill_name = _extract_skill_name(command_string)
    if skill_name != "unknown":
        return r".*\.grove/skills/" + re.escape(skill_name) + r"/.*"

    # 2. Verb-class broadening.
    verb = command_string.split()[0]

    in_workspace = False
    if cwd:
        try:
            from grove.utils.fs_utils import is_granted_workspace
            in_workspace = bool(is_granted_workspace(cwd))
        except Exception:  # noqa: BLE001 — never let workspace probing break rule-gen
            in_workspace = False

    # 2a. Read-only network verbs → broaden to the target domain.
    if verb in SAFE_NET_VERBS:
        for part in command_string.split()[1:]:
            # Strip surrounding quotes BEFORE the scheme check — the common
            # form is a quoted URL (curl -s "https://…"), which would otherwise
            # start with `"` and never match.
            candidate = part.strip("\"'")
            if candidate.startswith(("http://", "https://")):
                try:
                    netloc = urlparse(candidate).netloc
                except ValueError:
                    netloc = ""
                if netloc:
                    return (
                        "^" + re.escape(verb) + r" .*\b"
                        + re.escape(netloc) + r"\b.*$"
                    )
                break
        return "^" + re.escape(command_string) + "$"

    # 2b. Read-only local verbs.
    if verb in SAFE_READ_VERBS:
        if in_workspace:
            return "^" + re.escape(verb) + r" .*$"
        # Outside a workspace: forbid pipes, redirects, and command chaining.
        return "^" + re.escape(verb) + r" [^|><;]+$"

    # 2c. State-mutating verbs.
    if verb in MUTATING_VERBS:
        if in_workspace:
            # Allow mutation; forbid leading-slash absolute paths (/etc/…) and
            # `..` traversal (Fix 1). Internal slashes (src/main.py) are allowed.
            return "^" + re.escape(verb) + r" (?!/)(?!.*\.\.).+$"
        return "^" + re.escape(command_string) + "$"

    # 3. Conservative default — exact match.
    return "^" + re.escape(command_string) + "$"


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
    cwd: str = None,
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
    pattern = normalize_pattern(tool_name, command_string, cwd=cwd)
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
        proposer="kaizen_promotion",  # proposal-proposer-attribution-v1 (#12)
    )
    return proposal, payload
