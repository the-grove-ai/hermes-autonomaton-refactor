"""Grove ToolExecutor — Sprint 31 dispatcher-execute-tool-calls-extraction-v1.

GRV-005 § III: the Agent has no path to the substrate. Sprint 31
makes the symmetric statement true: the executor has no path back
to the Agent. Tool execution lives here; the Agent is a pure
reasoner.

Phase 1a (this commit) extracts the concurrent execution path
out of ``run_agent.py._execute_tool_calls_concurrent`` into
``ToolExecutor.execute_batch_concurrent``. Phase 1b extracts the
sequential path. Phase 2 deletes the agent state-stashing bridge
and changes the yield protocol to carry per-batch scalars.

The boundary is held by the ``ExecutionContext`` dataclass:

* The executor reads from ``ctx``; it never imports the Agent.
* Observability hooks fire as structured callbacks; the Agent's
  implementations drive whatever display surface is in use (CLI
  text, spinner, TUI events). The executor never imports display
  libraries directly — GATE-B path (i).
* Side-effects that need Agent-owned state (tool plumbing, the
  checkpoint manager, the file-mutation telemetry verifier, the
  nudge counters, model-aware result formatting) flow through
  ``SideEffectCallbacks`` as opaque callables.
* The transition period leaves ``agent._execute_tool_calls_concurrent``
  in place as a thin shim that builds the context, calls the
  executor, and applies the messages / steer / budget orchestration
  the executor used to do internally. Tests that monkey-patch the
  agent method keep working. Phase 2 deletes the shim and routes
  ``grove.dispatcher._drive_generator`` directly through the
  executor.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import threading
import time

from grove.operator_input import OperatorInputRequired
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol

from grove.intents import ToolIntent

logger = logging.getLogger(__name__)

__all__ = [
    "ToolResult",
    "ObservabilityCallbacks",
    "SideEffectCallbacks",
    "ExecutorConfig",
    "InterruptToken",
    "ExecutionContext",
    "ToolExecutor",
]


# Maximum concurrent tool workers per batch. Mirrors the prior
# constant in run_agent.py — moved with the execution path because
# it is execution-pool configuration, not agent state.
_MAX_TOOL_WORKERS = 8


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool invocation.

    The executor produces one ``ToolResult`` per intent and returns
    the list in input order. The caller (Dispatcher, or the
    transition-period shim on the Agent) appends a tool message to
    its conversation history — the executor never mutates messages.

    ``content`` is already model-formatted (multimodal unwrapped,
    subdir-hint appended) via ``SideEffectCallbacks.format_result_content``
    at execute time. The caller builds the tool-message envelope but
    does NOT touch the content body.
    """
    intent_id: str
    tool_name: str
    tool_args: dict
    success: bool
    content: Any  # str OR list-of-content-blocks for multimodal results
    error: Optional[str] = None
    latency_s: float = 0.0
    blocked: bool = False  # plugin-block or guardrail-block path
    metadata: dict = field(default_factory=dict)


# ── Callback bundles ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObservabilityCallbacks:
    """Hooks the executor fires at structured moments. The Agent
    implements them to drive display surface — CLI text lines,
    KawaiiSpinner, TUI events, structured telemetry. The executor
    never imports a display library (GATE-B path (i)).

    Every field is optional; ``None`` means "no-op at this hook".
    """
    # Batch lifecycle
    batch_started: Optional[Callable[[int, str, str], None]] = None
    # signature: (num_tools, names_summary, mode)  where mode in ("concurrent", "sequential")
    batch_completed: Optional[Callable[[int, int, float], None]] = None
    # signature: (completed_count, total, total_duration_s)

    # Per-tool lifecycle
    on_tool_start: Optional[Callable[[str, str, dict], None]] = None
    # signature: (call_id, tool_name, tool_args)
    on_tool_progress: Optional[Callable[..., None]] = None
    # signature: (event_name, tool_name, *, preview=None, args=None, duration=None, is_error=None)
    on_tool_complete: Optional[Callable[[str, str, dict, Any], None]] = None
    # signature: (call_id, tool_name, tool_args, result)

    # Per-tool descriptive print (the "📞 Tool N: name(args) - preview" lines)
    log_tool_call_line: Optional[Callable[[int, str, dict], None]] = None
    # signature: (index_1_based, tool_name, tool_args)
    log_tool_complete_line: Optional[Callable[[int, str, dict, Any, float], None]] = None
    # signature: (index_1_based, tool_name, tool_args, result, duration_s).
    # ``tool_args`` is the args dict the LLM emitted so the completion-line
    # cute message can show the same payload context the start-preview did
    # (Sprint 32.x bugfix — the prior signature dropped args and the
    # downstream renderer fell back to the literal "?" placeholder).
    log_batch_header_line: Optional[Callable[[int, str], None]] = None
    # signature: (num_tools, names_summary)

    # Per-tool display open/close (Sprint 31 Phase 1b — sequential path
    # uses these for spinner-per-tool orchestration; the executor never
    # imports KawaiiSpinner. GATE-B path (i).)
    tool_display_open: Optional[Callable[[str, dict], None]] = None
    # signature: (tool_name, tool_args)
    tool_display_close: Optional[Callable[[str, dict, Any, float], None]] = None
    # signature: (tool_name, tool_args, result, duration_s). Same Sprint 32.x
    # bugfix — args carried through so the cute message renders the real
    # action / target / content, not a placeholder.

    # Verbose tracing and console activity hooks
    vprint: Optional[Callable[..., None]] = None  # accepts (text, force=False) like agent._vprint
    touch_activity: Optional[Callable[[str], None]] = None
    log_interrupt_message: Optional[Callable[[str], None]] = None  # for "Interrupt: skipping..." line

    # Predicates that depend on agent display state
    should_emit_quiet: Optional[Callable[[], bool]] = None
    should_start_quiet_spinner: Optional[Callable[[], bool]] = None


@dataclass(frozen=True)
class SideEffectCallbacks:
    """Side-effects that cross back into Agent-owned subsystems.

    The executor fires callables; the Agent implementations route
    to the concrete state. The executor stays unaware of the
    underlying classes (CheckpointManager, MemoryManager, todo
    store, session DB, etc.).
    """
    # Single-call dispatch — Agent's _invoke_tool routes to the right
    # implementation. Returns the raw tool-result string (pre-formatting).
    invoke_tool: Callable[..., Any]
    # signature: (tool_name, args, effective_task_id, *, tool_call_id=None, pre_tool_block_checked=False)

    # Pre-execution gates
    pre_call_block_message: Optional[Callable[[str, dict, str], Optional[str]]] = None
    # signature: (tool_name, args, task_id) → block_message_or_None
    guardrail_check: Optional[Callable[[str, dict], Any]] = None
    # signature: (tool_name, args) → ToolGuardrailDecision-shaped object with .allows_execution
    guardrail_block_result: Optional[Callable[[Any], str]] = None
    # signature: (decision) → block_result_str

    # Post-execution hooks
    append_guardrail_observation: Optional[Callable[..., Any]] = None
    # signature: (tool_name, args, result, *, failed=False) → modified_result
    record_file_mutation: Optional[Callable[[str, dict, Any, bool], None]] = None
    # signature: (tool_name, args, result, is_error)
    format_result_content: Optional[Callable[[str, Any], Any]] = None
    # signature: (tool_name, result) → model-formatted content
    compute_subdir_hints: Optional[Callable[[str, dict], Any]] = None
    # signature: (tool_name, args) → hint_string_or_truthy_value
    on_tool_completed: Optional[Callable[[str, dict], None]] = None
    # nudge counter hook: agent decides whether to reset _turns_since_memory etc.

    # Pre-execute checkpoint snapshot (write_file, patch, terminal-destructive)
    pre_execute_checkpoint: Optional[Callable[[str, dict, str], None]] = None
    # signature: (tool_name, args, effective_task_id)

    # Concurrent-path worker thread setup — agent propagates approval/sudo
    # callbacks and activity callback to the worker thread. Called inside
    # _run_tool before invoking the tool.
    parent_thread_setup: Optional[Callable[[], None]] = None
    # signature: () — called inside worker thread context

    # Concurrent-path worker thread teardown — clears thread-local state
    # so a recycled worker doesn't hold stale references.
    worker_thread_teardown: Optional[Callable[[], None]] = None
    # signature: () — called in finally block

    # Tool failure detection — the (is_error, _) tuple used to decide
    # logging severity and downstream branching.
    detect_tool_failure: Optional[Callable[[str, Any], tuple]] = None

    # Multimodal helpers (called inline; kept as callbacks so the
    # executor doesn't import display-aware utilities directly).
    is_multimodal_result: Optional[Callable[[Any], bool]] = None
    append_subdir_hint_to_multimodal: Optional[Callable[[Any, str], None]] = None
    persist_tool_result: Optional[Callable[..., Any]] = None
    # signature: (*, content, tool_name, tool_use_id, env) → persisted_or_passthrough


# ── Config snapshot ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutorConfig:
    """Read-only config the executor consults for behavior decisions.
    All values are immutable for the duration of one ``execute_batch``
    call. The caller refreshes per batch if values change."""
    quiet_mode: bool = False
    verbose_logging: bool = False
    log_prefix: str = ""
    log_prefix_chars: int = 100
    active_model: Optional[str] = None
    max_workers: Optional[int] = None
    # Per-tool delay in the sequential path. Sprint 31 Phase 1b — the
    # legacy AIAgent.tool_delay attribute lives here now; the executor
    # consults it between tools when sleeping is desired (rare).
    tool_delay_seconds: float = 0.0
    # Active-env resolver — the executor calls it lazily inside per-tool
    # post-processing (persist_tool_result + budget enforcement need it).
    # Kept on config rather than callbacks because it's a pure derivation
    # from effective_task_id, not Agent state.
    env_for_task: Optional[Callable[[str], Any]] = None


# ── Interrupt protocol ──────────────────────────────────────────────────


class InterruptToken(Protocol):
    """Thread-safe interrupt signal. The executor polls ``is_set()``
    at iteration boundaries and inside long-running operations.

    The concurrent path additionally needs ``set_for_thread`` and
    ``clear_for_thread`` to propagate interrupts to specific worker
    threads (matches the Agent's prior per-thread interrupt fan-out
    via ``tools.interrupt._set_interrupt``)."""

    def is_set(self) -> bool: ...

    def set_for_thread(self, thread_id: int) -> None: ...

    def clear_for_thread(self, thread_id: int) -> None: ...


# ── ExecutionContext ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionContext:
    """Everything the executor needs for ONE batch. Constructed by
    the caller per yield. Frozen — the executor reads from it but
    never mutates, and there is no path back to the Agent through
    this object."""
    intents: List[ToolIntent]
    tool_registry: Any  # tools.registry.ToolRegistry; opaque to avoid circular import
    callbacks: ObservabilityCallbacks
    side_effects: SideEffectCallbacks
    interrupt: InterruptToken
    config: ExecutorConfig
    tool_guardrails: Any = None  # guardrail rules (NOT evaluation state)
    effective_task_id: str = ""
    api_call_count: int = 0


# ── The executor ───────────────────────────────────────────────────────


class ToolExecutor:
    """Owns the thread pool and the tool-batch execution machinery.

    One instance per ``AIAgent`` (transition) or per ``Dispatcher``
    (Phase 2+). Stateful only in the worker-thread tracking set,
    which the Agent's ``interrupt()`` fan-out reads to deliver
    per-thread interrupts.
    """

    def __init__(self) -> None:
        # Concurrent-tool worker thread tracking. The Agent's
        # interrupt() fan-out reads this set to deliver per-thread
        # signals (see grove.tool_executor.InterruptToken). The set
        # is mutated only inside _run_tool's enter/exit under the
        # lock.
        self._worker_threads: set = set()
        self._worker_threads_lock = threading.Lock()

    # Read-only views for the Agent's interrupt fan-out machinery.
    @property
    def worker_threads(self) -> set:
        with self._worker_threads_lock:
            return set(self._worker_threads)

    @property
    def worker_threads_lock(self) -> threading.Lock:
        return self._worker_threads_lock

    # ── execute_batch_concurrent ─────────────────────────────────

    def execute_batch_concurrent(self, ctx: ExecutionContext) -> List[ToolResult]:
        """Execute the batch concurrently using a thread pool.

        Returns ``List[ToolResult]`` in input order. The caller is
        responsible for appending tool messages to its conversation
        history, draining pending steer, and enforcing per-turn
        budget — those orchestration concerns live outside the
        executor by design (GATE-B / GATE-A § extraction boundary).
        """
        num_tools = len(ctx.intents)
        if num_tools == 0:
            return []

        cb = ctx.callbacks
        sx = ctx.side_effects
        cfg = ctx.config

        # ── Pre-flight: interrupt check ──────────────────────────
        if ctx.interrupt.is_set():
            if cb.log_interrupt_message is not None:
                try:
                    cb.log_interrupt_message(
                        f"{cfg.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)"
                    )
                except Exception:
                    pass
            cancelled: List[ToolResult] = []
            for intent in ctx.intents:
                cancelled.append(ToolResult(
                    intent_id=intent.call_id,
                    tool_name=intent.tool_name,
                    tool_args=dict(intent.arguments or {}),
                    success=False,
                    content=f"[Tool execution cancelled — {intent.tool_name} was skipped due to user interrupt]",
                    error="interrupted",
                    latency_s=0.0,
                    blocked=False,
                ))
            return cancelled

        # ── Parse + pre-execution bookkeeping per intent ─────────
        # Each entry: (intent, function_args, block_result, blocked_by_guardrail)
        parsed: List[tuple] = []
        for intent in ctx.intents:
            function_name = intent.tool_name
            function_args = dict(intent.arguments or {})

            # Nudge counter resets — fire the hook so the Agent owns
            # the actual counter mutation.
            if sx.on_tool_completed is not None:
                try:
                    sx.on_tool_completed(function_name, function_args)
                except Exception:
                    pass

            # Checkpoint snapshot for file-mutating and destructive
            # commands. The callback decides whether and where to snap.
            if sx.pre_execute_checkpoint is not None:
                try:
                    sx.pre_execute_checkpoint(function_name, function_args, ctx.effective_task_id)
                except Exception:
                    pass

            # Pre-call block check (plugin hook)
            block_result: Optional[str] = None
            blocked_by_guardrail = False
            block_message: Optional[str] = None
            if sx.pre_call_block_message is not None:
                try:
                    block_message = sx.pre_call_block_message(
                        function_name, function_args, ctx.effective_task_id,
                    )
                except Exception:
                    block_message = None

            if block_message is not None:
                import json as _json
                block_result = _json.dumps({"error": block_message}, ensure_ascii=False)
            elif sx.guardrail_check is not None:
                try:
                    decision = sx.guardrail_check(function_name, function_args)
                    if not decision.allows_execution:
                        if sx.guardrail_block_result is not None:
                            block_result = sx.guardrail_block_result(decision)
                        else:
                            block_result = '{"error": "blocked by guardrail"}'
                        blocked_by_guardrail = True
                except Exception:
                    pass

            parsed.append((intent, function_args, block_result, blocked_by_guardrail))

        # ── Batch-header display ─────────────────────────────────
        tool_names_str = ", ".join(intent.tool_name for intent, *_ in parsed)
        if not cfg.quiet_mode and cb.log_batch_header_line is not None:
            try:
                cb.log_batch_header_line(num_tools, tool_names_str)
            except Exception:
                pass

        # Per-tool call-line print
        if not cfg.quiet_mode and cb.log_tool_call_line is not None:
            for i, (intent, args, _block, _blocked) in enumerate(parsed, 1):
                try:
                    cb.log_tool_call_line(i, intent.tool_name, args)
                except Exception:
                    pass

        # Fire on_tool_progress("tool.started") for runnable intents
        if cb.on_tool_progress is not None:
            for intent, args, block_result, _blocked in parsed:
                if block_result is not None:
                    continue
                try:
                    cb.on_tool_progress("tool.started", intent.tool_name, preview=None, args=args)
                except Exception as cb_err:
                    logger.debug("Tool progress callback error: %s", cb_err)

        # Fire on_tool_start for runnable intents
        if cb.on_tool_start is not None:
            for intent, args, block_result, _blocked in parsed:
                if block_result is not None:
                    continue
                try:
                    cb.on_tool_start(intent.call_id, intent.tool_name, args)
                except Exception as cb_err:
                    logger.debug("Tool start callback error: %s", cb_err)

        # ── Concurrent execution ─────────────────────────────────
        # Each slot stores (tool_name, args, content, duration_s, is_error, blocked)
        # NOTE: the per-slot tuple shape mirrors the prior implementation
        # so the post-execution loop translates cleanly.
        results_slots: List[Optional[tuple]] = [None] * num_tools
        for i, (intent, args, block_result, blocked_by_guardrail) in enumerate(parsed):
            if block_result is not None:
                results_slots[i] = (intent.tool_name, args, block_result, 0.0, True, True)

        # Touch activity so the gateway knows we're executing
        if cb.touch_activity is not None:
            try:
                cb.touch_activity(f"executing {num_tools} tools concurrently: {tool_names_str}")
            except Exception:
                pass

        def _run_tool(slot_index: int, intent: ToolIntent, function_args: dict) -> None:
            """Worker function executed in a thread."""
            # Register this worker tid so the agent can fan out an
            # interrupt to it.
            _worker_tid = threading.current_thread().ident
            with self._worker_threads_lock:
                self._worker_threads.add(_worker_tid)
            # Race: if the agent was interrupted between fan-out and
            # this thread's registration, apply the interrupt to our
            # own tid now so the per-tool interrupt poll trips.
            if ctx.interrupt.is_set():
                try:
                    ctx.interrupt.set_for_thread(_worker_tid)
                except Exception:
                    pass

            # Parent-thread setup (approval/sudo/activity callbacks
            # propagation). The agent's wiring snapshots its parent
            # state before submission and applies it inside each
            # worker. Errors are swallowed — never let setup failure
            # block tool execution.
            if ctx.side_effects.parent_thread_setup is not None:
                try:
                    ctx.side_effects.parent_thread_setup()
                except Exception:
                    pass

            start = time.time()
            try:
                result = ctx.side_effects.invoke_tool(
                    intent.tool_name,
                    function_args,
                    ctx.effective_task_id,
                    tool_call_id=intent.call_id,
                    pre_tool_block_checked=True,
                )
            except Exception as tool_error:
                result = f"Error executing tool '{intent.tool_name}': {tool_error}"
                logger.error(
                    "invoke_tool raised for %s: %s",
                    intent.tool_name, tool_error, exc_info=True,
                )
            duration = time.time() - start

            is_error = False
            if ctx.side_effects.detect_tool_failure is not None:
                try:
                    is_error_flag, _ = ctx.side_effects.detect_tool_failure(intent.tool_name, result)
                    is_error = bool(is_error_flag)
                except Exception:
                    is_error = False

            if is_error:
                try:
                    preview = result[:200] if isinstance(result, str) else str(result)[:200]
                except Exception:
                    preview = "<non-string result>"
                logger.info("tool %s failed (%.2fs): %s", intent.tool_name, duration, preview)
            else:
                try:
                    rlen = len(result) if isinstance(result, str) else len(str(result))
                except Exception:
                    rlen = 0
                logger.info("tool %s completed (%.2fs, %d chars)", intent.tool_name, duration, rlen)

            results_slots[slot_index] = (intent.tool_name, function_args, result, duration, is_error, False)

            # Tear down worker-tid tracking.
            with self._worker_threads_lock:
                self._worker_threads.discard(_worker_tid)
            try:
                ctx.interrupt.clear_for_thread(_worker_tid)
            except Exception:
                pass
            if ctx.side_effects.worker_thread_teardown is not None:
                try:
                    ctx.side_effects.worker_thread_teardown()
                except Exception:
                    pass

        # ── Batch-start display hook (spinner setup on agent) ────
        if cb.batch_started is not None:
            try:
                cb.batch_started(num_tools, tool_names_str, "concurrent")
            except Exception:
                pass

        try:
            runnable = [
                (i, intent, args)
                for i, (intent, args, block_result, _blocked) in enumerate(parsed)
                if block_result is None
            ]
            futures: List[concurrent.futures.Future] = []
            if runnable:
                max_workers_eff = cfg.max_workers or _MAX_TOOL_WORKERS
                max_workers_eff = min(len(runnable), max_workers_eff)
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_eff) as pool:
                    for slot_i, intent, args in runnable:
                        # Propagate ContextVars; mirrors asyncio.to_thread.
                        run_ctx = contextvars.copy_context()
                        f = pool.submit(run_ctx.run, _run_tool, slot_i, intent, args)
                        futures.append(f)

                    # Wait with periodic heartbeats + interrupt polling.
                    conc_start = time.time()
                    interrupt_logged = False
                    while True:
                        done, not_done = concurrent.futures.wait(futures, timeout=5.0)
                        if not not_done:
                            break

                        if ctx.interrupt.is_set():
                            if not interrupt_logged:
                                interrupt_logged = True
                                if cb.vprint is not None:
                                    try:
                                        cb.vprint(
                                            f"{cfg.log_prefix}⚡ Interrupt: cancelling "
                                            f"{len(not_done)} pending concurrent tool(s)",
                                            force=True,
                                        )
                                    except Exception:
                                        pass
                            for f in not_done:
                                f.cancel()
                            # Give already-running tools a moment to exit
                            concurrent.futures.wait(not_done, timeout=3.0)
                            break

                        conc_elapsed = int(time.time() - conc_start)
                        if conc_elapsed > 0 and conc_elapsed % 30 < 6:
                            still_running = []
                            for f in not_done:
                                try:
                                    idx = futures.index(f)
                                    still_running.append(parsed[idx][0].tool_name)
                                except (ValueError, IndexError):
                                    pass
                            if cb.touch_activity is not None:
                                try:
                                    cb.touch_activity(
                                        f"concurrent tools running ({conc_elapsed}s, "
                                        f"{len(not_done)} remaining: {', '.join(still_running[:3])})"
                                    )
                                except Exception:
                                    pass
        finally:
            # Batch-completed display hook (spinner.stop on agent)
            if cb.batch_completed is not None:
                try:
                    completed_count = sum(1 for r in results_slots if r is not None)
                    total_dur = sum(r[3] for r in results_slots if r is not None)
                    cb.batch_completed(completed_count, num_tools, total_dur)
                except Exception:
                    pass

        # ── Post-execution: format ToolResults + fire post hooks ─
        results: List[ToolResult] = []
        for i, (intent, args, block_result, blocked_by_guardrail) in enumerate(parsed):
            r = results_slots[i]
            blocked_flag = False
            if r is None:
                # Tool was cancelled or thread didn't return
                if ctx.interrupt.is_set():
                    function_result: Any = (
                        f"[Tool execution cancelled — {intent.tool_name} was skipped due to user interrupt]"
                    )
                else:
                    function_result = f"Error executing tool '{intent.tool_name}': thread did not return a result"
                tool_duration = 0.0
                is_error = True
            else:
                _name, _args, function_result, tool_duration, is_error, blocked_flag = r

            if not blocked_flag and sx.append_guardrail_observation is not None:
                try:
                    function_result = sx.append_guardrail_observation(
                        intent.tool_name, args, function_result, failed=is_error,
                    )
                except Exception:
                    pass

            if not blocked_flag and sx.record_file_mutation is not None:
                try:
                    sx.record_file_mutation(intent.tool_name, args, function_result, is_error)
                except Exception as ver_err:
                    logger.debug("file-mutation verifier record failed: %s", ver_err)

            if not blocked_flag and cb.on_tool_progress is not None:
                try:
                    cb.on_tool_progress(
                        "tool.completed", intent.tool_name,
                        preview=None, args=None,
                        duration=tool_duration, is_error=is_error,
                    )
                except Exception as cb_err:
                    logger.debug("Tool progress callback error: %s", cb_err)

            # Per-tool completion display
            if cb.log_tool_complete_line is not None:
                try:
                    cb.log_tool_complete_line(
                        i + 1, intent.tool_name, dict(intent.arguments or {}),
                        function_result, tool_duration,
                    )
                except Exception:
                    pass

            # Activity touch on completion
            if cb.touch_activity is not None:
                try:
                    cb.touch_activity(f"tool completed: {intent.tool_name} ({tool_duration:.1f}s)")
                except Exception:
                    pass

            # on_tool_complete callback (UI-facing)
            if not blocked_flag and cb.on_tool_complete is not None:
                try:
                    cb.on_tool_complete(intent.call_id, intent.tool_name, args, function_result)
                except Exception as cb_err:
                    logger.debug("Tool complete callback error: %s", cb_err)

            # Persist tool result (turn-budget storage). The helper
            # signature mirrors the prior maybe_persist_tool_result call;
            # the executor invokes via callback so it doesn't import
            # tools.tool_result_storage directly.
            if sx.persist_tool_result is not None and sx.is_multimodal_result is not None:
                try:
                    if not sx.is_multimodal_result(function_result):
                        env = cfg.env_for_task(ctx.effective_task_id) if cfg.env_for_task else None
                        function_result = sx.persist_tool_result(
                            content=function_result,
                            tool_name=intent.tool_name,
                            tool_use_id=intent.call_id,
                            env=env,
                        )
                except Exception:
                    pass

            # Subdir hint append
            if sx.compute_subdir_hints is not None:
                try:
                    subdir_hints = sx.compute_subdir_hints(intent.tool_name, args)
                    if subdir_hints:
                        if sx.is_multimodal_result is not None and sx.is_multimodal_result(function_result):
                            if sx.append_subdir_hint_to_multimodal is not None:
                                sx.append_subdir_hint_to_multimodal(function_result, subdir_hints)
                        else:
                            function_result = (function_result or "") + (subdir_hints or "")
                except Exception:
                    pass

            # Model-aware content formatting
            content_formatted: Any = function_result
            if sx.format_result_content is not None:
                try:
                    content_formatted = sx.format_result_content(intent.tool_name, function_result)
                except Exception:
                    content_formatted = function_result

            results.append(ToolResult(
                intent_id=intent.call_id,
                tool_name=intent.tool_name,
                tool_args=args,
                success=not is_error and not blocked_flag,
                content=content_formatted,
                error=None if not is_error else (str(function_result)[:500] if isinstance(function_result, str) else None),
                latency_s=tool_duration,
                blocked=blocked_flag,
            ))

        return results

    # ── execute_batch_sequential ─────────────────────────────────

    def execute_batch_sequential(self, ctx: ExecutionContext) -> List[ToolResult]:
        """Execute the batch sequentially, one tool at a time.

        Sprint 31 Phase 1b. Used for batches that look entangled —
        file mutations against overlapping paths, interactive tools,
        single-call yields. The parallelism decision is made upstream
        (``tools._should_parallelize_tool_batch`` style logic in the
        agent's wrapper); the executor exposes both ``execute_batch_concurrent``
        and ``execute_batch_sequential`` and the caller picks.

        Returns ``List[ToolResult]`` in input order. The caller
        (Dispatcher, or the transition-period shim) appends tool
        messages to the conversation, drains pending steer, and
        enforces per-turn budget — orchestration lives outside the
        executor by design.

        Mid-batch interrupt: if ``ctx.interrupt.is_set()`` becomes
        True after a tool finishes, the remaining intents are
        skipped with cancellation messages (mirrors the legacy
        sequential behavior).
        """
        num_tools = len(ctx.intents)
        if num_tools == 0:
            return []

        cb = ctx.callbacks
        sx = ctx.side_effects
        cfg = ctx.config

        results: List[ToolResult] = []

        for i, intent in enumerate(ctx.intents, 1):
            # ── Pre-iteration interrupt check ────────────────────
            # Triggers for both the pre-flight case (interrupt set
            # before the batch starts) and the mid-batch case
            # (interrupt set after a prior tool finished).
            if ctx.interrupt.is_set():
                remaining = ctx.intents[i - 1:]
                if remaining and cb.vprint is not None:
                    try:
                        cb.vprint(
                            f"{cfg.log_prefix}⚡ Interrupt: skipping {len(remaining)} tool call(s)",
                            force=True,
                        )
                    except Exception:
                        pass
                for skipped in remaining:
                    results.append(ToolResult(
                        intent_id=skipped.call_id,
                        tool_name=skipped.tool_name,
                        tool_args=dict(skipped.arguments or {}),
                        success=False,
                        content=(
                            f"[Tool execution cancelled — {skipped.tool_name} was "
                            f"skipped due to user interrupt]"
                        ),
                        error="interrupted",
                        latency_s=0.0,
                        blocked=False,
                    ))
                break

            function_name = intent.tool_name
            function_args = dict(intent.arguments or {})

            # ── Pre-call block / guardrail gates ─────────────────
            block_result: Optional[str] = None
            blocked_by_guardrail = False
            block_message: Optional[str] = None
            if sx.pre_call_block_message is not None:
                try:
                    block_message = sx.pre_call_block_message(
                        function_name, function_args, ctx.effective_task_id,
                    )
                except Exception:
                    block_message = None

            if block_message is not None:
                import json as _json
                block_result = _json.dumps({"error": block_message}, ensure_ascii=False)
            elif sx.guardrail_check is not None:
                try:
                    decision = sx.guardrail_check(function_name, function_args)
                    if not decision.allows_execution:
                        if sx.guardrail_block_result is not None:
                            block_result = sx.guardrail_block_result(decision)
                        else:
                            block_result = '{"error": "blocked by guardrail"}'
                        blocked_by_guardrail = True
                except Exception:
                    pass

            execution_blocked = block_result is not None

            # Nudge counter reset (callback) — only when actually executing
            if not execution_blocked and sx.on_tool_completed is not None:
                try:
                    sx.on_tool_completed(function_name, function_args)
                except Exception:
                    pass

            # ── Per-tool call-line display ───────────────────────
            if not cfg.quiet_mode and cb.log_tool_call_line is not None:
                try:
                    cb.log_tool_call_line(i, function_name, function_args)
                except Exception:
                    pass

            # ── Activity + callback hooks (for executing tools) ──
            if not execution_blocked and cb.touch_activity is not None:
                try:
                    cb.touch_activity(f"executing tool: {function_name}")
                except Exception:
                    pass

            if not execution_blocked and cb.on_tool_progress is not None:
                try:
                    cb.on_tool_progress(
                        "tool.started", function_name,
                        preview=None, args=function_args,
                    )
                except Exception as cb_err:
                    logger.debug("Tool progress callback error: %s", cb_err)

            if not execution_blocked and cb.on_tool_start is not None:
                try:
                    cb.on_tool_start(intent.call_id, function_name, function_args)
                except Exception as cb_err:
                    logger.debug("Tool start callback error: %s", cb_err)

            # Pre-execute checkpoint (write_file / patch / terminal)
            if not execution_blocked and sx.pre_execute_checkpoint is not None:
                try:
                    sx.pre_execute_checkpoint(
                        function_name, function_args, ctx.effective_task_id,
                    )
                except Exception:
                    pass

            # ── Per-tool display open (spinner-per-tool, agent owns) ──
            if not execution_blocked and cb.tool_display_open is not None:
                try:
                    cb.tool_display_open(function_name, function_args)
                except Exception:
                    pass

            # ── Invoke the tool ──────────────────────────────────
            tool_start_time = time.time()
            if execution_blocked:
                function_result: Any = block_result
                tool_duration = 0.0
                is_error = True
            else:
                try:
                    function_result = sx.invoke_tool(
                        function_name,
                        function_args,
                        ctx.effective_task_id,
                        tool_call_id=intent.call_id,
                        pre_tool_block_checked=True,
                    )
                except OperatorInputRequired:
                    # Sprint 67 — control-flow contract, not a tool error.
                    # The clarify tool (NEVER-parallel, runs here in the
                    # sequential executor) raises this from a store-and-
                    # resume surface's callback to yield control and deliver
                    # its question as the turn's response. It MUST propagate
                    # past this generic catch, not become an "Error
                    # executing tool" observation. (Redundant under the
                    # BaseException base class; kept explicit so the
                    # contract is visible at the tool boundary — see
                    # grove/operator_input.py.)
                    raise
                except Exception as tool_error:
                    function_result = f"Error executing tool '{function_name}': {tool_error}"
                    logger.error(
                        "invoke_tool raised for %s: %s",
                        function_name, tool_error, exc_info=True,
                    )
                tool_duration = time.time() - tool_start_time
                is_error = False
                if sx.detect_tool_failure is not None:
                    try:
                        is_err_flag, _ = sx.detect_tool_failure(function_name, function_result)
                        is_error = bool(is_err_flag)
                    except Exception:
                        is_error = False

            # ── Per-tool display close ──────────────────────────
            if not execution_blocked and cb.tool_display_close is not None:
                try:
                    cb.tool_display_close(
                        function_name, dict(function_args or {}),
                        function_result, tool_duration,
                    )
                except Exception:
                    pass

            # ── Post-execution: guardrail observation + telemetry ──
            if not execution_blocked and sx.append_guardrail_observation is not None:
                try:
                    function_result = sx.append_guardrail_observation(
                        function_name, function_args, function_result, failed=is_error,
                    )
                except Exception:
                    pass

            if is_error:
                try:
                    preview = (
                        function_result[:200] if isinstance(function_result, str)
                        else str(function_result)[:200]
                    )
                except Exception:
                    preview = "<non-string result>"
                logger.warning(
                    "Tool %s returned error (%.2fs): %s",
                    function_name, tool_duration, preview,
                )
            else:
                try:
                    rlen = (
                        len(function_result) if isinstance(function_result, str)
                        else len(str(function_result))
                    )
                except Exception:
                    rlen = 0
                logger.info(
                    "tool %s completed (%.2fs, %d chars)",
                    function_name, tool_duration, rlen,
                )

            if not execution_blocked and sx.record_file_mutation is not None:
                try:
                    sx.record_file_mutation(
                        function_name, function_args, function_result, is_error,
                    )
                except Exception as ver_err:
                    logger.debug("file-mutation verifier record failed: %s", ver_err)

            if not execution_blocked and cb.on_tool_progress is not None:
                try:
                    cb.on_tool_progress(
                        "tool.completed", function_name,
                        preview=None, args=None,
                        duration=tool_duration, is_error=is_error,
                    )
                except Exception as cb_err:
                    logger.debug("Tool progress callback error: %s", cb_err)

            if cb.touch_activity is not None:
                try:
                    cb.touch_activity(
                        f"tool completed: {function_name} ({tool_duration:.1f}s)",
                    )
                except Exception:
                    pass

            if not execution_blocked and cb.on_tool_complete is not None:
                try:
                    cb.on_tool_complete(
                        intent.call_id, function_name, function_args, function_result,
                    )
                except Exception as cb_err:
                    logger.debug("Tool complete callback error: %s", cb_err)

            # ── Per-tool persist + subdir hints + model-aware format ──
            if (
                sx.persist_tool_result is not None
                and sx.is_multimodal_result is not None
            ):
                try:
                    if not sx.is_multimodal_result(function_result):
                        env = (
                            cfg.env_for_task(ctx.effective_task_id)
                            if cfg.env_for_task else None
                        )
                        function_result = sx.persist_tool_result(
                            content=function_result,
                            tool_name=function_name,
                            tool_use_id=intent.call_id,
                            env=env,
                        )
                except Exception:
                    pass

            if sx.compute_subdir_hints is not None:
                try:
                    subdir_hints = sx.compute_subdir_hints(function_name, function_args)
                    if subdir_hints:
                        if (
                            sx.is_multimodal_result is not None
                            and sx.is_multimodal_result(function_result)
                        ):
                            if sx.append_subdir_hint_to_multimodal is not None:
                                sx.append_subdir_hint_to_multimodal(function_result, subdir_hints)
                        else:
                            function_result = (function_result or "") + (subdir_hints or "")
                except Exception:
                    pass

            content_formatted: Any = function_result
            if sx.format_result_content is not None:
                try:
                    content_formatted = sx.format_result_content(function_name, function_result)
                except Exception:
                    content_formatted = function_result

            # ── Per-tool completion-line display ────────────────
            if cb.log_tool_complete_line is not None:
                try:
                    cb.log_tool_complete_line(
                        i, function_name, dict(function_args or {}),
                        function_result, tool_duration,
                    )
                except Exception:
                    pass

            results.append(ToolResult(
                intent_id=intent.call_id,
                tool_name=function_name,
                tool_args=function_args,
                success=not is_error and not execution_blocked,
                content=content_formatted,
                error=(
                    None if not is_error
                    else (
                        str(function_result)[:500] if isinstance(function_result, str)
                        else None
                    )
                ),
                latency_s=tool_duration,
                blocked=execution_blocked,
            ))

            # ── Mid-batch interrupt check ────────────────────────
            # Same shape as the pre-iteration check at the top of
            # the loop, but specifically catches the case where the
            # interrupt fired DURING this tool's execution.
            if ctx.interrupt.is_set() and i < num_tools:
                remaining = ctx.intents[i:]
                if cb.vprint is not None:
                    try:
                        cb.vprint(
                            f"{cfg.log_prefix}⚡ Interrupt: skipping "
                            f"{len(remaining)} remaining tool call(s)",
                            force=True,
                        )
                    except Exception:
                        pass
                for skipped in remaining:
                    results.append(ToolResult(
                        intent_id=skipped.call_id,
                        tool_name=skipped.tool_name,
                        tool_args=dict(skipped.arguments or {}),
                        success=False,
                        content=(
                            f"[Tool execution skipped — {skipped.tool_name} was "
                            f"not started. User sent a new message]"
                        ),
                        error="interrupted",
                        latency_s=0.0,
                        blocked=False,
                    ))
                break

            # ── Tool delay between tools ────────────────────────
            if cfg.tool_delay_seconds > 0 and i < num_tools:
                time.sleep(cfg.tool_delay_seconds)

        return results
