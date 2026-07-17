"""Redact the governance-write secret payload from tool-call args before they
reach any log / telemetry / stream sink.

propose-approve-deadlock-v1 Phase 1b-iii (DEPLOY BLOCKER). A ``.env`` write flows
through ``propose_governance_change``'s ``content`` (the FULL ``.env`` body — all
tokens). That arg dict is captured by the general tool-argument telemetry:

  * the durable intent feed (``~/.grove/intent_records.jsonl`` via
    ``IntentRecord.tool_invocation``),
  * the console / gateway.log tool-call line,
  * the ``/v1`` responses SSE ``function_call.arguments`` blob.

This module redacts the secret at each sink. Whole-payload redaction — NO
key/value parsing (fragile). ``target_file`` / ``path`` are kept intact for
legibility; only the value-bearing ``content`` / ``diff_or_content`` are replaced
with a correlation marker ``[redacted sha256=<first8>]`` — never the value.
Scoped to the governance-write tool ONLY; every other tool passes through
unchanged. Pure (stdlib only) so every sink layer can import it without a cycle.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

GOVERNANCE_WRITE_TOOL = "propose_governance_change"
# The value-bearing args (the .env body). ``content`` is canonical; ``diff_or_content``
# is its alias in the tool schema. ``target_file`` is deliberately NOT here — the
# path is non-secret and kept for legibility.
_SECRET_ARG_KEYS = ("content", "diff_or_content")


def redaction_marker(value: str) -> str:
    """``[redacted sha256=<first8>]`` — a correlation hash of the value, never the value."""
    return f"[redacted sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}]"


# ── display-only redaction of a shell command string ─────────────────────────
# unresolved-writer-execution-path-v1 Fix 3. The pending-RED portal card shows the
# command a shell proposal would run. A command line can itself carry a secret —
# an inline API key, an Authorization header, a KEY=value assignment, a
# --password flag. This scrubs those secret-SHAPED substrings to a fixed
# ``[redacted]`` marker BEFORE the string reaches the card (never the value, and
# unlike ``redaction_marker`` no hash — a command token is lower-entropy than an
# ``.env`` body and a hash would narrow it). Conservative by shape; the path /
# verb / flags stay legible. Pure-regex, stdlib only.
_CMD_REDACTED = "[redacted]"

# High-entropy credential tokens by known provider/VCS/cloud prefix.
_CMD_TOKEN_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),        # Anthropic api / oauth
    re.compile(r"sk-[A-Za-z0-9]{16,}"),              # OpenAI-style
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),       # GitHub PAT / oauth / refresh
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),    # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                 # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),          # Google api key
    re.compile(                                       # JWT (header.payload.sig)
        r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"
    ),
)
# Authorization: Bearer <tok> / token <tok> — keep the header name, drop the value.
_CMD_AUTH_HEADER = re.compile(
    r"(Authorization\s*:\s*(?:Bearer|token)\s+)(\S+)", re.IGNORECASE
)
# Secret-named env assignments: FOO_TOKEN=..., API_KEY=..., DB_PASSWORD=...
_CMD_SECRET_ENV = re.compile(
    r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|APIKEY|"
    r"ACCESS_?KEY|CREDENTIAL|PRIVATE_?KEY)[A-Za-z0-9_]*)=(\S+)",
    re.IGNORECASE,
)
# Secret-bearing flags: --password X, --token=X, --api-key X.
_CMD_SECRET_FLAG = re.compile(
    r"(--?(?:password|passwd|token|secret|api[-_]?key|access[-_]?key)(?:=|\s+))(\S+)",
    re.IGNORECASE,
)


def redact_command_string(command: Optional[str]) -> str:
    """Return *command* with secret-shaped substrings replaced by ``[redacted]``.

    Display-only, for the pending-RED portal card. Never the value. Order matters:
    structured shapes (auth header, KEY=value, --flag value) run first so their
    values are caught even when the value isn't itself a recognizable token; the
    bare-token prefixes then sweep any remaining inline keys.
    """
    if not command:
        return command or ""
    out = command
    out = _CMD_AUTH_HEADER.sub(lambda m: m.group(1) + _CMD_REDACTED, out)
    out = _CMD_SECRET_ENV.sub(lambda m: m.group(1) + "=" + _CMD_REDACTED, out)
    out = _CMD_SECRET_FLAG.sub(lambda m: m.group(1) + _CMD_REDACTED, out)
    for _pat in _CMD_TOKEN_PATTERNS:
        out = _pat.sub(_CMD_REDACTED, out)
    return out


def redact_governance_args(tool_name: Optional[str], args: Any) -> Any:
    """Return a COPY of *args* with the governance-write secret payload redacted.

    Only touches ``propose_governance_change`` (``GOVERNANCE_WRITE_TOOL``). Other
    tools / non-dict args are returned unchanged (same object). The original dict
    is never mutated (a defensive copy is made only when redaction applies).
    """
    if tool_name != GOVERNANCE_WRITE_TOOL or not isinstance(args, dict):
        return args
    redacted = None
    for key in _SECRET_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            if redacted is None:
                redacted = dict(args)
            redacted[key] = redaction_marker(value)
    return redacted if redacted is not None else args
