"""Grove Dispatcher — runtime entry point per GRV-005.

Sprint 26 (dispatch-pipeline-implementation-v1). The Dispatcher owns
the pre-agent pipeline per GRV-005 § II: substrate capture, heavy-
resource caching, tier selection, agent construction, intent-yield
classification, tool execution, and the Sovereign-Prompt disposition
flow at Andon halts.

Sprint 26 phase reference:
  * Phase 1a — structural seam (RuntimeContext + Dispatcher skeleton)
    landed the substrate snapshot the Agent reads through instead of
    touching ``os.environ`` / config directly.
  * Phase 1b — heavy-resource caching (LLM client, tool registry,
    memory store, compression-feasibility probe) dropped per-turn
    construction cost from 176ms → ~24ms (A7 averted).
  * Phase 2 — intent protocol types in grove.intents.
  * Phase 3 — generator-shaped agent loop. The Agent yields
    ``List[ToolIntent]`` and ``FinalResponse``; the Dispatcher's
    ``dispatch_turn`` consumes the generator under GRV-005 § II/III
    authority.
  * Phase 4 — tool-zone classification at intent yield. The Dispatcher
    classifies every yielded batch; Yellow / Red triggers AndonHalt
    (raised internally; caught by the dispatch loop).
  * Phase 5 — mid-execution Andon disposition. AndonHalt is caught
    and routed through the Sovereign Prompt; operator picks Skip
    (inject denial Observation; generator continues) or Drop
    (gen.close() flushes volatile turn state; persistent state
    unchanged). The ``pending_andon`` persistent marker survives a
    process restart so a session killed mid-prompt is recoverable.

Backward compatibility: existing callers (cli.py, oneshot.py, gateway
paths, tests) construct ``AIAgent(...)`` directly without going through
the Dispatcher. When ``runtime_ctx`` is ``None``, the Agent's substrate
sites fall through to the legacy direct-read path. Phase 7 cleanup
removes the fallback once every caller routes through the Dispatcher.
"""

from __future__ import annotations

import json as _json_mod
import logging
import os
import sys as _sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── RuntimeContext ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeContext:
    """A snapshot of substrate state the Dispatcher hands to the Agent.

    Sprint 26 D4 named 14 substrate violations in ``AIAgent.__init__`` and
    its runtime methods — direct ``os.environ`` reads (10) and direct
    ``hermes_cli.config.load_config()`` calls (4). GRV-005 § III forbids
    the Agent from accessing the system substrate; the Dispatcher captures
    that substrate once and passes the snapshot via this dataclass.

    Sprint 26 Phase 1b extends the snapshot to carry pre-built heavy
    resources the Agent's construction would otherwise rebuild per-turn:
    the tool registry, memory store, context compressor, and LLM clients
    (D5 enumeration). When these fields are populated, the Agent's
    construction sites skip their build step and use the injected
    instance. When ``None``, the Agent builds the resource itself
    (backward-compat for callers that haven't been migrated to a
    Dispatcher; removed at Phase 7 cleanup).

    The Agent reads through ``env_get()`` / ``config_get()`` (or via the
    helper methods on ``AIAgent`` that wrap them). When the Agent's
    ``_runtime_ctx`` attribute is ``None``, those helpers fall back to
    direct substrate reads.

    Frozen so the Agent cannot mutate substrate state during a turn; the
    contract is one-way (Dispatcher writes once, Agent reads only).
    """

    #: Snapshot of ``os.environ`` taken at Dispatcher construction.
    #: A plain dict copy — mutations to ``os.environ`` after the snapshot
    #: are not reflected. Operators who change an env var mid-session
    #: must restart the Dispatcher.
    env: Dict[str, str] = field(default_factory=dict)

    #: Snapshot of ``hermes_cli.config.load_config()`` taken at Dispatcher
    #: construction. The underlying ``hermes_cli.config`` module also
    #: caches in-memory after first load, so this snapshot is consistent
    #: with subsequent direct reads in the same process — but the contract
    #: is that the Agent reads through this snapshot, not through the
    #: module-level cache.
    config: Dict[str, Any] = field(default_factory=dict)

    # ── Sprint 26 Phase 1b — heavy resource injection slots ──────────────
    # Optional pre-built instances the Dispatcher caches once and the
    # Agent uses without rebuilding. Each field is keyed to a D5-named
    # resource. When ``None``, the Agent falls back to its existing
    # construction path (backward-compat removed at Phase 7).

    #: Pre-built tool definition list per the operator's enabled /
    #: disabled toolset configuration. Cost when rebuilt: 20-100ms.
    #: Built by the Dispatcher when ``tools_key`` (toolsets +
    #: quiet_mode) matches; the Dispatcher's cache keys by that tuple.
    tools: Optional[List[Dict[str, Any]]] = None

    #: Pre-loaded memory store. Cost when rebuilt: 10-50ms (disk read of
    #: ``~/.grove/MEMORY.md`` + USER.md). Type is Any to avoid importing
    #: ``tools.memory_tool.MemoryStore`` here (the tools package depends
    #: on grove modules transitively in some paths).
    memory_store: Optional[Any] = None

    #: Per-model context-length cache keyed by model name (e.g.
    #: ``"claude-sonnet-4-6"`` → ``200000``). Populated by the Dispatcher
    #: at construction; the Agent reads from this dict instead of
    #: re-probing model metadata via ``get_model_context_length()``,
    #: which is the dominant cost (50-150ms) in the Context Compressor
    #: construction site.
    context_length_by_model: Dict[str, int] = field(default_factory=dict)

    #: Pre-built Anthropic SDK client keyed by (model, base_url). Cost
    #: when rebuilt: 50-150ms warm (re-wraps already-cached transport).
    #: Type Any to avoid importing the Anthropic SDK here.
    anthropic_client: Optional[Any] = None

    #: Pre-built OpenAI-wire client (the dispatcher's shared client for
    #: chat_completions and codex_responses). Cost when rebuilt: 30-80ms
    #: warm. Type Any to avoid importing the OpenAI SDK here.
    openai_client: Optional[Any] = None

    #: Cached result of the compression-feasibility probe (Sprint 26
    #: Phase 1b A7 mitigation). The probe performs a 125ms synchronous
    #: HTTP roundtrip to the configured auxiliary compression model's
    #: metadata endpoint — D5 enumerated context-compressor construction
    #: as one heavy resource but did not separate this sub-probe, which
    #: cProfile identified as 96% of the warm-path AIAgent.__init__ cost.
    #: Caching it eliminates the dominant per-turn latency tax.
    compression_probe: Optional["CompressionProbe"] = None

    def env_get(self, key: str, default: str = "") -> str:
        """Read one env var from the snapshot.

        Returns the snapshotted value if present, else ``default``.
        Cast helpers (``env_get_int``, ``env_get_float``) wrap this for
        common type coercion needs.
        """
        return self.env.get(key, default)

    def env_get_int(self, key: str, default: int) -> int:
        """Read an env var as int, falling back to ``default`` on missing
        or unparseable values.

        Mirrors the existing call-site pattern of
        ``int(os.getenv("GROVE_X", "1800"))`` — preserves the default
        type ('1800' as a string in the legacy path, defaulted via int()
        on coerce). Returns the default unchanged when the value is
        missing or fails int conversion.
        """
        raw = self.env.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            logger.debug(
                "[grove.dispatcher] env %s=%r could not be parsed as int; "
                "using default %d",
                key, raw, default,
            )
            return default

    def env_get_float(self, key: str, default: float) -> float:
        """Read an env var as float, falling back to ``default``."""
        raw = self.env.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            logger.debug(
                "[grove.dispatcher] env %s=%r could not be parsed as float; "
                "using default %f",
                key, raw, default,
            )
            return default

    def config_get(self, *path: str, default: Any = None) -> Any:
        """Walk a nested config path, returning ``default`` if any step
        is absent or non-mapping.

        Example: ``ctx.config_get("memory", "enabled", default=True)``
        returns ``config["memory"]["enabled"]`` if present, else ``True``.
        """
        node: Any = self.config
        for step in path:
            if not isinstance(node, dict) or step not in node:
                return default
            node = node[step]
        return node


@dataclass(frozen=True)
class CompressionProbe:
    """Cached result of the auxiliary compression model's context-window probe.

    AIAgent.__init__ calls ``_check_compression_model_feasibility`` which
    asks the configured auxiliary compression model for its context
    window via ``get_model_context_length`` — a synchronous HTTP call
    (typically to a local Ollama server). The Dispatcher runs this probe
    once per session and caches the result; the Agent's feasibility
    method uses the cached values for its downstream threshold-adjustment
    logic instead of re-running the HTTP call.

    Fields mirror the local variables in the Agent's probe block: the
    Agent's existing validation + side-effect logic runs unchanged on
    these values. Cache key for the Dispatcher is session-process-scoped
    (operators who change ``auxiliary.compression`` in config.yaml must
    restart the session for the new value to take effect).
    """

    #: Resolved auxiliary compression model name (e.g. ``"qwen2.5:32b"``).
    aux_model: str

    #: Context-window size in tokens, or ``None`` if the probe could not
    #: determine it (the Agent's downstream code handles ``None`` by
    #: falling back to ``MINIMUM_CONTEXT_LENGTH`` checks).
    aux_context: Optional[int]

    #: Resolved base URL of the auxiliary client, for warning-message labels.
    aux_base_url: str

    #: Resolved API key of the auxiliary client (the Agent only uses this
    #: to construct warning messages and re-probe metadata if needed).
    aux_api_key: str

    #: Resolved auxiliary provider name from ``auxiliary.compression``
    #: config (may be ``"auto"`` or empty).
    aux_cfg_provider: str


# ── Sovereign Prompt (Sprint 26 Phase 5) ──────────────────────────────────
#
# Sprint 27 Phase 2 extracted the handler implementations to
# grove/sovereign_prompt_handlers.py. The TTY handler keeps its old
# private alias here for back-compat with existing import sites
# (tests/grove/test_dispatch_turn.py and internal Dispatcher defaulting).
# New callers should import from grove.sovereign_prompt_handlers directly
# and choose the variant (tty / batch / gateway / test) that fits the
# caller's surface.

from grove.sovereign_prompt_handlers import (
    tty_sovereign_prompt as _default_sovereign_prompt,
)


# ── Andon halt exception (Sprint 26 Phase 4) ──────────────────────────────


class AndonHalt(RuntimeError):
    """Pipeline halt fired by the Dispatcher when a ToolIntent batch
    crosses the Yellow/Red zone boundary at intent-yield (GRV-005 § V).

    Phase 4 wires the classification + halt mechanism. Phase 5 adds the
    Sovereign Prompt UX and disposition semantics (Skip / Drop) that
    determine what the operator sees and how dispatch resumes. For
    Phase 4 MVP, the halt is internal: ``Dispatcher.dispatch_turn``
    catches the exception and returns a result dict carrying the halt
    metadata; no operator interaction yet.

    Per the D6 lock: classification applies to the entire batch. If a
    single ``ToolIntent`` in the batch hits Yellow or Red, the whole
    batch halts. The exception carries every intent in the batch (so
    Phase 5's Sovereign Prompt can show the full context) plus the
    index + ZoneResult of the triggering intent.
    """

    def __init__(
        self,
        intents: List[Any],
        zone_results: List[Any],
        triggering_index: int,
    ):
        self.intents = intents
        self.zone_results = zone_results
        self.triggering_index = triggering_index
        triggering = zone_results[triggering_index]
        self.zone = triggering.zone
        self.matched_rule = triggering.matched_rule
        self.source = triggering.source
        self.reason = getattr(triggering, "reason", None)
        self.pattern_key = getattr(triggering, "pattern_key", None)
        tool = intents[triggering_index].tool_name
        super().__init__(
            f"Andon halt: tool {tool!r} (intent #{triggering_index}) "
            f"classified as {self.zone} zone "
            f"(rule={self.matched_rule!r}, source={self.source})"
        )


# ── Dispatcher ────────────────────────────────────────────────────────────


class Dispatcher:
    """Grove Autonomaton runtime entry point per GRV-005 § II.

    Sprint 26 Phase 1a — skeleton only. The Dispatcher captures substrate
    (env + config) into a ``RuntimeContext`` that downstream Agent
    construction can read through. Heavy-resource caching, tool execution
    authority, and the generator-shaped agent loop land in subsequent
    phases (1b, 3, 4 respectively).

    GRV-005 § II names the Dispatcher's full responsibilities:

    * MUST own message-zone classification.
    * MUST select the tier before constructing the Agent.
    * MUST own tool execution.
    * MUST own escalation decisions.
    * MUST observe post-turn for Kaizen.

    Phase 1a populates none of these; the class exists as the
    architectural seam. Each subsequent phase adds methods that
    realize one of the bullets.

    The Dispatcher is constructed once per session. The ``RuntimeContext``
    it produces is frozen for the session's lifetime — operators who
    change an env var or edit ``~/.grove/config.yaml`` mid-session must
    restart the session for the change to take effect. (Sprint 27 may
    add a ``/reload`` slash command that rebuilds the Dispatcher.)
    """

    def __init__(
        self,
        *,
        runtime_ctx: Optional[RuntimeContext] = None,
        sovereign_prompt_handler: Optional[Callable[["AndonHalt"], str]] = None,
        kaizen_ledger_dir: Optional[Path] = None,
        intent_store: Optional[Any] = None,
        agent_kwargs: Optional[Dict[str, Any]] = None,
        session_db: Optional[Any] = None,
    ) -> None:
        """Capture the substrate snapshot and install Phase 5 disposition handler.

        Reads ``os.environ`` and calls ``hermes_cli.config.load_config()``
        — the two substrate surfaces D4 audited. The Agent constructed
        downstream via ``build_runtime_context()`` reads through the
        snapshot instead of repeating these calls.

        Phase 1b heavy resources (tool registry, memory store, LLM
        clients, context-length cache) are built lazily on first request
        via ``runtime_context_for(...)``. Construction at Dispatcher
        ``__init__`` time stays light so the Dispatcher itself does not
        impose a startup tax.

        Phase 5 disposition: ``sovereign_prompt_handler`` is the function
        the Dispatcher calls when an AndonHalt fires during
        ``dispatch_turn``. Defaults to the TTY-mode
        ``_default_sovereign_prompt``. Non-TTY callers (gateway, web)
        inject a callback that surfaces the prompt through their UX
        layer and returns one of ``"skip"`` or ``"drop"``.
        """
        # Sprint 26 Phase 7 hotfix: prime the zone classifier singleton so
        # Phase 4's classify() at ToolIntent yield has it ready.
        import grove.zones as _zones; _zones.initialize()
        # Sprint 33 — runtime_ctx injection. Callers that hold a constructed
        # RuntimeContext pass it directly; the env/config fallback below is
        # the runtime_ctx=None path Sprint 34 removes once every production
        # caller supplies its own context. The fallback's behavior is
        # byte-identical to the pre-Sprint-33 lazy bootstrap.
        if runtime_ctx is not None:
            self._base_runtime_ctx = runtime_ctx
        else:
            config = self._load_config_safely()
            self._base_runtime_ctx = RuntimeContext(
                env=dict(os.environ),
                config=config,
            )
        self._sovereign_prompt_handler: Callable[["AndonHalt"], str] = (
            sovereign_prompt_handler or _default_sovereign_prompt
        )
        # ── Sprint 26 Phase 6 — Kaizen Ledger (foreground/background split) ──
        # Per GRV-005 § IX(4): operational telemetry routes out-of-band
        # to an isolated ledger; conversational payload stays on the
        # active context. One ledger per session — instantiated lazily
        # in dispatch_turn since session_id is per-agent, not per-
        # dispatcher.
        self._kaizen_ledger_dir = kaizen_ledger_dir
        self._kaizen_ledgers: Dict[str, Any] = {}
        # Phase 6 tier-override pathway. Sprint 27's escalation handler
        # writes here when granting an EscalationRequest; for v1 the
        # operator can also set it directly (via a future /tier slash
        # command wiring). Keyed by session_id so concurrent sessions
        # don't share overrides.
        self._tier_overrides: Dict[str, str] = {}
        # ── Sprint 26 Phase 1b — heavy resource caches ───────────────
        # Keyed by deterministic call-shape tuples. Each entry holds the
        # built resource; the cache survives the Dispatcher's lifetime.
        # The Agent receives a RuntimeContext that has the relevant cache
        # entries promoted into its frozen fields.
        self._tools_cache: Dict[tuple, List[Dict[str, Any]]] = {}
        self._memory_store_cache: Optional[Any] = None
        self._anthropic_client_cache: Dict[tuple, Any] = {}
        self._context_length_cache: Dict[str, int] = {}
        self._compression_probe_cache: Optional[CompressionProbe] = None
        # ── Sprint 28 Phase 3 — intent record store + per-turn state ──
        # The IntentStore is optional so legacy / test Dispatchers (which
        # construct ``Dispatcher()`` with no kwargs) skip the write path
        # entirely. Production callers inject
        # ``intent_store=grove.intent_store.get_store()`` so every turn
        # writes a record on the feed-first layer. Sprint 33 Phase 2:
        # ``AIAgent.run_conversation``'s inline lazy build wires the
        # default store for callers that bypass the Dispatcher
        # inversion (mostly tests).
        self._intent_store = intent_store
        self._turn_counter: int = 0
        # Per-turn state — reset at the top of every ``dispatch_turn``.
        # Read at terminal write sites (FinalResponse, Drop, exception)
        # to populate the IntentRecord without threading state through
        # every internal call. Per-Dispatcher singleton design means
        # only one turn is in flight per Dispatcher at a time, so this
        # instance-attribute carrier is race-free in practice.
        self._current_turn_id: Optional[str] = None
        self._current_turn_classification: Optional[Any] = None
        self._current_turn_start: Optional[float] = None
        self._current_turn_tools_yielded: List[str] = []
        self._current_turn_user_message: Optional[str] = None
        self._current_turn_outcome_written: bool = False
        # Sprint 31 Phase 2 — api_call_count rides the ToolBatchYield
        # protocol from the agent's generator. The Dispatcher tracks the
        # most recent value here for terminal intent-record writes
        # (FinalResponse, Drop, exception) that need the per-turn count.
        # Replaces ``getattr(agent, "_current_api_call_count", 0)`` reads
        # against the deleted Sprint 26 GATE-D bridge field.
        self._current_turn_api_call_count: int = 0
        # ── Sprint 30 — escalation policy + counters ─────────────────
        # Policy loaded lazily on first use so test Dispatchers that
        # never see an EscalationRequest don't pay the file-read cost.
        # Counters keyed per session so concurrent gateway sessions
        # don't share budgets. Per-turn counter resets at dispatch_turn
        # entry.
        self._escalation_policy: Optional[Any] = None
        self._session_escalation_counts: Dict[str, int] = {}
        self._current_turn_escalations: int = 0
        # Track all escalation events that fired this turn so the
        # IntentRecord write at terminal sites captures them.
        self._current_turn_escalation_events: List[Dict[str, Any]] = []
        if self._intent_store is not None:
            # Sprint 28 Phase 3 — Implicit Success Sweep on Dispatcher
            # construction. Stale ``pending`` records from previous
            # processes / abandoned sessions finalize as success per
            # the policy: laptop closed ≈ task complete.
            try:
                swept = self._intent_store.sweep_stale_pending()
                if swept > 0:
                    logger.info(
                        "[grove.dispatcher] Implicit Success Sweep: "
                        "finalized %d stale pending intent record(s) "
                        "as success", swept,
                    )
            except Exception as exc:
                logger.warning(
                    "[grove.dispatcher] Intent Store sweep failed at "
                    "Dispatcher init: %r — intent writes will still "
                    "proceed", exc,
                )
        logger.debug(
            "[grove.dispatcher] runtime context captured: "
            "%d env vars, config keys=%s",
            len(self._base_runtime_ctx.env),
            sorted(self._base_runtime_ctx.config.keys())[:10],
        )

        # ── Sprint 33 — inversion of construction ─────────────────────
        # When ``agent_kwargs`` is provided, the Dispatcher constructs
        # the per-turn Agent and exposes it as ``self.agent``. This is
        # the sole sanctioned Agent construction path after Sprint 33
        # Phase 2 migrates the caller sites and deletes the lazy
        # singleton inside AIAgent.
        #
        # The import is gated on the construction branch — fires only
        # when an Agent is actually being built, which (a) preserves
        # backward compatibility for test Dispatchers that don't need
        # an Agent and (b) keeps module-load-time imports symmetric
        # with the existing TYPE_CHECKING guard at run_agent.py:60-61.
        # When Sprint 34 makes RuntimeContext required and every
        # Dispatcher constructs an Agent, this import moves to the
        # module top.
        # ── Sprint 39 — session authority ────────────────────────────
        # Dispatcher.session is the single Agent-path SessionDB
        # authority. Caller-supplied (CLI / gateway / TUI gateway pass
        # their own pre-constructed instance) or self-built lazily on
        # first open_session(). The Agent does not hold the handle;
        # Agent-side reads route through the dispatcher back-reference,
        # Agent-side writes route through declarative intents
        # (SessionRotateIntent / SessionUpdateTokensIntent).
        self._session_db_supplied: Optional[Any] = session_db
        self.session: Optional[Any] = session_db
        # Current session id. Generated by open_session() if no resume
        # target supplied; rotated by rotate_session() at compression
        # boundaries. Reads via the back-reference; the Agent does not
        # own its own copy after Phase 2.
        self.session_id: Optional[str] = None
        # Tracks whether the live session row exists (Sprint 26 era
        # ``_session_db_created`` semantics relocated). Lazy creation
        # defers the DB write to the first turn-boundary, preserving
        # the existing UX of empty sessions not surfacing in lists.
        self._session_row_created: bool = False

        self.agent: Optional[Any] = None
        if agent_kwargs is not None:
            from run_agent import AIAgent
            # Sprint 34 — the Dispatcher owns the substrate snapshot,
            # so forward it into the Agent. setdefault preserves any
            # explicit ctx the caller put into agent_kwargs (rare;
            # mostly tests that build a richer ctx).
            agent_kwargs.setdefault("runtime_ctx", self._base_runtime_ctx)
            self.agent = AIAgent(**agent_kwargs)
            self.agent._dispatcher_singleton = self

    @property
    def runtime_ctx(self) -> RuntimeContext:
        """The bare substrate-snapshot RuntimeContext.

        Sprint 26 Phase 1a backward-compat: callers that want only the
        env + config snapshot (no heavy-resource injection) read this
        property. For per-call ephemeral Agent construction, use
        ``runtime_context_for(...)`` which returns a RuntimeContext with
        the relevant heavy resources promoted into its fields.
        """
        return self._base_runtime_ctx

    def build_runtime_context(self) -> RuntimeContext:
        """Return the bare substrate-snapshot RuntimeContext.

        Sprint 26 Phase 1a backward-compat. For Phase 1b ephemeral Agent
        construction, prefer ``runtime_context_for(...)``.
        """
        return self._base_runtime_ctx

    def runtime_context_for(
        self,
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        api_mode: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        anthropic_base_url: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        anthropic_timeout: Optional[float] = None,
        enabled_toolsets: Optional[List[str]] = None,
        disabled_toolsets: Optional[List[str]] = None,
        quiet_mode: bool = False,
        skip_memory: bool = False,
        skip_tools: bool = False,
        skip_compression_probe: bool = False,
    ) -> RuntimeContext:
        """Build a per-call RuntimeContext with heavy resources pre-populated.

        Each heavy resource is built lazily on first request and cached
        in the Dispatcher; subsequent calls with the same shape reuse
        the cache. The returned RuntimeContext is a fresh dataclass
        instance with the cached resources promoted into its fields.

        Args:
            model: model name (e.g. ``"claude-sonnet-4-6"``). When provided,
                the context_length cache is populated for this model and
                — for Anthropic provider — the Anthropic client is built
                and cached.
            provider: provider identifier (e.g. ``"anthropic"``).
            anthropic_base_url, anthropic_api_key, anthropic_timeout:
                client-construction parameters for the Anthropic SDK.
                Skipped when ``provider != "anthropic"``.
            enabled_toolsets, disabled_toolsets, quiet_mode: passed to
                ``get_tool_definitions``. When ``skip_tools`` is True,
                tool registry build is skipped.
            skip_memory: when True, the memory store is not loaded.
                Mirrors AIAgent's ``skip_memory`` constructor kwarg.
            skip_tools: when True, the tool registry is not built.

        Returns:
            A new RuntimeContext combining the base substrate snapshot
            with the requested pre-built heavy resources.
        """
        tools = None
        if not skip_tools:
            tools = self._get_or_build_tools(
                enabled_toolsets, disabled_toolsets, quiet_mode,
            )

        memory_store = None
        if not skip_memory:
            memory_store = self._get_or_build_memory_store()

        anthropic_client = None
        context_length_by_model: Dict[str, int] = dict(self._context_length_cache)
        if model is not None and provider == "anthropic":
            anthropic_client = self._get_or_build_anthropic_client(
                model=model,
                base_url=anthropic_base_url,
                api_key=anthropic_api_key,
                timeout=anthropic_timeout,
            )
        if model is not None and model not in context_length_by_model:
            ctx_len = self._get_or_build_context_length(model)
            if ctx_len is not None:
                context_length_by_model[model] = ctx_len

        compression_probe = None
        if not skip_compression_probe and model is not None:
            main_runtime = {
                "model": model or "",
                "provider": provider or "",
                "base_url": base_url or anthropic_base_url or "",
                "api_key": api_key or anthropic_api_key or "",
                "api_mode": api_mode or "",
            }
            compression_probe = self._get_or_build_compression_probe(main_runtime)

        return RuntimeContext(
            env=self._base_runtime_ctx.env,
            config=self._base_runtime_ctx.config,
            tools=tools,
            memory_store=memory_store,
            context_length_by_model=context_length_by_model,
            anthropic_client=anthropic_client,
            openai_client=None,  # Phase 1b defers OpenAI client caching
            compression_probe=compression_probe,
        )

    # ── Phase 1b heavy-resource builders (lazy, cached) ──────────────────

    def _get_or_build_tools(
        self,
        enabled_toolsets: Optional[List[str]],
        disabled_toolsets: Optional[List[str]],
        quiet_mode: bool,
    ) -> List[Dict[str, Any]]:
        """Build (or return cached) tool registry for the given toolset shape.

        Cache key is a tuple of sorted toolset names. The same call shape
        returns the same list instance across constructions, saving the
        20-100ms cost of ``get_tool_definitions``.
        """
        key = (
            tuple(sorted(enabled_toolsets or ())),
            tuple(sorted(disabled_toolsets or ())),
            bool(quiet_mode),
        )
        cached = self._tools_cache.get(key)
        if cached is not None:
            return cached
        try:
            from model_tools import get_tool_definitions
        except ImportError as exc:
            logger.warning(
                "[grove.dispatcher] model_tools unavailable for tools cache (%r); "
                "Agent will fall back to its own construction path",
                exc,
            )
            return []
        tools = get_tool_definitions(
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            quiet_mode=quiet_mode,
        )
        self._tools_cache[key] = tools
        return tools

    def _get_or_build_memory_store(self) -> Optional[Any]:
        """Build (or return cached) memory store with disk content pre-loaded.

        Single session-scoped singleton — memory is operator-owned, not
        per-turn. Saves the 10-50ms load_from_disk cost on per-turn
        ephemeral Agent construction.

        Reads memory configuration from the snapshot's ``config`` dict.
        Returns ``None`` when memory is disabled in config or when
        construction fails (graceful — the Agent's existing fallback
        path handles a missing memory store cleanly).
        """
        if self._memory_store_cache is not None:
            return self._memory_store_cache
        mem_cfg = (self._base_runtime_ctx.config or {}).get("memory") or {}
        if not isinstance(mem_cfg, dict):
            return None
        if not (mem_cfg.get("memory_enabled") or mem_cfg.get("user_profile_enabled")):
            return None
        try:
            from tools.memory_tool import MemoryStore
        except ImportError as exc:
            logger.warning(
                "[grove.dispatcher] memory_tool unavailable (%r); skipping cache",
                exc,
            )
            return None
        try:
            store = MemoryStore(
                memory_char_limit=mem_cfg.get("memory_char_limit", 2200),
                user_char_limit=mem_cfg.get("user_char_limit", 1375),
            )
            store.load_from_disk()
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] memory store construction failed (%r); "
                "Agent will fall back to its own construction path",
                exc,
            )
            return None
        self._memory_store_cache = store
        return store

    def _get_or_build_anthropic_client(
        self,
        *,
        model: str,
        base_url: Optional[str],
        api_key: Optional[str],
        timeout: Optional[float],
    ) -> Optional[Any]:
        """Build (or return cached) Anthropic SDK client for one model.

        Cache key includes the model + base_url + api_key fingerprint
        (api_key only by its first/last 4 chars to avoid carrying the
        full secret in a dict key). The wrapped transport pool is
        already cached at the SDK level, so this primarily saves the
        wrapper construction + auth-token-validation cost.

        Returns ``None`` if the agent.anthropic_adapter module is
        unavailable or construction fails (graceful).
        """
        # Key fingerprint: full key would leak in dict introspection; truncate.
        key_fp = "<none>" if not api_key else f"{api_key[:4]}…{api_key[-4:]}"
        key = (model, base_url or "<default>", key_fp)
        cached = self._anthropic_client_cache.get(key)
        if cached is not None:
            return cached
        try:
            from agent.anthropic_adapter import build_anthropic_client
        except ImportError as exc:
            logger.warning(
                "[grove.dispatcher] anthropic_adapter unavailable (%r); "
                "skipping client cache",
                exc,
            )
            return None
        try:
            client = build_anthropic_client(api_key, base_url, timeout=timeout)
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] anthropic client construction failed (%r); "
                "Agent will fall back to its own construction path",
                exc,
            )
            return None
        self._anthropic_client_cache[key] = client
        return client

    def _get_or_build_compression_probe(
        self, main_runtime: Dict[str, str],
    ) -> Optional["CompressionProbe"]:
        """Run the compression-feasibility HTTP probe once and cache the result.

        The probe asks the configured auxiliary compression model for its
        context window via ``get_model_context_length``. That call hits
        the provider's metadata endpoint synchronously — typically a
        125ms HTTP roundtrip to a local Ollama server. cProfile measured
        this as 96% of the warm-path AIAgent.__init__ cost in the
        Sprint 26 Phase 1b A7 measurement.

        Returns ``None`` when compression is disabled, when the
        auxiliary client/model resolution fails, or when any exception
        fires — the Agent's ``_check_compression_model_feasibility``
        method handles the ``None`` case by falling back to its legacy
        in-process probe.

        Cache key is process-scoped (one probe result per Dispatcher).
        Operators who change ``auxiliary.compression`` config mid-session
        must restart for the new value to take effect.
        """
        if self._compression_probe_cache is not None:
            return self._compression_probe_cache
        try:
            from agent.auxiliary_client import (
                _resolve_task_provider_model,
                get_text_auxiliary_client,
            )
            from agent.model_metadata import get_model_context_length
        except ImportError as exc:
            logger.debug(
                "[grove.dispatcher] auxiliary modules unavailable for "
                "compression probe (%r); Agent will run its own",
                exc,
            )
            return None
        try:
            client, aux_model = get_text_auxiliary_client(
                "compression", main_runtime=main_runtime,
            )
            if client is None or not aux_model:
                return None
            try:
                _aux_cfg_provider, _, _, _, _ = _resolve_task_provider_model(
                    "compression"
                )
            except Exception:
                _aux_cfg_provider = ""
            aux_base_url = str(getattr(client, "base_url", ""))
            aux_api_key = str(getattr(client, "api_key", ""))
            aux_context = get_model_context_length(
                aux_model,
                base_url=aux_base_url,
                api_key=aux_api_key,
                config_context_length=None,
                provider=(
                    _aux_cfg_provider
                    if _aux_cfg_provider and _aux_cfg_provider != "auto"
                    else main_runtime.get("provider", "")
                ),
                custom_providers=None,
            )
        except Exception as exc:
            logger.debug(
                "[grove.dispatcher] compression probe failed (%r); "
                "Agent will run its own",
                exc,
            )
            return None
        probe = CompressionProbe(
            aux_model=aux_model,
            aux_context=aux_context,
            aux_base_url=aux_base_url,
            aux_api_key=aux_api_key,
            aux_cfg_provider=_aux_cfg_provider,
        )
        self._compression_probe_cache = probe
        return probe

    # ── Sprint 26 Phase 3 — generator-shaped turn dispatch ──────────────

    def dispatch_turn(
        self,
        agent: Any,
        user_message: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Drive the Agent's ``_run_turn_generator`` under GRV-005 § II authority.

        The GRV-005-conformant entry point for running a turn. The
        Agent yields intents; the Dispatcher executes them and sends
        ``Observation`` instances back. The Agent never executes a
        tool directly — that authority lives here (§ II/III).

        Phase 3 MVP scope: executes ``List[ToolIntent]`` batches via
        ``agent._execute_tool_calls``, preserving the existing
        parallel-or-sequential semantics, tool guardrails, and
        telemetry the legacy ``run_conversation`` path uses.
        Tool-zone classification at intent-yield (§ V) is Phase 4;
        Andon halt + disposition handling (§ VI) is Phase 5;
        EscalationRequest decisions (§ VII) are Sprint 27.

        Args:
            agent: an ``AIAgent`` instance whose
                ``_run_turn_generator`` will be driven.
            user_message: the operator's input for this turn.
            **kwargs: forwarded to ``_run_turn_generator`` (system_message,
                conversation_history, task_id, stream_callback,
                persist_user_message, already_routed).

        Returns:
            The legacy result dict the Agent's generator produces via
            ``StopIteration.value``. Shape matches the pre-Phase-3
            ``run_conversation`` return value byte-for-byte.
        """
        ledger = self._get_or_create_ledger(agent)
        # Sprint 26 Phase 7 — Dispatcher broadcasts GROVE_SESSION_ID to
        # subprocess descendants on every turn. Authority moved here
        # from AIAgent.__init__ per GRV-005 § II/III: substrate writes
        # are Dispatcher-owned. Idempotent — safe to re-write per turn.
        session_id = getattr(agent, "session_id", None)
        if session_id:
            self.broadcast_session_id(str(session_id))
        # Sprint 28 Phase 4 — explicit success finalization. Capture the
        # PREVIOUS turn's id before reset so we can finalize its pending
        # record (if any) as success. The first turn for this Dispatcher
        # has no previous (``_current_turn_id`` is None) — skip then.
        previous_turn_id = self._current_turn_id
        # Sprint 28 Phase 3 — reset per-turn state and assign this turn's
        # id BEFORE driving the generator, so terminal write sites
        # (FinalResponse / Drop / exception) have a stable identity to
        # write under.
        import time as _time
        self._turn_counter += 1
        self._current_turn_id = f"{session_id or 'unknown'}#{self._turn_counter}"
        self._current_turn_classification = None
        self._current_turn_api_call_count = 0
        self._current_turn_start = _time.monotonic()
        self._current_turn_tools_yielded = []
        self._current_turn_user_message = user_message
        self._current_turn_outcome_written = False
        # Sprint 30 — reset per-turn escalation counter + events list.
        self._current_turn_escalations = 0
        self._current_turn_escalation_events = []
        if previous_turn_id is not None:
            self._finalize_previous_turn_pending(previous_turn_id)
        gen = agent._run_turn_generator(user_message=user_message, **kwargs)
        try:
            return self._drive_generator(agent, gen, ledger)
        except BaseException:
            # Sprint 28 Phase 3 — error terminal. Any exception escaping
            # the drive loop (generator raise, internal error, KeyboardInterrupt)
            # writes an IntentRecord with outcome="error" before
            # propagating. ``_write_intent_record`` is idempotent per
            # turn via the outcome_written flag, so an exception after a
            # FinalResponse already wrote "pending" does NOT double-write.
            self._write_intent_record(agent, outcome="error")
            raise
        finally:
            # Ensure the generator is closed even if _drive_generator
            # raised an unexpected exception. gen.close() is idempotent;
            # if Drop disposition already closed it, this is a no-op.
            # Python 3 raises GeneratorExit at the yield point, which
            # propagates cleanly through this codebase's `except
            # Exception` blocks (A6 audit confirmed zero bare-except /
            # except-BaseException sites in _run_turn_generator body).
            gen.close()

    def _drive_generator(
        self, agent: Any, gen: Any, ledger: Any,
    ) -> Dict[str, Any]:
        """Consume the agent's generator under GRV-005 § II/III authority.

        Sprint 26 Phase 5 routes AndonHalt through the Sovereign Prompt;
        on Skip, denial Observations resume the generator; on Drop, the
        Dispatcher closes the generator and returns a drop result.

        Sprint 26 Phase 6 routes every Dispatcher-observable event
        out-of-band to the Kaizen Ledger per § IX(4). The agent's
        active reasoning loop never sees ledger writes.
        """
        from grove.intents import (
            EscalationRequest,
            FinalResponse,
            Observation,
            SessionRotateIntent,
            SessionUpdateTokensIntent,
            ToolBatchYield,
        )
        import time as _time

        try:
            yielded = gen.send(None)  # advance to first yield
            # Sprint 28 Phase 3 — capture the turn's classification
            # immediately. ``route_for_agent`` fires inside the generator
            # during the first send; the module-global ``_last_classification``
            # is set by the time control returns here. Snapshot to the
            # Dispatcher's instance attr so terminal write sites
            # (FinalResponse / Drop / exception) embed THIS turn's
            # classification, immune to a concurrent session overwriting
            # the global between here and the terminal.
            from grove.providers import current_classification as _current_classification
            self._current_turn_classification = _current_classification()
            # Sprint 30.1 (post-completion patch) — record the classifier-
            # driven pre-route escalation, if route_for_agent took that
            # path on this turn. Mirrors Sprint 29's pattern: the router
            # stashes the decision in providers._last_pre_route_decision;
            # the Dispatcher reads + emits the ledger event so the Agent
            # stays unaware of the ledger per GRV-005 § III. ``source``
            # distinguishes this from the Agent-yielded EscalationRequest
            # path which carries source="agent_request".
            from grove.providers import current_pre_route_decision as _current_pre_route_decision
            _pre_route = _current_pre_route_decision()
            if _pre_route is not None:
                try:
                    ledger.record(
                        "escalation_decision",
                        source="pre_route",
                        granted=True,
                        current_tier=_pre_route.get("current_tier"),
                        target_tier=_pre_route.get("target_tier"),
                        complexity_signal=_pre_route.get("complexity_signal"),
                        confidence=_pre_route.get("confidence"),
                        reason=(
                            "classifier-driven pre-route — complexity_signal in "
                            "triggers and confidence below threshold"
                        ),
                    )
                except Exception as _exc:
                    logger.warning(
                        "[grove.dispatcher] pre_route escalation_decision "
                        "ledger write failed: %r", _exc,
                    )
            # Sprint 29 Phase 2 — record the per-turn tool selection the
            # Agent computed (post-route, pre-first-call). Agent stashes
            # the metadata on ``_last_tool_selection``; Dispatcher writes
            # the Kaizen Ledger event so the Agent stays unaware of the
            # ledger per GRV-005 § III.
            _tool_selection = getattr(agent, "_last_tool_selection", None)
            if _tool_selection is not None:
                try:
                    ledger.record("tool_selection", **_tool_selection)
                except Exception as _exc:
                    logger.warning(
                        "[grove.dispatcher] tool_selection ledger write "
                        "failed: %r", _exc,
                    )
            while True:
                if isinstance(yielded, ToolBatchYield):
                    # Sprint 31 Phase 2 — ToolBatchYield carries the
                    # batch's intents plus the per-batch scalars
                    # (effective_task_id, api_call_count) the legacy
                    # Sprint 26 GATE-D bridge fields used to back-
                    # channel via attribute access. Unpack the carrier
                    # so the rest of this branch can reference the
                    # intents list under the prior name (``_batch``) and
                    # the rest of the dispatcher reads scalars from the
                    # dispatcher's own per-turn state.
                    _batch = yielded.intents
                    self._current_turn_api_call_count = yielded.api_call_count
                    _batch_effective_task_id = yielded.effective_task_id
                    # Sprint 28 Phase 3 — accumulate the tool names this
                    # turn yielded for the intent record. Captured BEFORE
                    # classification halts the batch, so a halted batch's
                    # would-be tool names still surface in the record.
                    for _intent in _batch:
                        _name = getattr(_intent, "tool_name", None)
                        if isinstance(_name, str) and _name:
                            self._current_turn_tools_yielded.append(_name)
                    # Phase 4 — classify the batch at intent-yield
                    try:
                        self._classify_intents_batch_and_halt_or_raise(_batch)
                    except AndonHalt as halt:
                        # Phase 6 — record the halt in the Kaizen Ledger
                        ledger.record(
                            "andon_halt",
                            zone=halt.zone,
                            matched_rule=halt.matched_rule,
                            source=halt.source,
                            reason=halt.reason,
                            triggering_index=halt.triggering_index,
                            intents=[
                                {"tool_name": i.tool_name, "call_id": i.call_id}
                                for i in halt.intents
                            ],
                            zone_results=[
                                {
                                    "zone": zr.zone,
                                    "matched_rule": zr.matched_rule,
                                    "source": zr.source,
                                }
                                for zr in halt.zone_results
                            ],
                        )
                        # Phase 5 — Sovereign Prompt + disposition flow
                        disposition = self._handle_andon_halt(agent, halt)
                        ledger.record(
                            "andon_disposition",
                            disposition=disposition,
                            zone=halt.zone,
                            matched_rule=halt.matched_rule,
                            triggering_tool=halt.intents[halt.triggering_index].tool_name,
                        )
                        if disposition == "skip":
                            observations = self._build_skip_observations(
                                agent, halt.intents,
                            )
                            yielded = gen.send(observations)
                            continue
                        if disposition == "drop":
                            ledger.record(
                                "turn_dropped",
                                triggering_tool=halt.intents[halt.triggering_index].tool_name,
                                zone=halt.zone,
                                matched_rule=halt.matched_rule,
                            )
                            # Sprint 28 Phase 3 — drop terminal. Outcome
                            # is terminal (no Phase 4 finalization).
                            self._write_intent_record(agent, outcome="drop")
                            return self._format_drop_result(agent, halt)
                        if disposition != "shadow_approve":
                            raise ValueError(
                                f"Sovereign prompt returned unknown disposition: "
                                f"{disposition!r} (expected 'skip', 'drop', "
                                f"or 'shadow_approve')"
                            )
                        # shadow_approve: fall through to Green path. The
                        # halt is already in the ledger via "andon_halt"
                        # above; "andon_disposition" above records the
                        # shadow_approve outcome for calibration review.
                    # Green path: execute the batch via the executor.
                    # Sprint 31 Phase 2 — direct invocation, no agent
                    # shim in the path. ``_current_messages`` is still
                    # set by the agent at the yield site (transitional;
                    # used by Sprint 30 hot-swap and Phase 1b's
                    # context-engine branch in _invoke_tool). The
                    # context builders + orchestration helper live on
                    # the agent because they read agent-owned state to
                    # populate the ExecutionContext — they don't
                    # execute tools.
                    msgs = agent._current_messages
                    # Test-stub agents (constructed via ``object.__new__``,
                    # bypassing __init__) don't have a ToolExecutor and
                    # don't define the context builders / orchestration
                    # helper. The production AIAgent always has all of
                    # them. For dispatcher-drive-loop tests that use
                    # minimal stub agents, the new path is skipped and
                    # an empty result list is produced — matching the
                    # observable effect of the legacy stub pattern
                    # (``agent._execute_tool_calls = lambda *a, **k: None``).
                    _has_executor = (
                        getattr(agent, "_tool_executor", None) is not None
                        and hasattr(agent, "_build_execution_context_concurrent")
                        and hasattr(agent, "_build_execution_context_sequential")
                        and hasattr(agent, "_apply_execution_results_to_messages")
                    )
                    _exec_t0 = _time.monotonic()
                    if _has_executor:
                        from run_agent import _should_parallelize_intents as _spi
                        if _spi(_batch):
                            _ctx_for_batch = agent._build_execution_context_concurrent(
                                _batch, _batch_effective_task_id, yielded.api_call_count,
                            )
                            _execute_fn = agent._tool_executor.execute_batch_concurrent
                        else:
                            _ctx_for_batch = agent._build_execution_context_sequential(
                                _batch, _batch_effective_task_id, yielded.api_call_count,
                            )
                            _execute_fn = agent._tool_executor.execute_batch_sequential
                        agent._executing_tools = True
                        try:
                            _exec_results = _execute_fn(_ctx_for_batch)
                        finally:
                            agent._executing_tools = False
                        # Orchestration: append tool messages, drain
                        # per-tool /steer, enforce per-turn budget.
                        # Lives on the agent because it touches Agent-
                        # owned message + steer state.
                        agent._apply_execution_results_to_messages(
                            _exec_results, msgs, _batch_effective_task_id,
                        )
                    else:
                        _exec_results = []
                    _exec_latency_ms = (_time.monotonic() - _exec_t0) * 1000.0
                    # Phase 6 — record successful batch execution
                    ledger.record(
                        "tool_batch_executed",
                        intents=[
                            {"tool_name": i.tool_name, "call_id": i.call_id}
                            for i in _batch
                        ],
                        batch_size=len(_batch),
                        latency_ms=round(_exec_latency_ms, 2),
                    )
                    observations: List[Any] = []
                    for intent in _batch:
                        tool_msg = None
                        cid = intent.call_id
                        if cid:
                            for m in reversed(msgs):
                                if (
                                    isinstance(m, dict)
                                    and m.get("tool_call_id") == cid
                                ):
                                    tool_msg = m
                                    break
                        value = ""
                        if isinstance(tool_msg, dict):
                            value = tool_msg.get("content", "")
                        observations.append(Observation(
                            intent_id=intent.call_id,
                            success=True,
                            value=value,
                        ))
                    yielded = gen.send(observations)
                elif isinstance(yielded, EscalationRequest):
                    # Sprint 30 — EscalationRequest mid-turn. Apply the
                    # locked GATE-A deterministic-silent policy: auto-
                    # grant within budget+ceiling, auto-deny above. Both
                    # paths log to the Kaizen Ledger; both record on
                    # the IntentRecord at terminal. Per § VII: the
                    # decision is observable; no synchronous operator
                    # prompt.
                    result = self._handle_escalation_request(
                        agent, gen, yielded, ledger,
                    )
                    if result["action"] == "grant_hot_swap":
                        # _handle_escalation_request closed the original
                        # generator, constructed a fresh Agent at the
                        # escalated tier with full turn_history, and
                        # started a new generator. Resume the drive
                        # loop with the new generator + agent.
                        agent = result["new_agent"]
                        gen = result["new_gen"]
                        ledger = self._get_or_create_ledger(agent)
                        yielded = gen.send(None)
                        continue
                    # deny path — generator was resumed with None after
                    # the denial tool-response was injected into
                    # agent._current_messages.
                    yielded = result["next_yielded"]
                    continue
                elif isinstance(yielded, SessionRotateIntent):
                    # Sprint 39 — compression-boundary atomic rotation.
                    # The Agent yields this from inside its compression
                    # path; the Dispatcher executes the 7-call sequence
                    # against self.session and writes the new session_id
                    # back via the Observation. The Agent receives the
                    # new id and updates its own ``session_id``.
                    new_session_id = self.rotate_session(
                        reason=yielded.reason,
                        new_system_prompt=yielded.new_system_prompt,
                        source=getattr(agent, "platform", None) or "cli",
                        model=getattr(agent, "model", ""),
                        model_config=getattr(agent, "_session_init_model_config", None),
                    )
                    yielded = gen.send(Observation(
                        intent_id=None,
                        success=True,
                        value=new_session_id,
                    ))
                elif isinstance(yielded, SessionUpdateTokensIntent):
                    # Sprint 39 — per-API-call telemetry. Fire-and-forget
                    # from the Agent's perspective; an empty Observation
                    # is sent back so the generator's ``yield`` resumes
                    # without inspecting a value.
                    self.update_token_counts(yielded)
                    yielded = gen.send(Observation(
                        intent_id=None,
                        success=True,
                        value=None,
                    ))
                elif isinstance(yielded, FinalResponse):
                    # Phase 6 — Foreground/Background split per § IX(4):
                    # the conversational payload (FinalResponse.content)
                    # is what the operator sees in the active context;
                    # the operational metadata routes to the Ledger.
                    ledger.record(
                        "final_response",
                        content_length=len(yielded.content or ""),
                        metadata=dict(yielded.metadata),
                    )
                    # Sprint 28 Phase 3 — success terminal. Write a
                    # provisional record with outcome="pending"; Phase 4
                    # finalizes to success/correction at next turn start.
                    # If the operator walks away, the Implicit Success
                    # Sweep on a future Dispatcher init finalizes the
                    # orphaned pending as success.
                    self._write_intent_record(
                        agent,
                        outcome="pending",
                        final_response_chars=len(yielded.content or ""),
                    )
                    yielded = gen.send(None)
                else:
                    yielded = gen.send(None)
        except StopIteration as stop:
            return stop.value

    # ── Sprint 28 Phase 4 helper (Explicit Success Finalization) ────────

    def _finalize_previous_turn_pending(self, previous_turn_id: str) -> None:
        """Finalize the previous turn's pending record as success.

        Sprint 28 Phase 4 — explicit success only per the GATE-D
        disposition. Semantic correction detection is deferred (would
        require either injecting prior-turn context into the routing
        Haiku — degrading zero-shot accuracy — or a second LLM call,
        breaking Sprint 12's one-call economics). The Implicit Success
        Sweep at Dispatcher init catches abandoned sessions; this
        method catches in-session continuations. Together they close
        the loop with explicit-success semantics only.

        No-op when the previous turn already terminated at Drop or
        exception (its outcome is already terminal). No-op when no
        store is wired (legacy/test Dispatchers).

        Best-effort: any failure inside this method logs warning and
        swallows — the new turn's setup MUST NOT depend on the
        previous turn's finalization succeeding.
        """
        if self._intent_store is None:
            return
        try:
            from grove.intent_store import finalize_record
            latest: Optional[Any] = None
            for record in self._intent_store.latest_by_turn():
                if record.turn_id == previous_turn_id:
                    latest = record
                    break
            if latest is None or latest.outcome != "pending":
                return
            self._intent_store.append(finalize_record(
                latest,
                outcome="success",
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] explicit-success finalization "
                "failed for previous turn %r: %r",
                previous_turn_id, exc,
            )

    # ── Sprint 30 helper (Escalation policy + hot-swap) ─────────────────

    def _get_or_load_escalation_policy(self) -> Any:
        """Lazily load + cache the escalation policy from routing config.

        Cached on the Dispatcher instance so successive turns don't
        re-read the file. The policy is intentionally permissive: a
        missing or malformed config block produces an ``enabled=False``
        policy, NOT an exception. The Dispatcher must boot even when
        operators have no escalation block.
        """
        if self._escalation_policy is not None:
            return self._escalation_policy
        from grove.escalation_policy import load_escalation_policy
        self._escalation_policy = load_escalation_policy(
            self._base_runtime_ctx.config or {}
        )
        return self._escalation_policy

    def _handle_escalation_request(
        self,
        agent: Any,
        gen: Any,
        req: Any,
        ledger: Any,
    ) -> Dict[str, Any]:
        """Apply the deterministic-silent escalation policy.

        Returns a dict the caller (``_drive_generator``) branches on:

        * ``{"action": "grant_hot_swap", "new_agent": ..., "new_gen": ...}``
          — original generator closed; fresh Agent at the escalated
          tier constructed with the full turn_history (snapshotted
          messages list); fresh generator started. Caller resumes the
          drive loop with the swap.

        * ``{"action": "deny_inject", "next_yielded": ...}``
          — denial tool-response injected into ``agent._current_messages``;
          original generator resumed via ``gen.send(None)``; caller
          continues with the yielded value.

        Both paths:
        * Write an ``escalation_decision`` event to the Kaizen Ledger.
        * Append a summary dict to ``self._current_turn_escalation_events``
          so the IntentRecord at terminal sites captures the history.
        * Increment per-turn + per-session escalation counters.
        """
        from grove.escalation_policy import evaluate_escalation

        policy = self._get_or_load_escalation_policy()
        session_id = getattr(agent, "session_id", None) or "unknown"
        current_tier = None
        try:
            from grove.providers import current_tier as _current_tier
            current_tier = _current_tier()
        except Exception:
            pass

        request = req.request or {}
        requested_depth = request.get("reasoning_depth")
        requested_context = request.get("context_size")
        call_id = request.get("call_id")

        decision = evaluate_escalation(
            policy=policy,
            current_tier=current_tier,
            requested_depth=requested_depth,
            requested_context=requested_context,
            turn_escalations_so_far=self._current_turn_escalations,
            session_escalations_so_far=self._session_escalation_counts.get(
                session_id, 0,
            ),
        )

        # Counters tick regardless of grant/deny — the request happened,
        # the budget accounts for it.
        self._current_turn_escalations += 1
        self._session_escalation_counts[session_id] = (
            self._session_escalation_counts.get(session_id, 0) + 1
        )

        event_payload = {
            "granted": decision.granted,
            "reason": decision.reason,
            "requested_depth": requested_depth,
            "requested_context": requested_context,
            "current_tier": decision.current_tier,
            "target_tier": decision.target_tier,
            "blocker": req.reason,
            "turn_escalation_index": self._current_turn_escalations,
            "session_escalation_total": self._session_escalation_counts[session_id],
        }
        try:
            ledger.record("escalation_decision", **event_payload)
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] escalation_decision ledger write "
                "failed: %r", exc,
            )
        self._current_turn_escalation_events.append(event_payload)

        if decision.granted:
            return self._grant_escalation_hot_swap(
                agent, gen, req, decision, call_id,
            )
        return self._deny_escalation(agent, gen, req, decision, call_id)

    def _grant_escalation_hot_swap(
        self,
        agent: Any,
        gen: Any,
        req: Any,
        decision: Any,
        call_id: Optional[str],
    ) -> Dict[str, Any]:
        """Hot-swap to a fresh Agent at the escalated tier.

        Snapshots ``agent._current_messages`` (the picklable JSON-ish
        conversation history), closes the original generator, and
        constructs a new Agent with the carry kit from GATE-A:
        model (escalated), session_id, user/chat/platform fields,
        max_iterations, enabled_toolsets, sovereign_prompt_handler.
        Anything else (transports, caches, thread state) is rebuilt
        by the new Agent's ``__init__``.

        The new Agent starts a fresh ``_run_turn_generator`` with
        ``conversation_history=<snapshot>`` — the escalated LLM sees
        every prior assistant/tool message as conversation context.
        No tool re-execution.

        Per § III: the Agent never instantiates the new Agent (or the
        new tier). The Dispatcher owns both.
        """
        # Snapshot the messages list BEFORE closing the generator —
        # gen.close() raises GeneratorExit at the yield point, which
        # triggers the finally block that clears _current_messages.
        snapshot_messages = list(getattr(agent, "_current_messages", None) or [])
        snapshot_user_message = self._current_turn_user_message or ""

        # Write the grant tool-response into the snapshot BEFORE the
        # close — so the new Agent's first LLM call sees the escalate
        # tool call paired with its tool-response per provider API
        # requirements. Skip when call_id is missing (defensive — the
        # intercept always sets it from the original ToolIntent).
        if call_id:
            snapshot_messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": (
                    f"⬆ Escalation granted: {decision.reason}. "
                    f"Continue reasoning at the escalated tier."
                ),
            })

        gen.close()

        # Construct the carry kit. Only constructor args from the
        # GATE-A locked list — nothing else. Missing critical state
        # surfaces as a test failure, not a silent degradation.
        carry_kit = self._extract_agent_carry_kit(agent)
        carry_kit["model"] = self._resolve_model_for_tier(decision.target_tier)

        from run_agent import AIAgent
        new_agent = AIAgent(**carry_kit)
        # The new agent must reuse our Dispatcher instance so the
        # Kaizen Ledger continuity holds — without this back-reference,
        # the agent's inline lazy build inside ``run_conversation``
        # would create a fresh Dispatcher and lose the per-session
        # ledgers / counters.
        new_agent._dispatcher_singleton = self
        new_agent._sovereign_prompt_handler = getattr(
            agent, "_sovereign_prompt_handler", None,
        )

        # Start the new generator with the snapshotted messages. The
        # Agent's conversation_history kwarg accepts pre-seeded
        # messages — the existing path Sprint 27 exercised.
        new_gen = new_agent._run_turn_generator(
            user_message=snapshot_user_message,
            conversation_history=snapshot_messages,
            # already_routed=True so the new turn doesn't re-classify
            # (we know which tier we want; the routing decision is the
            # escalation grant). Sprint 12's classify_for_routing skips
            # when already_routed.
            already_routed=True,
        )
        return {
            "action": "grant_hot_swap",
            "new_agent": new_agent,
            "new_gen": new_gen,
        }

    def _deny_escalation(
        self,
        agent: Any,
        gen: Any,
        req: Any,
        decision: Any,
        call_id: Optional[str],
    ) -> Dict[str, Any]:
        """Inject an honest decline tool-response and resume the generator.

        The denial lands as a tool message in ``agent._current_messages``
        paired with the original ``escalate`` tool call's ``call_id``.
        The next LLM call sees it as a normal tool response and reasons
        about the denial in-stream. No contract extension; no second
        yield type.

        Per the locked decision: no synchronous operator prompt. The
        decision IS observable via the Kaizen Ledger (already written
        by the caller) but doesn't block the turn.
        """
        msgs = getattr(agent, "_current_messages", None)
        if msgs is not None and call_id:
            denial_text = f"⬆ Escalation denied: {decision.reason}"
            msgs.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": denial_text,
            })
        # Resume the generator with None — the Agent's yield site
        # discards the Observation list anyway (it's informational
        # per the Sprint 26 Phase 3 contract). The next LLM call
        # reads the appended denial from messages.
        next_yielded = gen.send(None)
        return {
            "action": "deny_inject",
            "next_yielded": next_yielded,
        }

    @staticmethod
    def _extract_agent_carry_kit(agent: Any) -> Dict[str, Any]:
        """Build the constructor-args dict for the hot-swap Agent.

        Per the GATE-A carry-kit list: model, session_id, user/chat/
        platform fields, max_iterations, enabled_toolsets,
        sovereign_prompt_handler. Caller overwrites model with the
        escalated tier's model before constructing.

        Returns a dict suitable to splat into ``AIAgent(...)``.
        Anything not in this dict gets rebuilt by the new Agent's
        ``__init__`` — that's the design.
        """
        return {
            "model": getattr(agent, "model", "") or "",
            "session_id": getattr(agent, "session_id", None),
            "platform": getattr(agent, "platform", None),
            "user_id": getattr(agent, "user_id", None),
            "user_name": getattr(agent, "user_name", None),
            "chat_id": getattr(agent, "chat_id", None),
            "chat_name": getattr(agent, "chat_name", None),
            "chat_type": getattr(agent, "chat_type", None),
            "thread_id": getattr(agent, "thread_id", None),
            "max_iterations": getattr(agent, "max_iterations", 90),
            "enabled_toolsets": list(
                getattr(agent, "_enabled_toolsets_at_construction", [])
                or getattr(agent, "enabled_toolsets", []) or []
            ) or None,
            "quiet_mode": getattr(agent, "quiet_mode", False),
            "sovereign_prompt_handler": getattr(
                agent, "_sovereign_prompt_handler", None,
            ),
        }

    def _resolve_model_for_tier(self, target_tier: Optional[str]) -> str:
        """Look up the model bound to a tier in the routing config.

        Returns empty string when no tier or no binding — the caller
        will pass that into the new Agent's constructor, which falls
        back to its own config-resolution path.
        """
        if not target_tier:
            return ""
        routing = (self._base_runtime_ctx.config or {}).get("routing") or {}
        prefs = routing.get("tier_preferences") or {}
        tier_block = prefs.get(target_tier) or {}
        return str(tier_block.get("model", "") or "")

    # ── Sprint 28 Phase 3 helper (Intent Record write) ──────────────────

    def _write_intent_record(
        self,
        agent: Any,
        *,
        outcome: str,
        final_response_chars: Optional[int] = None,
    ) -> None:
        """Write an IntentRecord for the current turn if the store is wired.

        Idempotent per turn via ``self._current_turn_outcome_written``:
        if a terminal site (FinalResponse) already wrote "pending" for
        this turn and a subsequent exception fires, the exception
        handler's call here is a no-op rather than a double-write.

        Best-effort: any failure inside this method is logged at
        WARNING and swallowed — intent-record writes MUST NOT crash
        the turn (Architectural Prime Directive: fail loud about the
        write failure, but don't degrade the operator's turn outcome).

        Args:
            agent: the AIAgent the turn is running for. Read for
                ``session_id``, ``model``, and ``_current_api_call_count``.
            outcome: one of the :data:`grove.intent_store.VALID_OUTCOMES`
                values appropriate to the terminal site.
            final_response_chars: only populated at the FinalResponse
                terminal; ``None`` at Drop / exception terminals.
        """
        if self._intent_store is None:
            return
        if self._current_turn_outcome_written:
            return
        if self._current_turn_id is None:
            # dispatch_turn never set per-turn state — nothing meaningful
            # to write. Happens when this helper is reached via a path
            # the Sprint 28 wiring did not cover.
            return
        try:
            from grove.intent_store import IntentRecord, normalize_message_stem
            from grove.providers import current_tier as _current_tier
            import time as _time

            classification = self._current_turn_classification
            if classification is None:
                # Classification failed (Sprint 12 D4 graceful
                # degradation) or never fired. Sentinel-fill so the
                # record schema validates; downstream consumers filter
                # by intent_class=="unknown" to identify these.
                pattern_hash = "unclassified"
                intent_class = "unknown"
                register_class = "unknown"
                complexity_signal = "unknown"
                confidence = 0.0
                goal_alignment = None
            else:
                pattern_hash = classification.pattern_hash
                intent_class = classification.intent_class
                register_class = classification.register_class
                complexity_signal = classification.complexity_signal
                confidence = classification.confidence
                goal_alignment = classification.goal_alignment

            duration_ms = 0.0
            if self._current_turn_start is not None:
                duration_ms = (
                    _time.monotonic() - self._current_turn_start
                ) * 1000.0

            session_id = getattr(agent, "session_id", None) or "unknown"
            api_calls = int(
                self._current_turn_api_call_count or 0
            )
            model_used = getattr(agent, "model", None) or None

            record = IntentRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                session_id=session_id,
                turn_id=self._current_turn_id,
                user_message_stem=normalize_message_stem(
                    self._current_turn_user_message or ""
                ),
                pattern_hash=pattern_hash,
                intent_class=intent_class,
                register_class=register_class,
                complexity_signal=complexity_signal,
                confidence=confidence,
                outcome=outcome,
                goal_alignment=goal_alignment,
                tier_selected=_current_tier(),
                model_used=model_used,
                tools_yielded=tuple(self._current_turn_tools_yielded),
                api_calls=api_calls,
                duration_ms=round(duration_ms, 2),
                final_response_chars=final_response_chars,
                escalation_count=self._current_turn_escalations,
            )
            self._intent_store.append(record)
            self._current_turn_outcome_written = True
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] intent record write failed "
                "(outcome=%r, turn_id=%r): %r",
                outcome, self._current_turn_id, exc,
            )

    # ── Phase 6 helpers (Kaizen Ledger + Tier Override) ─────────────────

    def _get_or_create_ledger(self, agent: Any) -> Any:
        """Return (creating if needed) the KaizenLedger for the agent's session.

        Ledgers are keyed by session_id and persist across turns within
        the same session. A Dispatcher serving multiple concurrent
        sessions (gateway path) holds one ledger per session_id.
        """
        from grove.kaizen_ledger import KaizenLedger
        session_id = getattr(agent, "session_id", None) or "unknown"
        ledger = self._kaizen_ledgers.get(session_id)
        if ledger is None:
            ledger = KaizenLedger(
                session_id=session_id,
                ledger_dir=self._kaizen_ledger_dir,
            )
            self._kaizen_ledgers[session_id] = ledger
        return ledger

    def ledger_for(self, agent_or_session_id: Any) -> Optional[Any]:
        """Return the KaizenLedger for an agent or session_id, or None.

        Operator / test query interface. Returns None when no ledger
        exists for the given session yet (i.e., dispatch_turn has not
        been called for that session).
        """
        if isinstance(agent_or_session_id, str):
            session_id = agent_or_session_id
        else:
            session_id = getattr(agent_or_session_id, "session_id", None) or "unknown"
        return self._kaizen_ledgers.get(session_id)

    def override_tier(
        self,
        agent_or_session_id: Any,
        target_tier: str,
        reason: str,
    ) -> None:
        """Phase 6 Tier Override pathway per GRV-005 § IX(4).

        Records an explicit tier override for the named session and
        writes a ``tier_override`` event to the session's Kaizen Ledger.
        Sprint 27's escalation handler will call this when granting an
        EscalationRequest; for v1 operators may also call it directly
        via a slash-command surface (Phase 7 wires that).

        The override is process-scoped — restart resets it. Persistent
        per-operator tier preferences belong in config.yaml's routing
        section, not here.

        Args:
            agent_or_session_id: an AIAgent (reads .session_id) or a
                session_id string directly.
            target_tier: the tier the next turn should use (e.g. "T3").
            reason: operator-visible explanation (e.g. "user requested
                apex for novel synthesis"). Stored in the ledger entry.
        """
        if isinstance(agent_or_session_id, str):
            session_id = agent_or_session_id
        else:
            session_id = getattr(agent_or_session_id, "session_id", None) or "unknown"
        self._tier_overrides[session_id] = target_tier
        # Best-effort ledger entry. If no ledger exists yet for this
        # session, create one so the override is captured.
        from grove.kaizen_ledger import KaizenLedger
        ledger = self._kaizen_ledgers.get(session_id)
        if ledger is None:
            ledger = KaizenLedger(
                session_id=session_id,
                ledger_dir=self._kaizen_ledger_dir,
            )
            self._kaizen_ledgers[session_id] = ledger
        ledger.record(
            "tier_override",
            target_tier=target_tier,
            reason=reason,
        )

    @staticmethod
    def broadcast_session_id(session_id: str) -> None:
        """Sprint 26 Phase 7 — write GROVE_SESSION_ID for subprocess descendants.

        Authority moved here from AIAgent per GRV-005 § II/III: env
        writes are Dispatcher-owned. Subprocess descendants (terminal
        tool, execute_code tool, etc.) read GROVE_SESSION_ID to
        correlate their telemetry with the parent session. The
        Dispatcher writes on every dispatch_turn entry; the Agent
        also calls this when session_id rotates mid-conversation
        (compression-driven session split).
        """
        os.environ["GROVE_SESSION_ID"] = str(session_id)

    # ── Sprint 39 — session authority + turn lifecycle ─────────────────

    def _ensure_session_db(self) -> Any:
        """Lazy-construct ``self.session`` if no caller supplied one.

        Building ``SessionDB()`` is deferred to first ``open_session``
        so test Dispatchers that never run a turn skip the file-system
        cost. Caller-supplied handles bypass this entirely.
        """
        if self.session is None:
            from hermes_state import SessionDB
            self.session = SessionDB()
        return self.session

    def open_session(
        self,
        session_id: Optional[str] = None,
        *,
        resume: bool = False,
    ) -> str:
        """Open a session for the upcoming turn(s) — pre-Agent-construction.

        Sprint 39 owns ``session_id`` generation: when ``resume`` is
        False and no id is supplied, a fresh timestamp + short-uuid id is
        generated here. When ``resume`` is True, the supplied id is
        resolved through ``SessionDB.resolve_resume_session_id`` to walk
        any compression-chain head pointers to the descendant that
        actually holds the transcript.

        Sprint 35 (classify-before-construct) is the load-bearing
        consumer of this hook + the companion ``hydrate_history()`` —
        both must be callable BEFORE any AIAgent exists. The Dispatcher
        does not need an Agent to perform either.
        """
        self._ensure_session_db()
        if resume:
            if not session_id:
                raise ValueError(
                    "Dispatcher.open_session: resume=True requires a session_id"
                )
            try:
                resolved = self.session.resolve_resume_session_id(session_id)
            except Exception:
                resolved = session_id
            self.session_id = resolved or session_id
            # Re-open the row (clear ended_at / end_reason) so resumed
            # sessions land in the active state. Mirrors the CLI's
            # legacy raw-SQL UPDATE at cli.py:4766-4770.
            try:
                self.session.reopen_session(self.session_id)
            except Exception:
                logger.debug("Dispatcher.open_session: reopen failed", exc_info=True)
            self._session_row_created = True
        else:
            self.session_id = session_id or self._generate_session_id()
            # Row creation deferred to first turn-boundary
            # (open_turn_row) — preserves the existing UX of empty
            # sessions not surfacing in lists.
            self._session_row_created = False
        self.broadcast_session_id(self.session_id)
        return self.session_id

    @staticmethod
    def _generate_session_id() -> str:
        """Fresh session id: ``YYYYMMDD_HHMMSS_<6 hex>``."""
        from datetime import datetime
        import uuid
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{ts}_{uuid.uuid4().hex[:6]}"

    def hydrate_history(self) -> List[Dict[str, Any]]:
        """Return the resumed session's conversation history — pre-Agent.

        Sprint 35 calls this before constructing the Agent so the
        classifier has full conversational context. Returns an empty
        list when no session is open, when the session has no messages,
        or when the read fails. ``session_meta`` rows are filtered out;
        the result is suitable for direct injection into an Agent's
        ``conversation_history`` parameter.
        """
        if self.session is None or not self.session_id:
            return []
        try:
            restored = self.session.get_messages_as_conversation(self.session_id)
        except Exception:
            logger.debug(
                "Dispatcher.hydrate_history: read failed", exc_info=True,
            )
            return []
        if not restored:
            return []
        return [m for m in restored if m.get("role") != "session_meta"]

    def open_turn_row(
        self,
        *,
        source: str,
        model: str,
        system_prompt: Optional[str] = None,
        model_config: Optional[Dict[str, Any]] = None,
        parent_session_id: Optional[str] = None,
    ) -> None:
        """Create the session DB row on first turn — lifecycle hook.

        Idempotent: subsequent calls within the same opened session are
        no-ops. Replaces ``AIAgent._ensure_db_session``.
        """
        if self._session_row_created or self.session is None or not self.session_id:
            return
        try:
            self.session.create_session(
                session_id=self.session_id,
                source=source,
                model=model,
                model_config=model_config,
                system_prompt=system_prompt,
                user_id=None,
                parent_session_id=parent_session_id,
            )
            self._session_row_created = True
        except Exception as exc:
            logger.warning(
                "Dispatcher.open_turn_row: create_session failed "
                "(will retry next turn): %s", exc,
            )

    def append_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        starting_index: int = 0,
    ) -> int:
        """Append messages to the current session — turn-boundary writer.

        Mirrors ``AIAgent._flush_messages_to_session_db``: appends every
        message at indices ``>= starting_index``, returning the new
        flush cursor. The Agent's per-turn flush yields this through
        the Dispatcher (Phase 2) instead of writing directly.
        """
        if self.session is None or not self.session_id:
            return starting_index
        if not self._session_row_created:
            # Best-effort: the Agent's pre-turn open_turn_row() may have
            # failed (SQLite lock). Drop the write silently; next turn
            # retries via the lifecycle hook.
            return starting_index
        flushed = starting_index
        for idx in range(starting_index, len(messages)):
            msg = messages[idx]
            try:
                self.session.append_message(
                    session_id=self.session_id,
                    role=msg.get("role", "assistant"),
                    content=msg.get("content"),
                    name=msg.get("name"),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                )
                flushed = idx + 1
            except Exception as exc:
                logger.warning(
                    "Dispatcher.append_messages: append_message %d failed: %s",
                    idx, exc,
                )
                break
        return flushed

    def rotate_session(
        self,
        *,
        reason: str,
        new_system_prompt: str,
        source: str,
        model: str,
        model_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compression-boundary atomic rotation — closes old, opens new.

        Mediates the 7-call sequence that ``AIAgent._compress_context``
        used to perform inline against its own handle. Returns the new
        session_id. The Agent yields ``SessionRotateIntent`` to drive
        this; the Dispatcher executes here and writes back the new
        session_id via the back-reference so the Agent's reasoning
        state stays consistent.
        """
        if self.session is None or not self.session_id:
            # No session to rotate — Agent's downstream paths will skip
            # session-DB work because the precondition (session_id set)
            # is absent. Return the current value unchanged.
            return self.session_id or ""
        old_session_id = self.session_id
        try:
            old_title = self.session.get_session_title(old_session_id)
        except Exception:
            old_title = None
        try:
            self.session.end_session(old_session_id, reason)
        except Exception as exc:
            logger.warning(
                "Dispatcher.rotate_session: end_session(%s) failed: %s",
                old_session_id, exc,
            )
        new_session_id = self._generate_session_id()
        self.session_id = new_session_id
        self.broadcast_session_id(new_session_id)
        try:
            from gateway.session_context import _SESSION_ID
            _SESSION_ID.set(new_session_id)
        except Exception:
            pass
        self._session_row_created = False
        try:
            self.session.create_session(
                session_id=new_session_id,
                source=source,
                model=model,
                model_config=model_config,
                parent_session_id=old_session_id,
            )
            self._session_row_created = True
        except Exception as exc:
            logger.warning(
                "Dispatcher.rotate_session: create_session(%s) failed: %s",
                new_session_id, exc,
            )
            return new_session_id
        if old_title:
            try:
                next_title = self.session.get_next_title_in_lineage(old_title)
                self.session.set_session_title(new_session_id, next_title)
            except Exception as exc:
                logger.debug(
                    "Dispatcher.rotate_session: title propagation failed: %s",
                    exc,
                )
        try:
            self.session.update_system_prompt(new_session_id, new_system_prompt)
        except Exception as exc:
            logger.debug(
                "Dispatcher.rotate_session: update_system_prompt failed: %s",
                exc,
            )
        return new_session_id

    def update_token_counts(self, intent: "SessionUpdateTokensIntent") -> None:
        """Per-API-call telemetry write — handler for SessionUpdateTokensIntent."""
        if self.session is None or not self.session_id:
            return
        if not self._session_row_created:
            # Row not yet created (turn-boundary open hadn't fired or
            # failed). Avoid silent loss of token deltas — try one more
            # row creation if we have enough context, else drop.
            return
        try:
            self.session.update_token_counts(
                self.session_id,
                input_tokens=intent.input_tokens,
                output_tokens=intent.output_tokens,
                cache_read_tokens=intent.cache_read_tokens,
                cache_write_tokens=intent.cache_write_tokens,
                reasoning_tokens=intent.reasoning_tokens,
                estimated_cost_usd=intent.estimated_cost_usd,
                cost_status=intent.cost_status,
                cost_source=intent.cost_source,
                billing_provider=intent.billing_provider,
                billing_base_url=intent.billing_base_url,
                billing_mode=intent.billing_mode,
            )
        except Exception as exc:
            logger.debug(
                "Dispatcher.update_token_counts: write failed: %s", exc,
            )

    def close_session(self, reason: str) -> None:
        """Close the current session — terminal lifecycle hook."""
        if self.session is None or not self.session_id:
            return
        try:
            self.session.end_session(self.session_id, reason)
        except Exception as exc:
            logger.debug(
                "Dispatcher.close_session: end_session(%s) failed: %s",
                self.session_id, exc,
            )

    @classmethod
    def acknowledge_pending_andon(
        cls,
        notice_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Sprint 26 Phase 7 — startup recovery hook for D3 markers.

        Called once at CLI / gateway boot. Reads every pending_andon
        marker left over from prior sessions (process killed mid-
        Sovereign-Prompt), invokes ``notice_callback`` for each marker
        so the caller can surface a user-facing message, then deletes
        the markers (Option 1: Discard with notice, per the operator's
        Phase 7 lock).

        Returns the list of marker payloads that were acknowledged
        (and deleted from disk). When no markers exist, returns an
        empty list and ``notice_callback`` is not invoked.

        Default ``notice_callback`` is None → no surfacing; the CLI
        wiring at startup passes a callback that prints to stderr.
        """
        markers = cls.check_pending_andon()
        if not markers:
            return []
        for marker in markers:
            if notice_callback is not None:
                try:
                    notice_callback(marker)
                except Exception as exc:
                    logger.debug(
                        "[grove.dispatcher] pending_andon notice callback "
                        "raised on marker %r: %r",
                        marker.get("session_id"), exc,
                    )
            # Delete the marker (Option 1: Discard with notice).
            marker_path = marker.get("_marker_path")
            if marker_path:
                try:
                    Path(marker_path).unlink()
                except OSError as exc:
                    logger.debug(
                        "[grove.dispatcher] could not unlink acknowledged "
                        "pending_andon marker %s: %r",
                        marker_path, exc,
                    )
        return markers

    def get_tier_override(self, agent_or_session_id: Any) -> Optional[str]:
        """Return the currently-set tier override for a session, or None.

        Sprint 27's escalation handler / the CognitiveRouter consumer
        reads this when deciding the next turn's tier. None means no
        override; the routing policy's default applies.
        """
        if isinstance(agent_or_session_id, str):
            session_id = agent_or_session_id
        else:
            session_id = getattr(agent_or_session_id, "session_id", None) or "unknown"
        return self._tier_overrides.get(session_id)

    # ── Phase 4 helpers ──────────────────────────────────────────────────

    def _classify_intents_batch_and_halt_or_raise(
        self,
        intents: List[Any],
    ) -> None:
        """Classify every ToolIntent in the yielded batch; raise AndonHalt
        on the first Yellow or Red zone hit.

        Per the D6 lock, the batch is the unit of disposition: a single
        Red/Yellow intent halts the whole batch. Classification of each
        intent is best-effort — for terminal-style tools we use the
        existing Sprint 06a command classifier (which handles arg-level
        denylists per Sprint 22); for other tools we use the generic
        ``classify(action)`` path with a derived dot-notation action.
        Tools with no schema entry default to ``yellow`` per the
        classifier's design — Phase 4 surfaces that as a halt so the
        operator can decide whether to whitelist or block.

        Sprint 27 may add per-tool classification rules so non-terminal
        tools have explicit Green coverage; for v1 the default-Yellow
        means the operator approves anything not on the auto_approve
        list. That matches Sprint 06a's design intent.
        """
        from grove import dispatch as _grove_dispatch
        from grove.zones import ZoneResult

        zone_results: List[ZoneResult] = []
        for intent in intents:
            zone_result = self._classify_one_intent(intent, _grove_dispatch)
            zone_results.append(zone_result)
            # First Yellow or Red halts the batch. Green continues.
            if zone_result.zone in ("yellow", "red"):
                # Continue classifying remaining intents for visibility,
                # then raise after collecting the full batch result so
                # Phase 5's Sovereign Prompt can show every intent's zone.
                pass

        triggering_index: Optional[int] = None
        for idx, zr in enumerate(zone_results):
            if zr.zone in ("yellow", "red"):
                triggering_index = idx
                break
        if triggering_index is not None:
            raise AndonHalt(
                intents=intents,
                zone_results=zone_results,
                triggering_index=triggering_index,
            )

    @staticmethod
    def _classify_one_intent(intent: Any, _grove_dispatch: Any) -> Any:
        """Classify a single ToolIntent via the Sprint 06a zone classifier.

        For ``terminal``-style intents (those whose tool_name is in the
        shell-runner set AND whose arguments carry a ``command`` string),
        derives the action via ``command_to_action`` and runs the
        hierarchical classifier (Sprint 22). For other intents, uses
        the generic ``classify(action)`` path with a derived
        ``tool.<tool_name>`` action.
        """
        from grove.zones import classify as _classify

        tool_name = getattr(intent, "tool_name", "") or ""
        args = getattr(intent, "arguments", None) or {}
        # Shell-runner detection: terminal-family tools with a `command`
        # argument route through the command-string classifier so
        # Sprint 22's argument-pattern denylists apply.
        if tool_name in {"terminal", "execute_code"} and isinstance(args, dict):
            command = args.get("command")
            if isinstance(command, str) and command.strip():
                return _grove_dispatch.classify_command(
                    command, tool_id=tool_name,
                )
        # Generic path: the action string IS the bare tool_name, matching
        # the convention in zones.schema.yaml::tool_zones (entries like
        # ``terminal``, ``calendar.read``, ``notion_search``). The earlier
        # ``tool.<name>`` prefix was a Phase 4 defect — it produced action
        # strings no schema entry could ever match, making every non-
        # terminal tool default-yellow regardless of configuration.
        action = tool_name if tool_name else "unknown"
        return _classify(action)

    # ── Phase 5 helpers (disposition flow + pending_andon marker) ────────

    def _handle_andon_halt(self, agent: Any, halt: "AndonHalt") -> str:
        """Write the pending_andon marker, prompt the operator, clear marker.

        Returns the operator's disposition: ``"skip"``, ``"drop"``, or
        ``"shadow_approve"`` (when ``GROVE_ZONE_SHADOW=1`` is set).

        The marker write happens BEFORE the prompt so a process killed
        mid-prompt leaves a recoverable trail. The marker clear runs in
        a ``finally`` so it always fires, including when the prompt
        raises.

        Per D3 lock: pending_andon is a structural persistent marker —
        not a serialization of the generator state (which contains
        unpicklable references like LLM clients and thread locks). On
        process restart, ``check_pending_andon()`` surfaces the marker
        so the operator can acknowledge the lost turn (Phase 5 MVP) or
        — in a future sprint — invoke a recovery flow.

        Shadow mode (``GROVE_ZONE_SHADOW=1``): the would-have-been halt
        is already captured in the Kaizen Ledger by the caller's
        ``andon_halt`` record (with full intent + zone_result detail).
        This handler short-circuits the marker write + sovereign
        prompt and returns ``"shadow_approve"`` so the caller falls
        through to the Green-path executor. The tool runs; the ledger
        carries the halt for later calibration review.
        """
        if os.environ.get("GROVE_ZONE_SHADOW") == "1":
            triggering = halt.intents[halt.triggering_index].tool_name
            print(
                f"[shadow] would halt: {triggering} "
                f"({halt.zone}, {halt.matched_rule})",
                file=_sys.stderr,
            )
            return "shadow_approve"
        marker_path = self._write_pending_andon(agent, halt)
        try:
            disposition = self._sovereign_prompt_handler(halt)
        finally:
            self._clear_pending_andon(agent, marker_path)
        return disposition

    def _build_skip_observations(
        self, agent: Any, intents: List[Any],
    ) -> List[Any]:
        """Phase 5 Skip — append denial tool messages + build Observations.

        For each intent in the halted batch:
          * Append a tool message to the agent's messages list with a
            denial body (so the next LLM call sees a paired tool
            response for every assistant tool_call — required by every
            provider's API).
          * Build an Observation carrying ``success=False`` and the
            denial body as ``value``; the generator's downstream
            consumer (Phase 4 says Observations are informational
            because messages is the source of truth — Phase 5's Skip
            keeps that invariant).
        """
        from grove.intents import Observation

        # CAREFUL: `or []` here would replace the agent's empty messages
        # list with a fresh detached list; mutations would not propagate
        # back to the generator's local. Use an explicit None check.
        msgs = getattr(agent, "_current_messages", None)
        if msgs is None:
            msgs = []
        observations: List[Any] = []
        for intent in intents:
            denial = (
                f"⚠ Operator skipped tool '{intent.tool_name}' at Andon halt. "
                f"This call did not execute; the operator declined to run it."
            )
            tool_call_id = intent.call_id or ""
            msgs.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": denial,
            })
            observations.append(Observation(
                intent_id=intent.call_id,
                success=False,
                value=denial,
                metadata={"disposition": "skip", "reason": "andon_skip"},
            ))
        return observations

    def _format_drop_result(
        self, agent: Any, halt: "AndonHalt",
    ) -> Dict[str, Any]:
        """Phase 5 Drop — flush volatile turn state; persistent unchanged.

        Per § IX(3): "Disposition: Drop — The Dispatcher MUST forcefully
        terminate the generator. The volatile context array MUST be
        flushed. The persistent state MUST remain identical to the
        millisecond before the operator initiated the turn."

        The ``gen.close()`` call in the outer ``dispatch_turn`` (via the
        ``finally`` block) raises GeneratorExit at the yield point, the
        generator's own finally clears ``agent._current_*``, and the
        in-flight messages list (volatile) is discarded — it was never
        committed to the persistent session store because the legacy
        code path only persists at specific commit points that haven't
        been reached when an intent-yield halt fires.

        For Phase 5 MVP, this method returns a result dict carrying the
        Drop outcome. The caller's persistent session_db is unchanged
        (no writes happen here).
        """
        triggering_intent = halt.intents[halt.triggering_index]
        return {
            "final_response": (
                f"⚠ Turn dropped by operator (Andon: tool "
                f"'{triggering_intent.tool_name}' classified as "
                f"{halt.zone} zone)."
            ),
            "completed": False,
            "interrupted": False,
            "partial": True,
            "messages": [],  # volatile state flushed per § IX(3)
            "api_calls": self._current_turn_api_call_count or 0,
            "turn_exit_reason": "andon_drop",
            "andon_disposition": {
                "disposition": "drop",
                "zone": halt.zone,
                "matched_rule": halt.matched_rule,
                "triggering_intent": {
                    "tool_name": triggering_intent.tool_name,
                    "arguments": dict(triggering_intent.arguments),
                    "call_id": triggering_intent.call_id,
                },
            },
            "model": getattr(agent, "model", ""),
            "provider": getattr(agent, "provider", ""),
        }

    # ── D3 pending_andon marker (Phase 5 — process-restart resilience) ───

    @staticmethod
    def _pending_andon_dir() -> Path:
        """The directory holding pending_andon markers.

        Operator-side path: ``~/.grove/.pending_andon/<session_id>.json``.
        A separate directory (not the session_db) keeps the marker
        independent of any specific DB schema and makes Phase 6/7
        startup recovery a trivial file-listing operation.
        """
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / ".pending_andon"

    @classmethod
    def _write_pending_andon(
        cls, agent: Any, halt: "AndonHalt",
    ) -> Path:
        """Write a pending_andon marker for D3 process-restart resilience.

        The marker carries the minimum information needed to acknowledge
        the lost turn on a restart: session_id, the triggering halt's
        zone + rule, the intent batch's tool names and call_ids, and a
        timestamp. The full generator state is NOT serialized —
        ``_run_turn_generator`` holds references to LLM clients,
        thread-local context, and other unpicklable objects, and
        re-creating that state cleanly is a horizon problem.

        On startup, ``check_pending_andon()`` surfaces these markers so
        the operator can acknowledge them (recovery from a paused turn
        is a Phase 6/7 capability; Phase 5 just records the marker and
        ensures the Dispatcher knows it exists).
        """
        marker_dir = cls._pending_andon_dir()
        marker_dir.mkdir(parents=True, exist_ok=True)
        session_id = getattr(agent, "session_id", None) or "unknown"
        # Sanitize session_id for filename use (defensive — session IDs
        # are typically safe but we don't want a malformed id to break
        # the marker write).
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in str(session_id)
        )[:128]
        marker_path = marker_dir / f"{safe_id}.json"
        payload = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "halt": {
                "zone": halt.zone,
                "matched_rule": halt.matched_rule,
                "source": halt.source,
                "reason": halt.reason,
                "triggering_index": halt.triggering_index,
            },
            "intents": [
                {
                    "tool_name": i.tool_name,
                    "call_id": i.call_id,
                }
                for i in halt.intents
            ],
        }
        marker_path.write_text(
            _json_mod.dumps(payload, indent=2), encoding="utf-8",
        )
        logger.debug(
            "[grove.dispatcher] pending_andon marker written: %s",
            marker_path,
        )
        return marker_path

    @classmethod
    def _clear_pending_andon(
        cls, agent: Any, marker_path: Optional[Path] = None,
    ) -> None:
        """Remove a pending_andon marker after disposition completes.

        Best-effort: a missing file or unlink failure is logged at debug
        but does not raise. The marker is informational; failing to
        clear it leaves a stale entry the operator can see on restart
        (worst case: an extra prompt to acknowledge).
        """
        if marker_path is None:
            session_id = getattr(agent, "session_id", None) or "unknown"
            safe_id = "".join(
                c if c.isalnum() or c in ("-", "_") else "_"
                for c in str(session_id)
            )[:128]
            marker_path = cls._pending_andon_dir() / f"{safe_id}.json"
        try:
            if marker_path.exists():
                marker_path.unlink()
        except OSError as exc:
            logger.debug(
                "[grove.dispatcher] could not clear pending_andon "
                "marker at %s: %r",
                marker_path, exc,
            )

    @classmethod
    def check_pending_andon(cls) -> List[Dict[str, Any]]:
        """Return any pending_andon markers from prior sessions.

        A Dispatcher / CLI starting fresh calls this to detect turns
        abandoned during a Sovereign Prompt (e.g., the process was
        killed while waiting for operator input). Phase 5 MVP just
        surfaces them; a future sprint can wire actual recovery
        (replay the user message that triggered the paused turn, or
        ack-and-discard).

        Returns a list of marker payload dicts, one per marker file.
        Markers that fail to parse are skipped (logged at debug).
        """
        marker_dir = cls._pending_andon_dir()
        if not marker_dir.exists():
            return []
        markers: List[Dict[str, Any]] = []
        for path in sorted(marker_dir.glob("*.json")):
            try:
                content = path.read_text(encoding="utf-8")
                payload = _json_mod.loads(content)
                payload["_marker_path"] = str(path)
                markers.append(payload)
            except (OSError, ValueError) as exc:
                logger.debug(
                    "[grove.dispatcher] could not read pending_andon "
                    "marker at %s: %r",
                    path, exc,
                )
        return markers

    @staticmethod
    def _format_andon_halt_result(
        agent: Any, halt: "AndonHalt",
    ) -> Dict[str, Any]:
        """Build a result dict for a Phase 4 internal AndonHalt.

        Phase 5 will replace this with the Sovereign Prompt + disposition
        flow. Phase 4 just returns a shape compatible with the legacy
        result dict so existing callers don't crash on a halt.
        """
        triggering_intent = halt.intents[halt.triggering_index]
        msgs = getattr(agent, "_current_messages", None) or []
        return {
            "final_response": (
                f"⚠ Andon halt: tool '{triggering_intent.tool_name}' "
                f"classified as {halt.zone} zone "
                f"(rule={halt.matched_rule!r})."
            ),
            "completed": False,
            "interrupted": False,
            "partial": True,
            "messages": list(msgs),
            "api_calls": self._current_turn_api_call_count or 0,
            "turn_exit_reason": "andon_halt",
            "andon_halt": {
                "zone": halt.zone,
                "matched_rule": halt.matched_rule,
                "source": halt.source,
                "reason": halt.reason,
                "pattern_key": halt.pattern_key,
                "triggering_index": halt.triggering_index,
                "triggering_intent": {
                    "tool_name": triggering_intent.tool_name,
                    "arguments": dict(triggering_intent.arguments),
                    "call_id": triggering_intent.call_id,
                },
                "batch_size": len(halt.intents),
                "zone_results": [
                    {
                        "zone": zr.zone,
                        "matched_rule": zr.matched_rule,
                        "source": zr.source,
                    }
                    for zr in halt.zone_results
                ],
            },
            "model": getattr(agent, "model", ""),
            "provider": getattr(agent, "provider", ""),
        }

    def _get_or_build_context_length(self, model: str) -> Optional[int]:
        """Build (or return cached) model context-length probe result.

        The underlying ``get_model_context_length`` call may hit an
        HTTP /models endpoint (50-150ms first-call cost), which the
        auxiliary client already caches with a 1-hour TTL. The
        Dispatcher's process-lifetime cache makes the result available
        across all Agent constructions without the auxiliary cache
        lookup.

        Returns ``None`` when the probe fails — the Agent's existing
        path handles a missing context_length gracefully.
        """
        cached = self._context_length_cache.get(model)
        if cached is not None:
            return cached
        try:
            from agent.auxiliary_client import get_model_context_length
        except ImportError:
            return None
        try:
            length = get_model_context_length(model)
        except Exception:
            return None
        if isinstance(length, int) and length > 0:
            self._context_length_cache[model] = length
            return length
        return None

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _load_config_safely() -> Dict[str, Any]:
        """Read config via ``hermes_cli.config.load_config``; fall back to
        empty dict on import failure.

        The fallback is the only graceful-degradation path in the Dispatcher
        — and it exists because grove-autonomaton's batch / trajectory
        modes intentionally construct AIAgents without a full CLI config
        environment. Production CLI / gateway paths always have a config.
        """
        try:
            from hermes_cli.config import load_config
        except ImportError as exc:
            logger.warning(
                "[grove.dispatcher] hermes_cli.config unavailable (%r); "
                "RuntimeContext.config will be empty",
                exc,
            )
            return {}
        try:
            cfg = load_config()
            return cfg if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] load_config() raised %r; "
                "RuntimeContext.config will be empty",
                exc,
            )
            return {}
