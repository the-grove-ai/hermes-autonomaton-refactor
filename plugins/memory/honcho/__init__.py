"""Honcho memory plugin — MemoryProvider for Honcho AI-native memory.

Provides cross-session user modeling with dialectic Q&A, semantic search,
peer cards, and persistent conclusions via the Honcho SDK. Honcho provides AI-native cross-session user
modeling with dialectic Q&A, semantic search, peer cards, and conclusions.

The honcho_* tool surface is DEMOLISHED (retrieval-ambient-class-v1 P1,
revised) — memory rides the CONTEXT path (prefetch / system_prompt_block)
only; get_tool_schemas returns [].

Config: Uses the existing Honcho config chain:
  1. $GROVE_HOME/honcho.json (profile-scoped)
  2. ~/.honcho/config.json (legacy global)
  3. Environment variables
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_manager import sanitize_context
from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas — DEMOLISHED (retrieval-ambient-class-v1 P1, revised).
# The honcho_* tool surface (PROFILE/SEARCH/REASONING/CONTEXT/CONCLUDE
# schemas + the dispatcher injection seam) is deleted: zero executions ever
# on prod (VM feed grep, liveness-gated), and the unconditional-injection
# seam minted ungoverned surface. Memory rides the CONTEXT path only.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HonchoMemoryProvider(MemoryProvider):
    """Honcho AI-native memory with dialectic Q&A and persistent user modeling."""

    def __init__(self):
        self._manager = None   # HonchoSessionManager
        self._config = None    # HonchoClientConfig
        self._session_key = ""
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._sync_thread: Optional[threading.Thread] = None

        # B1: recall_mode — set during initialize from config
        self._recall_mode = "hybrid"  # "context", "tools", or "hybrid"

        # Base context cache — refreshed on context_cadence, not frozen
        self._base_context_cache: Optional[str] = None
        self._base_context_lock = threading.Lock()

        # B5: Cost-awareness turn counting and cadence
        self._turn_count = 0
        self._injection_frequency = "every-turn"  # or "first-turn"
        self._context_cadence = 1   # minimum turns between context API calls
        self._dialectic_cadence = 1  # backwards-compat fallback; wizard writes 2 on new configs
        self._dialectic_depth = 1   # how many .chat() calls per dialectic cycle (1-3)
        self._dialectic_depth_levels: list[str] | None = None  # per-pass reasoning levels
        self._reasoning_heuristic: bool = True  # scale base level by query length
        self._reasoning_level_cap: str = "high"  # ceiling for auto-selected level
        self._last_context_turn = -999
        self._last_dialectic_turn = -999

        # Liveness + observability state
        self._prefetch_thread_started_at: float = 0.0   # monotonic ts of current thread
        self._prefetch_result_fired_at: int = -999      # turn the pending result was fired at
        self._dialectic_empty_streak: int = 0           # consecutive empty returns

        # Port #1957: lazy session init for tools-only mode
        self._session_initialized = False
        self._lazy_init_kwargs: Optional[dict] = None
        self._lazy_init_session_id: Optional[str] = None

        # Port #4053: cron guard — when True, plugin is fully inactive
        self._cron_skipped = False

    @property
    def name(self) -> str:
        return "honcho"

    def is_available(self) -> bool:
        """Check if Honcho is configured. No network calls."""
        try:
            from plugins.memory.honcho.client import HonchoClientConfig
            cfg = HonchoClientConfig.from_global_config()
            # Port #2645: baseUrl-only verification — api_key OR base_url suffices
            return cfg.enabled and bool(cfg.api_key or cfg.base_url)
        except Exception:
            return False

    def save_config(self, values, hermes_home):
        """Write config to $GROVE_HOME/honcho.json (Honcho SDK native format)."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "honcho.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2))

    def get_config_schema(self):
        return [
            {"key": "api_key", "description": "Honcho API key", "secret": True, "env_var": "HONCHO_API_KEY", "url": "https://app.honcho.dev"},
            {"key": "baseUrl", "description": "Honcho base URL (for self-hosted)"},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Run the full Honcho setup wizard after provider selection."""
        import types
        from plugins.memory.honcho.cli import cmd_setup
        cmd_setup(types.SimpleNamespace())

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize Honcho session manager.

        Handles: cron guard, recall_mode, session name resolution,
        peer memory mode, SOUL.md ai_peer sync, memory file migration,
        and pre-warming context at init.
        """
        try:
            # ----- Port #4053: cron guard -----
            agent_context = kwargs.get("agent_context", "")
            platform = kwargs.get("platform", "cli")
            if agent_context in ("cron", "flush") or platform == "cron":
                logger.debug("Honcho skipped: cron/flush context (agent_context=%s, platform=%s)",
                             agent_context, platform)
                self._cron_skipped = True
                return

            from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client
            from plugins.memory.honcho.session import HonchoSessionManager

            cfg = HonchoClientConfig.from_global_config()
            if not cfg.enabled or not (cfg.api_key or cfg.base_url):
                logger.debug("Honcho not configured — plugin inactive")
                return

            self._config = cfg

            # ----- B1: recall_mode from config -----
            self._recall_mode = cfg.recall_mode  # "context", "tools", or "hybrid"
            logger.debug("Honcho recall_mode: %s", self._recall_mode)

            # ----- B5: cost-awareness config -----
            try:
                raw = cfg.raw or {}
                self._injection_frequency = raw.get("injectionFrequency", "every-turn")
                self._context_cadence = int(raw.get("contextCadence", 1))
                # Backwards-compat: unset dialecticCadence falls back to 1
                # (every turn) so existing honcho.json configs without the key
                # behave as they did before. New setups via `hermes honcho setup`
                # get dialecticCadence=2 written explicitly by the wizard.
                self._dialectic_cadence = int(raw.get("dialecticCadence", 1))
                self._dialectic_depth = max(1, min(cfg.dialectic_depth, 3))
                self._dialectic_depth_levels = cfg.dialectic_depth_levels
                self._reasoning_heuristic = cfg.reasoning_heuristic
                if cfg.reasoning_level_cap in self._LEVEL_ORDER:
                    self._reasoning_level_cap = cfg.reasoning_level_cap
            except Exception as e:
                logger.debug("Honcho cost-awareness config parse error: %s", e)

            # ----- Port #1969: aiPeer sync from SOUL.md — REMOVED -----
            # SOUL.md is persona content, not identity config. aiPeer should
            # only come from honcho.json (host block or root) or the default.
            # See scratch/memory-plugin-ux-specs.md #10 for rationale.

            # ----- Port #1957: lazy session init for tools-only mode -----
            if self._recall_mode == "tools":
                if cfg.init_on_session_start:
                    # Eager init even in tools mode (opt-in)
                    self._do_session_init(cfg, session_id, **kwargs)
                    return
                # Defer actual session creation until first tool call
                self._lazy_init_kwargs = kwargs
                self._lazy_init_session_id = session_id
                # Still need a client reference for _ensure_session
                self._config = cfg
                logger.debug("Honcho tools-only mode — deferring session init until first tool call")
                return

            # ----- Eager init (context or hybrid mode) -----
            self._do_session_init(cfg, session_id, **kwargs)

        except ImportError:
            logger.debug("honcho-ai package not installed — plugin inactive")
        except Exception as e:
            logger.warning("Honcho init failed: %s", e)
            self._manager = None

    def _do_session_init(self, cfg, session_id: str, **kwargs) -> None:
        """Shared session initialization logic for both eager and lazy paths."""
        from plugins.memory.honcho.client import get_honcho_client
        from plugins.memory.honcho.session import HonchoSessionManager

        client = get_honcho_client(cfg)
        self._manager = HonchoSessionManager(
            honcho=client,
            config=cfg,
            context_tokens=cfg.context_tokens,
            runtime_user_peer_name=kwargs.get("user_id") or None,
        )

        # ----- B3: resolve_session_name -----
        session_title = kwargs.get("session_title")
        gateway_session_key = kwargs.get("gateway_session_key")
        self._session_key = (
            cfg.resolve_session_name(
                session_title=session_title,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )
            or session_id
            or "hermes-default"
        )
        logger.debug("Honcho session key resolved: %s", self._session_key)

        # Create session eagerly
        session = self._manager.get_or_create(self._session_key)
        self._session_initialized = True

        # ----- B6: Memory file migration (one-time, for new sessions) -----
        # Skip under per-session strategy: every Hermes run creates a fresh
        # Honcho session by design, so uploading MEMORY.md/USER.md/SOUL.md to
        # each one would flood the backend with short-lived duplicates instead
        # of performing a one-time migration.
        try:
            if not session.messages and cfg.session_strategy != "per-session":
                from hermes_constants import get_hermes_home
                mem_dir = str(get_hermes_home() / "memories")
                self._manager.migrate_memory_files(self._session_key, mem_dir)
                logger.debug("Honcho memory file migration attempted for new session: %s", self._session_key)
            elif cfg.session_strategy == "per-session":
                logger.debug(
                    "Honcho memory file migration skipped: per-session strategy creates a fresh session per run (%s)",
                    self._session_key,
                )
        except Exception as e:
            logger.debug("Honcho memory file migration skipped: %s", e)

        # ----- B7: Pre-warming at init -----
        # Context prewarm warms peer.context() (base layer), consumed via
        # pop_context_result() in prefetch(). Dialectic prewarm runs the
        # full configured depth and writes into _prefetch_result so turn 1
        # consumes the result directly.
        if self._recall_mode in ("context", "hybrid"):
            try:
                self._manager.prefetch_context(self._session_key)
            except Exception as e:
                logger.debug("Honcho context prewarm failed: %s", e)

            _prewarm_query = (
                "Summarize what you know about this user. "
                "Focus on preferences, current projects, and working style."
            )

            def _prewarm_dialectic() -> None:
                try:
                    r = self._run_dialectic_depth(_prewarm_query)
                except Exception as exc:
                    logger.debug("Honcho dialectic prewarm failed: %s", exc)
                    self._dialectic_empty_streak += 1
                    return
                if r and r.strip():
                    with self._prefetch_lock:
                        self._prefetch_result = r
                        self._prefetch_result_fired_at = 0
                    # Treat prewarm as turn 0 so cadence gating starts clean.
                    self._last_dialectic_turn = 0
                    self._dialectic_empty_streak = 0
                else:
                    self._dialectic_empty_streak += 1

            self._prefetch_thread_started_at = time.monotonic()
            self._prefetch_thread = threading.Thread(
                target=_prewarm_dialectic, daemon=True, name="honcho-prewarm-dialectic"
            )
            self._prefetch_thread.start()
            logger.debug("Honcho pre-warm started for session: %s", self._session_key)

    def _ensure_session(self) -> bool:
        """Lazily initialize the Honcho session (for tools-only mode).

        Returns True if the manager is ready, False otherwise.
        """
        if self._manager and self._session_initialized:
            return True
        if self._cron_skipped:
            return False
        if not self._config or not self._lazy_init_kwargs:
            return False

        try:
            self._do_session_init(
                self._config,
                self._lazy_init_session_id or "hermes-default",
                **self._lazy_init_kwargs,
            )
            # Clear lazy refs
            self._lazy_init_kwargs = None
            self._lazy_init_session_id = None
            return self._manager is not None
        except Exception as e:
            logger.warning("Honcho lazy session init failed: %s", e)
            return False

    def _format_first_turn_context(self, ctx: dict) -> str:
        """Format the prefetch context dict into a readable system prompt block."""
        parts = []

        # Session summary — session-scoped context, placed first for relevance
        summary = ctx.get("summary", "")
        if summary:
            parts.append(f"## Session Summary\n{summary}")

        rep = ctx.get("representation", "")
        if rep:
            parts.append(f"## User Representation\n{rep}")

        card = ctx.get("card", "")
        if card:
            parts.append(f"## User Peer Card\n{card}")

        ai_rep = ctx.get("ai_representation", "")
        if ai_rep:
            parts.append(f"## AI Self-Representation\n{ai_rep}")

        ai_card = ctx.get("ai_card", "")
        if ai_card:
            parts.append(f"## AI Identity Card\n{ai_card}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    def system_prompt_block(self) -> str:
        """Return system prompt text, adapted by recall_mode.

        Returns only the mode header and tool instructions — static text
        that doesn't change between turns (prompt-cache friendly).
        Live context (representation, card) is injected via prefetch().
        """
        if self._cron_skipped:
            return ""
        if not self._manager or not self._session_key:
            return ""

        # retrieval-ambient-class-v1 P1 (revised): the honcho_* tool surface is
        # DEMOLISHED — every recall_mode now renders the context-injection
        # header; the prompt must never advertise tools that do not exist.
        return (
            "# Honcho Memory\n"
            "Active (context-injection mode). Relevant user context is automatically "
            "injected before each turn. No memory tools are available — context is "
            "managed automatically."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return base context (representation + card) plus dialectic supplement.

        Assembles two layers:
        1. Base context from peer.context() — cached, refreshed on context_cadence
        2. Dialectic supplement — cached, refreshed on dialectic_cadence

        B1: Returns empty when recall_mode is "tools" (no injection).
        B5: Respects injection_frequency — "first-turn" returns cached/empty after turn 0.
        Port #3265: Truncates to context_tokens budget.
        """
        if self._cron_skipped:
            return ""

        # B1: tools-only mode — no auto-injection
        if self._recall_mode == "tools":
            return ""

        # B5: injection_frequency — if "first-turn" and past first turn, return empty.
        # _turn_count is 1-indexed (first user message = 1), so > 1 means "past first".
        if self._injection_frequency == "first-turn" and self._turn_count > 1:
            return ""

        # Trivial prompts ("ok", "yes", slash commands) carry no semantic signal.
        if self._is_trivial_prompt(query):
            return ""

        parts = []

        # ----- Layer 1: Base context (representation + card) -----
        # On first call, fetch synchronously so turn 1 isn't empty.
        # After that, serve from cache and refresh in background on cadence.
        with self._base_context_lock:
            if self._base_context_cache is None:
                # First call — synchronous fetch
                try:
                    ctx = self._manager.get_prefetch_context(self._session_key)
                    self._base_context_cache = self._format_first_turn_context(ctx) if ctx else ""
                    self._last_context_turn = self._turn_count
                except Exception as e:
                    logger.debug("Honcho base context fetch failed: %s", e)
                    self._base_context_cache = ""
            base_context = self._base_context_cache

        # Check if background context prefetch has a fresher result
        if self._manager:
            fresh_ctx = self._manager.pop_context_result(self._session_key)
            if fresh_ctx:
                formatted = self._format_first_turn_context(fresh_ctx)
                if formatted:
                    with self._base_context_lock:
                        self._base_context_cache = formatted
                    base_context = formatted

        if base_context:
            parts.append(base_context)

        # ----- Layer 2: Dialectic supplement -----
        # On the very first turn, no queue_prefetch() has run yet so the
        # dialectic result is empty.  Run with a bounded timeout so a slow
        # Honcho connection doesn't block the first response indefinitely.
        # On timeout we let the thread keep running and write its result into
        # _prefetch_result under the lock, so the next turn picks it up.
        #
        # Skip if the session-start prewarm already filled _prefetch_result —
        # firing another .chat() would be duplicate work.
        with self._prefetch_lock:
            _prewarm_landed = bool(self._prefetch_result)
        if _prewarm_landed and self._last_dialectic_turn == -999:
            self._last_dialectic_turn = self._turn_count

        if self._last_dialectic_turn == -999 and query:
            _first_turn_timeout = (
                self._config.timeout if self._config and self._config.timeout else 8.0
            )
            _fired_at = self._turn_count

            def _run_first_turn() -> None:
                try:
                    r = self._run_dialectic_depth(query)
                except Exception as exc:
                    logger.debug("Honcho first-turn dialectic failed: %s", exc)
                    self._dialectic_empty_streak += 1
                    return
                if r and r.strip():
                    with self._prefetch_lock:
                        self._prefetch_result = r
                        self._prefetch_result_fired_at = _fired_at
                    # Advance cadence only on a non-empty result so the next
                    # turn retries when the call returned nothing.
                    self._last_dialectic_turn = _fired_at
                    self._dialectic_empty_streak = 0
                else:
                    self._dialectic_empty_streak += 1

            self._prefetch_thread_started_at = time.monotonic()
            self._prefetch_thread = threading.Thread(
                target=_run_first_turn, daemon=True, name="honcho-prefetch-first"
            )
            self._prefetch_thread.start()
            self._prefetch_thread.join(timeout=_first_turn_timeout)
            if self._prefetch_thread.is_alive():
                logger.debug(
                    "Honcho first-turn dialectic still running after %.1fs — "
                    "will surface on next turn",
                    _first_turn_timeout,
                )

        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            dialectic_result = self._prefetch_result
            fired_at = self._prefetch_result_fired_at
            self._prefetch_result = ""
            self._prefetch_result_fired_at = -999

        # Discard stale pending results: if the fire happened more than
        # cadence × multiplier turns ago (e.g. a run of trivial-prompt turns
        # passed without consumption), the content likely no longer tracks
        # the current conversational pivot.
        stale_limit = self._dialectic_cadence * self._STALE_RESULT_MULTIPLIER
        if dialectic_result and fired_at >= 0 and (self._turn_count - fired_at) > stale_limit:
            logger.debug(
                "Honcho pending dialectic discarded as stale: fired_at=%d, "
                "turn=%d, limit=%d", fired_at, self._turn_count, stale_limit,
            )
            dialectic_result = ""

        if dialectic_result and dialectic_result.strip():
            parts.append(dialectic_result)

        if not parts:
            return ""

        result = "\n\n".join(parts)

        # ----- Port #3265: token budget enforcement -----
        result = self._truncate_to_budget(result)

        return result

    def _truncate_to_budget(self, text: str) -> str:
        """Truncate text to fit within context_tokens budget if set."""
        if not self._config or not self._config.context_tokens:
            return text
        budget_chars = self._config.context_tokens * 4  # conservative char estimate
        if len(text) <= budget_chars:
            return text
        # Truncate at word boundary
        truncated = text[:budget_chars]
        last_space = truncated.rfind(" ")
        if last_space > budget_chars * 0.8:
            truncated = truncated[:last_space]
        return truncated + " …"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire background prefetch threads for the upcoming turn.

        B5: Checks cadence independently for dialectic and context refresh.
        Context refresh updates the base layer (representation + card).
        Dialectic fires the LLM reasoning supplement.
        """
        if self._cron_skipped:
            return
        if not self._manager or not self._session_key or not query:
            return

        # B1: tools-only mode — no prefetch
        if self._recall_mode == "tools":
            return

        # Trivial prompts don't warrant either a context refresh or a dialectic call.
        if self._is_trivial_prompt(query):
            return

        # ----- Context refresh (base layer) — independent cadence -----
        if self._context_cadence <= 1 or (self._turn_count - self._last_context_turn) >= self._context_cadence:
            self._last_context_turn = self._turn_count
            try:
                self._manager.prefetch_context(self._session_key, query)
            except Exception as e:
                logger.debug("Honcho context prefetch failed: %s", e)

        # ----- Dialectic prefetch (supplement layer) -----
        # Thread-alive guard with stale-thread recovery: a hung Honcho call
        # older than timeout × multiplier is treated as dead so it can't
        # block subsequent fires.
        if self._thread_is_live():
            logger.debug("Honcho dialectic prefetch skipped: prior thread still running")
            return

        # Cadence gate, widened by the empty-streak backoff so a persistently
        # silent backend doesn't retry every turn forever.
        effective = self._effective_cadence()
        if (self._turn_count - self._last_dialectic_turn) < effective:
            logger.debug(
                "Honcho dialectic prefetch skipped: effective cadence %d "
                "(base %d, empty streak %d), turns since last: %d",
                effective, self._dialectic_cadence, self._dialectic_empty_streak,
                self._turn_count - self._last_dialectic_turn,
            )
            return

        # Cadence advances only on a non-empty result so empty returns
        # (transient API error, sparse representation) retry next turn.
        _fired_at = self._turn_count

        def _run():
            try:
                result = self._run_dialectic_depth(query)
            except Exception as e:
                logger.debug("Honcho prefetch failed: %s", e)
                self._dialectic_empty_streak += 1
                return
            if result and result.strip():
                with self._prefetch_lock:
                    self._prefetch_result = result
                    self._prefetch_result_fired_at = _fired_at
                self._last_dialectic_turn = _fired_at
                self._dialectic_empty_streak = 0
            else:
                self._dialectic_empty_streak += 1

        self._prefetch_thread_started_at = time.monotonic()
        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="honcho-prefetch"
        )
        self._prefetch_thread.start()

    # ----- Dialectic depth: multi-pass .chat() with cold/warm prompts -----

    # Proportional reasoning levels per depth/pass when dialecticDepthLevels
    # is not configured. The base level is dialecticReasoningLevel.
    # Index: (depth, pass) → level relative to base.
    _PROPORTIONAL_LEVELS: dict[tuple[int, int], str] = {
        # depth 1: single pass at base level
        (1, 0): "base",
        # depth 2: pass 0 lighter, pass 1 at base
        (2, 0): "minimal",
        (2, 1): "base",
        # depth 3: pass 0 lighter, pass 1 at base, pass 2 one above minimal
        (3, 0): "minimal",
        (3, 1): "base",
        (3, 2): "low",
    }

    _LEVEL_ORDER = ("minimal", "low", "medium", "high", "max")

    # Char-count thresholds for the query-length reasoning heuristic.
    _HEURISTIC_LENGTH_MEDIUM = 120
    _HEURISTIC_LENGTH_HIGH = 400

    # Liveness constants. A thread older than timeout × multiplier is treated
    # as dead so a hung Honcho call can't block future retries indefinitely.
    _STALE_THREAD_MULTIPLIER = 2.0
    # Pending result whose fire-turn is older than cadence × multiplier is
    # discarded on read so we don't inject context for a stale conversational
    # pivot after a gap of trivial-prompt turns.
    _STALE_RESULT_MULTIPLIER = 2
    # Cap on the empty-streak backoff so a persistently silent backend
    # eventually settles on a ceiling instead of unbounded widening.
    _BACKOFF_MAX = 8

    def _thread_is_live(self) -> bool:
        """Thread-alive guard that treats threads older than the stale
        threshold as dead, so a hung Honcho request can't block new fires."""
        if not self._prefetch_thread or not self._prefetch_thread.is_alive():
            return False
        timeout = (self._config.timeout if self._config and self._config.timeout else 8.0)
        age = time.monotonic() - self._prefetch_thread_started_at
        if age > timeout * self._STALE_THREAD_MULTIPLIER:
            logger.debug(
                "Honcho prefetch thread age %.1fs exceeds stale threshold "
                "%.1fs — treating as dead", age, timeout * self._STALE_THREAD_MULTIPLIER,
            )
            return False
        return True

    def _effective_cadence(self) -> int:
        """Cadence plus empty-streak backoff, capped at _BACKOFF_MAX × base."""
        if self._dialectic_empty_streak <= 0:
            return self._dialectic_cadence
        widened = self._dialectic_cadence + self._dialectic_empty_streak
        ceiling = self._dialectic_cadence * self._BACKOFF_MAX
        return min(widened, ceiling)

    def liveness_snapshot(self) -> dict:
        """In-process snapshot of dialectic liveness state for diagnostics.

        Returns current turn, last successful dialectic turn, pending-result
        fire turn, empty streak, effective cadence, and thread status.
        """
        thread_age = None
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            thread_age = time.monotonic() - self._prefetch_thread_started_at
        return {
            "turn_count": self._turn_count,
            "last_dialectic_turn": self._last_dialectic_turn,
            "pending_result_fired_at": self._prefetch_result_fired_at,
            "empty_streak": self._dialectic_empty_streak,
            "effective_cadence": self._effective_cadence(),
            "thread_alive": thread_age is not None,
            "thread_age_seconds": thread_age,
        }

    def _apply_reasoning_heuristic(self, base: str, query: str) -> str:
        """Scale `base` up by query length, clamped at reasoning_level_cap.

        Char-count heuristic: +1 at >=120 chars, +2 at >=400.
        """
        if not self._reasoning_heuristic or not query:
            return base
        if base not in self._LEVEL_ORDER:
            return base
        n = len(query)
        if n < self._HEURISTIC_LENGTH_MEDIUM:
            bump = 0
        elif n < self._HEURISTIC_LENGTH_HIGH:
            bump = 1
        else:
            bump = 2
        base_idx = self._LEVEL_ORDER.index(base)
        cap_idx = self._LEVEL_ORDER.index(self._reasoning_level_cap)
        return self._LEVEL_ORDER[min(base_idx + bump, cap_idx)]

    def _resolve_pass_level(self, pass_idx: int, query: str = "") -> str:
        """Resolve reasoning level for a given pass index.

        Precedence:
          1. dialecticDepthLevels (explicit per-pass) — wins absolutely
          2. _PROPORTIONAL_LEVELS table (depth>1 lighter-early passes)
          3. Base level = dialecticReasoningLevel, optionally scaled by the
             reasoning heuristic when the mapping falls through to 'base'
        """
        if self._dialectic_depth_levels and pass_idx < len(self._dialectic_depth_levels):
            return self._dialectic_depth_levels[pass_idx]

        base = (self._config.dialectic_reasoning_level if self._config else "low")
        mapping = self._PROPORTIONAL_LEVELS.get((self._dialectic_depth, pass_idx))
        if mapping is None or mapping == "base":
            return self._apply_reasoning_heuristic(base, query)
        return mapping

    def _build_dialectic_prompt(self, pass_idx: int, prior_results: list[str], is_cold: bool) -> str:
        """Build the prompt for a given dialectic pass.

        Pass 0: cold start (general user query) or warm (session-scoped).
        Pass 1: self-audit / targeted synthesis against gaps from pass 0.
        Pass 2: reconciliation / contradiction check across prior passes.
        """
        if pass_idx == 0:
            if is_cold:
                return (
                    "Who is this person? What are their preferences, goals, "
                    "and working style? Focus on facts that would help an AI "
                    "assistant be immediately useful."
                )
            return (
                "Given what's been discussed in this session so far, what "
                "context about this user is most relevant to the current "
                "conversation? Prioritize active context over biographical facts."
            )
        elif pass_idx == 1:
            prior = prior_results[-1] if prior_results else ""
            return (
                f"Given this initial assessment:\n\n{prior}\n\n"
                "What gaps remain in your understanding that would help "
                "going forward? Synthesize what you actually know about "
                "the user's current state and immediate needs, grounded "
                "in evidence from recent sessions."
            )
        else:
            # pass 2: reconciliation
            return (
                f"Prior passes produced:\n\n"
                f"Pass 1:\n{prior_results[0] if len(prior_results) > 0 else '(empty)'}\n\n"
                f"Pass 2:\n{prior_results[1] if len(prior_results) > 1 else '(empty)'}\n\n"
                "Do these assessments cohere? Reconcile any contradictions "
                "and produce a final, concise synthesis of what matters most "
                "for the current conversation."
            )

    @staticmethod
    def _signal_sufficient(result: str) -> bool:
        """Check if a dialectic pass returned enough signal to skip further passes.

        Heuristic: a response longer than 100 chars with some structure
        (section headers, bullets, or an ordered list) is considered sufficient.
        """
        if not result or len(result.strip()) < 100:
            return False
        # Structured output with sections/bullets is strong signal
        if "\n" in result and (
            "##" in result
            or "•" in result
            or re.search(r"^[*-] ", result, re.MULTILINE)
            or re.search(r"^\s*\d+\. ", result, re.MULTILINE)
        ):
            return True
        # Long enough even without structure
        return len(result.strip()) > 300

    def _run_dialectic_depth(self, query: str) -> str:
        """Execute up to dialecticDepth .chat() calls with conditional bail-out.

        Cold start (no base context): general user-oriented query.
        Warm session (base context exists): session-scoped query.
        Each pass is conditional — bails early if prior pass returned strong signal.
        Returns the best (usually last) result.
        """
        if not self._manager or not self._session_key:
            return ""

        is_cold = not self._base_context_cache
        results: list[str] = []

        for i in range(self._dialectic_depth):
            if i == 0:
                prompt = self._build_dialectic_prompt(0, results, is_cold)
            else:
                # Skip further passes if prior pass delivered strong signal
                if results and self._signal_sufficient(results[-1]):
                    logger.debug("Honcho dialectic depth %d: pass %d skipped, prior signal sufficient",
                                 self._dialectic_depth, i)
                    break
                prompt = self._build_dialectic_prompt(i, results, is_cold)

            level = self._resolve_pass_level(i, query=query)
            logger.debug("Honcho dialectic depth %d: pass %d, level=%s, cold=%s",
                         self._dialectic_depth, i, level, is_cold)

            result = self._manager.dialectic_query(
                self._session_key, prompt,
                reasoning_level=level,
                peer="user",
            )
            results.append(result or "")

        # Return the last non-empty result (deepest pass that ran)
        for r in reversed(results):
            if r and r.strip():
                return r
        return ""

    # Prompts that carry no semantic signal — trivial acknowledgements, slash
    # commands, empty input. Skipping injection here saves tokens and prevents
    # stale user-model context from derailing one-word replies.
    _TRIVIAL_PROMPT_RE = re.compile(
        r'^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|'
        r'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)$',
        re.IGNORECASE,
    )

    @classmethod
    def _is_trivial_prompt(cls, text: str) -> bool:
        """Return True if the prompt is too trivial to warrant context injection."""
        if not text:
            return True
        stripped = text.strip()
        if not stripped:
            return True
        if stripped.startswith("/"):
            return True
        if cls._TRIVIAL_PROMPT_RE.match(stripped):
            return True
        return False

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count for cadence and injection_frequency logic."""
        self._turn_count = turn_number

    @staticmethod
    def _chunk_message(content: str, limit: int) -> list[str]:
        """Split content into chunks that fit within the Honcho message limit.

        Splits at paragraph boundaries when possible, falling back to
        sentence boundaries, then word boundaries. Each continuation
        chunk is prefixed with "[continued] " so Honcho's representation
        engine can reconstruct the full message.
        """
        if len(content) <= limit:
            return [content]

        prefix = "[continued] "
        prefix_len = len(prefix)
        chunks = []
        remaining = content
        first = True
        while remaining:
            effective = limit if first else limit - prefix_len
            if len(remaining) <= effective:
                chunks.append(remaining if first else prefix + remaining)
                break

            segment = remaining[:effective]

            # Try paragraph break, then sentence, then word
            cut = segment.rfind("\n\n")
            if cut < effective * 0.3:
                cut = segment.rfind(". ")
                if cut >= 0:
                    cut += 2  # include the period and space
            if cut < effective * 0.3:
                cut = segment.rfind(" ")
            if cut < effective * 0.3:
                cut = effective  # hard cut

            chunk = remaining[:cut].rstrip()
            remaining = remaining[cut:].lstrip()
            if not first:
                chunk = prefix + chunk
            chunks.append(chunk)
            first = False

        return chunks

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Record the conversation turn in Honcho (non-blocking).

        Messages exceeding the Honcho API limit (default 25k chars) are
        split into multiple messages with continuation markers.
        """
        if self._cron_skipped:
            return
        if not self._manager or not self._session_key:
            return

        msg_limit = self._config.message_max_chars if self._config else 25000
        clean_user_content = sanitize_context(user_content or "").strip()
        clean_assistant_content = sanitize_context(assistant_content or "").strip()

        def _sync():
            try:
                session = self._manager.get_or_create(self._session_key)
                for chunk in self._chunk_message(clean_user_content, msg_limit):
                    session.add_message("user", chunk)
                for chunk in self._chunk_message(clean_assistant_content, msg_limit):
                    session.add_message("assistant", chunk)
                self._manager._flush_session(session)
            except Exception as e:
                logger.debug("Honcho sync_turn failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="honcho-sync"
        )
        self._sync_thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in user profile writes as Honcho conclusions.

        ``metadata`` is accepted for compatibility with the write-origin
        work landed in main (commit 6a957a74); it's not yet threaded into
        the Honcho conclusion payload.  Left as a follow-up so this PR
        stays focused on the 7-PR consolidation and its review follow-ups.
        """
        if action != "add" or target != "user" or not content:
            return
        if self._cron_skipped:
            return
        if not self._manager or not self._session_key:
            return

        def _write():
            try:
                self._manager.create_conclusion(self._session_key, content)
            except Exception as e:
                logger.debug("Honcho memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="honcho-memwrite")
        t.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush all pending messages to Honcho on session end."""
        if self._cron_skipped:
            return
        if not self._manager:
            return
        # Wait for pending sync
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        try:
            self._manager.flush_all()
        except Exception as e:
            logger.debug("Honcho session-end flush failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """DEMOLISHED (retrieval-ambient-class-v1 P1, revised): the
        honcho_* tool surface is deleted (zero executions ever on prod;
        liveness-gated). The override survives only because the ABC marks
        it abstract — memory rides the CONTEXT path, never tools. A tool
        call routed here anyway fail-louds via the base handle_tool_call
        NotImplementedError."""
        return []

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Flush any remaining messages
        if self._manager:
            try:
                self._manager.flush_all()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register Honcho as a memory provider plugin."""
    ctx.register_memory_provider(HonchoMemoryProvider())
