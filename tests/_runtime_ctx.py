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
