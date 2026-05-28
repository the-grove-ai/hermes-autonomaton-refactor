"""Synthetic ``escalate`` tool — Sprint 30 escalation-signal-v1.

The Agent's surface for emitting :class:`grove.intents.EscalationRequest`.
The LLM calls ``escalate(reasoning_depth, context_size, blocker)`` like any
other tool; the Agent's intent-extraction path (``_extract_tool_intents``)
intercepts the call BEFORE classification and yields an EscalationRequest
to the Dispatcher instead of routing through the normal tool dispatch.

GRV-005 § VII compliance:
* The Agent emits EscalationRequest as a structured-data payload (not a
  retry loop, not self-modification, not a parallel call).
* The Agent describes WHAT it needs (declarative payload); the
  Dispatcher decides HOW to satisfy it (tier mapping). § III preserved.
* The Agent never instantiates infrastructure.

The handler here is the cold path — fires only when the LLM mixes
``escalate`` into a batch with other tool calls. The intercept is
single-tool-call-only by design (the EscalationRequest semantics
require sole-purpose batches: granting an escalation while other tools
ran in the same batch is a state-management nightmare for the
hot-swap). Mixed-batch calls return an honest decline to the LLM so
it re-emits ``escalate`` alone.
"""

from __future__ import annotations

import json
from typing import Any, Dict


ESCALATE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "escalate",
        "description": (
            "Request a more capable cognitive tier. Use this when the "
            "current tier is genuinely insufficient — context is too "
            "large for clean reasoning, the problem requires deeper "
            "synthesis than your tier offers, or you're structurally "
            "blocked. The Dispatcher decides whether to grant; you "
            "continue regardless. Must be the only tool call in the "
            "batch — calling escalate alongside other tools is "
            "ignored. After granting, the conversation resumes with "
            "the same history; you do NOT re-execute prior tool calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning_depth": {
                    "type": "string",
                    "enum": ["shallow", "moderate", "deep", "apex"],
                    "description": (
                        "How much synthesis the next reasoning step needs. "
                        "shallow=T1-class, moderate=T2-class, deep=T3-class, "
                        "apex=T3-class with extended thinking. The "
                        "Dispatcher maps this to a tier."
                    ),
                },
                "context_size": {
                    "type": "string",
                    "enum": ["normal", "extended", "max"],
                    "description": (
                        "Context-window pressure. normal=fits current "
                        "tier's budget; extended=approaching limit; "
                        "max=structurally over budget."
                    ),
                },
                "blocker": {
                    "type": "string",
                    "description": (
                        "One sentence describing what you're stuck on. "
                        "Operator-facing; appears in the Kaizen Ledger."
                    ),
                },
            },
            "required": ["reasoning_depth", "context_size", "blocker"],
        },
    },
}


def escalate_tool(
    reasoning_depth: str,
    context_size: str,
    blocker: str,
    **_kwargs: Any,
) -> str:
    """Cold-path handler.

    Fired ONLY when ``escalate`` slipped through the
    ``_extract_tool_intents`` intercept — meaning the LLM mixed it
    into a batch with other tool calls. The intercept is
    single-call-only by design; this handler returns an honest
    decline so the LLM re-emits the call alone.
    """
    return json.dumps({
        "escalation": "ignored",
        "reason": (
            "escalate() must be the only tool call in the batch. "
            "Call it alone with no other tool calls in the same "
            "assistant message and the Dispatcher will receive it."
        ),
        "received": {
            "reasoning_depth": reasoning_depth,
            "context_size": context_size,
            "blocker": blocker,
        },
    })


def check_escalate_requirements() -> bool:
    """Always-available check — no external dependencies."""
    return True


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="escalate",
    toolset="escalate",
    schema=ESCALATE_SCHEMA,
    handler=lambda args, **kw: escalate_tool(
        reasoning_depth=args.get("reasoning_depth", ""),
        context_size=args.get("context_size", ""),
        blocker=args.get("blocker", ""),
    ),
    check_fn=check_escalate_requirements,
    emoji="⬆",
)
