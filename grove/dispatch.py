"""Grove dispatch — zone-classifier integration for tool dispatch.

Sprint 06a (jidoka-andon-implementation-v1) wires the zone classifier into
``tools/approval.py::check_all_command_guards``. The TPS sequence is:

    Jidoka (zone classifier) detects
    → Andon (this gate) halts the line for red and yellow
    → Kaizen (the sovereign prompt) proposes go-forward options at red

This module provides:

* ``command_to_action(command, env_type)`` — map a shell command line to
  a pure dot-notation action identifier
  (``"sudo apt install"`` → ``"command.execute.sudo"``).
* ``classify_command(command, env_type)`` — combine the mapper with the
  zone classifier; returns a ``ZoneResult``.
* ``descope_command(command)`` — strip a privilege-escalation wrapper
  (sudo / su / doas) and return the within-authority version, or ``None``
  if no de-scoping is possible. Used by Kaizen's "try de-scoped alternative"
  option at red boundaries.
* ``render_red_surface(command, zone_result)`` — produce the operator-facing
  surface text for a red-zone classification (per the design contract's
  "butler" register: name the file/line, name the reload, no "forbidden").
* ``kaizen_sovereign_prompt(command, zone_result, descoped)`` — present the
  three-option Kaizen prompt (Cancel / Operator handles / De-scoped
  alternative). TTY only; gateway/strict callers skip this.
* ``get_classifier()`` — lazy singleton initializer so importing this module
  doesn't read the schema file at import time.

The mapper is intentionally minimal for v0.1 — it extracts the verb after
stripping path prefixes and ``VAR=value`` assignments. More sophisticated
intent classification (``git push --force`` vs ``git push``) is a later
sprint when we have telemetry to know which distinctions matter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from grove.zones import ZoneResult


logger = logging.getLogger(__name__)


_DESCOPE_WRAPPERS = frozenset({"sudo", "su", "doas"})
_SUDO_OPTS_WITH_VALUE = frozenset({"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-t"})


# ----- classifier singleton --------------------------------------------------

_classifier = None


def get_classifier():
    """Return the zone classifier singleton, initializing on first call."""
    global _classifier
    if _classifier is None:
        from grove.zones import initialize
        _classifier = initialize()
    return _classifier


def reset_classifier() -> None:
    """Drop the singleton — test helper. Production code should not call this."""
    global _classifier
    _classifier = None


# ----- command → action mapper -----------------------------------------------

def command_to_action(command: str, env_type: str = "local") -> str:
    """Map a shell command to a pure dot-notation action identifier.

    Strips:
      * leading whitespace
      * ``VAR=value`` environment assignments at the start
      * path prefixes (``/usr/bin/sudo`` → ``sudo``)

    Returns ``"command.execute.<verb>"`` where ``verb`` is the first
    executable token. Empty / whitespace-only commands map to
    ``"command.execute.empty"``.

    Args:
        command: the shell command line as it would be passed to ``bash -c``.
        env_type: execution environment (``local``, ``docker``, …) —
            reserved for future use; not yet a discriminator.
    """
    if not command or not command.strip():
        return "command.execute.empty"

    tokens = command.strip().split()
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        # FOO=bar style assignment — peel until we hit the actual verb.
        tokens = tokens[1:]

    if not tokens:
        return "command.execute.empty"

    verb = tokens[0]
    if "/" in verb:
        verb = verb.rsplit("/", 1)[-1]

    return f"command.execute.{verb}"


# ----- classifier wrapper ----------------------------------------------------

def classify_command(
    command: str,
    env_type: str = "local",
    *,
    tool_id: Optional[str] = None,
) -> "ZoneResult":
    """Classify a command — Sprint 22 hierarchical-first.

    Maps ``command`` to a dot-notation action identifier, then asks
    the classifier to evaluate any hierarchical argument-level rules
    for the resolved tool before falling through to the legacy
    ``classify(action)`` path. The legacy path is unchanged for tools
    whose ``tool_zones`` entry is a bare string (or absent).

    Args:
        command: the shell command line as passed to ``bash -c``.
        env_type: execution environment (``local``, ``docker``, …) —
            reserved for future use.
        tool_id: which tool's hierarchical rules to consult. When
            ``None``, derive from the action prefix (v0.1 ships a
            single mapping: ``command.execute.* → terminal``). Future
            tools that want hierarchical rules should pass this
            explicitly rather than expanding the derivation map —
            see the note in ``grove/zones.py``.

    Returns:
        A ``ZoneResult``. Backward compatible — callers reading only
        ``.zone`` / ``.matched_rule`` / ``.source`` continue to work;
        the new ``reason`` and ``pattern_key`` fields are ``None``
        for legacy (dot-notation) classifications and populated for
        hierarchical rule matches.
    """
    # GRV-010 C1a (conformance-shell-containment-v1) — the regex
    # ``tool_zones.terminal.rules`` path is replaced by a bashlex-AST EFFECT
    # classifier. It parses the command and classifies by real command nodes,
    # verbs, targets, and redirects — structurally defeating the substring
    # evasions (B1 comment-suffix, B2 leading-.* prefix) and command chaining
    # that a string matcher cannot. ``env_type`` / ``tool_id`` are retained on
    # the signature for backward compatibility but no longer drive a per-tool
    # regex list. ``command_to_action`` / ``classify_command_string`` remain for
    # the de-scope path and any non-shell caller.
    from grove.shell_effects import classify_shell_effect
    return classify_shell_effect(command)


# ----- de-scoping (Kaizen's third option) ------------------------------------

def descope_command(command: str) -> Optional[str]:
    """Strip a privilege-escalation wrapper if present; return the wrapped form.

    Conservative for v0.1: only handles the simple ``<wrapper> <rest>`` shape
    for sudo / su / doas. Returns ``None`` when:

    * the command is empty,
    * the first verb is not a known wrapper, or
    * stripping the wrapper would leave nothing to execute.

    For ``sudo`` specifically, also peels common option flags (``-u USER``,
    ``-E``, ``-H``, ``-i``, ``-s``, ``-n``, ``--``) so
    ``sudo -u root apt install foo`` de-scopes to ``apt install foo``.
    Preserves any leading ``VAR=value`` environment assignments.

    The returned string is the command Kaizen would propose as a
    within-authority alternative; ``tools/approval.py`` re-classifies it
    through the normal flow before deciding whether to execute.
    """
    if not command or not command.strip():
        return None
    tokens = command.strip().split()
    if not tokens:
        return None

    # Peel leading VAR=val env assignments so they can be re-prepended later.
    leading_assignments: list[str] = []
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        leading_assignments.append(tokens[0])
        tokens = tokens[1:]
    if not tokens:
        return None

    verb = tokens[0]
    verb_name = verb.rsplit("/", 1)[-1] if "/" in verb else verb
    if verb_name not in _DESCOPE_WRAPPERS:
        return None

    rest = tokens[1:]
    if not rest:
        return None

    if verb_name == "sudo":
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in _SUDO_OPTS_WITH_VALUE and i + 1 < len(rest):
                i += 2
                continue
            if tok in ("-E", "-H", "-i", "-s", "-n", "-A", "-b", "-k", "-K", "-L", "-l", "-P", "-S", "-v", "--"):
                i += 1
                continue
            # Unknown short flag like -X: skip cautiously.
            if tok.startswith("-") and len(tok) <= 3 and not tok.startswith("--"):
                i += 1
                continue
            break
        rest = rest[i:]

    if not rest:
        return None

    return " ".join(leading_assignments + rest)


# ----- red-zone surface ------------------------------------------------------

def render_red_surface(command: str, zone_result) -> str:
    """Render the operator-facing red-zone surface message.

    Per ``docs/design/andon-design-v1.md`` "Red zone UX — the butler":

      1. Never say "access denied" or "forbidden."
      2. Read access is green — surface what we know.
      3. Name the blocked command + that the privilege stays with the operator.
      4. Ask the operator to run it and report the result.

    governance-gateway-parity-v1 (Strike 1) simplified the copy to the
    standards register: the config-lever instruction (edit
    ``~/.grove/zones.schema.yaml`` + restart) was dropped in favour of "run it
    yourself, then tell me the result". The actual text lives in
    :func:`grove.halt_renderer._render_tool_boundary`.

    The message is returned (not printed) so the caller — currently
    ``check_all_command_guards`` in ``tools/approval.py`` — can place it in
    the dispatch return dict alongside ``approved: False``.
    """
    # Sprint A (kaizen-voice) — this red-zone privilege surface is now produced
    # by the unified HaltEvent renderer (wiring, not copy: the Sprint 60 butler
    # wording, including the <=120-char snippet truncation, is preserved
    # byte-for-byte; the renderer owns the truncation). The halt is NON_TERMINAL
    # (the caller in ``tools/approval.py`` returns ``approved: False`` and the
    # agent may re-plan) and steering-capable (``can_operator_run`` — "run it
    # yourself"), so the Feed Invariant routes it TO the operator feed.
    from grove.halt_event import (
        HaltCapabilities,
        HaltEvent,
        HaltSeverity,
        HaltTrigger,
        OriginatingLayer,
        WhatHalted,
    )
    from grove.halt_renderer import render_halt_event

    return render_halt_event(HaltEvent(
        trigger=HaltTrigger.PRIVILEGE_REQUIRED,
        what_halted=WhatHalted(summary=command),
        zone=getattr(zone_result, "zone", None),
        severity=HaltSeverity.NON_TERMINAL,
        originating_layer=OriginatingLayer.TOOL_BOUNDARY,
        capabilities=HaltCapabilities(can_cancel=True, can_operator_run=True),
    ))


# ----- Kaizen sovereign prompt -----------------------------------------------

def kaizen_sovereign_prompt(
    command: str, zone_result, descoped: Optional[str]
) -> str:
    """Present the Kaizen three-option prompt at a red sovereignty boundary.

    TPS sequence: Jidoka (zone classifier) detected red. Andon (the gate)
    halted the line. **Kaizen** — this prompt — steps in as the butler with
    go-forward options.

    Returns one of:

    * ``"cancel"`` — operator declines all options.
    * ``"operator_handles"`` — operator will run the command directly.
    * ``"alternative"`` — operator wants to try the de-scoped variant. Only
      offered when ``descoped`` is not ``None``.

    TTY-only. Gateway and strict-mode callers skip this entirely and return
    the red-surface hard block directly — there is no operator present to
    interact with the prompt.
    """
    options: list[tuple[str, str, str]] = [
        ("1", "cancel", "Stand down — I'll drop it"),
        (
            "2",
            "operator_handles",
            "Hand it to me to run — I'll surface the exact command for you",
        ),
    ]
    if descoped is not None:
        options.append(
            (
                "3",
                "alternative",
                f"Try a safer version I can run myself: `{descoped[:100]}`",
            )
        )

    valid_keys = {key for key, _, _ in options}

    print()
    print("─── Sovereign zone — Andon halted ─────────────────────────")
    print(render_red_surface(command, zone_result))
    print()
    print("Here's how I'd move forward:")
    for key, _, label in options:
        print(f"  {key}) {label}")
    print()

    prompt_text = f"Choose [{'/'.join(sorted(valid_keys))}]: "
    while True:
        try:
            choice = input(prompt_text).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — treating as cancel)")
            return "cancel"
        for key, value, _ in options:
            if choice == key or choice == value:
                return value
        print(
            f"Unknown choice {choice!r}. Pick one of: "
            f"{', '.join(sorted(valid_keys))}."
        )
