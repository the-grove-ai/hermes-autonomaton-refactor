"""Sovereign Prompt handler implementations — GRV-005 § VI v1.1.

The Dispatcher accepts a ``sovereign_prompt_handler: Callable[[AndonHalt], str]``
at construction and calls it when ``_handle_andon_halt`` fires — unless
shadow mode (``GROVE_ZONE_SHADOW=1``) short-circuits the call.

Per GRV-005 § VI v1.1, the operator-facing Sovereign Prompt is a
Kaizen-register four-choice menu. The operator never sees zone names,
regex patterns, or disposition jargon. Plain language describes WHAT
the action will do; the four options describe WHAT THE OPERATOR
DECIDES.

Disposition vocabulary (the only strings handlers MAY return):

* ``"once"``    — execute the action this invocation only; same action
                  on a future turn re-prompts.
* ``"session"`` — execute the action and cache a session-scoped allow.
                  Subsequent identical invocations execute silently
                  within the session.
* ``"always"``  — execute the action, cache the session allow, AND
                  queue a ZonePromotionProposal to the GRV-008 proposal
                  queue. The green-rule promotion takes effect only
                  after operator approval via
                  ``autonomaton flywheel approve``.
* ``"deny"``    — inject a denial Observation; the Agent may recover,
                  re-reason, or pivot. The denial is cached for the
                  session.

Sprint 32.1 — the v1.0 vocabulary (``skip`` / ``drop`` /
``shadow_approve``) is removed entirely. Any other return value from a
handler raises ``ValueError`` at the Dispatcher's disposition gate.

This module ships handler implementations for each caller context:

* :func:`tty_sovereign_prompt` — the canonical operator-facing TTY
  prompt. Renders the Kaizen four-choice menu.
* :func:`batch_auto_allow_handler` — non-interactive auto-``once`` for
  batch callers (cron, eval, hygiene, compression). Returns ``"once"``
  with an INFO log.
* :func:`gateway_auto_allow_handler` — non-interactive auto-``once``
  for gateway callers (Telegram, Discord, API). Returns ``"once"``
  with an INFO log. Gateway callers MUST NOT return ``"always"`` —
  the operator has no CLI access for approval from a mobile surface.
* :func:`silent_allow_handler` — test fixture; returns ``"once"`` with
  no I/O.
* :func:`silent_deny_handler` — test fixture; returns ``"deny"`` with
  no I/O. Use when a test needs to force the deny-and-recover path.
* :func:`silent_promote_handler` — test fixture; returns ``"always"``
  with no I/O. Use when a test needs to drive the promotion-proposal
  path.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from grove.dispatcher import AndonHalt

__all__ = [
    "tty_sovereign_prompt",
    "batch_auto_allow_handler",
    "gateway_auto_allow_handler",
    "silent_allow_handler",
    "silent_deny_handler",
    "silent_promote_handler",
    # Public for the Dispatcher's Kaizen-register prompt rendering:
    "describe_action_kaizen",
]

logger = logging.getLogger(__name__)


# ── Kaizen prompt templates (Sprint 32 D2) ───────────────────────────
#
# Pure-dict, no LLM call. Each row is (tool_name, arg_substring,
# template). The first matching row wins. The fallback row
# (None, None, ...) catches every tool not declared above.
#
# Argument substring matching: when ``arg_substring`` is set, the
# template fires only if the substring appears anywhere in the
# stringified arguments dict (case-sensitive). Useful for tools
# whose semantic varies by argument shape (terminal skill execution
# vs terminal generic command).
#
# Skill-name extraction: when the template includes ``{skill_name}``,
# the renderer extracts the skill directory name from the argument by
# locating ``.grove/skills/`` and taking the next path segment.
#
# Extension path: add a row for any new tool that surfaces yellow
# halts. Operators with a non-technical posture see the new template
# the next time the tool halts; no code recompile required for the
# UI text.
_KAIZEN_PROMPT_TEMPLATES: Tuple[Tuple[Optional[str], Optional[str], str], ...] = (
    ("terminal",     ".grove/skills/",  "run a skill ({skill_name})"),
    ("terminal",     None,              "run a command on your machine"),
    ("execute_code", None,              "execute code"),
    # Fallback row — matches every tool not above.
    (None,           None,              "perform an action ({tool_name})"),
)


def _extract_skill_name(arguments_str: str) -> str:
    """Pull the skill directory name out of a command argument blob.

    Looks for ``.grove/skills/<name>/`` or ``.grove/skills/<name>``
    and returns the ``<name>`` segment. Returns ``"unknown"`` when
    no skill path is present.
    """
    marker = ".grove/skills/"
    idx = arguments_str.find(marker)
    if idx < 0:
        return "unknown"
    tail = arguments_str[idx + len(marker):]
    end = 0
    for ch in tail:
        if ch in "/'\" \t\n":
            break
        end += 1
    return tail[:end] or "unknown"


def describe_action_kaizen(tool_name: str, arguments: dict) -> str:
    """Render a Kaizen-register plain-language description of the action.

    Used by :func:`tty_sovereign_prompt` to build the prompt's header
    line. Exposed publicly so the Dispatcher's batch / gateway INFO
    log lines can carry the same description for telemetry parity.
    """
    args_str = str(dict(arguments)) if arguments else ""
    for tmpl_tool, tmpl_substring, tmpl_text in _KAIZEN_PROMPT_TEMPLATES:
        if tmpl_tool is not None and tmpl_tool != tool_name:
            continue
        if tmpl_substring is not None and tmpl_substring not in args_str:
            continue
        return tmpl_text.format(
            tool_name=tool_name,
            skill_name=_extract_skill_name(args_str),
        )
    # Unreachable: the fallback row matches every tool. Keep an
    # explicit return for type-checker happiness.
    return f"perform an action ({tool_name})"


# ── TTY (operator-facing) prompt ─────────────────────────────────────


def tty_sovereign_prompt(halt: "AndonHalt") -> str:
    """The Kaizen-register Sovereign Prompt (Sprint 32 v1.1).

    Renders a plain-language two-sentence description of the action
    the Agent wants to perform, followed by four operator choices:

        The agent wants to <kaizen description>.
        This requires <one-line context>.

          [1] Allow this once
          [2] Allow for this session
          [3] Always allow this — I'll save the preference
          [4] Don't allow this

    Returns one of ``"once"``, ``"session"``, ``"always"``, ``"deny"``.
    Defaults to ``"deny"`` on EOF / KeyboardInterrupt (fail-safe).

    Zone names, regex patterns, match sources, and intent indices
    are deliberately absent from the prompt — they belong in the
    Kaizen Ledger (the Dispatcher's upstream ``andon_halt`` record
    carries them). Operator-facing text is plain language; debug
    detail lives in telemetry.
    """
    triggering = halt.intents[halt.triggering_index]
    description = describe_action_kaizen(
        triggering.tool_name, triggering.arguments or {},
    )

    print(file=sys.stderr)
    print(
        f"The agent wants to {description}.",
        file=sys.stderr,
    )
    print(
        "This requires a decision before it can continue.",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("  [1] Allow this once", file=sys.stderr)
    print("  [2] Allow for this session", file=sys.stderr)
    print(
        "  [3] Always allow this — I'll save the preference",
        file=sys.stderr,
    )
    print("  [4] Don't allow this", file=sys.stderr)
    print(file=sys.stderr)

    while True:
        try:
            choice = input("Choose [1-4]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — declining the action)", file=sys.stderr)
            return "deny"
        if choice in ("1", "once", "allow", "yes", "y"):
            return "once"
        if choice in ("2", "session"):
            return "session"
        if choice in ("3", "always"):
            return "always"
        if choice in ("4", "deny", "no", "n", "don't allow", "dont allow"):
            return "deny"
        print(
            f"Unknown choice {choice!r}; pick 1, 2, 3, or 4.",
            file=sys.stderr,
        )


# ── Non-interactive handlers ─────────────────────────────────────────


def batch_auto_allow_handler(halt: "AndonHalt") -> str:
    """Non-interactive auto-``once`` for batch callers.

    Used by callers with no live operator surface (cron jobs, eval
    runs, compression hygiene). Returns ``"once"`` so the Agent
    receives the action's result and continues. The halt's full
    detail is already captured in the Kaizen Ledger via the
    Dispatcher's upstream ``andon_halt`` record.

    v1.0 → v1.1 behavior change: previously auto-denied (returned
    ``"skip"``). Sprint 32 inverts to auto-allow-once on the
    rationale that batch callers are themselves operator-initiated
    and the four-choice prompt cannot reach the operator from a
    background context. Operators who want batch contexts to
    auto-deny use :func:`silent_deny_handler`.
    """
    triggering = halt.intents[halt.triggering_index]
    logger.info(
        "Kaizen auto-allow (batch): tool=%s description=%r",
        triggering.tool_name,
        describe_action_kaizen(triggering.tool_name, triggering.arguments or {}),
    )
    return "once"


def gateway_auto_allow_handler(halt: "AndonHalt") -> str:
    """Non-interactive auto-``once`` for gateway turns.

    Used by platform-driven callers (Telegram, Discord, Feishu, HTTP
    API) where the operator is reachable via the platform but not
    via TTY. Returns ``"once"`` with an INFO log.

    Sprint 32 A4 lock: gateway callers MUST NOT queue zone-promotion
    proposals from a non-TTY surface. The operator has no
    ``autonomaton flywheel approve`` access from a mobile messaging
    client. The Dispatcher's promotion path is gated on the handler
    identity — gateway handlers map any ``"always"`` semantic at the
    Dispatcher layer to ``"session"`` silently. This handler itself
    never returns ``"always"``.
    """
    triggering = halt.intents[halt.triggering_index]
    logger.info(
        "Kaizen auto-allow (gateway): tool=%s description=%r",
        triggering.tool_name,
        describe_action_kaizen(triggering.tool_name, triggering.arguments or {}),
    )
    return "once"


def silent_allow_handler(halt: "AndonHalt") -> str:
    """Silent auto-``once`` for test fixtures.

    Returns ``"once"`` with no I/O. Tests injecting this handler
    drive the Dispatcher past an Andon halt deterministically AND
    let the tool actually execute via the Green path. Use this when
    a test needs to verify flow control around incidental yellow
    halts (a tool name not in the schema's tool_zones map defaulting
    to yellow, etc.).
    """
    return "once"


def silent_deny_handler(halt: "AndonHalt") -> str:
    """Silent ``"deny"`` for test fixtures.

    Returns ``"deny"`` with no I/O. Tools are NOT executed; the
    Dispatcher injects a denial Observation. Use this when a test
    needs to exercise the deny-then-recover path (the v1.0 "skip"
    semantic).
    """
    return "deny"


def silent_promote_handler(halt: "AndonHalt") -> str:
    """Silent ``"always"`` for test fixtures.

    Returns ``"always"`` with no I/O. Tools execute via the Green
    path AND the Dispatcher queues a ZonePromotionProposal. Use this
    when a test needs to drive the promotion-proposal flow without
    mocking a TTY prompt.

    This is NOT for production use; returning ``"always"`` from a
    handler bypasses operator oversight unconditionally. Production
    gateway / batch callers SHOULD use the auto-allow handlers so
    the proposal queue stays operator-approved.
    """
    return "always"


