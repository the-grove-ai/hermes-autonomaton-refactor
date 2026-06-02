"""Sprint 34 — Shared RuntimeContext singleton for tests.

``AIAgent.__init__`` now requires a ``RuntimeContext`` (no None default,
no silent substrate fallback). Tests construct AIAgent with the
``MOCK_RUNTIME_CTX`` exported below to satisfy the contract.

The instance is a thin subclass that overrides ``env`` and ``config``
attribute access via ``__getattribute__`` to read **live** substrate:

* ``ctx.env`` returns ``dict(os.environ)`` at access time, so
  ``monkeypatch.setenv(...)`` in a test still affects what the Agent's
  ``_env_or`` helper sees.
* ``ctx.config`` returns ``hermes_cli.config.load_config()`` at access
  time, so ``patch("hermes_cli.config.load_config", return_value=...)``
  still drives the Agent's ``_config_load_or`` helper.

Without this delegation, the Sprint 34 contract change (helpers no
longer fall through to direct ``os.environ`` / ``load_config()``) would
silently break every existing test that uses ``monkeypatch.setenv`` or
patches ``load_config`` as the test substrate. The delegation preserves
that test ergonomics — the Agent's helpers route through the
RuntimeContext exclusively, and the test's substrate patch lands at the
RuntimeContext boundary instead.

Heavy-resource fields (``tools``, ``memory_store``,
``context_length_by_model``, ``anthropic_client``, ``openai_client``,
``compression_probe``) stay at their RuntimeContext defaults (mostly
None / empty). Tests that need cached heavy resources construct a
richer context inline (see ``tests/grove/test_dispatcher.py`` for
examples).

The pytest ``mock_runtime_ctx`` fixture in ``tests/conftest.py`` returns
this same instance for tests written in the fixture-parameter style.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from grove.dispatcher import RuntimeContext


class _LiveSubstrateRuntimeContext(RuntimeContext):
    """Test substrate: duck-types RuntimeContext, reads env/config live.

    ``__getattribute__`` intercepts ``env`` and ``config`` so the base
    class's ``env_get`` / ``env_get_int`` / ``env_get_float`` /
    ``config_get`` methods (which read ``self.env`` / ``self.config``
    directly) pick up live substrate. Every other attribute — including
    the heavy-resource slots — falls through to the normal dataclass
    field lookup.
    """

    def __getattribute__(self, name: str) -> Any:
        if name == "env":
            return dict(os.environ)
        if name == "config":
            try:
                from hermes_cli.config import load_config
                cfg = load_config()
            except Exception:
                return {}
            return cfg if isinstance(cfg, dict) else {}
        return super().__getattribute__(name)


MOCK_RUNTIME_CTX: RuntimeContext = _LiveSubstrateRuntimeContext()


# Sprint 53 — capability provider callback for tests that construct
# ``AIAgent`` directly (bypassing the Dispatcher). The Agent requires
# ``get_available_tools`` to be supplied; production callers route via
# ``Dispatcher(agent_kwargs=...).agent``. Tests use this helper which
# constructs an ad-hoc ``ToolRegistry`` once and returns the standard
# filtered tool list shape.
def _mock_capability_provider(
    enabled_toolsets=None, disabled_toolsets=None, quiet_mode=True,
):
    """Return the same filtered tool list a Dispatcher would supply."""
    from tools.registry import ToolRegistry, register_builtin_tools
    global _mock_capability_provider_registry
    try:
        registry = _mock_capability_provider_registry
    except NameError:
        registry = ToolRegistry()
        register_builtin_tools(registry)
        _mock_capability_provider_registry = registry
    from model_tools import get_tool_definitions
    return get_tool_definitions(
        registry,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=quiet_mode,
    )


MOCK_CAPABILITY_PROVIDER = _mock_capability_provider


def wire_mock_dispatcher(agent):
    """Attach a stub ``_dispatcher_singleton`` with the test registry
    AND a working ``dispatch_turn`` that drives the Agent's generator.

    Sprint 53 — Agent dispatch paths read the registry through
    ``self._dispatcher_singleton.registry`` and ``run_conversation``
    routes through ``self._dispatcher_singleton.dispatch_turn``. Tests
    that construct ``AIAgent`` directly use this helper to install a
    minimal back-reference object that satisfies both contracts.

    Returns the agent for convenient chaining.
    """
    from types import SimpleNamespace
    from tools.registry import ToolRegistry, register_builtin_tools
    global _mock_capability_provider_registry
    try:
        registry = _mock_capability_provider_registry
    except NameError:
        registry = ToolRegistry()
        register_builtin_tools(registry)
        _mock_capability_provider_registry = registry

    # Lazy import — avoid Dispatcher import time at module load.
    from grove.dispatcher import Dispatcher
    from grove.sovereign_prompt_handlers import silent_allow_handler

    # Build the smallest Dispatcher object that can drive the Agent's
    # generator. Construct without ``agent_kwargs`` so it doesn't try
    # to build a fresh Agent — we already have one. Forward the
    # Agent's sovereign_prompt_handler when set (tests inject
    # ``silent_allow_handler`` to keep zone classification halts from
    # blocking deterministic runs).
    handler = getattr(agent, "_sovereign_prompt_handler", None) or silent_allow_handler
    disp = Dispatcher(sovereign_prompt_handler=handler)
    disp.registry = registry
    disp.agent = agent
    agent._dispatcher_singleton = disp
    return agent
