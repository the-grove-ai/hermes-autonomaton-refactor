"""Shared action-fact layer (kaizen-voice Sprint B1).

The OBJECTIVE fact formatter — :func:`describe_action_kaizen`, "WHAT the agent
is trying to do" — extracted here so BOTH operator-facing surfaces can render
the same fact without sharing TONE:

* the RED / Yellow halt surface (:mod:`grove.halt_renderer`), and
* the proposal-offering surface (:mod:`grove.flywheel_cli`).

§VI lock: the fact is shared; the register is not. The hard-boundary-interrupt
voice of a halt and the helpful-suggestion voice of a proposal stay in their own
modules — this layer carries ONLY the plain-language description of the action,
never the framing around it.

This module is dependency-free of the halt / sovereign-prompt surfaces (it imports
no ``grove`` module), which is what lets :mod:`grove.halt_renderer` import it after
Sprint B1 folds the Yellow prompt text into the renderer — a fold that would
otherwise cycle (``halt_renderer`` ← ``sovereign_prompt_handlers`` ←
``halt_renderer``). :mod:`grove.sovereign_prompt_handlers` re-exports every symbol
below for byte-identical backward compatibility with its prior callers.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple


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
    # Sprint 62 — loading a quarantined skill via skill_view is the operator's
    # "try it" moment; render it as a skill run, not a generic tool use.
    ("skill_view",   None,               "run the {skill_name} skill"),
    ("execute_code", None,               "run a Python script ({peek_code})"),
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


# S0 — MCP tools are registered as ``mcp_{server}_{tool}`` with single-underscore
# separators (tools/mcp_tool.py). The Kaizen template table matches a tool name
# EXACTLY or a substring of the arguments, so it cannot prefix-match the dynamic
# ``mcp_*`` namespace — these are described by the helper below instead. Known
# (server, action) pairs get a concierge-register phrase; everything else falls
# back to a generic, still-specific "use the {server} tool ({action})".
_MCP_KAIZEN_PHRASES: Dict[Tuple[str, str], str] = {
    # Hosted Notion MCP (Sprint 69). Tools register as
    # ``mcp_notion_notion_<op>``; ``_describe_mcp_kaizen`` splits on the
    # FIRST underscore, so the action key carries the leading ``notion_``.
    ("notion", "notion_search"): "search your Notion workspace",
    ("notion", "notion_fetch"): "fetch a page from Notion",
    ("notion", "notion_create_pages"): "create a page in Notion",
    ("notion", "notion_update_page"): "update a page in Notion",
}


def _describe_mcp_kaizen(tool_name: str) -> str:
    """Plain-language Kaizen description for an MCP tool call (S0).

    MCP tools arrive as ``mcp_{server}_{tool}`` (single-underscore
    separators, components sanitized so hyphens become underscores).
    Best-effort split: strip the ``mcp_`` prefix, then split on the FIRST
    underscore into ``server`` and ``action``. This is exact for
    single-word server names (e.g. ``notion`` — the only configured MCP
    server today). A multi-word server name (``google_drive``) would
    mis-split, leaving part of the server in ``action`` — the headline
    still reaches the operator, and known servers are covered by the phrase
    map. Config-lookup disambiguation was considered and deliberately
    deferred to keep this a string-only renderer with no config I/O.
    """
    remainder = tool_name[len("mcp_"):]
    server, sep, action = remainder.partition("_")
    if not sep:
        # No action segment (e.g. a bare ``mcp_notion``) — name what we have.
        server, action = remainder, ""
    phrase = _MCP_KAIZEN_PHRASES.get((server, action))
    if phrase:
        return phrase
    if action:
        return f"use the {server} tool ({action})"
    return f"use the {server} tool"


def describe_action_kaizen(tool_name: str, arguments: dict) -> str:
    """Render a Kaizen-register plain-language description of the action.

    Used by :func:`grove.halt_renderer.render_yellow_sovereign_prompt` to build
    the prompt's header line (and by the Dispatcher's batch / gateway INFO log
    lines so they carry the same description for telemetry parity).

    Sprint 32.2 — the raw stringified arguments are passed through
    :func:`normalize_command` before substring matching so a skill
    invocation written as ``${HOME}/.grove/skills/<name>/...`` is
    matched against the skill template instead of falling through to
    the generic "run a command on your machine" row.

    Sprint S0 — MCP tool calls (``mcp_{server}_{tool}``) are rendered by
    :func:`_describe_mcp_kaizen` before the template walk, since the table
    matches on exact tool name or arguments substring and cannot prefix-match
    the dynamic ``mcp_*`` namespace.
    """
    if tool_name.startswith("mcp_"):
        return _describe_mcp_kaizen(tool_name)

    raw_args_str = str(dict(arguments)) if arguments else ""
    args_str = normalize_command(raw_args_str)
    # Hotfix 62.2 — substring template matching must not see heredoc/script
    # bodies. A `python3 - << 'PY' … PY` body can contain words like "rm -rf"
    # that false-trip the destructive-op rows. Match only the command up to
    # the first heredoc delimiter; the body is excluded from evaluation.
    match_str = args_str.split("<<", 1)[0]
    # Per-argument detail for the Peek-bearing rows (Sprint 60).
    # ``command`` is the terminal tool's argument; ``path`` is
    # write_file's. Each is normalized then center-truncated so the
    # prompt shows the real thing without swamping the surface.
    args_dict = dict(arguments) if isinstance(arguments, dict) else {}
    peek_cmd = peek(normalize_command(str(args_dict.get("command", ""))))
    peek_path = peek(str(args_dict.get("path", "")))
    # S0 — execute_code carries its Python source in the ``code`` arg (no
    # ``language`` param; the tool is Python-only). Peek-truncate it for the
    # execute_code row, same graceful-degradation contract as command/path.
    peek_code = peek(str(args_dict.get("code", "")))
    # Sprint 62 — skill_view of a quarantined skill carries the skill in the
    # ``name`` arg (no .grove/skills path to extract), so resolve it directly.
    if tool_name == "skill_view":
        view_skill = str(args_dict.get("name", "")).strip() or "unknown"
    else:
        view_skill = _extract_skill_name(args_str)
    for tmpl_tool, tmpl_substring, tmpl_text in _KAIZEN_PROMPT_TEMPLATES:
        if tmpl_tool is not None and tmpl_tool != tool_name:
            continue
        if tmpl_substring is not None and tmpl_substring not in match_str:
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
        elif "{peek_code}" in text and not peek_code:
            text = "run a Python script"
        # Per-template interpolation: ``{skill_name}`` pulls the directory
        # under .grove/skills/; ``{package}`` pulls the first non-flag
        # token after the install verb; ``{peek_cmd}`` / ``{peek_path}``
        # carry the truncated argument; ``{tool_name}`` names the
        # dispatching tool. Placeholder-free templates pass through
        # unchanged via str.format's keyword args.
        return text.format(
            tool_name=tool_name,
            skill_name=view_skill,
            package=(
                _extract_install_package(args_str, tmpl_substring)
                if "{package}" in tmpl_text and tmpl_substring
                else ""
            ),
            peek_cmd=peek_cmd,
            peek_path=peek_path,
            peek_code=peek_code,
        )
    # Unreachable: the fallback row matches every tool. Keep an
    # explicit return for type-checker happiness.
    return f"use {tool_name}"
