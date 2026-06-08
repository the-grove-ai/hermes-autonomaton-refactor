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
import hashlib
import logging
import os
import re
import sys as _sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

from grove.pattern_cache import pattern_cache_enabled

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Sprint 73 Phase 4a — sentinel for the "no tier gating applied yet" state, so
# the first carrier application always triggers a recompose (a real frozenset,
# including the empty one, can never compare equal to this).
_TIER_CTX_UNSET = object()


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
from grove.operator_input import OperatorInputRequired


# ── Sprint 32 Phase 3a — red-zone strike threshold ────────────────────
#
# Three red-zone halts on the same tool within a single turn force a
# hard-denial Observation that explicitly directs the LLM not to
# attempt the tool with the same arguments again. The counter resets
# at the turn boundary; cross-turn red-zone enforcement is
# architectural (the zone rule persists), so a reset-per-turn pattern
# prevents intra-turn loops without weakening cross-turn discipline.
_RED_ZONE_STRIKE_LIMIT = 3


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


# Sprint 48 — cap on captured response_content in the intent record (T0
# static-pattern evidence). Factual answers are short; the cap keeps the
# append-only store from bloating on a pathologically long response.
_T0_RESPONSE_CONTENT_CAP = 4000


# Sprint 53.2 — extract a quarantined skill's name + directory from a
# terminal command that references ``~/.grove/skills/.andon/<name>/``.
# ``path`` captures the quarantine directory; ``name`` the skill folder.
_ANDON_SKILL_RE = re.compile(
    r"(?P<path>[^\s'\"]*\.grove/skills/\.andon/(?P<name>[^/\s'\"]+))"
)


def _synth_skill_eval_hash(skill_name: str, skill_path: str) -> str:
    """Synthesize the proposal's ``eval_hash`` for a skill promotion.

    ``RoutingProposal.eval_hash`` is mandatory and normally projects an
    ``EvalReport`` — a skill promotion has none. Per the GATE-A minor
    call, derive a defined, content-addressable value so the queue's
    ``_read_records`` round-trips cleanly instead of carrying an empty
    sentinel.
    """
    seed = f"skill_promotion|{skill_name}|{skill_path}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


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
        post_execution_prompt_handler: Optional[Callable[[Any], str]] = None,
        kaizen_ledger_dir: Optional[Path] = None,
        intent_store: Optional[Any] = None,
        agent_kwargs: Optional[Dict[str, Any]] = None,
        session_db: Optional[Any] = None,
        session_id: Optional[str] = None,
        resume: bool = False,
        memory_store: Optional[Any] = None,
        memory_manager: Optional[Any] = None,
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
        layer and returns one of the four GRV-005 § VI v1.1
        disposition strings (``"once"`` / ``"session"`` / ``"always"``
        / ``"deny"``).
        """
        # Sprint 26 Phase 7 hotfix: prime the zone classifier singleton so
        # Phase 4's classify() at ToolIntent yield has it ready.
        import grove.zones as _zones; _zones.initialize()

        # ── Sprint 53 — Dispatcher-owned ToolRegistry (router-resident) ──
        # GRV-005 § III: tool registration is router-resident.  The
        # Dispatcher constructs its own ToolRegistry, populates it with
        # built-in tools via the explicit ``register_builtin_tools``
        # bootstrap, then discovers user plugins and MCP servers against
        # it.  The Agent never holds a reference to the registry — it
        # reads the authorized tool set through the
        # ``_get_available_tools`` callback below.
        from tools.registry import ToolRegistry, register_builtin_tools
        self.registry: ToolRegistry = ToolRegistry()
        register_builtin_tools(self.registry)
        # Discover user / project / pip plugins.  Idempotent — multiple
        # Dispatchers in one process replay registrations against each
        # owned registry. Plugin discovery failures (manifest errors,
        # backend import errors) are non-fatal and surfaced via the
        # PluginManager's per-plugin ``error`` field for introspection.
        try:
            from hermes_cli.plugins import discover_plugins as _discover_plugins
            _discover_plugins(registry=self.registry)
        except Exception as _plugin_exc:
            logger.debug(
                "[grove.dispatcher] plugin discovery failed at Dispatcher init: %r",
                _plugin_exc,
            )
        # MCP discovery — explicit, against this Dispatcher's registry.
        # Failures are non-fatal; the rest of the Dispatcher init
        # continues and tools that depend on MCP servers fail at
        # call-time with a clear error.
        try:
            from tools.mcp_tool import discover_mcp_tools as _discover_mcp_tools
            _discover_mcp_tools(registry=self.registry)
        except Exception as _mcp_exc:
            logger.debug(
                "[grove.dispatcher] MCP discovery failed at Dispatcher init: %r",
                _mcp_exc,
            )
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
        # Sprint 53.2 — post-execution Kaizen promotion prompt handler.
        # Distinct from the four-choice Sovereign Prompt above (different
        # vocabulary: Promote / Not yet / Never). TTY callers (cli.py)
        # inject a handler that renders the three-choice prompt; headless
        # surfaces (gateway, batch, run_agent, tests) leave it None, in
        # which case the Dispatcher auto-logs a pending skill_promotion
        # proposal to the Flywheel queue — never silently discarded.
        self._post_execution_prompt_handler: Optional[Callable[[Any], str]] = (
            post_execution_prompt_handler
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
        # ── Sprint 73 Phase 4a — tier-budget carriers ────────────────
        # ``_tier_budgets_cache`` holds the validated load_tier_budgets()
        # result, loaded lazily on the first routed turn (fail-loud at load,
        # D7). ``_last_applied_tier_context_blocks`` tracks the last gating
        # set applied so the carrier-change recompose stays cache-friendly;
        # the sentinel guarantees the first application always recomposes.
        self._tier_budgets_cache: Optional[Dict[str, Any]] = None
        self._last_applied_tier_context_blocks: Any = _TIER_CTX_UNSET
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
        # Sprint 53.2 — turn-scoped quarantine-execution flag. Set in
        # ``_handle_andon_halt`` when an "allow once" disposition lets a
        # ``.andon/`` skill run; checked at the ``FinalResponse`` site to
        # fire the post-execution promotion prompt; RESET at the top of
        # every ``_drive_generator`` (turn boundary) so it never bleeds
        # across turns or survives a tool-call retry loop within a turn.
        # Holds a dict (skill_name, skill_path, execution_turn_id,
        # cache_key) or None.
        self._quarantine_skill_executed_this_turn: Optional[Dict[str, Any]] = None
        # Sprint 35 — RoutingDecision the per-turn classify call produced.
        # Read via back-reference by ``AIAgent.run_conversation``'s tail
        # to surface ``result["routing_decision"]`` for webui consumers.
        self._current_turn_routing_decision: Optional[Any] = None
        # Sprint 49 — set to a served pattern's pattern_id when THIS turn
        # resolved via a T0 cache hit (else None). Captured into a local at
        # the next ``dispatch_turn`` entry so the correction-driven
        # auto-demotion check (Phase 2) can ask "was the previous turn a T0
        # hit?" before this turn's reset clears it.
        self._current_turn_t0_pattern_id: Optional[str] = None
        # Sprint 49 — lazily-constructed PatternCacheStore, reused across
        # turns so the hot path doesn't reopen the DB handle each lookup.
        self._pattern_store: Optional[Any] = None
        self._current_turn_start: Optional[float] = None
        self._current_turn_tools_yielded: List[str] = []
        # Sprint 48 — per-turn tool invocations (name + args) for the T0
        # pattern compiler's EXECUTABLE evidence. Captured alongside the
        # names; only single-invocation turns yield a clean executable
        # pattern (set on the intent record below).
        self._current_turn_tool_invocations: List[Dict[str, Any]] = []
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
        # ── Sprint 32 — Kaizen session caches (sovereignty-ux-v1) ────
        # Operator dispositions remembered for the lifetime of THIS
        # Dispatcher instance. Keyed by (tool_name, sha256(canonical
        # JSON of arguments)). The deny cache populates on operator
        # "Don't allow"; the allow cache populates on "Allow for this
        # session" and "Always allow". Subsequent identical halts
        # auto-apply silently and log a session_cache_hit telemetry
        # event to the Kaizen Ledger. The caches live on the
        # Dispatcher (not the handler) so they survive a per-call
        # handler swap and reset automatically when the operator
        # starts a new session (new Dispatcher = empty caches).
        self._session_deny_cache: Set[Tuple[str, str]] = set()
        self._session_allow_cache: Set[Tuple[str, str]] = set()
        # ── Sprint 32 Phase 3a — red-zone strike counter ─────────────
        # Per-turn, per-tool. Increments on each red-zone halt. At
        # ``_RED_ZONE_STRIKE_LIMIT`` strikes the Dispatcher forces a
        # hard-denial Observation containing the directive
        # "HARD DENIAL: ... Do not attempt this tool with these
        # arguments again." per the operator's Trap-B lock — making
        # the denial structurally terminal for that specific vector
        # within the turn. Resets at every ``dispatch_turn`` entry
        # so cross-turn enforcement remains architectural (the zone
        # rule itself persists across turns).
        self._current_turn_andon_strikes: Dict[str, int] = {}
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

        # Sprint 39 — when a caller hands the Dispatcher a session_id
        # (or a resume target), open the session BEFORE constructing
        # the Agent so the Dispatcher's session_id and row state are
        # populated by the time the Agent's first method touches them.
        # This is the load-bearing path for Sprint 35: open_session
        # works pre-Agent-construction.
        if session_id is not None or resume:
            self.open_session(session_id=session_id, resume=resume)

        # ── Sprint 40 — memory authority ─────────────────────────────
        # Dispatcher.memory_store and Dispatcher.memory_manager are
        # the single Agent-path memory authorities. Caller-supplied
        # (rare; mostly tests) or built lazily by open_memory(). The
        # Agent does not hold either; reads route through the back-
        # reference, writes route through MemoryWriteIntent /
        # MemoryLifecycleIntent yields.
        self.memory_store: Optional[Any] = memory_store
        self.memory_manager: Optional[Any] = memory_manager
        # Memory-enable flags mirror the Agent's existing config-driven
        # behavior; open_memory() populates them from runtime_ctx.config.
        self._memory_enabled: bool = False
        self._user_profile_enabled: bool = False

        # Sprint 40 — when memory is enabled in config AND an Agent is
        # being constructed (agent_kwargs is not None) AND the caller
        # hasn't opted out via skip_memory, open the memory store +
        # manager BEFORE constructing the Agent. This is the Sprint 35
        # precondition path: hydrate_memory_context() works pre-Agent.
        # Test Dispatchers that omit agent_kwargs (or pre-supply
        # memory_store/memory_manager) skip the auto-build; Sprint 35
        # call sites that classify pre-construction call open_memory()
        # explicitly.
        if agent_kwargs is not None:
            _skip_memory = bool(agent_kwargs.get("skip_memory", False))
            if not _skip_memory and (
                self.memory_store is None or self.memory_manager is None
            ):
                self.open_memory(
                    platform=agent_kwargs.get("platform"),
                    provider_init_kwargs=self._collect_provider_init_kwargs(
                        agent_kwargs,
                    ),
                )

        self.agent: Optional[Any] = None
        if agent_kwargs is not None:
            from run_agent import AIAgent
            # Sprint 34 — the Dispatcher owns the substrate snapshot,
            # so forward it into the Agent. setdefault preserves any
            # explicit ctx the caller put into agent_kwargs (rare;
            # mostly tests that build a richer ctx).
            agent_kwargs.setdefault("runtime_ctx", self._base_runtime_ctx)
            # Sprint 53 — capability provider callback. The Agent has
            # no registry reference; it reads the authorized tool set
            # exclusively through this callback. setdefault preserves
            # any explicit override (tests inject a stub).
            agent_kwargs.setdefault(
                "get_available_tools", self.get_authorized_tools,
            )
            # Sprint 39 — session_id flows from the Dispatcher's owned
            # value when one was opened pre-Agent; setdefault preserves
            # any explicit caller value (rare; mostly tests).
            if self.session_id is not None:
                agent_kwargs.setdefault("session_id", self.session_id)
            # Sprint 39 — pre-read session_title here so the Agent's
            # memory-provider init (which runs during __init__, before
            # ``_dispatcher_singleton`` is wired) can scope by it. For
            # fresh sessions this is None; for resumed sessions it is
            # the title the previous session ended with.
            if self.session is not None and self.session_id:
                try:
                    pre_title = self.session.get_session_title(self.session_id)
                except Exception:
                    pre_title = None
                if pre_title:
                    agent_kwargs.setdefault("session_title", pre_title)
            self.agent = AIAgent(**agent_kwargs)
            self.agent._dispatcher_singleton = self
            # Sprint 36 — compose the system prompt POST-construction so
            # the providers see the Agent's resolved ``valid_tool_names``
            # (Sprint 29 filter runs at runtime, not at compose time, but
            # the base toolset is set during ``AIAgent.__init__``).
            # The composed string lives on the Agent under
            # ``_composed_system_prompt`` per GRV-007 § II — the Agent
            # receives the prompt, does not produce it.
            self._compose_and_inject_system_prompt(self.agent)
            # Sprint 39 — sync Agent-generated session_id back so the
            # Dispatcher's lifecycle methods (open_turn_row, append,
            # rotate, etc.) see the live value. Production callers who
            # pre-opened via session_id Dispatcher-kwarg already have
            # self.session_id set; this catches the case where session_id
            # was passed only through agent_kwargs and AIAgent auto-
            # generated or echoed it.
            if self.session_id is None:
                _agent_sid = getattr(self.agent, "session_id", None)
                if _agent_sid:
                    self.session_id = _agent_sid
            # Sprint 40 — inject memory-provider tool schemas into the
            # Agent's tool list. Previously the Agent's __init__ did
            # this against ``self._memory_manager``; with manager
            # ownership relocated to the Dispatcher, injection must
            # happen here after the Agent's tools list is built.
            self._inject_memory_tool_schemas()

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

        Sprint 53 — reads through the Dispatcher-owned ``self.registry``;
        ``get_tool_definitions`` no longer reaches a module-level singleton.
        """
        key = (
            tuple(sorted(enabled_toolsets or ())),
            tuple(sorted(disabled_toolsets or ())),
            bool(quiet_mode),
        )
        cached = self._tools_cache.get(key)
        if cached is not None:
            return cached
        from model_tools import get_tool_definitions
        tools = get_tool_definitions(
            self.registry,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            quiet_mode=quiet_mode,
        )
        self._tools_cache[key] = tools
        return tools

    # ── Sprint 53 — Capability Provider ──────────────────────────────────
    def get_authorized_tools(
        self,
        enabled_toolsets: Optional[List[str]] = None,
        disabled_toolsets: Optional[List[str]] = None,
        quiet_mode: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return the filtered OpenAI-format tool list the Agent may call.

        Sprint 53 § III — this is the capability provider the Agent
        receives as ``_get_available_tools``. It wraps
        :func:`model_tools.get_tool_definitions` against the Dispatcher's
        owned registry and threads through the same memoized cache used
        by ``_get_or_build_tools``. The Agent never sees the registry
        itself; it only reads the filtered snapshot through this
        callback.
        """
        return self._get_or_build_tools(
            enabled_toolsets, disabled_toolsets, quiet_mode,
        )

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
        # Sprint 35 — defer classification until AFTER the per-turn
        # reset below so the reset can't overwrite the captured value
        # to ``None``. Capture the gate flag here; fire the classify
        # call after the reset.
        # Sprint 35 — ``already_routed`` is the CLI/oneshot pre-routing
        # signal honored by the Dispatcher's pre-construction classify
        # path. It is NOT forwarded to the generator (Phase 2 deletes
        # ``_maybe_route_for_turn`` and the kwarg from the generator's
        # signature).
        already_routed = bool(kwargs.pop("already_routed", False))
        # Sprint 26 Phase 7 — Dispatcher broadcasts GROVE_SESSION_ID to
        # subprocess descendants on every turn. Authority moved here
        # from AIAgent.__init__ per GRV-005 § II/III: substrate writes
        # are Dispatcher-owned. Idempotent — safe to re-write per turn.
        # Sprint 39 — prefer self.session_id (the Dispatcher's owned id)
        # over the Agent's. Falls back to the Agent's for legacy callers
        # that bypass open_session() and pass session_id only through
        # agent_kwargs (e.g. test paths constructing AIAgent directly).
        session_id = self.session_id or getattr(agent, "session_id", None)
        if session_id:
            self.broadcast_session_id(str(session_id))
        # Sprint 39 — turn-boundary lifecycle hook. The Dispatcher
        # creates the session DB row on first use, replacing the
        # AIAgent._ensure_db_session() call that previously fired
        # inside the Agent's run_conversation flow. Idempotent: a row
        # already created (_session_row_created=True) is a no-op.
        if self.session is not None and self.session_id:
            self.open_turn_row(
                source=getattr(agent, "platform", None) or "cli",
                model=getattr(agent, "model", ""),
                model_config=getattr(agent, "_session_init_model_config", None),
                system_prompt=getattr(agent, "_cached_system_prompt", None),
                parent_session_id=getattr(agent, "_parent_session_id", None),
            )
        # Sprint 28 Phase 4 — explicit success finalization. Capture the
        # PREVIOUS turn's id before reset so we can finalize its pending
        # record (if any) as success. The first turn for this Dispatcher
        # has no previous (``_current_turn_id`` is None) — skip then.
        previous_turn_id = self._current_turn_id
        # Sprint 49 Phase 2 — capture whether the PREVIOUS turn resolved via
        # T0 before the reset below clears the flag. The correction-driven
        # auto-demotion check consumes it after this turn classifies.
        previous_turn_t0_pattern_id = self._current_turn_t0_pattern_id
        # Sprint 28 Phase 3 — reset per-turn state and assign this turn's
        # id BEFORE driving the generator, so terminal write sites
        # (FinalResponse / Drop / exception) have a stable identity to
        # write under.
        import time as _time
        self._turn_counter += 1
        self._current_turn_id = f"{session_id or 'unknown'}#{self._turn_counter}"
        self._current_turn_classification = None
        self._current_turn_routing_decision = None
        self._current_turn_t0_pattern_id = None
        self._current_turn_api_call_count = 0
        self._current_turn_start = _time.monotonic()
        self._current_turn_tools_yielded = []
        self._current_turn_tool_invocations = []
        self._current_turn_user_message = user_message
        self._current_turn_outcome_written = False
        # Sprint 30 — reset per-turn escalation counter + events list.
        self._current_turn_escalations = 0
        self._current_turn_escalation_events = []
        # Sprint 32 Phase 3a — reset per-turn red-zone strike counter.
        # Cross-turn enforcement remains architectural (the zone rule
        # itself blocks every turn); per-turn counter prevents the
        # agent from looping within a single turn.
        self._current_turn_andon_strikes = {}
        # Sprint 73 Phase 4a — wipe the tier-budget carriers every turn. They
        # are repopulated below by _classify_and_bind_turn (or the
        # already_routed branch) from the turn's resolved tier. No carryover
        # across turns — especially after a Sprint 30 escalation hot-swap,
        # which builds a fresh agent that must not inherit a stale T2 budget.
        agent._tier_budget = None
        agent._tier_context_blocks = None
        # Sprint 35 — pre-construction classification + tier binding.
        # Fires AFTER the per-turn reset block above so the reset
        # cannot null out the captured classification. Pre-Sprint-35
        # this work happened on the first ``send(None)`` inside the
        # generator via ``_maybe_route_for_turn``. Sprint 35 moves it
        # here so the Agent's reasoning never fires at the wrong tier
        # and Sprint 28 IntentRecord / Sprint 29 tool filter both see
        # the classification BEFORE the LLM call. ``already_routed``
        # short-circuits the route_for_agent call (CLI pre-routed via
        # ``_resolve_turn_agent_config``, or the Sprint 30 hot-swap
        # rebuild path) but the Dispatcher still snapshots whatever
        # ``providers._last_classification`` holds — pre-routing fills
        # the global, and downstream consumers expect the snapshot.
        #
        # Sprint 38 — classification MUST fire BEFORE the previous-turn
        # finalization. The classifier's learning_envelope.is_correction
        # is the signal that branches the previous turn's outcome
        # between success and correction. Reordering: classify → then
        # finalize → then drive the generator.
        # Sprint 49 — T0 Pattern Cache short-circuit. BEFORE classification,
        # BEFORE the reasoning generator. If the raw message resolves to an
        # active compiled pattern, serve it deterministically (no model call,
        # no classifier, no agent reasoning) and return the legacy result
        # dict directly. This is the cost-curve endpoint: T0 remembers.
        # ``_t0_intercept`` finalizes the previous turn, records the hit,
        # writes telemetry + the intent record, and returns the result dict;
        # a miss returns None and falls through to the normal flow unchanged.
        if isinstance(user_message, str) and pattern_cache_enabled():
            _t0_result = self._t0_intercept(
                agent, user_message, previous_turn_id, kwargs,
            )
            if _t0_result is not None:
                return _t0_result
        if not already_routed and isinstance(user_message, str):
            self._classify_and_bind_turn(agent, user_message, ledger)
        else:
            from grove.providers import (
                current_classification as _current_classification,
                current_tier as _current_tier,
            )
            self._current_turn_classification = _current_classification()
            # Sprint 73 Phase 4a — the pre-routed (CLI / hot-swap rebuild)
            # path still resolves a tier; thread its budget carriers from the
            # same single source so context gating applies here too.
            self._apply_tier_budget(agent, _current_tier())
        if previous_turn_id is not None:
            self._finalize_previous_turn_pending(previous_turn_id)
            # Sprint 49 Phase 2 — correction-driven auto-demotion. If the
            # previous turn was served from T0 and this turn's classifier
            # flagged a correction, suspend the served pattern. Runs AFTER
            # finalize so ``_current_turn_classification`` is in place; only
            # the normal (non-T0) flow reaches here, which is where a real
            # correction lands — its text misses the cache, so the classifier
            # ran and ``is_correction`` is readable.
            self._maybe_demote_on_correction(previous_turn_t0_pattern_id)
        gen = agent._run_turn_generator(user_message=user_message, **kwargs)
        try:
            return self._drive_generator(agent, gen, ledger)
        except OperatorInputRequired:
            # Sprint 67 — NOT an error terminal. A store-and-resume surface
            # (web /v1/chat/completions) deliberately yielded control to
            # await operator input. Record a deferral outcome — never
            # "error" — then re-raise so the surface's terminal catch
            # persists the PendingOperatorRequest and surfaces the prompt.
            # This guard sits ABOVE the BaseException catch precisely so
            # the deferral is not mislabeled as a failure in the ledger.
            self._write_intent_record(agent, outcome="awaiting_operator")
            raise
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
            MemoryLifecycleIntent,
            MemoryWriteIntent,
            Observation,
            SessionRotateIntent,
            SessionUpdateTokensIntent,
            ToolBatchYield,
        )
        import time as _time

        # Sprint 53.2 — reset the quarantine-execution flag at the turn
        # boundary. ``_drive_generator`` is invoked exactly once per
        # ``dispatch_turn`` (after ``_current_turn_id`` is set), so this is
        # turn-scoped: a dirty flag from a prior turn cannot leak forward,
        # and the flag persists across THIS turn's tool-call loop until the
        # FinalResponse site reads it.
        self._quarantine_skill_executed_this_turn = None

        try:
            yielded = gen.send(None)  # advance to first yield
            # Sprint 35 — classification + pre-route ledger event already
            # fired in ``dispatch_turn`` BEFORE this generator ran. The
            # in-generator ``_maybe_route_for_turn`` call site is dead
            # code Phase 2 deletes; the Dispatcher's
            # ``_current_turn_classification`` was populated by
            # ``_classify_and_bind_turn``.

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
                            # Sprint 48 — capture name + args for the T0
                            # compiler's executable evidence.
                            self._current_turn_tool_invocations.append({
                                "tool": _name,
                                "args": getattr(_intent, "arguments", None),
                            })
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
                        # Sprint 32 — Kaizen Sovereign Prompt + disposition.
                        # The handler may now return v1.1 vocabulary
                        # (once / session / always / deny) or v1.0
                        # legacy values (skip / drop / shadow_approve).
                        disposition = self._handle_andon_halt(agent, halt, ledger=ledger)
                        ledger.record(
                            "andon_disposition",
                            disposition=disposition,
                            zone=halt.zone,
                            matched_rule=halt.matched_rule,
                            triggering_tool=halt.intents[halt.triggering_index].tool_name,
                        )
                        # ── Deny branch ──────────────────────────────
                        # ``deny`` injects a denial Observation and
                        # lets the agent recover. ``deny_hard`` is the
                        # Sprint 32 Phase 3a red-zone strike-limit
                        # forced denial; same Observation pipeline with
                        # explicit directive text so the LLM does not
                        # re-attempt this tool with these arguments on
                        # the turn. (``deny_hard`` is set internally
                        # by the Dispatcher's strike counter, never
                        # returned by a handler.)
                        if disposition in ("deny", "deny_hard"):
                            observations = self._build_skip_observations(
                                agent, halt.intents,
                                hard=(disposition == "deny_hard"),
                            )
                            yielded = gen.send(observations)
                            continue
                        # ── Allow branches ───────────────────────────
                        # ``once``, ``session``, and ``always`` all
                        # fall through to the Green-path executor
                        # below. The handler already mutated caches
                        # per disposition.
                        if disposition not in ("once", "session", "always"):
                            raise ValueError(
                                f"Sovereign prompt returned unknown "
                                f"disposition: {disposition!r}. "
                                f"Valid: 'once' / 'session' / 'always' / "
                                f"'deny' per GRV-005 § VI v1.1."
                            )
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
                elif isinstance(yielded, MemoryWriteIntent):
                    # Sprint 40 — synchronous memory write. The Agent
                    # treats the returned ``MemoryWriteResult.value`` as
                    # the LLM tool result. Bidirectional yield-and-inject
                    # mirrors the Sprint 26 ToolIntent protocol applied
                    # to memory.
                    result = self.execute_memory_write(yielded)
                    yielded = gen.send(result)
                elif isinstance(yielded, MemoryLifecycleIntent):
                    # Sprint 40 — fire-and-forget memory-manager lifecycle
                    # event. Empty Observation back so the generator
                    # resumes without inspecting a value (matches the
                    # SessionUpdateTokensIntent pattern).
                    self.execute_memory_lifecycle(yielded)
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
                        response_content=yielded.content or "",
                    )
                    # Sprint 53.2 — the operator has now seen the skill's
                    # output (FinalResponse is recorded). If a quarantined
                    # skill ran this turn under "allow once", fire the
                    # post-execution promotion prompt BEFORE resuming the
                    # generator. One-shot per turn: clear so a retry /
                    # second FinalResponse does not re-prompt.
                    if self._quarantine_skill_executed_this_turn is not None:
                        flag = self._quarantine_skill_executed_this_turn
                        self._quarantine_skill_executed_this_turn = None
                        self._emit_post_execution_kaizen(flag, ledger=ledger)
                    yielded = gen.send(None)
                else:
                    yielded = gen.send(None)
        except StopIteration as stop:
            return stop.value

    # ── Sprint 28 Phase 4 helper (Explicit Success Finalization) ────────

    def _finalize_previous_turn_pending(self, previous_turn_id: str) -> None:
        """Finalize the previous turn's pending record.

        Sprint 28 Phase 4 wired the explicit success path. Sprint 38
        adds the correction branch: when the current turn's classifier
        sets ``learning_envelope.is_correction = true``, the previous
        turn's pending record finalizes as ``correction`` rather than
        ``success``. The Dispatcher MUST have classified the current
        turn before this method runs — ``dispatch_turn`` reorders the
        per-turn block so classification fires first.

        Outcome decision table (current turn's classification):

        * ``is_correction = True``  → previous turn finalizes as ``correction``
        * ``is_correction = False`` → previous turn finalizes as ``success``
        * ``is_correction = None``  → previous turn finalizes as ``success``
          (graceful: classifier failed or pre-Sprint-38 schema)

        The Implicit Success Sweep at Dispatcher init catches abandoned
        sessions; this method catches in-session continuations. The
        sweep is unconditional success — only the in-session path can
        produce ``correction``.

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
            classification = self._current_turn_classification
            is_correction = bool(getattr(classification, "is_correction", False))
            outcome = "correction" if is_correction else "success"
            self._intent_store.append(finalize_record(
                latest,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] finalization failed for previous "
                "turn %r: %r",
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
        # Sprint 73 Phase 4b (D8/D10) — tool-budget-strip escalations carry the
        # provenance that makes over-escalation OBSERVABLE: tier + stripped
        # groups + intent. A high rate is the signal to widen allow_groups in
        # config (config-over-code), not a hidden cost. Logged to BOTH the
        # ledger event and the IntentRecord-bound events list below.
        _esc_source = request.get("source")
        if _esc_source:
            event_payload["source"] = _esc_source
        if request.get("stripped_groups") is not None:
            event_payload["stripped_groups"] = request.get("stripped_groups")
        if request.get("intent_class") is not None:
            event_payload["intent_class"] = request.get("intent_class")
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
        # Sprint 73 Phase 4a — the escalated tier is a new routing decision;
        # re-apply the budget carriers on the fresh agent (PER-TURN WIPE +
        # SINGLE SOURCE) so context gating reflects target_tier, not the
        # retired T2 shell's. Recompose fires here because the carrier changed.
        self._apply_tier_budget(new_agent, decision.target_tier)

        # Start the new generator with the snapshotted messages. The
        # Agent's conversation_history kwarg accepts pre-seeded
        # messages — the existing path Sprint 27 exercised.
        new_gen = new_agent._run_turn_generator(
            user_message=snapshot_user_message,
            conversation_history=snapshot_messages,
            # Sprint 35 — the new turn doesn't re-classify because the
            # in-generator ``_maybe_route_for_turn`` is deleted. The
            # escalation grant IS the routing decision; this generator
            # picks up at the escalated tier (model already bound on
            # ``new_agent``).
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
            # Sprint 34 — runtime_ctx is mandatory on AIAgent. The hot-
            # swap rebuild inherits the parent agent's ctx; falling back
            # to the Sprint 34 tests' shared empty mock when the parent
            # somehow lacks one (test-bypass paths).
            "runtime_ctx": getattr(agent, "_runtime_ctx", None),
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
        response_content: Optional[str] = None,
        intent_class_override: Optional[str] = None,
        tier_override: Optional[str] = None,
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
                #
                # Sprint 49 — a T0 cache hit also reaches here with no
                # classifier output (by design: T0 never classifies). The
                # caller passes ``intent_class_override`` from the served
                # pattern's stored intent_class so the record is attributed
                # correctly (A4: downstream reads intent_class off the
                # record; the pattern fills it identically to a classified
                # turn). A T0 hit is deterministic, so confidence is 1.0.
                pattern_hash = "t0_cache_hit" if intent_class_override else "unclassified"
                intent_class = intent_class_override or "unknown"
                register_class = "unknown"
                complexity_signal = "unknown"
                confidence = 1.0 if intent_class_override else 0.0
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

            # Sprint 48 — T0 pattern-compiler evidence (GATE-A decision 3).
            # response_content (capped) feeds STATIC compilation; a SINGLE
            # tool invocation (name + args, JSON-encoded) feeds EXECUTABLE
            # compilation. Multi-tool turns leave tool_invocation None — they
            # are not clean executable patterns.
            _resp_content: Optional[str] = None
            if response_content:
                _resp_content = response_content[:_T0_RESPONSE_CONTENT_CAP]
            _tool_invocation: Optional[str] = None
            if len(self._current_turn_tool_invocations) == 1:
                try:
                    _tool_invocation = _json_mod.dumps(
                        self._current_turn_tool_invocations[0],
                        sort_keys=True, default=str,
                    )
                except Exception:
                    _tool_invocation = None

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
                tier_selected=(tier_override or _current_tier()),
                model_used=model_used,
                tools_yielded=tuple(self._current_turn_tools_yielded),
                api_calls=api_calls,
                duration_ms=round(duration_ms, 2),
                final_response_chars=final_response_chars,
                escalation_count=self._current_turn_escalations,
                response_content=_resp_content,
                tool_invocation=_tool_invocation,
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

    # ── Sprint 35 — pre-construction classification + tier binding ──────

    def _classify_and_bind_turn(
        self,
        agent: Any,
        user_message: str,
        ledger: Any,
    ) -> None:
        """Classify the inbound message and bind the routed tier to the Agent.

        Sprint 35 — replaces ``AIAgent._maybe_route_for_turn``. Fires
        from ``dispatch_turn`` BEFORE the generator runs so the Agent
        never produces an LLM call at the wrong tier. The Agent's
        reasoning loop receives the result (via the
        ``self._current_turn_classification`` capture + the
        ``providers._last_classification`` module global the existing
        Sprint 29 tool filter and Sprint 28 intent record already
        consume), it does not produce it.

        Vanilla install (no ``routing.config.yaml``) returns silently —
        ``route_for_agent`` returns ``None`` and the caller's chosen
        model is used unchanged.
        """
        from grove.providers import (
            route_for_agent,
            current_classification,
            current_pre_route_decision,
            resolve_tier_to_runtime,
        )
        decision = route_for_agent(
            message=user_message, explicit_model=None, explicit_tier=None,
        )
        if decision is None:
            # Vanilla install (no routing config) OR caller pre-set the
            # classification via the module global. Still snapshot the
            # global so downstream consumers (Sprint 28 IntentRecord
            # terminal write, Sprint 29 tool filter) see any pre-set
            # value. Empty global on a true vanilla install is the
            # expected None.
            self._current_turn_classification = current_classification()
            self._current_turn_routing_decision = None
            return
        # Sprint 28 Phase 3 — capture for terminal IntentRecord writes
        # and Sprint 29 tool filter. The Dispatcher's instance attr
        # snapshot is immune to a concurrent session overwriting the
        # ``providers._last_classification`` module global between here
        # and the terminal write.
        self._current_turn_classification = current_classification()
        # Sprint 35 — webui consumers expect the RoutingDecision in the
        # terminal result dict (``result["routing_decision"]``); store
        # it on the Dispatcher so ``run_conversation``'s tail reads it
        # via the back-reference instead of the deleted Agent field.
        self._current_turn_routing_decision = decision
        # Sprint 30.1 — classifier-driven pre-route escalation ledger
        # event. Moved here from ``_drive_generator`` since classification
        # now fires before the generator runs.
        pre_route = current_pre_route_decision()
        if pre_route is not None:
            try:
                ledger.record(
                    "escalation_decision",
                    source="pre_route",
                    granted=True,
                    current_tier=pre_route.get("current_tier"),
                    target_tier=pre_route.get("target_tier"),
                    complexity_signal=pre_route.get("complexity_signal"),
                    confidence=pre_route.get("confidence"),
                    reason=(
                        "classifier-driven pre-route — complexity_signal in "
                        "triggers and confidence below threshold"
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "Dispatcher pre_route ledger write failed: %s", exc,
                )
        # Bind the pre-built Agent shell to the routed tier. apply_tier
        # is the lightweight same-provider swap (model + max_tokens);
        # switch_model rebuilds the LLM client when the provider /
        # base_url / api_mode change. The selection rule mirrors the
        # pre-Sprint-35 ``_maybe_route_for_turn`` logic byte-for-byte.
        self._bind_agent_to_tier(agent, decision, resolve_tier_to_runtime)
        # Sprint 73 Phase 4a — thread this turn's tier budget carriers from the
        # one resolved tier (SINGLE SOURCE). decision.tier is always an
        # inference tier here (T0 is intercepted before classification), so a
        # missing budget fails loud rather than reverting to eager.
        self._apply_tier_budget(agent, decision.tier)

    # ── Sprint 49 — T0 Pattern Cache dispatch path ──────────────────────

    def _t0_store(self) -> Any:
        """Lazily-constructed, turn-reused PatternCacheStore handle."""
        if self._pattern_store is None:
            from grove.pattern_cache import PatternCacheStore
            self._pattern_store = PatternCacheStore()
        return self._pattern_store

    def _t0_intercept(
        self,
        agent: Any,
        user_message: str,
        previous_turn_id: Optional[str],
        kwargs: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Serve an active compiled pattern, or return None on a miss.

        The deterministic short-circuit (GATE-A D1/D2). On a hit: finalize
        the previous turn, compute the response (static cached_response, or
        an executable tool invocation fired model-free), record the hit,
        emit telemetry, write the intent record (attributed to the pattern's
        stored intent_class), persist the exchange to the transcript, and
        return the legacy result dict. On a miss: log ``t0_cache_miss`` and
        return None so ``dispatch_turn`` proceeds to normal classification.

        No classifier call. No agent reasoning. The reasoning generator is
        never driven for a hit — the LLM never boots."""
        import time as _time
        from grove.telemetry import (
            log_pattern_cache_event,
            log_routing_decision,
        )

        store = self._t0_store()
        pattern = store.get_active_for_message(user_message)
        if pattern is None:
            # Miss — log a normalized-text handle only (never the message) so
            # future pattern identification can mine misses (D5). intent_class
            # is unknown on a miss, so the key is seeded with an empty intent.
            try:
                from grove.pattern_cache import t0_key
                log_pattern_cache_event(
                    event_type="t0_cache_miss",
                    t0_key=t0_key("", user_message),
                )
            except Exception as exc:
                logger.debug("[grove.dispatcher] t0_cache_miss log failed: %r", exc)
            return None

        # ── Hit ──────────────────────────────────────────────────────────
        # Finalize the previous turn first. A T0 hit never classifies, so
        # ``_current_turn_classification`` is None → is_correction unread →
        # the previous turn finalizes as success (a cached answer is not a
        # correction; corrections miss the cache and take the normal path).
        if previous_turn_id is not None:
            self._finalize_previous_turn_pending(previous_turn_id)

        store.record_hit(pattern.pattern_id)

        if pattern.cacheable_type == "static":
            response_text = pattern.cached_response or ""
        else:
            response_text = self._execute_t0_invocation(agent, pattern)

        elapsed_ms = 0.0
        if self._current_turn_start is not None:
            elapsed_ms = (_time.monotonic() - self._current_turn_start) * 1000.0

        # Telemetry (D5): the rich pattern-cache event + a routing_decision
        # carrying the existing pattern_cache_hit flag so tier dashboards and
        # the routing feed both register the T0 resolution.
        log_pattern_cache_event(
            event_type="t0_cache_hit",
            pattern_id=pattern.pattern_id,
            t0_key=pattern.t0_key,
            intent_class=pattern.intent_class,
            cacheable_type=pattern.cacheable_type,
            response_time_ms=round(elapsed_ms, 2),
        )
        log_routing_decision(
            tier="T0",
            reason="pattern_cache hit — deterministic, no model call",
            model="pattern_cache",
            pattern_cache_hit=True,
            intent_class=pattern.intent_class,
        )

        # Intent record (A4): no classifier ran, so attribute the record to
        # the pattern's stored intent_class. Pending now; the NEXT turn's
        # finalize closes it (success, or correction → Phase 2 demotion).
        self._write_intent_record(
            agent,
            outcome="pending",
            final_response_chars=len(response_text),
            intent_class_override=pattern.intent_class,
            tier_override="T0",
        )

        # Mark this turn as a T0 hit so the next turn's correction check
        # (Phase 2) can find the served pattern.
        self._current_turn_t0_pattern_id = pattern.pattern_id

        # Persist the exchange so multi-turn continuity holds (the reasoning
        # generator — which normally persists — was skipped).
        self._persist_t0_turn(user_message, response_text)

        return self._t0_result_dict(agent, response_text)

    def _execute_t0_invocation(self, agent: Any, pattern: Any) -> str:
        """Fire an EXECUTABLE pattern's compiled tool invocation, model-free.

        Parses the stored ``{"tool", "args"}`` and dispatches through the
        agent's own ``_invoke_tool`` primitive — the same call the Dispatcher's
        ToolExecutor routes to via ``SideEffectCallbacks.invoke_tool``. No
        reasoning generator, no LLM. The agent object hosts the tool registry;
        its cognition never fires.

        Scoping note (Andon-loud, not silent): this path does NOT re-run the
        tool-zone classifier that the normal intent-yield flow applies. An
        executable pattern is only promoted from evidence where the tool
        already ran successfully under the operator's gates; re-gating each
        cached invocation is a follow-up hardening, flagged in the HANDOFF."""
        inv: Dict[str, Any] = {}
        try:
            inv = _json_mod.loads(pattern.compiled_invocation or "{}")
        except (ValueError, TypeError) as exc:
            # Fail loud: a promoted executable pattern with unparseable
            # invocation is a compile-time defect, not a runtime to swallow.
            logger.error(
                "[grove.dispatcher] T0 executable pattern %s has unparseable "
                "compiled_invocation %r: %r",
                pattern.pattern_id, pattern.compiled_invocation, exc,
            )
            raise
        tool_name = inv.get("tool")
        tool_args = inv.get("args") or {}
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(
                f"T0 executable pattern {pattern.pattern_id} names no tool: {inv!r}"
            )
        result = agent._invoke_tool(
            tool_name, tool_args, self._current_turn_id or "",
        )
        return result if isinstance(result, str) else str(result)

    def _persist_t0_turn(self, user_message: str, response_text: str) -> None:
        """Append the user + assistant messages for the served turn.

        Best-effort (``append_messages`` logs and continues on failure). The
        normal path persists inside the reasoning generator; a T0 hit skips
        the generator, so the Dispatcher writes the transcript itself to keep
        session history coherent for the next turn."""
        try:
            self.append_messages([
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text},
            ])
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] T0 transcript persist failed: %r", exc,
            )

    def _t0_result_dict(self, agent: Any, response_text: str) -> Dict[str, Any]:
        """Build the legacy turn-result dict for a T0 hit.

        Mirrors the shape ``_run_turn_generator`` returns via
        ``StopIteration.value`` (run_agent.py), zeroed for the no-inference
        path: no tokens, no cost, no API calls. ``final_response`` is the key
        every caller reads (``chat()`` returns ``result["final_response"]``)."""
        return {
            "final_response": response_text,
            "last_reasoning": None,
            "messages": [],
            "api_calls": 0,
            "completed": True,
            "turn_exit_reason": "t0_cache_hit",
            "partial": False,
            "interrupted": False,
            "response_previewed": False,
            "model": "pattern_cache",
            "provider": getattr(agent, "provider", None),
            "base_url": getattr(agent, "base_url", None),
            "tier": "T0",
            "pattern_cache_hit": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "cost_status": "t0_cache_hit",
            "cost_source": "pattern_cache",
        }

    def _maybe_demote_on_correction(self, pattern_id: Optional[str]) -> None:
        """Auto-suspend a T0 pattern the operator just corrected (Phase 2).

        Called after ``_finalize_previous_turn_pending`` when the previous
        turn resolved via T0. If this turn's classifier flagged a correction
        (``is_correction`` true), the served pattern was wrong often enough to
        warrant pulling it from the cache: suspend it (suspended patterns stop
        serving immediately — operator protection), log ``pattern_drift_detected``,
        and queue a demotion proposal so the operator confirms or reverses.

        No-op when ``pattern_id`` is None (previous turn was not T0) or the
        current turn is not a correction. Reuses the Sprint 38 correction
        signal — no parallel detection path."""
        if not pattern_id:
            return
        classification = self._current_turn_classification
        if not bool(getattr(classification, "is_correction", False)):
            return
        from grove.pattern_cache import STATUS_SUSPENDED
        from grove.telemetry import log_pattern_cache_event

        store = self._t0_store()
        pattern = store.get(pattern_id)
        if pattern is None or pattern.status != "active":
            # Already demoted / suspended / gone — nothing to pull.
            return
        store.set_status(pattern_id, STATUS_SUSPENDED)
        log_pattern_cache_event(
            event_type="pattern_drift_detected",
            pattern_id=pattern_id,
            intent_class=pattern.intent_class,
            correction_turn_id=self._current_turn_id,
        )
        logger.info(
            "[grove.dispatcher] T0 pattern %s auto-suspended — correction on "
            "turn %s (intent_class=%s). Demotion proposal queued for operator "
            "review.",
            pattern_id, self._current_turn_id, pattern.intent_class,
        )
        self._queue_pattern_demotion_proposal(pattern)

    def _queue_pattern_demotion_proposal(self, pattern: Any) -> None:
        """Queue a pattern_demotion proposal for operator confirm/reverse.

        Best-effort: the pattern is already suspended (operator-protected),
        so a queue write failure is logged loud but does not crash the turn."""
        try:
            from grove.eval.proposal_queue import (
                RoutingProposal,
                PROPOSAL_TYPE_PATTERN_DEMOTION,
                compute_proposal_id,
                append as _queue_append,
            )
            payload = {
                "pattern_id": pattern.pattern_id,
                "intent_class": pattern.intent_class,
                "cacheable_type": pattern.cacheable_type,
                "suggested_action": "demote",
                "trigger": "correction_drift",
                "correction_turn_id": self._current_turn_id or "",
            }
            evidence = (self._current_turn_id or "",)
            eval_hash = "sha256:" + hashlib.sha256(
                f"pattern_demotion:{pattern.pattern_id}".encode("utf-8")
            ).hexdigest()
            proposal = RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_PATTERN_DEMOTION,
                    payload=payload,
                    evidence=evidence,
                ),
                type=PROPOSAL_TYPE_PATTERN_DEMOTION,
                payload=payload,
                evidence=evidence,
                eval_hash=eval_hash,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            _queue_append(proposal)
        except Exception as exc:
            logger.warning(
                "[grove.dispatcher] failed to queue pattern_demotion proposal "
                "for %s: %r (pattern is already suspended)",
                getattr(pattern, "pattern_id", "?"), exc,
            )

    @staticmethod
    def _bind_agent_to_tier(
        agent: Any,
        decision: Any,
        resolve_tier_to_runtime: Callable[[Any], Dict[str, Any]],
    ) -> None:
        """Bind the pre-built Agent shell to the routed tier.

        Replaces the apply_tier / switch_model branching that lived
        inside ``AIAgent._maybe_route_for_turn``. Same selection rule:
        a same-provider routing change goes through the lightweight
        ``apply_tier``; a cross-provider change rebuilds the LLM client
        via ``switch_model``. An empty current provider is treated as
        same-provider — the pre-Sprint-35 safe-default for fixtures
        that replace ``self.client`` wholesale.
        """
        cur_provider = (getattr(agent, "provider", None) or "").strip().lower()
        dec_provider = (decision.tier_config.provider or "").strip().lower()
        same_provider = (not cur_provider) or (cur_provider == dec_provider)
        if same_provider:
            agent.apply_tier(
                decision.tier_config.model,
                decision.tier_config.max_tokens,
            )
            return
        runtime = resolve_tier_to_runtime(decision.tier_config)
        agent.switch_model(
            new_model=runtime["model"],
            new_provider=runtime["provider"] or "",
            api_key=runtime.get("api_key") or "",
            base_url=runtime.get("base_url") or "",
            api_mode=runtime.get("api_mode") or "",
        )
        if decision.tier_config.max_tokens is not None:
            agent.max_tokens = decision.tier_config.max_tokens

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

        Each message dict in ``messages[starting_index:]`` is splatted
        as kwargs into ``session.append_message`` after stamping
        ``session_id``. The Agent's ``_flush_messages_to_session_db``
        normalizes content (multimodal strip, tool_calls extraction)
        before calling this method; the Dispatcher does the write.

        Returns the new flush cursor (one past the last successfully
        written index). On a write failure the cursor is left at the
        last successful index so the next call retries the offender.
        """
        if self.session is None or not self.session_id:
            return starting_index
        if not self._session_row_created:
            # Pre-turn open_turn_row() may have failed (SQLite lock).
            # Drop the write silently; next turn retries via the
            # lifecycle hook.
            return starting_index
        flushed = starting_index
        for idx in range(starting_index, len(messages)):
            msg = messages[idx]
            try:
                self.session.append_message(session_id=self.session_id, **msg)
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
                model=intent.model,
                api_call_count=intent.api_call_count,
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

    # ── Sprint 40 — memory authority + pre-construction read ────────────

    @staticmethod
    def _collect_provider_init_kwargs(
        agent_kwargs: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Extract the gateway/user identity fields the memory manager's
        providers expect at ``initialize_all(...)``.

        Pre-Sprint-40 the Agent's ``__init__`` did this inline. Sprint 40
        relocates the construction; this helper preserves the same field
        set so providers (Honcho, etc.) see identical scoping kwargs.
        """
        out: Dict[str, Any] = {}
        if not agent_kwargs:
            return out
        for key in (
            "session_title",
            "user_id",
            "user_name",
            "chat_id",
            "chat_name",
            "chat_type",
            "thread_id",
            "gateway_session_key",
        ):
            v = agent_kwargs.get(key)
            if v:
                out[key] = v
        return out

    def open_memory(
        self,
        *,
        memory_config: Optional[Dict[str, Any]] = None,
        platform: Optional[str] = None,
        provider_init_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Build the memory store + manager — pre-Agent-construction.

        Sprint 40 owns the construction the Agent used to do inside its
        ``__init__``. Idempotent at the store level (already-built handle
        is preserved); the manager is rebuilt when ``provider_init_kwargs``
        changes (e.g. session id rotation), but that path is exercised
        by Sprint 39's session machinery, not here.

        ``memory_config`` defaults to ``self._base_runtime_ctx.config
        .get("memory", {})`` per the GATE-A disposition. ``platform`` is
        the Agent's platform string (used by some providers for scoping
        — Honcho, etc.). ``provider_init_kwargs`` carries the gateway/
        user/profile identity the manager forwards to each provider's
        ``initialize_all(...)``.

        After this returns:

        * ``self.memory_store`` is a ``MemoryStore`` (or stays ``None``
          when neither MEMORY.md nor USER.md is enabled in config).
        * ``self.memory_manager`` is a ``MemoryManager`` with its
          providers added and initialized (or stays ``None`` when
          config has no ``memory.provider`` set).
        * Sprint 35 can call ``hydrate_memory_context()`` to read the
          three system-prompt blocks the classifier needs.
        """
        if memory_config is None:
            cfg = self._base_runtime_ctx.config or {}
            memory_config = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
        memory_config = memory_config or {}
        self._memory_enabled = bool(memory_config.get("memory_enabled", False))
        self._user_profile_enabled = bool(memory_config.get("user_profile_enabled", False))

        # MemoryStore — built only when at least one of the two operator-
        # memory surfaces (MEMORY.md or USER.md) is enabled. Honors the
        # Sprint 26 Phase 1b cache on runtime_ctx.
        if self.memory_store is None and (self._memory_enabled or self._user_profile_enabled):
            cached = getattr(self._base_runtime_ctx, "memory_store", None)
            if cached is not None:
                self.memory_store = cached
            else:
                try:
                    from tools.memory_tool import MemoryStore
                    self.memory_store = MemoryStore(
                        memory_char_limit=memory_config.get("memory_char_limit", 2200),
                        user_char_limit=memory_config.get("user_char_limit", 1375),
                    )
                    self.memory_store.load_from_disk()
                except Exception as exc:
                    logger.debug(
                        "Dispatcher.open_memory: MemoryStore build failed: %s",
                        exc,
                    )
                    self.memory_store = None

        # MemoryManager — built only when an external provider is
        # configured. Same flow the Agent used pre-Sprint-40.
        if self.memory_manager is None:
            provider_name = memory_config.get("provider", "")
            if provider_name:
                try:
                    from agent.memory_manager import MemoryManager
                    from plugins.memory import load_memory_provider
                    manager = MemoryManager()
                    mp = load_memory_provider(provider_name)
                    if mp and mp.is_available():
                        manager.add_provider(mp)
                    if manager.providers:
                        init_kwargs = {
                            "session_id": self.session_id,
                            "platform": platform or "cli",
                            "agent_context": "primary",
                        }
                        try:
                            from hermes_state import get_hermes_home
                            init_kwargs["hermes_home"] = str(get_hermes_home())
                        except Exception:
                            pass
                        try:
                            from hermes_cli.profiles import get_active_profile_name
                            init_kwargs["agent_identity"] = get_active_profile_name()
                            init_kwargs["agent_workspace"] = "hermes"
                        except Exception:
                            pass
                        if provider_init_kwargs:
                            init_kwargs.update(provider_init_kwargs)
                        manager.initialize_all(**init_kwargs)
                        self.memory_manager = manager
                        logger.info(
                            "[grove.dispatcher] memory provider '%s' activated",
                            provider_name,
                        )
                except Exception as exc:
                    logger.warning(
                        "[grove.dispatcher] memory provider plugin init failed: %s",
                        exc,
                    )
                    self.memory_manager = None

    def hydrate_memory_context(self) -> Dict[str, str]:
        """Return the three memory system-prompt blocks — pre-Agent.

        Sprint 35 calls this before constructing the Agent so the
        classifier can see operator memory + user profile + external
        provider context. Missing blocks default to empty strings.
        """
        result = {"memory": "", "user": "", "external": ""}
        if self.memory_store is not None:
            try:
                if self._memory_enabled:
                    block = self.memory_store.format_for_system_prompt("memory")
                    if block:
                        result["memory"] = block
                if self._user_profile_enabled:
                    block = self.memory_store.format_for_system_prompt("user")
                    if block:
                        result["user"] = block
            except Exception as exc:
                logger.debug(
                    "Dispatcher.hydrate_memory_context: store read failed: %s",
                    exc,
                )
        if self.memory_manager is not None:
            try:
                ext = self.memory_manager.build_system_prompt()
                if ext:
                    result["external"] = ext
            except Exception as exc:
                logger.debug(
                    "Dispatcher.hydrate_memory_context: manager read failed: %s",
                    exc,
                )
        return result

    def _inject_memory_tool_schemas(self) -> None:
        """Inject memory-provider tool schemas into ``self.agent.tools``.

        Sprint 40 — relocated from ``AIAgent.__init__`` where the Agent
        used to do this against ``self._memory_manager``. With manager
        ownership on the Dispatcher, injection happens after Agent
        construction so the Agent's tools list (built from its
        toolsets) gets the memory-provider tools appended without
        duplicating names already registered via the plugin path.
        """
        if self.memory_manager is None:
            return
        agent_tools = getattr(self.agent, "tools", None)
        if agent_tools is None:
            return
        existing_names = {
            t.get("function", {}).get("name")
            for t in agent_tools
            if isinstance(t, dict)
        }
        valid_names = getattr(self.agent, "valid_tool_names", None)
        try:
            schemas = self.memory_manager.get_all_tool_schemas()
        except Exception:
            return
        for schema in schemas:
            name = schema.get("name", "")
            if name and name in existing_names:
                continue
            agent_tools.append({"type": "function", "function": schema})
            if name:
                existing_names.add(name)
                if isinstance(valid_names, set):
                    valid_names.add(name)

    # ── Sprint 36 — prompt composition (GRV-007) ────────────────────────

    def _get_or_build_prompt_composer(self) -> Any:
        """Lazily build the ``PromptComposer`` with config from
        ``runtime_ctx.config["prompt"]``. One composer per Dispatcher.

        Multiple ``compose()`` calls on the same composer are safe per
        GRV-007 § IX.3; the composer holds only registration state.
        """
        cached = getattr(self, "_prompt_composer", None)
        if cached is not None:
            return cached
        from grove.prompt import build_default_composer
        cfg = self._base_runtime_ctx.config or {}
        prompt_cfg = cfg.get("prompt") if isinstance(cfg, dict) else None
        composer = build_default_composer(config=prompt_cfg)
        self._prompt_composer = composer
        return composer

    def compose_system_prompt(
        self,
        agent: Any,
        *,
        system_message: Optional[str] = None,
    ) -> str:
        """Compose the system prompt for ``agent`` and return the joined text.

        Pulls turn-and-Agent state out of ``agent``'s public attributes
        and feeds them into the composer's ``compose(**context)`` call.
        The composer's providers never touch the Agent directly — all
        state flows through the context dict per GRV-007 § III.

        Callers that recompose mid-session (compression rotation,
        session_register change) re-invoke this method via the Agent's
        back-reference and store the new string back on the Agent.
        """
        composer = self._get_or_build_prompt_composer()
        memory_store = self.memory_store
        memory_manager = self.memory_manager
        try:
            terminal_cwd = self._base_runtime_ctx.env.get("TERMINAL_CWD") or None
        except Exception:
            terminal_cwd = None
        classification = getattr(self, "_current_turn_classification", None)
        pattern_hash = getattr(classification, "pattern_hash", None) if classification else None
        intent_class = getattr(classification, "intent_class", None) if classification else None
        result = composer.compose(
            valid_tool_names=getattr(agent, "valid_tool_names", set()) or set(),
            # Sprint 53 — composer providers that need toolset
            # membership (e.g. _skills_index_provider) read the
            # Dispatcher-owned registry through this ctx field.
            registry=self.registry,
            model=getattr(agent, "model", "") or "",
            provider=getattr(agent, "provider", "") or "",
            platform=getattr(agent, "platform", "") or "",
            session_id=self.session_id or getattr(agent, "session_id", None),
            skip_context_files=bool(getattr(agent, "skip_context_files", False)),
            load_soul_identity=bool(getattr(agent, "load_soul_identity", False)),
            memory_enabled=bool(getattr(agent, "_memory_enabled", False)),
            user_profile_enabled=bool(getattr(agent, "_user_profile_enabled", False)),
            pass_session_id=bool(getattr(agent, "pass_session_id", False)),
            system_message=system_message,
            session_register=getattr(agent, "session_register", None),
            tool_use_enforcement=getattr(agent, "_tool_use_enforcement", None),
            memory_store=memory_store,
            memory_manager=memory_manager,
            terminal_cwd=terminal_cwd,
            pattern_hash=pattern_hash,
            intent_class=intent_class,
            # Sprint 73 (D5) — per-tier context allow-list. None on a
            # non-routed / construction-time compose (no gating); populated by
            # Phase 4a's _apply_tier_budget on every routed inference tier, so
            # any recompose (tier change, compression, session_register change)
            # applies the current tier's context gate.
            tier_context_blocks=getattr(agent, "_tier_context_blocks", None),
        )
        # Sprint 73 Phase 5 — retain the structured composition RESULT as data
        # on the agent (NOT a recomposing method — GRV-007 deleted that). The
        # /context report reads agent._composed_prompt to attribute the ACTUAL
        # injected prompt (sections + gated_context_blocks); it is set from the
        # SAME result whose .text becomes _composed_system_prompt, so the two
        # can never diverge.
        agent._composed_prompt = result
        return result.text

    def _compose_and_inject_system_prompt(
        self,
        agent: Any,
        *,
        system_message: Optional[str] = None,
    ) -> None:
        """Compose and store the prompt on ``agent._composed_system_prompt``.

        Called from ``__init__`` after agent construction and from
        ``recompose_system_prompt`` for mid-session rebuilds.
        """
        agent._composed_system_prompt = self.compose_system_prompt(
            agent, system_message=system_message,
        )

    def recompose_system_prompt(
        self,
        *,
        system_message: Optional[str] = None,
    ) -> str:
        """Recompose the prompt for ``self.agent`` and update its field.

        Mid-session rebuild path — invoked by the Agent via the back-
        reference when a compression boundary fires or the operator
        changes ``session_register``. Returns the new composed text.
        """
        if self.agent is None:
            return ""
        self._compose_and_inject_system_prompt(
            self.agent, system_message=system_message,
        )
        return self.agent._composed_system_prompt

    # ── Sprint 73 Phase 4a — tier-budget carriers (context side) ─────────

    def _get_tier_budgets(self) -> Dict[str, Any]:
        """Lazily load + cache the validated ``tier_budgets`` map.

        Loaded on the first routed turn (not at construction), so test /
        gateway Dispatchers that never route are unaffected. ``load_tier_budgets``
        fails loud at load (D7) when the active ``routing.config.yaml`` lacks the
        block or an inference tier's entry — the operator syncs the template
        block to ``~/.grove/routing.config.yaml`` before live turns.
        """
        cached = self._tier_budgets_cache
        if cached is not None:
            return cached
        from grove.tier_budget import load_tier_budgets
        budgets = load_tier_budgets()
        self._tier_budgets_cache = budgets
        return budgets

    def _apply_tier_budget(self, agent: Any, tier: Optional[str]) -> None:
        """Resolve THIS turn's tier budget once and thread both carriers from
        it (Phase 4a — context side; the tools-side read lands in 4b).

        SINGLE SOURCE: ``agent._tier_budget`` and ``agent._tier_context_blocks``
        both derive from one resolved ``TierBudget``, so context and tools can
        never disagree on the tier. FAIL LOUD (invariant 3): a routed tier with
        no resolvable budget raises ``TierBudgetMissing`` — never a silent eager
        fallthrough. A falsy ``tier`` (no routed tier — a legacy / non-router
        path) is a no-op; the carriers stay ``None`` and the enforcers behave as
        pre-Sprint-73. That is the legacy path, NOT an inference tier.
        """
        if not tier:
            return
        budgets = self._get_tier_budgets()
        budget = budgets.get(tier)
        if budget is None:
            from grove.tier_budget import TierBudgetMissing
            raise TierBudgetMissing(
                f"routed tier {tier!r} has no tier_budgets entry; the prefill "
                f"budget cannot be resolved. Add a tier_budgets[{tier!r}] block "
                f"to routing.config.yaml (D7 — no silent full-load)."
            )
        agent._tier_budget = budget
        agent._tier_context_blocks = frozenset(budget.context)
        self._maybe_recompose_for_tier(agent)

    def _maybe_recompose_for_tier(self, agent: Any) -> None:
        """Recompose the system prompt when this turn's context-block set
        differs from the last applied (cache-friendly).

        ADDITIVE: this is ONE recompose trigger among several. Sprint 36's
        compression-boundary and ``session_register``-change recomposes call
        ``recompose_system_prompt`` independently; this method never gates them
        off — it only short-circuits ITS OWN trigger. A ``session_register``
        change with an unchanged tier therefore still recomposes via Sprint 36's
        path (``compose_system_prompt`` re-reads the carrier on every recompose).
        """
        if agent is not self.agent:
            # The escalation hot-swap's new agent is not yet ``self.agent``;
            # compose it directly so its prompt reflects the escalated tier's
            # carrier before its generator starts.
            self._compose_and_inject_system_prompt(agent)
            self._last_applied_tier_context_blocks = getattr(
                agent, "_tier_context_blocks", None,
            )
            return
        current = getattr(agent, "_tier_context_blocks", None)
        if current == self._last_applied_tier_context_blocks:
            return
        self._last_applied_tier_context_blocks = current
        self.recompose_system_prompt()

    def execute_memory_write(self, intent: "MemoryWriteIntent") -> "MemoryWriteResult":
        """Handle a ``MemoryWriteIntent`` — synchronous return.

        Routes by ``intent.kind``:

        * ``"builtin_memory"`` — executes the built-in ``memory`` tool
          against ``self.memory_store`` (``add`` or ``replace``), then
          fires the bridge notification to
          ``self.memory_manager.on_memory_write(...)`` so external
          providers stay in sync. Sprint 40 owns the bridge — Phase 2
          deletes the Agent-side call.
        * ``"provider_tool"`` — delegates to
          ``self.memory_manager.handle_tool_call(...)``.

        Returns a ``MemoryWriteResult`` the Dispatcher injects back into
        the generator via ``.send()`` — the Agent treats ``value`` as the
        tool's LLM-visible result string.
        """
        from grove.intents import MemoryWriteResult
        if intent.kind == "builtin_memory":
            if self.memory_store is None:
                return MemoryWriteResult(
                    success=False, value="",
                    error="memory store not available",
                )
            try:
                from tools.memory_tool import memory_tool as _memory_tool
                value = _memory_tool(
                    action=intent.action,
                    target=intent.target or "memory",
                    content=intent.content,
                    old_text=intent.old_text,
                    store=self.memory_store,
                )
            except Exception as exc:
                return MemoryWriteResult(
                    success=False, value="", error=str(exc),
                )
            # Bridge: notify external memory provider of built-in writes.
            if (
                self.memory_manager is not None
                and intent.action in {"add", "replace"}
            ):
                try:
                    self.memory_manager.on_memory_write(
                        intent.action or "",
                        intent.target or "memory",
                        intent.content or "",
                        metadata=dict(intent.metadata),
                    )
                except Exception:
                    pass
            return MemoryWriteResult(success=True, value=str(value))
        if intent.kind == "provider_tool":
            if self.memory_manager is None:
                return MemoryWriteResult(
                    success=False, value="",
                    error="memory manager not available",
                )
            try:
                value = self.memory_manager.handle_tool_call(
                    intent.tool_name or "",
                    dict(intent.arguments),
                )
            except Exception as exc:
                return MemoryWriteResult(
                    success=False, value="", error=str(exc),
                )
            return MemoryWriteResult(success=True, value=str(value))
        return MemoryWriteResult(
            success=False, value="",
            error=f"unknown MemoryWriteIntent.kind: {intent.kind!r}",
        )

    def execute_memory_lifecycle(self, intent: "MemoryLifecycleIntent") -> None:
        """Handle a ``MemoryLifecycleIntent`` — fire-and-forget.

        Routes by ``intent.event`` to the corresponding memory-manager
        lifecycle method. All event handlers are wrapped in try/except
        because lifecycle failures must not break the conversational
        flow (matches the Agent's pre-Sprint-40 behavior).
        """
        if self.memory_manager is None:
            return
        event = intent.event
        try:
            if event == "on_session_end":
                self.memory_manager.on_session_end(intent.messages or [])
            elif event == "on_session_switch":
                self.memory_manager.on_session_switch(
                    self.session_id or "",
                    parent_session_id=intent.parent_session_id,
                    reset=False,
                    reason=intent.reason or "session_switch",
                )
            elif event == "on_pre_compress":
                self.memory_manager.on_pre_compress(intent.messages or [])
            elif event == "sync_turn":
                # MemoryManager.sync_all(user_content, assistant_content, *, session_id)
                # MemoryManager.queue_prefetch_all(query, *, session_id)
                # Skip on interrupted turns — matches the Agent's pre-Sprint-40
                # behavior (partial output is not durable conversational truth).
                if intent.interrupted:
                    return
                sid = self.session_id or ""
                self.memory_manager.sync_all(
                    intent.original_user_message or "",
                    intent.final_response or "",
                    session_id=sid,
                )
                self.memory_manager.queue_prefetch_all(
                    intent.original_user_message or "",
                    session_id=sid,
                )
            elif event == "shutdown":
                self.memory_manager.shutdown_all()
            else:
                logger.debug(
                    "Dispatcher.execute_memory_lifecycle: unknown event %r",
                    event,
                )
        except Exception as exc:
            logger.debug(
                "Dispatcher.execute_memory_lifecycle(%s) failed: %s",
                event, exc,
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

        # Sprint 63 — acceptance flow: a freshly-accepted synthesized skill is
        # not on disk yet, so an invoke_skill targeting it would classify Green
        # and skip governance. Materialize any pending synthesized skill named
        # by an invoke_skill intent into the quarantine BEFORE classifying, so
        # the .andon Yellow gate fires. A synthesized skill never auto-executes.
        self._maybe_materialize_synthesized_skills(intents)

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
        # Sprint 62 — procedural skill "try it" gate. Loading a quarantined
        # (.andon) skill via skill_view is the operator's try-before-promote
        # moment, so classify it yellow → the existing Andon halt + S1/S4
        # prompt fire. Script skills already route through the terminal-.andon
        # rule; this is the no-script (procedural) equivalent. In-code (not a
        # zones rule) because a bare skill name can't be path-matched.
        if tool_name == "skill_view" and isinstance(args, dict):
            view_name = args.get("name")
            if isinstance(view_name, str) and view_name.strip():
                from grove.skills import proposal_path
                if proposal_path(view_name.strip()).exists():
                    from grove.zones import ZoneResult
                    return ZoneResult(
                        zone="yellow",
                        matched_rule=(
                            "skill.quarantine.andon "
                            f"(.grove/skills/.andon/{view_name.strip()})"
                        ),
                        source="skill_view_quarantine",
                    )
        # Sprint 63 — invoke_skill is the dedicated skill-execution intent.
        # An invoke_skill targeting a quarantined (.andon) skill gets the same
        # Yellow gate as skill_view: the Sovereign Prompt fires before the
        # handler loads the procedure, and PostExecutionKaizenYield fires after
        # FinalResponse. Promoted skills are not under .andon, so they fall
        # through to the generic path and classify Green. A freshly-accepted
        # synthesized skill is materialized into .andon by
        # ``_maybe_materialize_synthesized_skills`` BEFORE this runs, so
        # ``proposal_path().exists()`` is authoritative here.
        if tool_name == "invoke_skill" and isinstance(args, dict):
            inv_name = args.get("name")
            if isinstance(inv_name, str) and inv_name.strip():
                from grove.skills import proposal_path
                if proposal_path(inv_name.strip()).exists():
                    from grove.zones import ZoneResult
                    return ZoneResult(
                        zone="yellow",
                        matched_rule=(
                            "skill.quarantine.andon "
                            f"(.grove/skills/.andon/{inv_name.strip()})"
                        ),
                        source="invoke_skill_quarantine",
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

    def _kaizen_cache_key(
        self, tool_name: str, arguments: Any,
    ) -> Tuple[str, str]:
        """Compute the session-cache key for a halted intent.

        Sprint 32 — keyed by ``(tool_name, sha256(canonical JSON of
        arguments))``. Canonical JSON: ``json.dumps(args, sort_keys=
        True, default=str)``. Non-JSON-serializable values stringify
        safely so the hash never crashes on an unusual argument type.
        """
        import hashlib
        try:
            payload = _json_mod.dumps(
                arguments or {}, sort_keys=True, default=str,
            )
        except Exception:
            payload = str(arguments)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return (tool_name, digest)

    def _handle_andon_halt(
        self, agent: Any, halt: "AndonHalt", ledger: Optional[Any] = None,
    ) -> str:
        """Write the pending marker, check caches, prompt, clear marker.

        Returns one of the GRV-005 § VI v1.1 disposition strings:
        ``"once"``, ``"session"``, ``"always"``, or ``"deny"``. The
        Dispatcher itself may also set ``"deny_hard"`` internally
        when the red-zone strike counter overflows (the handler is
        bypassed on that path).

        Flow:

        1. Shadow mode short-circuit: ``GROVE_ZONE_SHADOW=1`` returns
           ``"once"`` without writing the marker or prompting (the
           Green-path executor runs the tool; the would-have-been halt
           remains in the ledger for calibration review).
        2. Red-zone strike check (Phase 3a) — increments the per-turn
           per-tool counter; at threshold returns ``"deny_hard"``
           silently.
        3. Cache check — keyed by ``(tool_name, sha256(arguments))``:
           * Deny cache hit → log telemetry, return ``"deny"`` silently.
           * Allow cache hit → log telemetry, return ``"once"`` silently.
        4. Write the pending_andon marker (recoverable trail).
        5. Invoke the operator handler.
        6. Mutate caches by disposition:
           * ``"deny"`` → add to deny cache.
           * ``"session"`` / ``"always"`` → add to allow cache.
           * ``"once"`` → no cache mutation.
        7. ``"always"`` applies a zone rule immediately (Sprint 67):
           operator-initiated "always" is self-approving, so the rule
           is written to zones.schema.yaml rather than queued.
        8. Clear the pending_andon marker in ``finally``.

        Per D3 lock: pending_andon is a structural persistent marker —
        not a serialization of the generator state (which contains
        unpicklable references like LLM clients and thread locks). On
        process restart, ``check_pending_andon()`` surfaces the marker
        so the operator can acknowledge the lost turn.
        """
        if os.environ.get("GROVE_ZONE_SHADOW") == "1":
            triggering = halt.intents[halt.triggering_index].tool_name
            print(
                f"[shadow] would halt: {triggering} "
                f"({halt.zone}, {halt.matched_rule})",
                file=_sys.stderr,
            )
            return "once"

        triggering_intent = halt.intents[halt.triggering_index]
        cache_key = self._kaizen_cache_key(
            triggering_intent.tool_name, triggering_intent.arguments,
        )

        # ── Sprint 32 Phase 3a — red-zone strike counter ─────────────
        # Red halts count strikes per-tool per-turn. At threshold the
        # Dispatcher forces a hard-denial Observation whose text
        # explicitly directs the LLM not to attempt the same tool
        # with the same arguments again — making the denial
        # structurally terminal for that specific vector within the
        # turn (Trap-B mitigation locked at GATE-A clarification).
        # The hard-denial Observation is wired into the
        # ``_build_skip_observations`` path with a sentinel disposition
        # ``"deny_hard"`` so the LLM-visible denial text differs from
        # the soft-deny case.
        if halt.zone == "red":
            tool_name = triggering_intent.tool_name
            strikes_now = self._current_turn_andon_strikes.get(tool_name, 0) + 1
            self._current_turn_andon_strikes[tool_name] = strikes_now
            if strikes_now >= _RED_ZONE_STRIKE_LIMIT:
                logger.warning(
                    "[grove.dispatcher] Red-zone strike limit reached "
                    "for tool=%s this turn (strikes=%d, limit=%d) — "
                    "forcing hard denial; handler bypassed.",
                    tool_name, strikes_now, _RED_ZONE_STRIKE_LIMIT,
                )
                if ledger is not None:
                    try:
                        ledger.record(
                            "andon_hard_denial",
                            tool=tool_name,
                            strikes=strikes_now,
                            limit=_RED_ZONE_STRIKE_LIMIT,
                            zone="red",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[grove.dispatcher] andon_hard_denial ledger "
                            "write failed (non-fatal): %r", exc,
                        )
                return "deny_hard"

        # Cache check — silent auto-apply on hit.
        if cache_key in self._session_deny_cache:
            self._log_session_cache_hit(
                triggering_intent.tool_name, "deny", ledger=ledger,
            )
            return "deny"
        if cache_key in self._session_allow_cache:
            self._log_session_cache_hit(
                triggering_intent.tool_name, "allow", ledger=ledger,
            )
            return "once"

        marker_path = self._write_pending_andon(agent, halt)
        try:
            disposition = self._sovereign_prompt_handler(halt)
        finally:
            self._clear_pending_andon(agent, marker_path)

        # Cache mutation by disposition.
        if disposition == "deny":
            self._session_deny_cache.add(cache_key)
        elif disposition in ("session", "always"):
            self._session_allow_cache.add(cache_key)
        # "once" — no cache mutation.

        # Sprint 67 (kaizen-governance-parity-v1) — operator-initiated
        # "always" APPLIES the zone rule immediately rather than queuing
        # a proposal. Reaching this branch means an operator tapped or
        # typed "always" on a live Andon prompt (CLI [a] or Telegram
        # kz:always) — the tap IS the approval, so there is no second
        # gate. This supersedes the Sprint 32 A4 lock that queued from
        # non-TTY surfaces: a mobile operator cannot reach `flywheel
        # approve`, so queuing stranded the decision (the bug this
        # fixes). System-initiated promotions (Ratchet / observed
        # patterns) are written to the queue by other code paths and are
        # untouched here. Failures degrade with a loud warning — the
        # session_allow cache mutation above already gave this turn's
        # action its relief.
        if disposition == "always":
            self._apply_zone_promotion(triggering_intent)

        # Sprint 53.2 — if an "allow once" disposition just let a
        # quarantined (.andon) skill run, flag it so the post-execution
        # promotion prompt fires after FinalResponse. Reached only on the
        # active-disposition path (cache hits early-return above), so
        # silently-cached sessions do not nag the operator each turn.
        self._maybe_flag_quarantine_execution(
            triggering_intent, halt, disposition, cache_key, ledger,
        )

        return disposition

    def _apply_zone_promotion(self, intent: Any) -> None:
        """Apply an operator-initiated "always" promotion immediately.

        Sprint 67 (kaizen-governance-parity-v1). Mirrors the apply step
        that ``autonomaton flywheel approve`` performs
        (``grove.flywheel_cli._approve_zone_promotion`` →
        ``grove.zone_rules.save_zone_rule``) so an operator who taps
        "Always" on a gateway surface — where ``flywheel approve`` is out
        of reach — gets their decision honored without a second gate.
        The pattern/reason are derived through
        ``build_zone_promotion_proposal`` so the rule written here is
        byte-identical to the one the queue+approve path would have
        produced.

        Best-effort: a save failure logs a warning and returns. The
        session_allow cache mutation in the caller already gave this
        turn's action its relief; persistence failing is observable but
        must not block the line.
        """
        try:
            from grove.kaizen_promotion import build_zone_promotion_proposal
            from grove.zone_rules import save_zone_rule

            arguments = intent.arguments or {}
            # For terminal halts the operator-faced command string
            # lives under the ``command`` key by convention; fall
            # back to the stringified arguments dict otherwise so
            # the regex generator still produces a usable pattern.
            command_string = (
                arguments.get("command")
                if isinstance(arguments, dict) and "command" in arguments
                else str(arguments)
            )
            evidence_turn_id = self._current_turn_id or ""
            _proposal, payload = build_zone_promotion_proposal(
                tool_name=intent.tool_name,
                command_string=command_string or "",
                evidence_turn_id=evidence_turn_id,
            )
            save_zone_rule(
                tool_id=payload["tool"],
                pattern=payload["pattern"],
                zone=payload.get("zone", "green"),
                reason=payload.get("reason", ""),
            )
            logger.info(
                "[grove.dispatcher] operator 'always' applied immediately: "
                "tool=%s pattern=%r zone=%s",
                payload["tool"],
                payload["pattern"],
                payload.get("zone", "green"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] operator 'always' promotion apply "
                "failed (non-fatal; session_allow cache still applies this "
                "turn): %r", exc,
            )

    # ── Sprint 63 — synthesized-skill acceptance materialization ─────────

    def _maybe_materialize_synthesized_skills(self, intents: List[Any]) -> None:
        """Drop accepted synthesized skills into the quarantine before classify.

        Sprint 63 §3 acceptance flow: when the operator accepts a drafted-skill
        proposal, the model calls ``invoke_skill(name)`` but the SKILL.md only
        exists as a staged ``skill_synthesis`` proposal in ``proposals.jsonl``,
        not on disk. Left alone, the Dispatcher would classify that call Green
        (no ``.andon`` dir) and skip the Yellow gate. This writes the staged
        SKILL.md into ``~/.grove/skills/.andon/<name>/`` so the existing
        quarantine gate fires — the operator approves the run, then the
        promotion prompt. Idempotent: a skill already on disk (active or
        quarantined), or no matching pending proposal, is a no-op.

        On failure the skill is simply NOT placed on disk; the ``invoke_skill``
        handler then returns a loud "not found" rather than running anything
        ungoverned. There is no silent Green-path execution of a synthesized
        skill — the failure surfaces as a failed invocation.
        """
        for intent in intents or ():
            if getattr(intent, "tool_name", None) != "invoke_skill":
                continue
            args = getattr(intent, "arguments", None)
            if not isinstance(args, dict):
                continue
            name = args.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            skill_name = name.strip()
            try:
                from grove.skills import (
                    active_path,
                    proposal_path,
                    write_proposal,
                )
                if (
                    active_path(skill_name).exists()
                    or proposal_path(skill_name).exists()
                ):
                    continue
                from grove.eval.proposal_queue import (
                    PROPOSAL_TYPE_SKILL_SYNTHESIS,
                    read_all,
                )
                pending = [
                    p for p in read_all()
                    if p.type == PROPOSAL_TYPE_SKILL_SYNTHESIS
                    and (p.payload or {}).get("skill_name") == skill_name
                ]
                if not pending:
                    continue
                skill_md = (pending[-1].payload or {}).get("skill_md")
                if not isinstance(skill_md, str) or not skill_md.strip():
                    continue
                write_proposal(skill_name, skill_md)
                logger.info(
                    "[grove.dispatcher] Materialized accepted synthesized "
                    "skill %r into quarantine for governed invoke_skill.",
                    skill_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[grove.dispatcher] synthesized-skill materialization "
                    "failed for %r (non-fatal; invoke_skill will report "
                    "not-found): %r", skill_name, exc,
                )

    # ── Sprint 53.2 — post-execution skill promotion ────────────────────

    def _maybe_flag_quarantine_execution(
        self,
        intent: Any,
        halt: "AndonHalt",
        disposition: str,
        cache_key: Any,
        ledger: Optional[Any],
    ) -> None:
        """Flag a successful "allow once" execution of a quarantined skill.

        Sets the turn-scoped ``_quarantine_skill_executed_this_turn`` carrier
        when ALL hold: disposition is ``"once"``, the matched zone rule is the
        ``.andon`` quarantine rule, and the triggering tool is either the
        terminal running a ``~/.grove/skills/.andon/<name>/`` script OR (Sprint
        62) skill_view loading a quarantined procedural skill by ``name``.
        Also records the additive ``quarantine_skill_disposition`` ledger
        event (GATE-A decision 2) that ``--strict`` promotion reads — kept
        separate from Sprint 32's ``andon_disposition`` so that schema is
        untouched.
        """
        if disposition != "once":
            return
        if ".andon" not in (getattr(halt, "matched_rule", "") or ""):
            return
        tool_name = getattr(intent, "tool_name", None)
        arguments = getattr(intent, "arguments", None)
        arguments = arguments if isinstance(arguments, dict) else {}
        if tool_name == "terminal":
            command = arguments.get("command")
            if not command:
                return
            match = _ANDON_SKILL_RE.search(command)
            if match is None:
                return
            skill_name = match.group("name")
            skill_path = match.group("path")
        elif tool_name == "skill_view":
            # Sprint 62 — procedural skills have no script; the operator's
            # try-it gate is loading the quarantined skill via skill_view.
            # ``name`` carries the skill; the path is its quarantine dir.
            name = arguments.get("name")
            if not isinstance(name, str) or not name.strip():
                return
            from grove.skills import proposal_path
            skill_name = name.strip()
            skill_path = str(proposal_path(skill_name))
        elif tool_name == "invoke_skill":
            # Sprint 63 — invoke_skill is the governed execution entrypoint.
            # Same quarantine resolution as skill_view: ``name`` → its .andon
            # dir. This is what makes PostExecutionKaizenYield a mechanical
            # guarantee for invoked quarantined skills, not a model habit.
            name = arguments.get("name")
            if not isinstance(name, str) or not name.strip():
                return
            from grove.skills import proposal_path
            skill_name = name.strip()
            skill_path = str(proposal_path(skill_name))
        else:
            return
        self._quarantine_skill_executed_this_turn = {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "execution_turn_id": self._current_turn_id or "",
            "cache_key": cache_key,
        }
        if ledger is not None:
            try:
                ledger.record(
                    "quarantine_skill_disposition",
                    skill_name=skill_name,
                    skill_path=skill_path,
                    disposition="once",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[grove.dispatcher] quarantine_skill_disposition ledger "
                    "write failed (non-fatal): %r", exc,
                )

    def _emit_post_execution_kaizen(
        self, flag: Dict[str, Any], *, ledger: Optional[Any],
    ) -> None:
        """Surface the Promote / Not yet / Never prompt after a skill ran.

        TTY callers inject ``post_execution_prompt_handler``; headless
        surfaces (handler is None) auto-log a pending ``skill_promotion``
        proposal so the operator can approve later — never silently
        discarded (locked decision 4). A handler that raises degrades to
        the same auto-log path.
        """
        from grove.intents import PostExecutionKaizenYield

        payload = PostExecutionKaizenYield(
            skill_name=flag["skill_name"],
            skill_path=flag["skill_path"],
            exit_status="success",
            execution_turn_id=flag.get("execution_turn_id") or "",
            suggested_action="promote",
        )

        handler = self._post_execution_prompt_handler
        if handler is None:
            self._autolog_pending_promotion(payload, ledger, reason="non_tty")
            return
        try:
            choice = handler(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] post-execution prompt handler failed "
                "(%r); auto-logging pending promotion instead.", exc,
            )
            self._autolog_pending_promotion(
                payload, ledger, reason="handler_error",
            )
            return

        choice = (choice or "not_yet").strip().lower()
        if ledger is not None:
            try:
                ledger.record(
                    "post_execution_kaizen",
                    skill_name=payload.skill_name,
                    choice=choice,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[grove.dispatcher] post_execution_kaizen ledger write "
                    "failed (non-fatal): %r", exc,
                )

        if choice == "promote":
            self._promote_quarantined_skill(payload, ledger)
        elif choice in ("never", "never_purge"):
            self._deny_quarantined_skill(
                flag, payload, purge=(choice == "never_purge"), ledger=ledger,
            )
        # "not_yet" / unknown → no-op: the skill stays quarantined and the
        # four-choice Kaizen re-prompts on its next execution.

    def _autolog_pending_promotion(
        self, payload: Any, ledger: Optional[Any], *, reason: str,
    ) -> None:
        """Append a pending ``skill_promotion`` proposal to the Flywheel queue."""
        try:
            from grove.eval.proposal_queue import (
                RoutingProposal,
                PROPOSAL_TYPE_SKILL_PROMOTION,
                compute_proposal_id,
                append as _queue_append,
            )

            payload_dict = {
                "skill_name": payload.skill_name,
                "skill_path": payload.skill_path,
                "execution_turn_id": payload.execution_turn_id,
                "suggested_action": "promote",
            }
            evidence = (payload.execution_turn_id,) if payload.execution_turn_id else ()
            proposal = RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_SKILL_PROMOTION,
                    payload=payload_dict,
                    evidence=evidence,
                ),
                type=PROPOSAL_TYPE_SKILL_PROMOTION,
                payload=payload_dict,
                evidence=evidence,
                eval_hash=_synth_skill_eval_hash(
                    payload.skill_name, payload.skill_path,
                ),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            appended = _queue_append(proposal)
            short_id = proposal.proposal_id.split(":")[-1][:12]
            if appended:
                logger.info(
                    "[grove.dispatcher] Skill %r queued for promotion "
                    "(reason=%s). Run: autonomaton flywheel approve %s",
                    payload.skill_name, reason, short_id,
                )
            else:
                logger.info(
                    "[grove.dispatcher] Skill %r promotion already queued — "
                    "idempotent skip (reason=%s)", payload.skill_name, reason,
                )
            if ledger is not None:
                try:
                    ledger.record(
                        "skill_promotion_queued",
                        skill_name=payload.skill_name,
                        reason=reason,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[grove.dispatcher] skill_promotion_queued ledger "
                        "write failed (non-fatal): %r", exc,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] pending skill_promotion queue write "
                "failed (non-fatal): %r", exc,
            )

    def _skill_promotion_is_strict(self) -> bool:
        """True when ``skills.skill_promotion: strict`` in config (default normal)."""
        mode = self.runtime_ctx.config_get(
            "skills", "skill_promotion", default="normal",
        )
        return str(mode).strip().lower() == "strict"

    def _promote_quarantined_skill(self, payload: Any, ledger: Optional[Any]) -> None:
        """Promote a quarantined skill to the trusted set (Sprint 53.2 Phase 3).

        Normal mode: move ``.andon/<name>`` → active via
        ``grove.sovereignty.promote`` (Phase 3a — NOT re-implemented),
        write a green zone rule for the promoted path (3b/3c), and
        invalidate the skills prompt cache (3e) — all within this turn.
        Strict mode: queue a pending ``skill_promotion`` proposal only
        (3d); the operator applies it later via ``flywheel approve
        --strict``.
        """
        if self._skill_promotion_is_strict():
            self._autolog_pending_promotion(payload, ledger, reason="strict_mode")
            logger.info(
                "[grove.dispatcher] Skill %r queued for promotion (strict "
                "mode). Run: autonomaton flywheel approve --strict <id>",
                payload.skill_name,
            )
            return

        try:
            from grove.sovereignty import promote as _promote
            _promote(payload.skill_name)
        except FileNotFoundError as exc:
            logger.warning(
                "[grove.dispatcher] Promote: no quarantined skill %r to "
                "promote (%r).", payload.skill_name, exc,
            )
            return
        except FileExistsError as exc:
            logger.warning(
                "[grove.dispatcher] Promote: an active skill %r already "
                "exists; not overwriting (%r). Resolve manually with "
                "`hermes andon promote --replace`.", payload.skill_name, exc,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] Promote of %r failed (non-fatal): %r",
                payload.skill_name, exc,
            )
            return

        self._write_promoted_skill_zone_rule(payload.skill_name)
        self._invalidate_skills_cache()
        logger.info(
            "[grove.dispatcher] Skill %r promoted from quarantine and "
            "greenlit.", payload.skill_name,
        )
        if ledger is not None:
            try:
                ledger.record(
                    "skill_promoted", skill_name=payload.skill_name, mode="normal",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[grove.dispatcher] skill_promoted ledger write failed "
                    "(non-fatal): %r", exc,
                )

    def _write_promoted_skill_zone_rule(self, skill_name: str) -> None:
        """Auto-approve a green zone rule for the promoted skill path (3b/3c).

        Pattern ``.*\\.grove/skills/<name>/.* → green``. Redundant with the
        broad promoted-skills green rule, but makes each promotion an
        explicit, auditable zone act that survives if the broad rule is
        ever narrowed. ``save_zone_rule`` reloads zones synchronously so
        the rule is live within this turn.
        """
        try:
            from grove.zone_rules import save_zone_rule
            pattern = r".*\.grove/skills/" + re.escape(skill_name) + r"/.*"
            save_zone_rule(
                tool_id="terminal",
                pattern=pattern,
                zone="green",
                reason=(
                    f"Skill '{skill_name}' promoted from quarantine "
                    f"(Sprint 53.2)."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] Promoted-skill zone rule write for %r "
                "failed (non-fatal; broad skills green rule still applies): "
                "%r", skill_name, exc,
            )

    def _invalidate_skills_cache(self) -> None:
        """Drop the skills prompt cache so a promoted skill appears active (3e)."""
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[grove.dispatcher] skills prompt cache invalidation failed "
                "(non-fatal): %r", exc,
            )

    def _deny_quarantined_skill(
        self, flag: Dict[str, Any], payload: Any, *, purge: bool,
        ledger: Optional[Any],
    ) -> None:
        """Handle the "Never" choice: deny + optionally purge the quarantine.

        Adds the exact command's cache key to the session deny cache so an
        immediate re-attempt of the same invocation auto-denies. When
        ``purge`` is set (operator confirmed removal), the quarantine
        directory is deleted via ``grove.sovereignty.reject`` — the
        decisive skill-scoped deny.
        """
        cache_key = flag.get("cache_key")
        if cache_key is not None:
            self._session_deny_cache.add(cache_key)
        if purge:
            try:
                from grove.sovereignty import reject as _reject
                _reject(
                    payload.skill_name,
                    reason="operator chose Never at the post-execution prompt",
                )
                logger.info(
                    "[grove.dispatcher] Quarantined skill %r purged on Never.",
                    payload.skill_name,
                )
            except FileNotFoundError:
                logger.info(
                    "[grove.dispatcher] Never: no quarantine dir for %r to "
                    "purge (already gone).", payload.skill_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[grove.dispatcher] Never: failed to purge quarantine "
                    "dir for %r (non-fatal): %r", payload.skill_name, exc,
                )
        if ledger is not None:
            try:
                ledger.record(
                    "skill_promotion_denied",
                    skill_name=payload.skill_name,
                    purged=purge,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[grove.dispatcher] skill_promotion_denied ledger write "
                    "failed (non-fatal): %r", exc,
                )

    def _log_session_cache_hit(
        self,
        tool_name: str,
        cache_type: str,
        *,
        ledger: Optional[Any] = None,
    ) -> None:
        """Emit a kaizen_ledger ``session_cache_hit`` event.

        Sprint 32 — every silent cache hit MUST land in the ledger so
        the operator's audit trail captures actions the agent
        executed (or refused) without re-prompting. The prompt-shown
        case writes its own ledger record upstream via the
        ``andon_halt`` / ``andon_disposition`` pair.

        Best-effort: a ledger that raises on record does NOT block
        the dispatch — the cache decision is the operational truth;
        telemetry is observability around it.
        """
        if ledger is None:
            return
        try:
            ledger.record(
                "session_cache_hit",
                tool=tool_name,
                type=cache_type,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[grove.dispatcher] session_cache_hit ledger write "
                "failed (non-fatal): %r", exc,
            )

    def _build_skip_observations(
        self,
        agent: Any,
        intents: List[Any],
        *,
        hard: bool = False,
    ) -> List[Any]:
        """Phase 5 Skip + Sprint 32 Phase 3a Hard-denial — denial Observations.

        For each intent in the halted batch:
          * Append a tool message to the agent's messages list with a
            denial body (so the next LLM call sees a paired tool
            response for every assistant tool_call — required by every
            provider's API).
          * Build an Observation carrying ``success=False`` and the
            denial body as ``value``.

        When ``hard=True`` (the Sprint 32 Phase 3a forced-denial path
        after three red-zone strikes), the denial body uses an
        explicit directive phrasing:

            HARD DENIAL: This action is prohibited. Do not attempt
            this tool with these arguments again.

        plus a ``metadata.is_hard_denial=True`` marker so future
        Agent logic can detect "do not retry" without parsing the
        denial text. The per-turn architecture stays correct — the
        strike counter resets at the next turn — but the explicit
        directive prevents the agent from looping within the turn.
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
            if hard:
                denial = (
                    f"HARD DENIAL: This action is prohibited. "
                    f"Do not attempt this tool with these arguments again. "
                    f"(tool: {intent.tool_name})"
                )
                metadata = {
                    "disposition": "deny_hard",
                    "reason": "andon_hard_denial",
                    "is_hard_denial": True,
                }
            else:
                # Sprint 57 — operator-friendly wording in the agent's context.
                # The agent reads this Observation value; it must NOT carry
                # governance implementation terms (Andon / zone / sovereignty)
                # it would then parrot to the operator.
                denial = (
                    f"This action was paused and the operator declined to run "
                    f"it ('{intent.tool_name}'). It did not execute. Continue "
                    f"with an alternative approach."
                )
                metadata = {
                    "disposition": "deny",
                    "reason": "andon_deny",
                }
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
                metadata=metadata,
            ))
        return observations

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
                f"⚠ Action paused for approval: '{triggering_intent.tool_name}'."
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
