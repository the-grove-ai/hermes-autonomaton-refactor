"""Grove Dispatcher — runtime entry point per GRV-005.

Sprint 26 Phase 1a (dispatch-pipeline-implementation-v1) lands the
structural seam: a Dispatcher class that captures substrate (env vars
+ config snapshot) into a typed RuntimeContext, and an injection
parameter on ``AIAgent.__init__`` that lets the Agent read from the
context instead of touching substrate directly.

Phase 1a is structural only. The Dispatcher does NOT yet:

* Build heavy singletons (LLM client, tool registry, context compressor,
  memory store). That lands in Phase 1b.
* Invert tool execution. That lands in Phase 3.
* Wire into ``cli.py`` as the runtime entry point. That lands when
  the generator-shaped agent loop is in place.

The full GRV-005 § II Dispatcher responsibilities — message-zone
classification, tier selection before agent construction, tool
execution authority, escalation decisions, post-turn Kaizen observation
— populate across Phases 1b through 7. This Phase 1a module exists so
the substrate extraction (Sprint 26 D4: 14 violations of the Agent
contract that the GRV-005 inversion otherwise breaks) can land without
also rewriting the heavy-resource construction path.

Backward compatibility: existing callers (cli.py, oneshot.py, gateway
paths, tests) construct ``AIAgent(...)`` directly without going through
the Dispatcher. When ``runtime_ctx`` is ``None``, the Agent's substrate
sites fall through to the legacy direct-read path. Phase 7 cleanup
removes the fallback once every caller routes through the Dispatcher.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, TypeVar

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

    def __init__(self) -> None:
        """Capture the substrate snapshot.

        Reads ``os.environ`` and calls ``hermes_cli.config.load_config()``
        — the two substrate surfaces D4 audited. The Agent constructed
        downstream via ``build_runtime_context()`` reads through the
        snapshot instead of repeating these calls.

        Phase 1b heavy resources (tool registry, memory store, LLM
        clients, context-length cache) are built lazily on first request
        via ``runtime_context_for(...)``. Construction at Dispatcher
        ``__init__`` time stays light so the Dispatcher itself does not
        impose a startup tax.
        """
        config = self._load_config_safely()
        self._base_runtime_ctx = RuntimeContext(
            env=dict(os.environ),
            config=config,
        )
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
        logger.debug(
            "[grove.dispatcher] runtime context captured: "
            "%d env vars, config keys=%s",
            len(self._base_runtime_ctx.env),
            sorted(self._base_runtime_ctx.config.keys())[:10],
        )

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
        from grove.intents import FinalResponse, Observation

        gen = agent._run_turn_generator(user_message=user_message, **kwargs)
        try:
            yielded = gen.send(None)  # advance to first yield
            while True:
                if isinstance(yielded, list):
                    # ── Sprint 26 Phase 4 — tool-zone classification at intent-yield ──
                    # Per GRV-005 § V: classification fires per ToolIntent
                    # batch at the yield boundary, before execution. Per the
                    # D6 lock, the batch is the unit — if ANY intent crosses
                    # Yellow / Red, the whole batch halts via AndonHalt.
                    # Phase 5 adds the Sovereign Prompt + disposition
                    # semantics (Skip/Drop); Phase 4 just fires + halts.
                    self._classify_intents_batch_and_halt_or_raise(yielded)

                    # ── Phase 3 path — execute the batch ──
                    # ToolIntent batch — execute via the Agent's existing
                    # _execute_tool_calls (which mutates the messages list
                    # the generator exposed on agent._current_messages).
                    asst = agent._current_assistant_message
                    msgs = agent._current_messages
                    task = agent._current_effective_task_id
                    api_n = agent._current_api_call_count
                    agent._execute_tool_calls(asst, msgs, task, api_n)
                    # Package informational Observations for the generator.
                    observations: List[Any] = []
                    for intent in yielded:
                        tool_msg = None
                        cid = intent.call_id
                        if cid:
                            for m in reversed(msgs):
                                if isinstance(m, dict) and m.get("tool_call_id") == cid:
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
                elif isinstance(yielded, FinalResponse):
                    # The generator's contract: FinalResponse signals
                    # the turn is ending; the next .send() drives it to
                    # the return statement and raises StopIteration.
                    yielded = gen.send(None)
                else:
                    # Unrecognized payload (defensive). Advance with None.
                    yielded = gen.send(None)
        except StopIteration as stop:
            return stop.value
        except AndonHalt as halt:
            # Close the generator cleanly so its `finally` blocks run
            # (clears agent._current_* stash etc.). Phase 5 will replace
            # this close() with the Sovereign Prompt + disposition routing
            # (Skip: resume with denial Observation; Drop: close + flush).
            gen.close()
            return self._format_andon_halt_result(agent, halt)

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
        # Generic path: derive a dot-notation action from the tool name.
        action = f"tool.{tool_name}" if tool_name else "tool.unknown"
        return _classify(action)

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
            "api_calls": getattr(agent, "_current_api_call_count", 0) or 0,
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
