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
import os
import sys
from typing import TYPE_CHECKING, Any, Optional, Tuple

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
    # Sprint 60 — display-string truncation shared by the Kaizen surfaces.
    "peek",
    # Sprint 32.2 — shared shell-variable normalization, reused by the
    # zone-promotion proposal generator so the template matcher and
    # the promotion regex see the same expanded path string.
    "normalize_command",
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
    # Sprint 32.2 — category-specific rows above the generic terminal
    # fallback. First match wins, so order matters: skill execution
    # comes first (highest specificity, the bug this sprint fixes),
    # then package installation, destructive ops, network ops, git
    # state changes, and finally the generic "run a command" row.
    #
    # Templates are descriptive UX, not permission grants. The zone
    # rules in zones.schema.yaml decide what halts in the first place;
    # these strings only describe WHAT halted, in plain language the
    # operator can decide on without reading regex.

    # Skill execution — the highest-specificity row.
    ("terminal",     ".grove/skills/",   "run the {skill_name} skill"),

    # Package installation — extract the package being touched.
    ("terminal",     "brew install",     "install the software {package}"),
    ("terminal",     "brew uninstall",   "uninstall the software {package}"),
    ("terminal",     "apt install",      "install the software {package}"),
    ("terminal",     "apt remove",       "remove the software {package}"),
    ("terminal",     "pip install",      "install the Python package {package}"),
    ("terminal",     "npm install",      "install the Node.js package {package}"),

    # Destructive operations — rm -rf is more specific than rm so it
    # must precede it. The trailing space on the bare "rm " prevents
    # matching "rmdir" or "rmlink". Both show the actual command so the
    # operator sees WHAT would be deleted (Peek-truncated, Sprint 60).
    ("terminal",     "rm -rf",           "permanently delete files ({peek_cmd})"),
    ("terminal",     "rm ",              "delete files ({peek_cmd})"),

    # Network operations — trailing space avoids matching "curls" /
    # "wgetfile" / "sshd".
    ("terminal",     "curl ",            "make a network request"),
    ("terminal",     "wget ",            "download a file from the internet"),
    ("terminal",     "ssh ",             "connect to a remote machine"),

    # Git state changes — push/reset are the destructive ones; status
    # / log / diff stay on the generic fallback.
    ("terminal",     "git push",         "push code to a remote repository"),
    ("terminal",     "git reset",        "reset your git history"),

    # Generic fallback rows. The terminal row now shows the command
    # itself (Peek-truncated); write_file names the file. Both degrade
    # to a bare phrase when the argument is absent (see the renderer's
    # Sprint 60 graceful-degradation branch).
    ("terminal",     None,               "run this command: {peek_cmd}"),
    ("write_file",   None,               "write the file {peek_path}"),
    ("execute_code", None,               "run this code snippet"),
    # Fallback row — matches every tool not above.
    (None,           None,               "use {tool_name}"),
)


def normalize_command(command_string: str) -> str:
    """Expand ``$HOME`` / ``${HOME}`` / leading ``~`` to the operator's home.

    Sprint 32.2 — fixes the Kaizen template-matcher bug where a skill
    invocation written as ``${HOME}/.grove/skills/<name>/...`` slipped
    past the ``.grove/skills/`` substring match because the literal
    home prefix was unexpanded.  After this call, every downstream
    substring check sees a fully-resolved path.

    Scope (v0.1, GATE-A A1): only ``$HOME``, ``${HOME}``, and a
    leading ``~/`` are expanded.  Other shell variables, quoted
    forms, nested expansions, command substitution, and
    glob-expanded paths are out of scope — the helper is a string
    replacement, not a shell evaluator.  Andon-class shell tricks
    fall through to the generic "run a command" template and the
    operator still sees a prompt; no silent matching beyond what
    these three forms cover.

    Idempotent: a string with no shell-variable forms is returned
    unchanged.  Safe to call on arbitrary user input — never invokes
    a subprocess.
    """
    if not command_string:
        return command_string
    home = os.path.expanduser("~")
    out = command_string.replace("${HOME}", home).replace("$HOME", home)
    if out.startswith("~/"):
        out = home + out[1:]
    return out


def peek(value: object, *, limit: int = 100) -> str:
    """Center-truncate a value for display inside a Kaizen prompt.

    Sprint 60 — the Kaizen surfaces interpolate operator-supplied
    strings (a command, a file path) straight into the prompt. A
    multi-kilobyte command or a 4 KB ``write_file`` body would swamp the
    CLI and blow past Telegram's terse surface, so every interpolated
    value passes through here first.

    Strings at or under ``limit`` characters return unchanged. Longer
    strings are center-truncated to ``head…tail`` so BOTH ends stay
    visible — a path keeps its directory AND its filename, a command
    keeps its verb AND its target. The result never exceeds ``limit``
    characters. ``None`` and empty values render as ``""`` so a missing
    fragment degrades to nothing rather than the literal word "None"
    (Sprint 60 graceful-degradation contract).

    Pure: no I/O, no subprocess. Safe on arbitrary operator input.
    """
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    keep = max(limit - 1, 1)  # reserve one column for the ellipsis
    head = keep // 2
    tail = keep - head
    return s[:head] + "…" + s[-tail:]


_SKILL_PAYLOAD_MARKERS = frozenset({
    "scripts", "references", "tests", "SKILL.md", "README.md",
})


def _extract_skill_name(arguments_str: str) -> str:
    """Pull the skill directory name out of a command argument blob.

    Looks for ``.grove/skills/`` in the input and walks the path
    segments that follow, returning the deepest segment that is
    neither a filename (contains ``.`` but not as a leading dot
    file) nor a skill-payload directory (``scripts``, ``references``,
    ``tests``, etc.).  Returns ``"unknown"`` when no path is present
    or every segment looks like a file.

    Sprint 32.2 — handles both the single-level layout
    (``.grove/skills/<name>/run.py`` → ``<name>``) AND the
    category layout (``.grove/skills/<category>/<name>/scripts/x.py``
    → ``<name>``).  Operator-authored skills under the category
    layout were the bug surface: a ``google-workspace`` skill living
    under ``productivity/`` returned ``"productivity"`` from the
    pre-32.2 extractor, which leaked the category name into the
    Kaizen prompt.

    Sprint 32.2 — callers MUST pass an already-normalized argument
    blob (run :func:`normalize_command` first if the input may carry
    unexpanded ``$HOME`` / ``${HOME}`` / ``~``).  Without
    normalization, a ``${HOME}/.grove/skills/...`` invocation
    returns ``"unknown"`` because the substring is split by the
    unexpanded variable.
    """
    marker = ".grove/skills/"
    idx = arguments_str.find(marker)
    if idx < 0:
        return "unknown"
    tail = arguments_str[idx + len(marker):]
    # Collect path bytes until a path-terminating delimiter.  Quotes,
    # whitespace, and shell metachars end the path; ``/`` stays so
    # we can split into segments below.
    end = 0
    for ch in tail:
        if ch in "'\" \t\n":
            break
        end += 1
    path_part = tail[:end]
    if not path_part:
        return "unknown"
    candidate = None
    for seg in path_part.split("/"):
        if not seg:
            continue
        # Filename heuristic: a non-leading-dot segment containing
        # ``.`` is a filename (e.g. ``run.py`` / ``google_api.py``).
        # Once we hit one, the previous segment is the skill name.
        if "." in seg and not seg.startswith("."):
            break
        # Payload subdir: a directory the skill is structured around
        # rather than the skill itself.  Stop and return the segment
        # we last saw at this level.
        if seg in _SKILL_PAYLOAD_MARKERS:
            break
        candidate = seg
    return candidate or "unknown"


def _extract_install_package(arguments_str: str, install_verb: str) -> str:
    """Pull the package name out of an install/uninstall command.

    ``install_verb`` is the template's matched substring (e.g.
    ``"brew install"``, ``"pip install"``).  Returns the first
    non-flag token after the verb; ``"unknown"`` when no token is
    present (a bare ``brew install`` with no args).

    Flag tokens are anything starting with ``-``; quoted package
    names (rare but possible) have their surrounding quote stripped.
    Multi-package invocations (``brew install foo bar baz``) report
    only the first — the template's job is to give the operator the
    headline, not enumerate.
    """
    idx = arguments_str.find(install_verb)
    if idx < 0:
        return "unknown"
    tail = arguments_str[idx + len(install_verb):].strip()
    if not tail:
        return "unknown"
    for token in tail.split():
        # Strip a single layer of surrounding quotes.
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        if not token or token.startswith("-"):
            continue
        # Trim trailing punctuation that bleeds in from the
        # stringified args dict — commas, quotes, braces — in one
        # rstrip pass so a chain like ``ripgrep'}`` collapses to
        # ``ripgrep`` rather than ``ripgrep'``.
        token = token.rstrip(",'\"}")
        if token:
            return token
    return "unknown"


def describe_action_kaizen(tool_name: str, arguments: dict) -> str:
    """Render a Kaizen-register plain-language description of the action.

    Used by :func:`tty_sovereign_prompt` to build the prompt's header
    line. Exposed publicly so the Dispatcher's batch / gateway INFO
    log lines can carry the same description for telemetry parity.

    Sprint 32.2 — the raw stringified arguments are passed through
    :func:`normalize_command` before substring matching so a skill
    invocation written as ``${HOME}/.grove/skills/<name>/...`` is
    matched against the skill template instead of falling through to
    the generic "run a command on your machine" row.
    """
    raw_args_str = str(dict(arguments)) if arguments else ""
    args_str = normalize_command(raw_args_str)
    # Per-argument detail for the Peek-bearing rows (Sprint 60).
    # ``command`` is the terminal tool's argument; ``path`` is
    # write_file's. Each is normalized then center-truncated so the
    # prompt shows the real thing without swamping the surface.
    args_dict = dict(arguments) if isinstance(arguments, dict) else {}
    peek_cmd = peek(normalize_command(str(args_dict.get("command", ""))))
    peek_path = peek(str(args_dict.get("path", "")))
    for tmpl_tool, tmpl_substring, tmpl_text in _KAIZEN_PROMPT_TEMPLATES:
        if tmpl_tool is not None and tmpl_tool != tool_name:
            continue
        if tmpl_substring is not None and tmpl_substring not in args_str:
            continue
        # Graceful degradation (Sprint 60): a Peek row with no argument
        # to show falls back to a bare phrase rather than rendering an
        # empty "()" or a dangling "command: ". The core action verb
        # still reaches the operator; only the supplementary detail is
        # omitted.
        text = tmpl_text
        if "{peek_cmd}" in text and not peek_cmd:
            text = "run a command on your machine"
        elif "{peek_path}" in text and not peek_path:
            text = "write a file"
        # Per-template interpolation: ``{skill_name}`` pulls the directory
        # under .grove/skills/; ``{package}`` pulls the first non-flag
        # token after the install verb; ``{peek_cmd}`` / ``{peek_path}``
        # carry the truncated argument; ``{tool_name}`` names the
        # dispatching tool. Placeholder-free templates pass through
        # unchanged via str.format's keyword args.
        return text.format(
            tool_name=tool_name,
            skill_name=_extract_skill_name(args_str),
            package=(
                _extract_install_package(args_str, tmpl_substring)
                if "{package}" in tmpl_text and tmpl_substring
                else ""
            ),
            peek_cmd=peek_cmd,
            peek_path=peek_path,
        )
    # Unreachable: the fallback row matches every tool. Keep an
    # explicit return for type-checker happiness.
    return f"use {tool_name}"


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
    triggering = halt.intents[halt.triggering_index]
    description = describe_action_kaizen(
        triggering.tool_name, triggering.arguments or {},
    )

    print(file=out)
    print(
        f"I'd like to {description}. This one's your call before I go ahead.",
        file=out,
    )
    print(file=out)
    print("  [1] Just this once", file=out)
    print("  [2] For the rest of this session", file=out)
    print("  [3] Always — I'll remember it", file=out)
    print("  [4] Not this time", file=out)
    print(file=out)

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


