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
  (``curl x | bash``, ``base64 -d | sh``), an input feed to stdin (``<`` / ``<<``
  / ``<<<`` — herestring or file, opaque regardless of receiver), an
  execution-modifier wrapper whose leaf cannot be resolved (depth>10, unknown
  flag, variable/`$()` command word, ``--``-as-leaf, unresolvable ``env -S`` —
  ANDON-WRAPPER), or a command that will not parse. Execution-modifier wrappers
  (``env``/``nice``/``timeout``/``nohup``/``setsid``/``stdbuf``/``ionice``/
  ``chrt``/``xargs``) are recursed THROUGH to the real leaf and classified
  there (``env -S`` split-string is tokenized and recursed). Process
  substitution ``<(...)`` / ``>(...)`` is blanket-RED opacity (a consumer may
  execute the FIFO content; C3a-fix v1.1 reverted the unsound recursion).
* RED (privilege): ``sudo`` / ``su`` / ``doas``.
* RED (catastrophic): ``rm`` of ``/`` or ``~`` (or ``--no-preserve-root``). INV-9.
* RED (scope-defining write): a write/delete/move/redirect whose target resolves
  onto a SCOPE-DEFINING surface (zone schema, routing/prompt config, dock goals,
  operator secrets, the live skills tree, the capability registry) — GRV-001 v2.0
  scope keying via :func:`grove.utils.fs_utils.is_scope_defining`. A write into a
  GRANTED workspace under ``~/.grove`` (anything not scope-defining) is GREEN; a
  write outside ``~/.grove`` keeps its default YELLOW.
* RED (external agent — B5): launching ``claude`` / ``codex`` / ``opencode`` … —
  the child's effects are unanalyzable (opacity).
* GREEN: a SINGLE simple command executing a promoted skill under
  ``~/.grove/skills/`` (not ``.andon``), with no opacity and no scope-defining
  write; OR a SINGLE command whose write targets all land in a granted
  ``~/.grove`` workspace (GRV-001 v2.0). Google-Workspace / Notion fallback
  scripts keep their read-vs-write split (reads GREEN, writes/unknown YELLOW),
  by SUBCOMMAND on the parsed argv.
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
    if kind == "commandsubstitution":
        # $(...) / backticks — output becomes data/argv; the ultimate effect is
        # not statically resolvable. Fail closed (RED), do not descend.
        ctx.cmdsub = True
        return
    if kind == "processsubstitution":
        # <(...) / >(...) — blanket RED opacity (C3a-fix revert).
        #
        # C3a recursed into .command and classified the INNER command's static
        # effect. That is UNSOUND when the consumer EXECUTES the FIFO: the
        # runtime payload is the inner command's *stdout*, not its
        # classification — bash <(echo "rm -rf ~") runs "rm -rf ~" though
        # `echo` is benign; tee >(sh) feeds sh whatever is teed. Fail closed.
        # (A fail-closed data-only-consumer allowlist — diff/comm/paste/join,
        # exec-capable consumers excluded — is deferred, not in v1.1.)
        ctx.cmdsub = True
        return
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


# Input-feed redirect types: the fed content is invisible to static analysis.
_INPUT_FEED_REDIRECTS = frozenset({"<", "<<", "<<<"})


def _extract_command(node: object) -> Tuple[List[str], List[str], bool]:
    """Return (argv words, output-redirect targets, has_input_feed) for one
    CommandNode.

    Output redirects (``>`` / ``>>`` / ``&>`` …) yield target words for the
    governed-write check. Input-feed redirects (``<`` / ``<<`` / ``<<<``) set
    *has_input_feed* — a herestring or file fed to stdin is opaque (C3a),
    classified RED regardless of receiver (no allowlist).
    """
    argv: List[str] = []
    redirects: List[str] = []
    has_input_feed = False
    for part in getattr(node, "parts", []) or []:
        pkind = getattr(part, "kind", None)
        if pkind == "word":
            argv.append(part.word)
        elif pkind == "redirect":
            rtype = getattr(part, "type", None)
            if rtype in _INPUT_FEED_REDIRECTS:
                has_input_feed = True
                continue
            out = getattr(part, "output", None)
            word = getattr(out, "word", None) if out is not None else None
            if word:
                redirects.append(word)
    return argv, redirects, has_input_feed


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


# ── Execution-modifier wrappers (C3a) ────────────────────────────────────────
# A wrapper word is not itself the effecting command — it launches a leaf. The
# classifier MUST recurse to the leaf and classify THERE; bubbling a wrapper's
# own (benign) classification silently passes the leaf's effect. Privilege
# wrappers (sudo/su/doas/pkexec) stay terminal-RED via _PRIV — not recursable.
#
# Strict fail-closed arity (operator-ruled): only each wrapper's KNOWN flags are
# handled. Any unrecognized flag, missing operand, or non-literal command word
# → RED opacity + ANDON-WRAPPER. No generic skip-flags loop.
MAX_WRAPPER_DEPTH = 10

# Per-wrapper flag spec:
#   bool      : flags taking no argument
#   arg_short : short flags taking an argument (attachable, e.g. -n10 / -uNAME)
#   arg_long  : long flags taking an argument (space- or =-separated)
#   red       : flags that force RED (semantics we will not statically model)
#   pos       : count of leading wrapper-positionals BEFORE the command
#               (timeout DURATION, chrt PRIORITY)
#   neg_int   : wrapper accepts a bare -NUM adjustment (nice)
#   no_operand_ok : a missing command operand is benign (xargs → echo)
_WRAPPER_SPEC = {
    "env": {
        # -S / --split-string is NOT red here — it is tokenized and recursed
        # in _strip_wrapper (env -S is an execution vector; its split-string IS
        # the command). Unresolvable split-string → RED + ANDON-WRAPPER there.
        "bool": {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"},
        "arg_short": {"-u", "-C"}, "arg_long": {"--unset", "--chdir"},
        "red": {"-P", "--argv0", "-a"},
        "pos": 0,
    },
    "nice": {
        "bool": set(), "arg_short": {"-n"}, "arg_long": {"--adjustment"},
        "red": set(), "pos": 0, "neg_int": True,
    },
    "timeout": {
        "bool": {"--preserve-status", "--foreground", "-v", "--verbose"},
        "arg_short": {"-s", "-k"}, "arg_long": {"--signal", "--kill-after"},
        "red": set(), "pos": 1,
    },
    "nohup": {"bool": set(), "arg_short": set(), "arg_long": set(), "red": set(), "pos": 0},
    "setsid": {
        "bool": {"-f", "--fork", "-w", "--wait", "-c", "--ctty"},
        "arg_short": set(), "arg_long": set(), "red": set(), "pos": 0,
    },
    "stdbuf": {
        "bool": set(), "arg_short": {"-i", "-o", "-e"},
        "arg_long": {"--input", "--output", "--error"}, "red": set(), "pos": 0,
    },
    "ionice": {
        "bool": {"-t"}, "arg_short": {"-c", "-n"}, "arg_long": {"--class", "--classdata"},
        "red": {"-p", "--pid", "-P", "-u", "--uid"}, "pos": 0,
    },
    "chrt": {
        "bool": {"-b", "-f", "-i", "-o", "-r", "-R", "-d", "-m"},
        "arg_short": set(), "arg_long": set(),
        "red": {"-p", "--pid", "-a", "--all-tasks", "-v", "--verbose"}, "pos": 1,
    },
    "xargs": {
        "bool": {
            "-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose",
            "-x", "--exit", "-p", "--interactive", "-o", "--open-tty",
        },
        "arg_short": {"-n", "-P", "-I", "-i", "-d", "-s", "-L", "-l", "-E", "-e", "-a"},
        "arg_long": {
            "--max-args", "--max-procs", "--replace", "--delimiter",
            "--max-chars", "--max-lines", "--eof", "--arg-file",
        },
        "red": set(), "pos": 0, "no_operand_ok": True,
    },
}
_WRAPPERS = frozenset(_WRAPPER_SPEC)


def _is_literal_command_word(tok: str) -> bool:
    """True iff *tok* is a concrete command word — not a variable / substitution
    / computed target the AST cannot resolve to a leaf."""
    return bool(tok) and "$" not in tok and "`" not in tok and not tok.startswith(("<(", ">("))


def _strip_wrapper(exe: str, rest: List[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Strip *exe*'s KNOWN flags (+ leading wrapper-positionals) from *rest* and
    return (operand_argv, None), or (None, red_signature) on strict-arity
    failure. Operand is the leaf command to recurse into."""
    spec = _WRAPPER_SPEC[exe]
    i, n = 0, len(rest)
    while i < n:
        tok = rest[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-") or tok == "-":
            if exe == "env" and tok == "-":  # env: "-" == --ignore-environment
                i += 1
                continue
            break  # positional region begins
        if spec.get("neg_int") and len(tok) > 1 and tok[1:].lstrip("-").isdigit():
            i += 1  # nice -10
            continue
        # env -S "<string>" — the split-string IS the command. Tokenize it and
        # return as the operand to recurse. Any failure → RED + ANDON-WRAPPER.
        if exe == "env" and (
            tok == "-S" or tok == "--split-string"
            or tok.startswith("-S") or tok.startswith("--split-string=")
        ):
            import shlex
            if tok.startswith("--split-string="):
                val = tok.split("=", 1)[1]
            elif tok in ("-S", "--split-string"):
                if i + 1 >= n:
                    return None, "opacity:wrapper-no-operand:env"
                val = rest[i + 1]
            else:  # attached short form: -S<string>
                val = tok[2:]
            try:
                split = shlex.split(val)
            except ValueError:
                return None, "opacity:wrapper-flag:env"  # unresolvable split-string
            if not split:
                return None, "opacity:wrapper-no-operand:env"
            return split, None
        if tok in spec["red"]:
            return None, f"opacity:wrapper-flag:{exe}"
        if tok.startswith("--") and "=" in tok:
            base = tok.split("=", 1)[0]
            if base in spec["arg_long"]:
                i += 1
                continue
            return None, f"opacity:wrapper-flag:{exe}"
        if tok in spec["arg_long"]:
            i += 2
            continue
        if tok in spec["bool"]:
            i += 1
            continue
        short = tok[:2]
        if short in spec["arg_short"]:
            i += 1 if len(tok) > 2 else 2  # attached -n10 vs separate -n 10
            continue
        return None, f"opacity:wrapper-flag:{exe}"
    if exe == "env":
        while i < n and _is_assignment(rest[i]):
            i += 1
    for _ in range(spec["pos"]):  # consume DURATION / PRIORITY
        if i >= n:
            return None, f"opacity:wrapper-no-operand:{exe}"
        i += 1
    # POSIX end-of-options `--` can appear AFTER assignments / the duration /
    # priority — not only inside the flag loop. Strip one here (position-
    # independent) so it is never mistaken for the leaf command word
    # (env A=1 -- sh -c …, timeout 5 -- sh -c …). Each wrapper level strips its
    # own single `--`; a `--` that is a genuine arg to the resolved leaf is not
    # at the operand head and is left intact.
    if i < n and rest[i] == "--":
        i += 1
    operand = rest[i:]
    if not operand:
        if spec.get("no_operand_ok"):
            return ["echo"], None  # xargs with no command → echo (benign)
        return None, f"opacity:wrapper-no-operand:{exe}"
    return operand, None


def _resolve_leaf(
    argv: List[str], depth: int = 0, dynamic: bool = False,
) -> Tuple[Optional[List[str]], Optional[str], bool]:
    """Recurse execution-modifier wrappers to the ultimate leaf argv.

    Returns (leaf_argv, None, dynamic) or (None, red_signature, dynamic). The
    *dynamic* flag rides along — set once an ``xargs`` is crossed (the leaf's
    operands then come from stdin, not statically bounded). NEVER returns the
    wrapper's own classification.
    """
    if depth > MAX_WRAPPER_DEPTH:
        return None, "opacity:wrapper-depth", dynamic
    if not argv:
        return None, "opacity:wrapper-empty", dynamic
    exe = _basename(argv[0])
    if exe not in _WRAPPERS:
        return argv, None, dynamic  # leaf reached
    operand, err = _strip_wrapper(exe, argv[1:])
    if err is not None:
        return None, err, dynamic
    if exe == "xargs":
        dynamic = True
    if not _is_literal_command_word(operand[0]):
        return None, "opacity:wrapper-dynamic", dynamic
    return _resolve_leaf(operand, depth + 1, dynamic)


def _is_andon_wrapper_sig(sig: str) -> bool:
    return sig.startswith("opacity:wrapper-")


# ── Governed / catastrophic / skills helpers ─────────────────────────────────


def _classify_write_zone(target_path: str) -> str:
    """workspace-governance-unification-v1 — positive-allowlist zone for a shell
    WRITE target.

    * scope-defining surface (``is_scope_defining``)  -> RED (non-grantable)
    * operator-granted workspace (``is_granted_workspace``) -> GREEN (autonomous)
    * under ``~/.grove`` but NOT granted              -> RED (fail-closed: this
      protects substrate/secrets/tokens and closes the credential-overwrite path
      that v2's blanket-GREEN complement left open)
    * outside ``~/.grove``                            -> YELLOW (four-choice grant)

    Used by the per-node classifier for redirect / mutator / find WRITE targets
    ONLY — read operands are never promoted to GREEN, so e.g. ``cat ~/.grove/.env``
    stays YELLOW.
    """
    from grove.utils.fs_utils import is_granted_workspace, is_scope_defining
    from hermes_constants import get_hermes_home

    if is_scope_defining(target_path):
        return _RED
    if is_granted_workspace(target_path):
        return _GREEN
    try:
        resolved = os.path.realpath(os.path.expanduser(target_path))
        grove = os.path.realpath(get_hermes_home())
    except (OSError, ValueError):
        return _RED  # unresolvable → fail closed
    if resolved == grove or resolved.startswith(grove + os.sep):
        return _RED  # under ~/.grove but un-granted → fail-closed RED
    return _YELLOW


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


def _classify_find(rest: List[str]) -> Tuple[str, str]:
    """Classify a ``find`` invocation by its REAL effect (C3a).

    Action-flag-keyed: ``-delete`` → fs-mutation (catastrophic + governed check
    on the search roots, like ``rm``); ``-exec``/``-execdir``/``-ok``/``-okdir``
    → recurse into the executed command AND check the search roots; pure filters
    (``-name``/``-print``/``-type`` …) → read (not RED).
    """
    # Search roots are the leading non-flag operands; the expression follows.
    paths: List[str] = []
    i = 0
    while i < len(rest) and not rest[i].startswith("-"):
        paths.append(rest[i])
        i += 1
    expr = rest[i:]

    mutating = False
    exec_red: Optional[str] = None
    j = 0
    while j < len(expr):
        tok = expr[j]
        if tok in ("-exec", "-execdir", "-ok", "-okdir"):
            mutating = True
            j += 1
            cmd: List[str] = []
            while j < len(expr) and expr[j] not in (";", "+", "\\;"):
                cmd.append(expr[j])
                j += 1
            j += 1  # skip the ';' / '+' terminator
            if cmd:
                # The executed command runs once per matched file — a dynamic,
                # stdin-equivalent fileset. dynamic_targets=True so an fs-mutator
                # leaf (rm/mv/…) → RED (mutation:dynamic), while echo/grep/cat →
                # benign. -ok/-okdir execute like -exec (with a prompt) — same
                # hostility, same recursion.
                z, s = _classify_argv(cmd, 0, [], dynamic_targets=True)
                if z == _RED and exec_red is None:
                    exec_red = s
            continue
        if tok == "-delete":
            mutating = True
        j += 1

    if mutating:
        if _is_catastrophic_rm(paths):
            return (_RED, "rm:catastrophic")
        for p in paths:
            if _classify_write_zone(p) == _RED:
                return (_RED, "govwrite:find")
        if exec_red is not None:
            return (_RED, exec_red)
        # Bounded mutation on non-governed paths → operator-gated, like rm file.
        sig = "argv:" + hashlib.sha1(
            json.dumps(["find"] + rest, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return (_YELLOW, f"cmd:find:{sig}")

    # Filters only → read.
    sig = "argv:" + hashlib.sha1(
        json.dumps(["find"] + rest, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return (_YELLOW, f"cmd:find:{sig}")


def _classify_node(node: object, pipe_stage: int) -> Tuple[str, str]:
    """Classify ONE command node → (zone, effect-signature)."""
    argv_raw, redirects, has_input_feed = _extract_command(node)
    # Input-stream opacity: a herestring / file fed to stdin is invisible to
    # static analysis → RED, regardless of receiver (no allowlist — awk
    # system(), sed `e` would re-open the bypass on the receiving side).
    if has_input_feed:
        return (_RED, "opacity:input-redirect")
    return _classify_argv(argv_raw, pipe_stage, redirects)


def _classify_argv(
    argv_raw: List[str], pipe_stage: int, redirects: List[str],
    dynamic_targets: bool = False,
) -> Tuple[str, str]:
    """Classify a leaf argv (post-extraction). Recurses execution-modifier
    wrappers to the real leaf before classifying. *dynamic_targets* is True when
    the operands arrive from stdin (xargs) and so are not statically bounded."""
    argv = _strip_env(argv_raw)
    if not argv:
        return (_YELLOW, "empty")

    # Wrapper recursion → resolve to the ultimate leaf (env/nice/timeout/xargs …).
    leaf, red_sig, dynamic_targets = _resolve_leaf(argv, dynamic=dynamic_targets)
    if red_sig is not None:
        return (_RED, red_sig)  # ANDON-WRAPPER (depth / flag / dynamic / no-operand)
    argv = leaf

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

    # Scope-defining redirect target → RED (short-circuit). Granted-workspace and
    # outside-tree redirects are resolved with the other write targets at the
    # GREEN/YELLOW decision below — a GREEN redirect must NOT mask a later RED
    # (e.g. ``python -c '...' > ~/.grove/research/x``).
    for r in redirects:
        if _classify_write_zone(r) == _RED:
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

    # find — classify by its action flags (real effect), not blanket-mutator.
    if exe == "find":
        return _classify_find(rest)

    # Catastrophic delete.
    if exe == "rm" and _is_catastrophic_rm(rest):
        return (_RED, "rm:catastrophic")

    # Filesystem mutators.
    if exe in _FS_MUTATORS:
        # Targets fed from stdin (xargs rm / xargs mv) are unbounded and not
        # statically resolvable → fail closed (RED). xargs echo stays benign.
        if dynamic_targets:
            return (_RED, f"mutation:dynamic:{exe}")
        for t in _positionals(rest):
            if _classify_write_zone(t) == _RED:
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

    # GRV-001 v2.0 granted-workspace GREEN: a command whose WRITE targets all
    # resolve into a ~/.grove granted workspace (none scope-defining — those
    # returned RED above; none outside the tree) is autonomous. WRITE targets are
    # FS-mutator positionals and output redirects ONLY; read operands never count
    # (so ``cat ~/.grove/.env`` stays YELLOW, never GREEN).
    write_targets: List[str] = list(redirects)
    if exe in _FS_MUTATORS:
        write_targets.extend(_positionals(rest))
    if write_targets and all(
        _classify_write_zone(t) == _GREEN for t in write_targets
    ):
        return (_GREEN, f"workspace:{exe}")

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
            "Command or process substitution ($(...) / backticks / <(...) / "
            ">(...)) — the ultimate payload is not statically resolvable (a "
            "consumer may EXECUTE the FIFO content); refusing (RED).",
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
        # Surface ANDON-WRAPPER when the RED came from an unresolvable wrapper
        # operand or exceeded recursion depth (discovery-gate condition).
        andon = " [ANDON-WRAPPER]" if red_reason and _is_andon_wrapper_sig(red_reason) else ""
        return _result(
            _RED, f"shell.effect.red ({red_reason}){andon}",
            f"A command effect requires sovereign approval: {red_reason}.{andon}",
            signature,
        )
    # GREEN only for a SINGLE simple promoted-skill/read command with no other
    # effecting node. Any chaining or extra effect drops to YELLOW.
    if worst == _GREEN and len(ctx.commands) == 1:
        return _result(
            _GREEN, "shell.effect.green",
            "Promoted-skill / read execution, or a granted-workspace write.",
            signature,
        )
    # A quarantined (.andon) skill execution carries a ".andon" matched_rule so
    # the Dispatcher's quarantine try-before-promote flow fires (it keys on
    # ".andon" in the matched_rule).
    rule = "shell.effect.default"
    reason = "Operator approval required."
    if any(s.startswith("skills.andon") for s in sigs):
        rule = "shell.effect.quarantine (.andon)"
        reason = "Quarantined (.andon) skill execution — try-before-promote gate."
    return _result(_YELLOW, rule, reason, signature)
