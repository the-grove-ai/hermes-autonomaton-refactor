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

    The Agent reads through ``env_get()`` / ``config_get()`` (or via the
    helper methods on ``AIAgent`` that wrap them). When the Agent's
    ``_runtime_ctx`` attribute is ``None``, those helpers fall back to
    direct substrate reads — the backward-compatibility path for callers
    that haven't yet been migrated to construct the Agent via a Dispatcher.

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

        Phase 1b will extend ``__init__`` to also build heavy singletons
        (LLM client, tool registry, context compressor, memory store).
        Phase 3 will wire tool execution.
        """
        config = self._load_config_safely()
        self._runtime_ctx = RuntimeContext(
            env=dict(os.environ),
            config=config,
        )
        logger.debug(
            "[grove.dispatcher] runtime context captured: "
            "%d env vars, config keys=%s",
            len(self._runtime_ctx.env),
            sorted(self._runtime_ctx.config.keys())[:10],
        )

    @property
    def runtime_ctx(self) -> RuntimeContext:
        """The captured substrate snapshot. Frozen for the Dispatcher's lifetime."""
        return self._runtime_ctx

    def build_runtime_context(self) -> RuntimeContext:
        """Return the runtime context the Agent constructor accepts.

        Phase 1b extends this to also return pre-built heavy singletons
        (LLM client, tool registry, etc.) bundled with the context.
        For Phase 1a, this is a thin getter — kept as a method so the
        signature can grow without breaking callers.
        """
        return self._runtime_ctx

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
