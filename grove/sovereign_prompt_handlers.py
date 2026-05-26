"""Sovereign Prompt handler implementations — Sprint 27 Phase 2.

The Dispatcher accepts a ``sovereign_prompt_handler: Callable[[AndonHalt], str]``
at construction (dispatcher.py:396) and calls it when ``_handle_andon_halt``
fires — unless shadow mode (``GROVE_ZONE_SHADOW=1``) short-circuits the call.
The handler must return one of ``"skip"`` or ``"drop"`` per GRV-005 § VI.
(``"shadow_approve"`` is returned by ``_handle_andon_halt`` itself in shadow
mode; handlers never produce it.)

This module ships four handler implementations, one per caller context:

* :func:`tty_sovereign_prompt` — the interactive TTY prompt that surfaces
  the Andon halt detail to stderr and reads disposition via ``input()``.
  This is the canonical operator-facing surface and the default handler
  the Dispatcher installs when no override is provided (preserved via
  the back-compat alias ``grove.dispatcher._default_sovereign_prompt``).

* :func:`batch_auto_skip_handler` — non-interactive auto-skip for batch
  callers (cron, eval, hygiene, compression). Returns ``"skip"`` so the
  Agent receives a denial Observation and re-reasons. The halt detail is
  already captured upstream by the Dispatcher's ``andon_halt`` ledger
  record (dispatcher.py:875), so the handler emits a short INFO log line
  rather than duplicating that payload.

* :func:`gateway_auto_skip_handler` — non-interactive auto-skip for live
  gateway turns (Telegram, Discord, API server, Feishu). Same semantics
  as the batch handler for v1; carries a distinct identity so future
  work can route the Sovereign Prompt back through the platform adapter
  (a Telegram message asking the operator for disposition, etc.) without
  touching batch behavior.

* :func:`silent_skip_handler` — auto-skip with no I/O, for test fixtures
  that need to drive the Dispatcher past an Andon halt deterministically
  without polluting test output.

GRV-005 § VI conformance note. The Standard requires the Sovereign
Prompt to "present at least two disposition options" to the operator.
The non-interactive auto-skip handlers degrade that surface to a single
auto-disposition (Skip) plus a ledger record, accepted as the v1
trade-off for caller contexts where blocking on operator input is
infeasible (background queues, HTTP request handlers). Routing
interactive prompts back through platform adapters is a Sprint 28
deliverable.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grove.dispatcher import AndonHalt

__all__ = [
    "tty_sovereign_prompt",
    "batch_auto_skip_handler",
    "gateway_auto_skip_handler",
    "silent_skip_handler",
]

logger = logging.getLogger(__name__)


def tty_sovereign_prompt(halt: "AndonHalt") -> str:
    """The Phase 5 MVP TTY Sovereign Prompt.

    GRV-005 § IX(3) requires the Sovereign Prompt to "decouple the
    decision payload from standard conversational text, presenting the
    intent, arguments, and disposition options in a structured,
    deterministic interface." This function prints the structured
    block to stderr and reads the operator's disposition via ``input()``.

    Returns one of:
      * ``"skip"`` — the operator wants to skip this batch; the
        Dispatcher injects a denial Observation and the Agent re-reasons.
      * ``"drop"`` — the operator wants to abandon the turn entirely;
        the Dispatcher calls ``gen.close()`` to flush volatile state.

    Defaults to ``"drop"`` on EOF / KeyboardInterrupt (safest default
    when stdin is unavailable). Non-TTY callers (gateway, batch) must
    inject one of the auto-skip handlers via the Dispatcher's constructor.
    """
    triggering = halt.intents[halt.triggering_index]
    print(file=sys.stderr)
    print(
        "─── Andon Halt — Sovereign Disposition Required ──────────",
        file=sys.stderr,
    )
    print(f"  Zone:        {halt.zone}", file=sys.stderr)
    print(f"  Matched:     {halt.matched_rule}", file=sys.stderr)
    print(f"  Source:      {halt.source}", file=sys.stderr)
    if halt.reason:
        print(f"  Reason:      {halt.reason}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"  Batch ({len(halt.intents)} intent"
        f"{'s' if len(halt.intents) != 1 else ''}):",
        file=sys.stderr,
    )
    for idx, (intent, zr) in enumerate(zip(halt.intents, halt.zone_results)):
        marker = "→" if idx == halt.triggering_index else " "
        args_preview = str(dict(intent.arguments))[:80]
        print(
            f"  {marker} #{idx} [{zr.zone:6}] {intent.tool_name}: {args_preview}",
            file=sys.stderr,
        )
    print(file=sys.stderr)
    print("  Dispositions:", file=sys.stderr)
    print(
        "    [1] Skip — inject denial; let the agent re-reason or pivot",
        file=sys.stderr,
    )
    print(
        "    [2] Drop — flush this turn; persistent state unchanged",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    while True:
        try:
            choice = input("Choose [1/2 or skip/drop]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("(no input — defaulting to Drop)", file=sys.stderr)
            return "drop"
        if choice in ("1", "skip"):
            return "skip"
        if choice in ("2", "drop"):
            return "drop"
        print(
            f"Unknown choice {choice!r}; pick 1/skip or 2/drop.",
            file=sys.stderr,
        )


def batch_auto_skip_handler(halt: "AndonHalt") -> str:
    """Non-interactive auto-skip for batch callers.

    Used by callers with no live operator surface (cron jobs, eval runs,
    compression hygiene). Returns ``"skip"`` so the Agent receives a
    denial Observation and continues. The halt's full detail is already
    captured in the Kaizen Ledger via the Dispatcher's ``andon_halt``
    record upstream (dispatcher.py:875), so this handler emits a single
    INFO log line and returns.
    """
    triggering = halt.intents[halt.triggering_index].tool_name
    logger.info(
        "Andon auto-skip (batch): tool=%s zone=%s matched_rule=%s",
        triggering, halt.zone, halt.matched_rule,
    )
    return "skip"


def gateway_auto_skip_handler(halt: "AndonHalt") -> str:
    """Non-interactive auto-skip for live gateway turns.

    Used by platform-driven callers (Telegram, Discord, Feishu, HTTP API)
    where the operator is reachable via the platform but not via TTY.
    Identical Skip semantics to :func:`batch_auto_skip_handler` in v1;
    distinct identity so future work can route the Sovereign Prompt back
    through the platform adapter (e.g., a Telegram message asking the
    operator for disposition) without changing batch behavior. The halt's
    full detail is in the Kaizen Ledger via the upstream ``andon_halt``
    record.
    """
    triggering = halt.intents[halt.triggering_index].tool_name
    logger.info(
        "Andon auto-skip (gateway): tool=%s zone=%s matched_rule=%s",
        triggering, halt.zone, halt.matched_rule,
    )
    return "skip"


def silent_skip_handler(halt: "AndonHalt") -> str:
    """Silent auto-skip for test fixtures.

    Returns ``"skip"`` with no I/O. Tests injecting this handler can
    drive the Dispatcher past an Andon halt deterministically without
    polluting test output. The Dispatcher's upstream ``andon_halt``
    ledger record is unaffected — tests that want to assert ledger
    contents inspect the ledger directly.
    """
    return "skip"
