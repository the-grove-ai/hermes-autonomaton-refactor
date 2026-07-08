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
from typing import Any, Optional

GOVERNANCE_WRITE_TOOL = "propose_governance_change"
# The value-bearing args (the .env body). ``content`` is canonical; ``diff_or_content``
# is its alias in the tool schema. ``target_file`` is deliberately NOT here — the
# path is non-secret and kept for legibility.
_SECRET_ARG_KEYS = ("content", "diff_or_content")


def redaction_marker(value: str) -> str:
    """``[redacted sha256=<first8>]`` — a correlation hash of the value, never the value."""
    return f"[redacted sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}]"


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
