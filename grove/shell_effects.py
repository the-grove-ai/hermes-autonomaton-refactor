"""Bash-AST shell-effect classifier — GRV-010 C1a (conformance-shell-containment).

Replaces the regex ``tool_zones.terminal.rules`` (B1/B2: substring matching over a
shell string is not a sound gate — a comment suffix or a leading ``.*`` smuggled
arbitrary commands to GREEN, and chaining slipped past). This module parses the
command to a ``bashlex`` AST and classifies by EFFECT — the real command nodes,
their verbs, targets, and redirects — so string tricks have nothing to grab.

Classification (most-restrictive-wins across all command nodes in a chain/pipe):

* RED (opacity — fail closed): the AST cannot statically resolve the payload —
  ``sh -c`` / ``bash -c``, ``eval``/``source``, ``python -c`` / ``perl -e`` …,
  command substitution ``$(...)`` / backticks, any pipe INTO a shell/interpreter
  (``curl x | bash``, ``base64 -d | sh``), or a command that will not parse.
* RED (privilege): ``sudo`` / ``su`` / ``doas``.
* RED (catastrophic): ``rm`` of ``/`` or ``~`` (or ``--no-preserve-root``). INV-9.
* RED (governed write): a write/delete/move/redirect whose target resolves into
  the ``~/.grove`` governance tree — deferred to :func:`grove.utils.fs_utils.is_governed_path`
  (NOT reimplemented here).
* RED (external agent — B5): launching ``claude`` / ``codex`` / ``opencode`` … —
  the child's effects are unanalyzable (opacity).
* GREEN: a SINGLE simple command executing a promoted skill under
  ``~/.grove/skills/`` (not ``.andon``), with no opacity and no governed write.
  Google-Workspace / Notion fallback scripts keep their read-vs-write split
  (reads GREEN, writes/unknown YELLOW), by SUBCOMMAND on the parsed argv.
* YELLOW: everything else — the operator approves at Stage 04.

The returned ``ZoneResult.pattern_key`` is an AST-derived EFFECT SIGNATURE (not a
hash of the raw string), so the approval cache keys on the effect: a re-approved
entry cannot smuggle a different effect (B3). Comments/whitespace/quoting that do
not change the parsed argv collapse to the same signature.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from typing import List, Optional, Tuple

from grove.zones import ZoneResult

# ── Effecting verb sets ──────────────────────────────────────────────────────
_PRIV = frozenset({"sudo", "su", "doas", "pkexec"})
_SHELL_INTERP = frozenset({"sh", "bash", "zsh", "ksh", "dash", "ash", "fish", "csh", "tcsh"})
_CODE_INTERP = frozenset({"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php", "Rscript", "deno", "bun"})
_EVAL_BUILTINS = frozenset({"eval", "exec", "source", "."})
# B5 — external autonomous coding agents; their child tool loop is Grove-invisible.
_EXTERNAL_AGENTS = frozenset({"claude", "codex", "opencode", "cursor", "aider", "goose", "cline", "gemini", "amp"})
# Filesystem mutators whose path operands matter for governed-tree effects.
_FS_MUTATORS = frozenset({
    "rm", "mv", "cp", "tee", "dd", "truncate", "install", "ln", "mkdir", "rmdir",
    "touch", "chmod", "chown", "chgrp", "unlink", "shred", "rsync",
})

# Google-Workspace read subcommands (read → GREEN; everything else → YELLOW).
_GAPI_READ = frozenset({
    "gmail search", "gmail get", "gmail labels", "calendar list",
    "drive search", "drive get", "drive download", "contacts list",
    "sheets get", "docs get",
})
_NOTION_READ = frozenset({"search", "get", "query"})

_RED, _YELLOW, _GREEN = "red", "yellow", "green"
_ZONE_RANK = {_GREEN: 0, _YELLOW: 1, _RED: 2}


def _max_zone(a: str, b: str) -> str:
    return a if _ZONE_RANK[a] >= _ZONE_RANK[b] else b


def _result(zone: str, rule: str, reason: str, sig: str) -> ZoneResult:
    return ZoneResult(
        zone=zone, matched_rule=rule, source="shell_effect",
        reason=reason, pattern_key=sig,
    )


# ── AST walking ──────────────────────────────────────────────────────────────


class _Ctx:
    __slots__ = ("commands", "cmdsub")

    def __init__(self) -> None:
        self.commands: List[Tuple[object, int]] = []  # (CommandNode, pipe_stage)
        self.cmdsub = False


def _walk(node: object, ctx: _Ctx, pipe_stage: int = 0) -> None:
    kind = getattr(node, "kind", None)
    if kind == "commandsubstitution" or kind == "processsubstitution":
        ctx.cmdsub = True
        return  # opaque payload — do not descend for effect collection
    if kind == "command":
        ctx.commands.append((node, pipe_stage))
        for part in getattr(node, "parts", []) or []:
            _walk(part, ctx, pipe_stage)
        return
    if kind == "pipeline":
        stage = 0
        for part in getattr(node, "parts", []) or []:
            if getattr(part, "kind", None) == "command":
                _walk(part, ctx, pipe_stage=stage)
                stage += 1
            else:
                _walk(part, ctx, pipe_stage)
        return
    for part in getattr(node, "parts", []) or []:
        _walk(part, ctx, pipe_stage)
    out = getattr(node, "output", None)
    if out is not None:
        _walk(out, ctx, pipe_stage)


def _extract_command(node: object) -> Tuple[List[str], List[str]]:
    """Return (argv words, redirect-output targets) for one CommandNode."""
    argv: List[str] = []
    redirects: List[str] = []
    for part in getattr(node, "parts", []) or []:
        pkind = getattr(part, "kind", None)
        if pkind == "word":
            argv.append(part.word)
        elif pkind == "redirect":
            out = getattr(part, "output", None)
            word = getattr(out, "word", None) if out is not None else None
            if word:
                redirects.append(word)
    return argv, redirects


def _is_assignment(token: str) -> bool:
    eq = token.find("=")
    if eq <= 0:
        return False
    name = token[:eq]
    return name[0].isalpha() or name[0] == "_" and all(
        c.isalnum() or c == "_" for c in name
    )


def _strip_env(argv: List[str]) -> List[str]:
    i = 0
    while i < len(argv) and _is_assignment(argv[i]):
        i += 1
    return argv[i:]


def _basename(token: str) -> str:
    return posixpath.basename(token)


def _positionals(args: List[str]) -> List[str]:
    return [a for a in args if not a.startswith("-")]


# ── Governed / catastrophic / skills helpers ─────────────────────────────────


def _is_governed(token: str) -> bool:
    from grove.utils.fs_utils import is_governed_path
    return is_governed_path(token)


_CATASTROPHIC_TARGETS = frozenset({"/", "//", "/*", "~", "~/", "/.", "/*/"})


def _is_catastrophic_rm(args: List[str]) -> bool:
    if any(a == "--no-preserve-root" for a in args):
        return True
    for t in _positionals(args):
        norm = t.strip()
        if norm in _CATASTROPHIC_TARGETS:
            return True
        # ~ or ~/ exactly (home root); expanded absolute root.
        expanded = os.path.expanduser(norm)
        if expanded in ("/", os.path.expanduser("~"), os.path.expanduser("~") + "/"):
            return True
    return False


def _promoted_skill_subzone(script_token: str, rest: List[str]) -> Optional[Tuple[str, str]]:
    """If *script_token* resolves to a promoted skill script under
    ``~/.grove/skills/`` (NOT ``.andon``), return (zone, signature). Else None.

    google_api.py / notion.py keep their read-vs-write split by subcommand; other
    promoted skills are GREEN. A ``.andon`` (quarantined) skill is NOT promoted —
    return None so it falls through to YELLOW (the operator's try-before-promote
    gate fires via the Dispatcher).
    """
    from hermes_constants import get_hermes_home

    try:
        resolved = os.path.realpath(os.path.expanduser(script_token))
        skills_root = os.path.realpath(os.path.join(os.path.realpath(get_hermes_home()), "skills"))
    except (OSError, ValueError):
        return None
    if os.path.commonpath([resolved, skills_root]) != skills_root:
        return None
    andon_root = os.path.join(skills_root, ".andon")
    if resolved == andon_root or os.path.commonpath([resolved, andon_root]) == andon_root:
        # Quarantined (not promoted) — YELLOW try-before-promote gate. The
        # ".andon" marker in the signature lets the Dispatcher's
        # _maybe_flag_quarantine_execution fire the post-execution promotion
        # prompt (it keys on ".andon" in the matched_rule).
        return (_YELLOW, "skills.andon")
    base = _basename(resolved)
    pos = _positionals(rest)
    if base == "google_api.py":
        two = " ".join(pos[:2])
        one = pos[0] if pos else ""
        if two in _GAPI_READ or one in {"contacts", "calendar"} and two in _GAPI_READ:
            return (_GREEN, f"skills:google_api.py:{two}")
        return (_YELLOW, f"skills:google_api.py:{two or one}")
    if base == "notion.py":
        sub = pos[0] if pos else ""
        if sub in _NOTION_READ:
            return (_GREEN, f"skills:notion.py:{sub}")
        return (_YELLOW, f"skills:notion.py:{sub}")
    if base == "gws":
        return (_YELLOW, "skills:gws")  # external CLI grammar not enumerated
    return (_GREEN, f"skills:{base}")


# ── Per-node classification ──────────────────────────────────────────────────


def _classify_node(node: object, pipe_stage: int) -> Tuple[str, str]:
    """Classify ONE command node → (zone, effect-signature)."""
    argv_raw, redirects = _extract_command(node)
    argv = _strip_env(argv_raw)
    if not argv:
        return (_YELLOW, "empty")

    full0 = argv[0]
    exe = _basename(full0)
    rest = argv[1:]

    # Privilege escalation.
    if exe in _PRIV:
        return (_RED, f"priv:{exe}")
    # External coding agents (B5).
    if exe in _EXTERNAL_AGENTS:
        return (_RED, f"external:{exe}")
    # eval / source — opaque.
    if exe in _EVAL_BUILTINS:
        return (_RED, f"opacity:{exe}")

    # Any redirect into a governed tree.
    for r in redirects:
        if _is_governed(r):
            return (_RED, "govwrite:redirect")

    # Shell interpreters: -c is opacity; being a pipe target is opacity
    # (``... | bash`` executes piped script). A shell running a script FILE
    # falls through to the script-path logic below.
    if exe in _SHELL_INTERP:
        if any(a == "-c" or a.startswith("-c") for a in rest):
            return (_RED, f"opacity:{exe}-c")
        if pipe_stage > 0:
            return (_RED, f"opacity:pipe-into-{exe}")
    # Code interpreters: -c / -e inline is opacity; pipe target is opacity.
    if exe in _CODE_INTERP:
        if any(a in ("-c", "-e") or a.startswith(("-c", "-e")) for a in rest):
            return (_RED, f"opacity:{exe}-c")
        if pipe_stage > 0:
            return (_RED, f"opacity:pipe-into-{exe}")

    # Catastrophic delete.
    if exe == "rm" and _is_catastrophic_rm(rest):
        return (_RED, "rm:catastrophic")

    # Filesystem mutators into a governed tree.
    if exe in _FS_MUTATORS:
        for t in _positionals(rest):
            if _is_governed(t):
                return (_RED, f"govwrite:{exe}")

    # Promoted-skill execution (GREEN/YELLOW by script).
    #   direct:        ~/.grove/skills/foo/run.py [args]
    #   via interp:    python3 ~/.grove/skills/foo/run.py [args]
    script_token, script_rest = None, []
    if "/" in full0 or full0.startswith("~"):
        script_token, script_rest = full0, rest
    elif exe in _SHELL_INTERP or exe in _CODE_INTERP:
        pos = _positionals(rest)
        if pos:
            script_token = pos[0]
            script_rest = rest[rest.index(pos[0]) + 1:]
    if script_token is not None:
        sub = _promoted_skill_subzone(script_token, script_rest)
        if sub is not None:
            return sub

    # Default: a parsed-argv-derived signature (comment/whitespace immune).
    sig = "argv:" + hashlib.sha1(
        json.dumps(argv, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return (_YELLOW, f"cmd:{exe}:{sig}")


# ── Public entrypoint ────────────────────────────────────────────────────────


def classify_shell_effect(command: str) -> ZoneResult:
    """Classify a shell command by EFFECT via its bash AST. See module docstring."""
    import bashlex

    if not command or not command.strip():
        return _result(_YELLOW, "shell.empty", "Empty command.", "empty")

    try:
        trees = bashlex.parse(command)
    except Exception:
        # The AST cannot see the execution tree → the command does not run.
        return _result(
            _RED, "shell.opacity.unparseable",
            "Command could not be parsed into an analyzable AST — refusing "
            "(fail-closed: an unanalyzable effect does not run).",
            "opacity:unparseable",
        )

    ctx = _Ctx()
    for tree in trees:
        _walk(tree, ctx)

    if ctx.cmdsub:
        return _result(
            _RED, "shell.opacity.substitution",
            "Command/process substitution ($(...), backticks, <(...)) — the "
            "ultimate payload is not statically resolvable; refusing (RED).",
            "opacity:substitution",
        )

    if not ctx.commands:
        return _result(_YELLOW, "shell.effect.default", "No command node parsed.", "empty")

    worst = _GREEN
    sigs: List[str] = []
    red_reason: Optional[str] = None
    for node, stage in ctx.commands:
        zone, sig = _classify_node(node, stage)
        sigs.append(sig)
        if zone == _RED and red_reason is None:
            red_reason = sig
        worst = _max_zone(worst, zone)

    signature = "||".join(sorted(sigs))

    if worst == _RED:
        return _result(
            _RED, f"shell.effect.red ({red_reason})",
            f"A command effect requires sovereign approval: {red_reason}.",
            signature,
        )
    # GREEN only for a SINGLE simple promoted-skill/read command with no other
    # effecting node. Any chaining or extra effect drops to YELLOW.
    if worst == _GREEN and len(ctx.commands) == 1:
        return _result(_GREEN, "shell.effect.green", "Promoted-skill / read execution.", signature)
    # A quarantined (.andon) skill execution carries a ".andon" matched_rule so
    # the Dispatcher's quarantine try-before-promote flow fires (it keys on
    # ".andon" in the matched_rule).
    rule = "shell.effect.default"
    reason = "Operator approval required."
    if any(s.startswith("skills.andon") for s in sigs):
        rule = "shell.effect.quarantine (.andon)"
        reason = "Quarantined (.andon) skill execution — try-before-promote gate."
    return _result(_YELLOW, rule, reason, signature)
