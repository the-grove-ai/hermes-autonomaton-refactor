"""Intent Protocol v1 — Agent ↔ Dispatcher contract data types.

GRV-005 § IV enumerates the bidirectional Intent Protocol the Agent and
Dispatcher use to communicate. The Agent yields intents; the Dispatcher
executes; the Dispatcher yields ``Observation``s back. The contract is
generator-shaped — the Agent does not call the Dispatcher synchronously.

This module defines the v1 protocol's data types as pure frozen
dataclasses. No behavior is wired here; serialization helpers,
generator orchestration, and tool-execution semantics live in
``grove.dispatcher`` and ``run_agent.py`` (Sprint 26 Phase 3 onward).

v1 intent types (Agent → Dispatcher), exhaustive:

* ``ToolIntent`` — execute a named tool with named arguments
* ``EscalationRequest`` — structured request for more capability
* ``FinalResponse`` — the turn's output; dispatch ends
* ``ClarificationRequest`` — needs operator input; turn pauses

v1 observation type (Dispatcher → Agent), exhaustive:

* ``Observation`` — result of an executed ``ToolIntent`` or outcome of
  an ``EscalationRequest`` decision; carries status, return value, and
  metadata

Type discrimination uses ``isinstance`` rather than a string ``kind``
field. Telemetry consumers that need a wire-format discriminator can
read the class name via ``type(intent).__name__``.

Architectural horizons documented in GRV-005 § X (deferred to future
Standards, NOT implemented here): ``MemoryWriteIntent``,
``SubAgentIntent`` on the Agent→Dispatcher side;
``OperatorDispositionObservation`` on the Dispatcher→Agent side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "ToolIntent",
    "ToolBatchYield",
    "EscalationRequest",
    "FinalResponse",
    "ClarificationRequest",
    "Observation",
    "SessionRotateIntent",
    "SessionUpdateTokensIntent",
    "MemoryWriteIntent",
    "MemoryWriteResult",
    "MemoryLifecycleIntent",
    "PostExecutionKaizenYield",
]


# ── Agent → Dispatcher (four intent types, exhaustive in v1) ──────────────


@dataclass(frozen=True)
class ToolIntent:
    """A request to execute a named tool with named arguments.

    GRV-005 § IV: "execute a named tool with named arguments. Dispatcher
    returns an Observation."

    The Dispatcher MUST own tool execution (GRV-005 § II); the Agent
    MUST NOT execute the tool itself (§ III). When the Agent yields a
    ``ToolIntent``, the Dispatcher classifies it (tool-zone Andon at
    intent-yield per § V), executes if approved, and injects an
    ``Observation`` back into the Agent's reasoning context.

    Fields:
        tool_name: the canonical tool name as registered in
            ``tools.registry`` (matches ``self.tool_definitions[*]
            .function.name`` in the OpenAI-format tool array).
        arguments: named argument dictionary; values are JSON-serialisable
            primitives plus nested dicts and lists. Type-validation
            against the tool's declared schema happens in the Dispatcher
            before execution.
        call_id: optional identifier the model emits in tool-calling
            responses; used by the Dispatcher to match the resulting
            ``Observation.intent_id`` back to this intent when multiple
            ``ToolIntent``s are yielded in the same generator step
            (parallel batch).
    """

    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None


@dataclass(frozen=True)
class ToolBatchYield:
    """A batch of ``ToolIntent`` plus the per-batch scalars the
    Dispatcher needs to execute them.

    Sprint 31 Phase 2. Before this sprint, the Agent's
    ``_run_turn_generator`` yielded a bare ``List[ToolIntent]`` and
    the Dispatcher reached into the Agent for ``effective_task_id``
    and ``api_call_count`` via four state-stashing bridge fields
    (Sprint 26 GATE-D pragmatic choice). Phase 2 deletes the bridge
    and moves those per-batch scalars onto this dataclass —
    carrying them through the yield protocol where they belong
    rather than back-channel attribute access.

    The Dispatcher catches ``ToolBatchYield`` in ``_drive_generator``,
    decides concurrent vs sequential via the pure-function
    parallelization heuristic, and routes directly to
    ``grove.tool_executor.ToolExecutor.execute_batch_concurrent`` or
    ``execute_batch_sequential`` — no Agent shim in the path.

    Fields:
        intents: the batch of ``ToolIntent``s to execute, in input
            order. The order is preserved end-to-end through the
            executor's per-slot result list.
        effective_task_id: per-turn / per-task identifier the
            executor threads into telemetry and the persistent
            tool-result storage's environment resolver.
        api_call_count: ordinal API call counter for this turn
            (1-indexed). Carried for intent-record telemetry and
            the ledger's per-batch context.
    """

    intents: List[ToolIntent]
    effective_task_id: str = ""
    api_call_count: int = 0


@dataclass(frozen=True)
class EscalationRequest:
    """A structured request for more capability.

    GRV-005 § IV: "request more capability (more compute, larger model,
    longer context). Dispatcher returns granted capability or denial in
    the next Observation."

    GRV-005 § VII normative requirements:
        * ``EscalationRequest`` MUST be structured data — not a retry
          loop, not self-modification, not a parallel call.
        * The Agent MUST escalate only via ``EscalationRequest``.
        * The Agent MUST NOT fake capability it was not granted.

    Specific decision rules (when the Dispatcher auto-grants vs surfaces
    to Kaizen vs denies) are Sprint 27 concerns; v1 fields carry the
    request shape but do not constrain the Dispatcher's policy.

    Fields:
        reason: operator-visible explanation of why the agent is
            escalating (e.g. "context window exceeded mid-reasoning",
            "task complexity warrants Apex tier"). Used by the
            Dispatcher's policy + by Kaizen-surfaced borderline cases.
        request: structured capability request. The shape is v1-loose;
            Sprint 27 may formalize specific keys (``tier``,
            ``max_tokens``, ``context_window``, etc.). For v1, callers
            populate whatever fields the dispatcher's policy
            understands.
    """

    reason: str
    request: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FinalResponse:
    """The autonomaton's reasoning has produced the turn's output.

    GRV-005 § IV: "The turn's output. Dispatcher terminates dispatch."

    When the Agent yields a ``FinalResponse``, the Dispatcher's
    foreground/background split (§ IX(4)) fires: the conversational
    payload is written to the active context window; the operational
    telemetry routes out-of-band to the Kaizen Ledger. Dispatch ends.

    Fields:
        content: the conversational payload — text the operator sees in
            the active context window. The Dispatcher MUST decouple
            this from the operational telemetry per § IX(4).
        metadata: optional turn-summary fields (tier used, token counts,
            latency, etc.) the Dispatcher may include in the Kaizen
            ledger entry. Not surfaced to the operator's active context
            window.
    """

    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostExecutionKaizenYield:
    """Sprint 53.2 — a quarantined skill ran successfully; offer promotion.

    NOT yielded by the Agent's generator. The Dispatcher synthesizes this
    carrier at the ``FinalResponse`` site (after the operator has seen the
    skill's output) when a ``.andon/`` skill executed under an "allow once"
    disposition this turn. The Dispatcher hands it to its
    ``post_execution_prompt_handler`` (TTY: a Promote / Not yet / Never
    prompt) or, on headless surfaces with no handler, auto-logs a pending
    ``skill_promotion`` proposal to the Flywheel queue. Distinct from
    Sprint 32's four-choice Sovereign Prompt (Allow once / Allow for
    session / Always allow / Don't allow): different vocabulary, different
    handler, different return-value space — no collision.

    Fields:
        skill_name: the quarantined skill's directory name under .andon/.
        skill_path: absolute path of the quarantined skill directory.
        exit_status: outcome of the gated execution ("success").
        execution_turn_id: the Dispatcher turn id that ran the skill.
        suggested_action: the promotion the system proposes ("promote").
    """

    skill_name: str
    skill_path: str
    exit_status: str
    execution_turn_id: str
    suggested_action: str


@dataclass(frozen=True)
class ClarificationRequest:
    """The autonomaton needs operator input before continuing.

    GRV-005 § IV: "needs operator input. Dispatcher surfaces an operator
    prompt; the turn pauses pending response."

    The Dispatcher MUST surface the prompt to the operator via the
    platform's clarification surface (CLI: input(); gateway: structured
    prompt). The turn pauses until a response arrives. When the response
    arrives, the Dispatcher injects an ``Observation`` carrying the
    operator's reply, and the Agent's reasoning resumes.

    Fields:
        question: free-form text the operator answers. Required.
        choices: optional multiple-choice options the operator may
            select from. Mirrors the existing ``clarify`` tool's
            convention — when provided, the UI presents them as a
            numbered list with an automatic 'Other (type your answer)'
            option. ``None`` means open-ended; the operator types a
            free-form response.
    """

    question: str
    choices: Optional[List[str]] = None


# ── Dispatcher → Agent (one observation type, exhaustive in v1) ──────────


@dataclass(frozen=True)
class Observation:
    """The result of an executed ``ToolIntent`` or the outcome of an
    ``EscalationRequest`` decision.

    GRV-005 § IV: "the result of an executed ``ToolIntent`` or the
    outcome of an ``EscalationRequest`` decision. Carries status, return
    value, and metadata. The Agent's reasoning resumes with this
    ``Observation`` as new context."

    The Agent receives an ``Observation`` from the Dispatcher by
    advancing the generator with the Observation as the ``send()``
    value (Sprint 26 Phase 3 wires the actual generator dispatch).

    Observation also carries the result of a ``ClarificationRequest`` —
    when the operator responds, the Dispatcher packages the reply in an
    ``Observation`` whose ``intent_id`` matches the originating
    ``ClarificationRequest`` (matched by reference at the dispatcher
    level since ``ClarificationRequest`` has no call_id field).

    GRV-005 § X horizon: a future ``OperatorDispositionObservation``
    subtype may split out the resumed-after-Andon flow's reciprocal
    payload. v1 handles that case as metadata on this base type.

    Fields:
        intent_id: the originating intent's identifier (matches
            ``ToolIntent.call_id`` for tool results). ``None`` for
            observations not tied to a specific intent (e.g.
            ``EscalationRequest`` outcomes when the request had no
            explicit id).
        success: ``True`` when the dispatched action completed without
            error; ``False`` when the action raised, was denied
            (escalation), or was skipped (Andon disposition). The Agent
            uses this flag to branch its recovery logic.
        value: the action's return value. For ``ToolIntent``: whatever
            the tool handler returned (string, dict, JSON-serialisable
            structure). For ``EscalationRequest``: the granted
            capability descriptor or a denial reason. For
            ``ClarificationRequest``: the operator's reply string.
        metadata: ancillary fields (latency_ms, tier, error_type,
            zone_classification, etc.) the Dispatcher may attach for
            telemetry / Kaizen routing. Not part of the contract's
            normative surface — Sprint 27 may formalize specific keys.
    """

    intent_id: Optional[str]
    success: bool
    value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Sprint 39 — session-authority Agent→Dispatcher intents ─────────────


@dataclass(frozen=True)
class SessionRotateIntent:
    """Atomic compression-boundary session rotation.

    GRV-005 § III bars the Agent from holding substrate authority. The
    pre-Sprint-39 compression flow had the Agent reach directly into a
    ``SessionDB`` handle to ``end_session`` the old session, generate a
    new id, ``create_session`` for it, propagate the title with
    auto-numbering, and ``update_system_prompt`` — seven calls in a
    cohesive sequence. Sprint 39 expresses that whole sequence as one
    declarative intent. The Agent yields ``SessionRotateIntent``; the
    Dispatcher catches it and executes the rotation atomically against
    its owned ``self.session``.

    Mirrors the Sprint 26 ``ToolIntent`` yield pattern: the Agent
    declares what should happen, the Dispatcher does it.

    Fields:
        reason: lifecycle marker recorded against the closing session
            (e.g. ``"compression"``). Surfaces in ``end_session``'s
            ``end_reason`` column.
        new_system_prompt: the system prompt to install on the freshly
            created continuation session. Computed by the Agent's
            ``_build_system_prompt`` after compression.
    """

    reason: str
    new_system_prompt: str


@dataclass(frozen=True)
class SessionUpdateTokensIntent:
    """Per-API-call telemetry write against the active session.

    The Agent emits one of these after every API call so the session
    row's token / cost / billing columns track the conversation. The
    Dispatcher catches it and updates ``self.session`` — the Agent
    never holds the handle. High-frequency intent (multiple per turn);
    the Dispatcher's handling is a single ``update_token_counts`` call.

    Fields mirror ``SessionDB.update_token_counts`` so the Dispatcher's
    handler can splat them through unchanged.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    cost_status: Optional[str] = None
    cost_source: Optional[str] = None
    billing_provider: Optional[str] = None
    billing_base_url: Optional[str] = None
    billing_mode: Optional[str] = None
    model: Optional[str] = None
    api_call_count: int = 1


# ── Sprint 40 — memory-authority Agent↔Dispatcher intents ──────────────


@dataclass(frozen=True)
class MemoryWriteIntent:
    """Agent → Dispatcher: synchronous memory write whose result the
    Agent treats as a tool output.

    Sprint 40 mediates the two Agent-initiated memory tool calls (the
    built-in ``memory`` tool and external memory-provider tools registered
    via the memory manager). Both return a string the LLM sees as the
    tool result — they need the bidirectional yield-and-inject protocol
    Sprint 26 established for ``ToolIntent``: the Agent yields, the
    Dispatcher executes, the Dispatcher ``.send()``s a
    ``MemoryWriteResult`` back into the generator.

    Two operation kinds:

    * ``kind="builtin_memory"`` — the built-in ``memory`` tool. Dispatcher
      executes against ``self.memory_store`` (``MemoryStore.add`` or
      ``replace`` depending on ``action``), then fires the bridge
      notification to ``self.memory_manager.on_memory_write(...)`` so
      external providers stay in sync. Sprint 40 owns the bridge.
    * ``kind="provider_tool"`` — an external memory-provider tool
      registered via the manager's tool schemas. Dispatcher routes to
      ``self.memory_manager.handle_tool_call(tool_name, arguments)``.
    """

    kind: str  # "builtin_memory" | "provider_tool"
    # builtin_memory fields
    action: Optional[str] = None
    target: Optional[str] = None
    content: Optional[str] = None
    old_text: Optional[str] = None
    # provider_tool fields
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    # shared metadata (used by the on_memory_write bridge for the
    # builtin_memory path; carries effective_task_id + tool_call_id +
    # whatever else the Agent's metadata builder produces).
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryWriteResult:
    """Dispatcher → Agent: result of a ``MemoryWriteIntent``.

    Injected back into the generator via ``.send()`` exactly like
    ``Observation`` is returned for a ``ToolIntent``. The Agent treats
    ``value`` as the tool's return string (passed back to the LLM as the
    tool result).
    """

    success: bool
    value: str = ""
    error: Optional[str] = None


@dataclass(frozen=True)
class MemoryLifecycleIntent:
    """Agent → Dispatcher: fire-and-forget memory-manager lifecycle hook.

    The Dispatcher executes the corresponding ``memory_manager.*`` call
    and returns a trivial ``Observation`` so the generator resumes
    immediately. Five events:

    * ``on_session_end`` — turn-end or session-rotation extraction.
      Carries ``messages`` (the conversation state at that boundary).
    * ``on_session_switch`` — compression-driven session id rotation.
      Carries ``parent_session_id`` and ``reason``.
    * ``on_pre_compress`` — pre-compression notification. Carries
      ``messages``.
    * ``sync_turn`` — turn-end ``sync_all(...)`` + ``queue_prefetch_all
      (...)`` (the manager's per-turn external-sync pair). Carries
      ``original_user_message`` / ``final_response`` / ``interrupted``.
    * ``shutdown`` — session-shutdown ``shutdown_all()``.
    """

    event: str
    messages: Optional[List[Dict[str, Any]]] = None
    parent_session_id: Optional[str] = None
    reason: Optional[str] = None
    original_user_message: Any = None
    final_response: Any = None
    interrupted: bool = False
