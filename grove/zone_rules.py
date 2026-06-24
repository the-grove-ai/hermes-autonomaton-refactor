"""Pattern synthesis + zone-rule write path (Sprint 22).

Two surfaces:

* ``synthesize_pattern(command)`` — produces a conservative,
  directory-scoped regex from an example command string. Inputs come
  from the "Approve Always" choice in any of the 13 governed paths;
  outputs are written into ``zones.schema.yaml`` via
  ``save_zone_rule``. The synthesis is intentionally narrow — it
  scopes to a directory rather than the exact file (so the operator
  doesn't have to re-approve every individual file in a directory
  they trust) but never to a parent that would include destructive
  targets. A denylist refuses to synthesise patterns for tools that
  cannot be safely greenlisted under any circumstances (privilege
  escalation, root-level destruction, mass-permission changes on
  system paths).

* ``save_zone_rule(tool_id, pattern, zone, reason)`` — appends a
  rule entry to the operator's ``~/.grove/zones.schema.yaml`` and
  triggers ``ZoneClassifier.reload()`` so the new rule takes effect
  immediately. Uses ``ruamel.yaml`` for round-trip preservation of
  comments and key order — the schema is the operator's primary
  governance interface and stripping its humanity is a non-starter.
  If the synthesised pattern fails ``check_pattern_safety`` (which
  it should not, given synthesis is conservative — but defence in
  depth), the write is refused with an explanatory error.

Tool-id resolution mirrors ``grove/zones.py``'s
``_ACTION_PREFIX_TO_TOOL`` for the v0.1 ``command.execute.* →
terminal`` mapping. Future tools that want to wire Approve Always
should pass ``tool_id`` explicitly into the calling chain.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from grove import zones as _zones

logger = logging.getLogger(__name__)


# ── Denylist — verbs that can never be greenlisted ────────────────────────────
#
# These verbs always require an interactive operator decision, even if the
# operator clicks "Approve Always". The blast radius of permanent
# greenlisting on these is unbounded by their nature; the rule type does
# not exist for them in the synthesised-pattern path.
#
# Operators who genuinely need persistent allowlist entries for these
# verbs must edit ``zones.schema.yaml`` directly and accept full
# responsibility for the scope they choose. The synthesiser refuses to
# do it on their behalf.

_DENYLISTED_VERBS = frozenset({
    "sudo",
    "su",
    "doas",
    "pkexec",
})

# Denylisted shapes (full-command regex). If any of these match, refuse
# synthesis regardless of verb. Catches `rm -rf /`, `chmod 777 /` and
# similar root-level catastrophes that an operator should never be able
# to greenlist accidentally.
_DENYLISTED_COMMAND_SHAPES = (
    re.compile(r"^rm\s+(-[fir]+\s+)*-\w*r\w*\s+/(\s|$)"),   # rm -rf /
    re.compile(r"^rm\s+(-[fir]+\s+)*-\w*r\w*f\w*\s+/(\s|$)"),
    re.compile(r"^chmod\s+\d*7\d*\s+/(\s|$)"),               # chmod 777 /
    re.compile(r"^dd\s+.*of=/dev/[sh]d[a-z](\s|$)"),         # dd to raw disk
    re.compile(r"^mkfs(\.|\s+).*"),                          # mkfs.anything
    re.compile(r"^>\s*/dev/[sh]d[a-z]"),                     # > /dev/sda
)


# ── Subcommand-style verbs that take a single target argument ────────────────
#
# For these, the synthesised pattern matches the exact verb+subcommand+target
# rather than a directory glob — the target is a package or service name, not
# a filesystem path, so "scope to directory" is meaningless.

_SUBCOMMAND_VERBS = {
    "pip":    {"install", "uninstall", "download"},
    "pip3":   {"install", "uninstall", "download"},
    "npm":    {"install", "uninstall", "update"},
    "yarn":   {"add", "remove", "upgrade"},
    "apt":    {"install", "remove", "purge"},
    "apt-get": {"install", "remove", "purge"},
    "brew":   {"install", "uninstall", "upgrade"},
    "systemctl": {"start", "stop", "restart", "enable", "disable", "reload"},
    "service": {"start", "stop", "restart", "reload"},
    "docker": {"run", "exec", "pull", "build", "push", "rm", "rmi"},
    "git":    {"push", "pull", "fetch", "clone"},
}


# Sensitive system directories — directory-scoped greenlisting is
# refused for paths under these roots. Operator approves "rm
# /etc/old.conf" once → re-prompted for any other /etc file, because
# wholesale /etc greenlisting is a foot-gun. Operators who genuinely
# want broad system-path approval must edit zones.schema.yaml by hand.
_SENSITIVE_SYSTEM_PREFIXES = (
    "/etc",
    "/bin",
    "/sbin",
    "/usr",
    "/var",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/root",
    "/lib",
    "/lib64",
    "/opt",
)


# Verbs that commonly take short flags between the verb and the target.
# When synthesis sees one of these, the rule includes an optional
# ``(-[flagchars]+\s+)?`` group so the same operator-approved path
# matches across flag-bundle variations (``rm /tmp/x`` and ``rm -rf
# /tmp/x`` both hit the same rule).
_FLAG_TAKING_VERBS = frozenset({
    "rm", "rmdir", "ls", "cp", "mv", "mkdir", "ln",
    "chmod", "chown", "chgrp", "touch", "find",
    "tar", "cat", "grep", "sed", "awk", "head", "tail",
})


# Verbs whose first numeric argument is a mode/permissions value that
# should generalise to ``\d+`` rather than be pinned to the example
# octal. ``chmod 644 /x`` → matches ``chmod 755 /x`` too.
_NUMERIC_FIRST_ARG_VERBS = frozenset({"chmod", "chown", "chgrp", "umask"})


@dataclass(frozen=True)
class SynthesisResult:
    """Outcome of ``synthesize_pattern``.

    Attributes:
        ok: True when ``pattern`` is safe to write to the schema.
        pattern: the synthesised regex when ``ok``; an empty string
            otherwise.
        reason: when ``ok``, a human-readable explanation suitable
            for the schema's ``reason`` field. When not ``ok``, the
            explanation of why synthesis was refused.
    """

    ok: bool
    pattern: str
    reason: str


def _tokenize(command: str) -> list[str]:
    """Shell-aware tokenisation. Strips empty trailing tokens."""
    try:
        return [t for t in shlex.split(command) if t]
    except ValueError:
        # Unclosed quotes etc. — fall back to whitespace split. The
        # caller's safety check will reject anything weird.
        return [t for t in command.strip().split() if t]


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    """Skip past leading ``VAR=value`` env assignments to find the verb."""
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        tokens = tokens[1:]
    return tokens


def _strip_path_prefix(verb: str) -> str:
    """``/usr/bin/sudo`` → ``sudo`` so denylist checks against bare verbs."""
    if "/" in verb:
        return verb.rsplit("/", 1)[-1]
    return verb


def _first_fs_path_token(tokens: list[str]) -> Optional[tuple[int, str]]:
    """Find the first token that looks like a filesystem path.

    A path is any token starting with ``/`` (absolute), ``./`` /
    ``../`` (relative), or ``~`` (home expansion). Flag-style tokens
    (starting with ``-``) are skipped.
    """
    for idx, tok in enumerate(tokens):
        if not tok:
            continue
        if tok.startswith("-"):
            continue
        if tok.startswith("/") or tok.startswith("./") or tok.startswith("../") or tok.startswith("~"):
            return idx, tok
    return None


def _directory_of(path: str) -> Optional[str]:
    """Return the parent directory of ``path``, or ``None`` for a root path.

    A bare ``/`` or ``~`` has no narrower scope to greenlist; refuse.
    """
    p = path.rstrip("/")
    if not p or p == "/" or p == "~":
        return None
    if "/" not in p:
        return None
    parent = p.rsplit("/", 1)[0]
    if not parent:
        # Path was like ``/etc`` — parent is the root, too broad.
        return None
    return parent


def synthesize_pattern(command: str) -> SynthesisResult:
    """Produce a conservative regex from an example command string.

    Returns a ``SynthesisResult`` rather than raising — callers
    (notably the future webui Approve-Always endpoint) can surface
    refusals as operator-visible explanations rather than 5xx errors.

    Refuses when:
      * the verb is on the privilege-escalation denylist (sudo, su,
        doas, pkexec)
      * the full command matches a denylisted shape (rm -rf /,
        chmod 777 /, dd of=/dev/sda, mkfs.*, > /dev/sda)
      * the FS path is the root or has no narrower parent
      * the synthesised pattern fails ``check_pattern_safety``
        (defence-in-depth — should not happen)
    """
    if not command or not command.strip():
        return SynthesisResult(False, "", "empty command")

    tokens = _tokenize(command)
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return SynthesisResult(False, "", "no verb after env assignments")

    verb = _strip_path_prefix(tokens[0])
    verb_lc = verb.lower()
    if verb_lc in _DENYLISTED_VERBS:
        return SynthesisResult(
            False, "",
            f"`{verb}` cannot be greenlisted — privilege escalation always "
            f"requires an interactive operator decision.",
        )

    for shape_re in _DENYLISTED_COMMAND_SHAPES:
        if shape_re.search(command.strip()):
            return SynthesisResult(
                False, "",
                f"command shape matches a denylisted root-level destructive "
                f"pattern; refuse to synthesise a permanent allowlist for it.",
            )

    # Subcommand-style: verb subcommand TARGET → exact match on verb+sub+target
    if verb_lc in _SUBCOMMAND_VERBS and len(tokens) >= 3:
        sub = tokens[1].lower()
        if sub in _SUBCOMMAND_VERBS[verb_lc]:
            # Exact match for the verb+subcommand+target tuple — these
            # take package / service / branch names, not file paths.
            target = tokens[2]
            pattern = rf"^{re.escape(verb)}\s+{re.escape(sub)}\s+{re.escape(target)}$"
            ok, why = _zones.check_pattern_safety(pattern)
            if not ok:
                return SynthesisResult(False, "", f"synthesis failed safety check: {why}")
            return SynthesisResult(
                True, pattern,
                f"Exact `{verb} {sub} {target}` invocation greenlit.",
            )

    # FS-path command: scope to the parent directory + /.*
    path_info = _first_fs_path_token(tokens)
    if path_info is not None:
        idx, path = path_info
        directory = _directory_of(path)
        if directory is None:
            return SynthesisResult(
                False, "",
                f"`{path}` has no narrower scope than root; refuse to "
                f"greenlist a root-level pattern.",
            )
        # Refuse to greenlist anything under a sensitive system
        # directory — operators who want broad system-path approval
        # must edit zones.schema.yaml by hand and own the scope.
        for sensitive in _SENSITIVE_SYSTEM_PREFIXES:
            if directory == sensitive or directory.startswith(sensitive + "/"):
                return SynthesisResult(
                    False, "",
                    f"`{directory}/` is a sensitive system path; refuse "
                    f"to greenlist a directory-scoped pattern. Approve "
                    f"individual commands or edit zones.schema.yaml by "
                    f"hand if you need broader scope.",
                )
        # Build: ^<verb>\s+(<flag/mode group>\s+)?<directory>/.*
        # Reconstruct intermediate tokens (between verb and path):
        #  - short flags collapse to ``(-[chars]+\s+)?``
        #  - chmod/chown numeric modes generalise to ``\d+``
        #  - long flags or unrecognised tokens pin exactly
        # Synthesis ALWAYS emits an optional flag-group when the verb
        # is in _FLAG_TAKING_VERBS, even if the example command had no
        # flags, so ``rm /tmp/x`` and ``rm -rf /tmp/x`` both match the
        # synthesised rule.
        intermediate_tokens = tokens[1:idx]
        flag_chars: set[str] = set()
        long_flags: list[str] = []
        has_numeric_mode = False
        for tok in intermediate_tokens:
            if tok.startswith("--"):
                long_flags.append(tok)
            elif tok.startswith("-") and len(tok) > 1:
                rest = tok.lstrip("-")
                if rest.isalpha():
                    flag_chars.update(rest)
                else:
                    long_flags.append(tok)
            elif verb_lc in _NUMERIC_FIRST_ARG_VERBS and tok.isdigit():
                has_numeric_mode = True
            else:
                # Some other intermediate token (e.g. a flag value).
                # Pin it exactly so we don't accidentally widen scope.
                long_flags.append(tok)
        # Synthesise the intermediate group.
        groups: list[str] = []
        if flag_chars or verb_lc in _FLAG_TAKING_VERBS:
            # Include the operator's flags (if any) plus the default
            # flag-character set for this verb so future invocations
            # with different short-flag bundles still match.
            chars_for_group = flag_chars.copy()
            if verb_lc == "rm":
                chars_for_group.update("firRv")
            elif verb_lc == "ls":
                chars_for_group.update("laRhA")
            elif verb_lc == "cp" or verb_lc == "mv":
                chars_for_group.update("firRv")
            elif verb_lc == "mkdir":
                chars_for_group.update("pv")
            if not chars_for_group:
                chars_for_group = {"a"}  # placeholder; the group is optional
            groups.append(rf"(-[{''.join(sorted(chars_for_group))}]+\s+)?")
        if has_numeric_mode:
            groups.append(r"(\d+\s+)?")
        for lf in long_flags:
            groups.append(rf"({re.escape(lf)}\s+)?")
        intermediate = "".join(groups)
        pattern = rf"^{re.escape(verb)}\s+{intermediate}{re.escape(directory)}/.*"
        ok, why = _zones.check_pattern_safety(pattern)
        if not ok:
            return SynthesisResult(False, "", f"synthesis failed safety check: {why}")
        return SynthesisResult(
            True, pattern,
            f"`{verb}` within `{directory}/` greenlit.",
        )

    # Fallback: exact-match the full command. Conservative — operator
    # gets re-prompted for any variation. Better than synthesising
    # something too broad.
    pattern = rf"^{re.escape(command.strip())}$"
    ok, why = _zones.check_pattern_safety(pattern)
    if not ok:
        return SynthesisResult(False, "", f"synthesis failed safety check: {why}")
    return SynthesisResult(
        True, pattern,
        f"Exact `{command.strip()[:80]}` invocation greenlit.",
    )


# ── save_zone_rule — append a rule to ~/.grove/zones.schema.yaml ──────────────
#
# Round-trips with ruamel.yaml when available (preserves the schema's
# inline comments — the operator's primary governance interface).
# Falls back to pyyaml safe_dump if ruamel fails to load at runtime,
# with a loud log so the comment loss is visible.

def _schema_path() -> Path:
    """Return the operator overlay path, creating parent dir if needed."""
    overlay = Path.home() / ".grove" / "zones.autonomaton.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    return overlay


def _write_with_ruamel(path: Path, data) -> bool:
    try:
        from ruamel.yaml import YAML
    except Exception:
        return False
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    yaml_rt.width = 100
    with open(path, "w") as fh:
        yaml_rt.dump(data, fh)
    return True


def _read_with_ruamel(path: Path):
    try:
        from ruamel.yaml import YAML
    except Exception:
        return None
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    with open(path) as fh:
        return yaml_rt.load(fh)


def save_zone_rule(
    tool_id: str,
    pattern: str,
    zone: str,
    reason: str,
) -> None:
    """Append a rule to the operator's ``zones.schema.yaml``.

    The tool's ``tool_zones`` entry is normalised in place: a bare-
    string entry becomes a dict with ``default_zone`` (the original
    value) and a new ``rules`` list containing the new rule. An
    already-dict entry has the new rule appended to its existing
    rules list.

    The new rule is inserted at the END of the rules list. Operators
    who need a different ordering (a new very-specific rule that
    should beat existing patterns) must edit the YAML by hand.

    Raises:
        ValueError: zone is not green/yellow/red, or the pattern
            fails ``check_pattern_safety``, or the tool is not a
            string identifier.
    """
    if zone not in ("green", "yellow", "red"):
        raise ValueError(f"zone must be green/yellow/red; got {zone!r}")
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise ValueError(f"tool_id must be a non-empty string; got {tool_id!r}")
    safe_ok, safe_why = _zones.check_pattern_safety(pattern)
    if not safe_ok:
        raise ValueError(f"pattern rejected by safety check: {safe_why}")

    path = _schema_path()
    if not path.exists():
        # Overlay does not exist yet — seed with an empty schema dict.
        data = {"schema_version": 1, "tool_zones": {}}
    else:
        data = _read_with_ruamel(path)
        if data is None:
            # ruamel failed — fall back to pyyaml on the read side too so
            # we at least produce a valid schema; loud log makes the
            # comment loss visible.
            logger.error(
                "[zone_rules] ruamel.yaml unavailable; falling back to pyyaml. "
                "Schema comments will be stripped from %s.",
                path,
            )
            import yaml as _yaml
            with open(path) as fh:
                data = _yaml.safe_load(fh) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")

    tool_zones = data.setdefault("tool_zones", {})
    existing = tool_zones.get(tool_id)

    # Tool-id-as-pattern: synthesize_pattern returned the tool_id itself,
    # meaning this tool has no command-string classification path (not
    # terminal or similar). The operator's intent is default_zone promotion,
    # not a command-string rule. Append a rule with match_pattern==tool_id
    # would be semantically inert — the classifier never fullmatches a
    # command string against a bare tool_id.
    if pattern == tool_id:
        if isinstance(existing, dict):
            existing["default_zone"] = zone
            # Drop inert rules (match_pattern == tool_id) — clean slate.
            clean = [
                r for r in (existing.get("rules") or [])
                if isinstance(r, dict) and r.get("match_pattern") != tool_id
            ]
            if clean:
                existing["rules"] = clean
            else:
                existing.pop("rules", None)
        else:
            # Absent or bare string — replace with explicit default_zone dict.
            tool_zones[tool_id] = {"default_zone": zone}
        logger.info(
            "[zone_rules] promoted %r default_zone to %s (tool-id-as-pattern)",
            tool_id, zone,
        )
        wrote_with_ruamel = _write_with_ruamel(path, data)
        if not wrote_with_ruamel:
            import yaml as _yaml
            with open(path, "w") as fh:
                _yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
        try:
            _zones.reload()
        except RuntimeError:
            pass
        return

    if existing is None:
        # New tool entry — start with the operator's chosen zone as default.
        tool_zones[tool_id] = {
            "default_zone": "yellow",
            "rules": [
                {"match_pattern": pattern, "zone": zone, "reason": reason or ""},
            ],
        }
    elif isinstance(existing, str):
        # Normalise: convert the bare-string entry to a dict, preserving the
        # original zone as default_zone.
        tool_zones[tool_id] = {
            "default_zone": existing,
            "rules": [
                {"match_pattern": pattern, "zone": zone, "reason": reason or ""},
            ],
        }
    elif isinstance(existing, dict):
        rules = existing.setdefault("rules", [])
        # Dedup guard: skip if (match_pattern, zone) already present in rules.
        if any(
            r.get("match_pattern") == pattern and r.get("zone") == zone
            for r in rules
            if isinstance(r, dict)
        ):
            logger.info(
                "[zone_rules] duplicate zone rule skipped for %r: pattern=%r zone=%s",
                tool_id, pattern, zone,
            )
            # Reload so callers holding the singleton stay current (overlay unchanged).
            try:
                _zones.reload()
            except RuntimeError:
                pass
            return
        else:
            rules.append({"match_pattern": pattern, "zone": zone, "reason": reason or ""})
    else:
        raise ValueError(
            f"tool_zones[{tool_id!r}] is not a string or mapping; got "
            f"{type(existing).__name__}"
        )

    wrote_with_ruamel = _write_with_ruamel(path, data)
    if not wrote_with_ruamel:
        logger.error(
            "[zone_rules] ruamel.yaml write failed; falling back to "
            "pyyaml safe_dump. Schema comments will be stripped from %s.",
            path,
        )
        import yaml as _yaml
        with open(path, "w") as fh:
            _yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)

    # Reload so the new rule takes effect immediately for any caller
    # holding the singleton.
    try:
        _zones.reload()
    except RuntimeError:
        # Classifier wasn't initialised yet — first read will pick it up.
        pass
    logger.info(
        "[zone_rules] appended rule to tool_zones[%r]: zone=%s pattern=%r reason=%r",
        tool_id, zone, pattern, reason,
    )
