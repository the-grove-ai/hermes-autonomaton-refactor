"""Sovereign Prompt handler implementations — GRV-005 § VI v1.1.

The Dispatcher accepts a ``sovereign_prompt_handler: Callable[[AndonHalt], str]``
at construction and calls it when ``_handle_andon_halt`` fires.

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
* :func:`non_interactive_deny_handler` — fail-closed handler for
  callers with no interactive Stage-04 channel (background / batch
  tasks, non-keyboard gateway adapters, ``/v1/runs`` + ``/v1/responses``,
  and transport-delivery failures). Returns ``"deny"`` with a WARNING
  log naming the denied action. C0 (conformance-disarm-seal-v1)
  replaced the prior ``gateway_auto_allow_handler`` /
  ``batch_auto_allow_handler`` auto-``once`` instruments with this: a
  raised Andon on a surface that cannot reach the operator now fails
  loud and never silently executes. Surfaces that CAN reach the
  operator (TTY four-choice prompt; Telegram inline keyboards; the
  web store-and-resume governance handler) keep their own handlers.
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
import os
import sys
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from grove.dispatcher import AndonHalt

__all__ = [
    "tty_sovereign_prompt",
    "tty_red_resolution",
    "non_interactive_deny_handler",
    "silent_allow_handler",
    "silent_deny_handler",
    "silent_promote_handler",
    # Public for the Dispatcher's Kaizen-register prompt rendering:
    "describe_action_kaizen",
    # Sprint 60 — display-string truncation shared by the Kaizen surfaces.
    "peek",
    # Sprint 32.2 — shared shell-variable normalization, reused by the
    # zone-promotion proposal generator so the template matcher and
    # the promotion regex see the same expanded path string.
    "normalize_command",
]

logger = logging.getLogger(__name__)


# ── Kaizen action-fact formatter (extracted to grove.action_facts, B1) ───────
#
# kaizen-voice Sprint B1 §VI item 7 — the objective fact formatter
# ``describe_action_kaizen`` (and its template table + helpers) moved to
# :mod:`grove.action_facts`, a dependency-free shared layer, so the Yellow-prompt
# fold in :mod:`grove.halt_renderer` can import the fact WITHOUT cycling back
# through this module. The names are re-exported here byte-for-byte so every
# prior caller — :mod:`grove.kaizen_promotion`, :mod:`grove.prompt.composer`,
# the Dispatcher, and the test-suite — keeps importing the same symbols from the
# same place. ``__all__`` (above) marks ``describe_action_kaizen`` / ``peek`` /
# ``normalize_command`` as the public re-export surface.
from grove.action_facts import (
    describe_action_kaizen,
    normalize_command,
    peek,
)


# ── TTY (operator-facing) prompt ─────────────────────────────────────


def tty_sovereign_prompt(halt: "AndonHalt", *, out=None) -> str:
    """The Kaizen-register Sovereign Prompt (Sprint 32 v1.1; copy
    refreshed to the concierge register in Sprint 60).

    Renders a plain-language, first-person description of the action
    the Agent wants to perform, followed by four operator choices:

        I'd like to <kaizen description>. This one's your call before
        I go ahead.

          [1] Just this once
          [2] For the rest of this session
          [3] Always — I'll remember it
          [4] Not this time

    Returns one of ``"once"``, ``"session"``, ``"always"``, ``"deny"``.
    Defaults to ``"deny"`` on EOF / KeyboardInterrupt (fail-safe).

    ``out`` is the destination stream for the menu and the
    fail-safe / unknown-choice messages; defaults to ``sys.stderr``
    so direct callers (oneshot, ``--quiet`` mode, unit tests using
    ``capsys``) get the normal capture behavior. The interactive CLI
    bridge in ``HermesCLI._sovereign_prompt_callback`` overrides this
    with ``sys.__stderr__`` to bypass prompt_toolkit's
    ``patch_stdout`` buffering — without that override the menu text
    sits in the StdoutProxy queue until the renderer flushes, which
    it doesn't reliably do from inside ``run_in_terminal``. Sprint 51
    Phase 3 finding.

    Zone names, regex patterns, match sources, and intent indices
    are deliberately absent from the prompt — they belong in the
    Kaizen Ledger (the Dispatcher's upstream ``andon_halt`` record
    carries them). Operator-facing text is plain language; debug
    detail lives in telemetry.
    """
    if out is None:
        out = sys.stderr
    # §VI fold (kaizen-voice B1) — the Yellow Sovereign Prompt TEXT now lives in
    # grove.halt_renderer.render_yellow_sovereign_prompt, byte-identical to the
    # prior inline copy (Sprint 32/60 vocabulary). Only the input() loop below is
    # I/O and stays here. NO RED branch is added to this surface: post-§VI a RED
    # halt is a workflow resolution, resolved upstream by the Dispatcher's
    # red-resolution handler, never by this four-choice prompt.
    from grove.halt_renderer import render_yellow_sovereign_prompt

    triggering = halt.intents[halt.triggering_index]

    print(
        render_yellow_sovereign_prompt(
            triggering.tool_name, triggering.arguments or {},
        ),
        file=out,
    )

    while True:
        try:
            choice = input("Choose [1-4]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — declining the action)", file=out)
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
            file=out,
        )


def tty_red_resolution(halt: "AndonHalt", *, out=None) -> str:
    """The §VI RED workflow-resolution menu (kaizen-voice Sprint B2).

    RED is a STRUCTURAL block, NOT a permission grant: the four-choice
    disposition menu (once / session / always / deny) does not apply and minting
    STOPS at RED. This operator-facing surface offers the two RED resolutions —
    Cancel (abort the structurally-blocked workflow) and De-scope (drop the
    blocked action; let the autonomaton re-plan within authority):

        <RED interrupt header carrying describe_action_kaizen>

          [1] Cancel — stop here
          [2] De-scope — drop it and let me re-plan within bounds

    Returns one of ``"cancel"`` / ``"descoped"`` — the
    ``grove.dispatcher.RED_RESOLUTIONS`` values (returned as literals to avoid a
    module import cycle, exactly as :func:`tty_sovereign_prompt` returns literal
    dispositions). Defaults to ``"cancel"`` on EOF / KeyboardInterrupt — the
    fail-safe SAFE direction is to abort the blocked workflow, matching
    :func:`grove.dispatcher.headless_red_resolution`. There is NO
    once/session/always/deny branch here, and NO Operator-Runs-It (the resumable
    bridge is a deferred track).

    The fact (WHAT the agent wanted to do) comes from the shared
    :mod:`grove.action_facts` layer via
    :func:`grove.halt_renderer.render_red_resolution_prompt`; the TONE (RED
    hard-boundary interrupt) is owned by the renderer and shared with no other
    surface (§VI ANDON-tone isolation). ``out`` mirrors
    :func:`tty_sovereign_prompt`: defaults to ``sys.stderr``; the CLI bridge
    overrides with ``sys.__stderr__`` to bypass prompt_toolkit buffering.
    """
    if out is None:
        out = sys.stderr
    from grove.halt_renderer import render_red_resolution_prompt

    triggering = halt.intents[halt.triggering_index]
    print(
        render_red_resolution_prompt(
            triggering.tool_name, triggering.arguments or {},
        ),
        file=out,
    )

    while True:
        try:
            choice = input("Choose [1-2]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — cancelling the action)", file=out)
            return "cancel"
        if choice in ("1", "cancel", "stop", "c"):
            return "cancel"
        if choice in ("2", "descope", "de-scope", "descoped", "d"):
            return "descoped"
        print(
            f"Unknown choice {choice!r}; pick 1 or 2.",
            file=out,
        )


def tty_post_execution_prompt(payload: Any, *, out=None) -> str:
    """Sprint 53.2 — the post-execution skill-promotion prompt.

    Fires AFTER a quarantined (.andon) skill ran successfully under an
    "allow once" disposition and the operator has seen its output
    (copy refreshed to the concierge register in Sprint 60):

        The <name> skill ran cleanly. I can add it to your active
        library so it won't need approval next time.

          [1] Promote it — no more prompts for this skill
          [2] Not yet — keep asking me each time
          [3] Never — don't run this skill again

    Returns ``"promote"``, ``"not_yet"``, ``"never"``, or ``"never_purge"``.
    Picking Never asks a follow-up — "Should I also clear it out so it
    stops appearing? [y/N]" — returning ``"never_purge"`` on yes (delete
    the .andon dir) and ``"never"`` on no (deny only). Defaults to
    ``"not_yet"`` on EOF /
    KeyboardInterrupt (fail-safe: the skill stays quarantined and
    re-prompts on its next run).

    Distinct vocabulary from the Sprint 32 four-choice Sovereign Prompt
    (Allow once / session / always / deny) — different handler, different
    return space, no collision. ``out`` mirrors ``tty_sovereign_prompt``:
    defaults to ``sys.stderr``; the CLI bridge overrides with
    ``sys.__stderr__`` to bypass prompt_toolkit buffering.
    """
    if out is None:
        out = sys.stderr
    skill_name = getattr(payload, "skill_name", "this skill")

    print(file=out)
    print(
        f"The {skill_name} skill ran cleanly. I can add it to your active "
        f"library so it won't need approval next time.",
        file=out,
    )
    print(file=out)
    print("  [1] Promote it — no more prompts for this skill", file=out)
    print("  [2] Not yet — keep asking me each time", file=out)
    print("  [3] Never — don't run this skill again", file=out)
    print(file=out)

    while True:
        try:
            choice = input("Choose [1-3]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — keeping the skill in quarantine)", file=out)
            return "not_yet"
        if choice in ("1", "promote", "yes", "y"):
            return "promote"
        if choice in ("2", "not yet", "not_yet", "later"):
            return "not_yet"
        if choice in ("3", "never", "no", "n", "deny"):
            try:
                purge = input(
                    "Should I also clear it out so it stops appearing? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                purge = "n"
            return "never_purge" if purge in ("y", "yes") else "never"
        print(f"Unknown choice {choice!r}; pick 1, 2, or 3.", file=out)


# ── Non-interactive handlers ─────────────────────────────────────────


def non_interactive_deny_handler(halt: "AndonHalt") -> str:
    """Fail-closed Sovereign-Prompt handler for non-interactive surfaces.

    C0 (conformance-disarm-seal-v1) — replaces the deleted
    ``gateway_auto_allow_handler`` / ``batch_auto_allow_handler``
    auto-``once`` instruments. Used by callers that have NO channel to
    reach the operator for a Stage-04 verdict: background / batch tasks
    (cron, eval, hygiene), non-keyboard gateway adapters, the
    ``/v1/runs`` + ``/v1/responses`` API endpoints, and the
    transport-delivery-failure path of the interactive Kaizen handler.

    Returns ``"deny"`` so the Dispatcher injects a denial Observation —
    the Agent may recover, re-reason, or pivot — and the action does NOT
    execute. Logs at WARNING (fail loud per the Architectural Prime
    Directive): a Yellow/Red action that could not be governed must be
    visible, not silently swallowed. The halt's full detail is already
    captured in the Kaizen Ledger via the Dispatcher's upstream
    ``andon_halt`` record.

    Rationale for the inversion (Sprint 32 auto-once → C0 deny): an
    auto-``once`` on an unreachable surface resolved a raised Andon to
    EXECUTION without an operator verdict — a disposition-layer bypass.
    Conformance requires that no path resolve a raised Andon to
    execution without a logged, operator-approved Stage-04 verdict.
    Non-interactive surfaces therefore run Green-zone only; Yellow and
    Red fail loud here.
    """
    triggering = halt.intents[halt.triggering_index]
    logger.warning(
        "Andon denied (no interactive Stage-04 channel): tool=%s zone=%s "
        "description=%r — action NOT executed (C0 fail-closed).",
        triggering.tool_name,
        getattr(halt, "zone", "unknown"),
        describe_action_kaizen(triggering.tool_name, triggering.arguments or {}),
    )
    return "deny"


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


