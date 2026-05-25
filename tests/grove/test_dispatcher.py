"""Tests for grove.dispatcher — Sprint 26 Phase 1a.

Covers the RuntimeContext snapshot dataclass and the Dispatcher skeleton.
Phase 1a is structural only: the Dispatcher captures env + config; it
does not yet build heavy singletons or invert tool execution. These
tests verify the substrate snapshot semantics and the graceful
degradation when load_config raises.

Subsequent phases will extend this file as the Dispatcher grows
responsibilities (heavy singletons → Phase 1b, intent protocol →
Phase 2, generator-shaped loop → Phase 3, etc.).
"""

from __future__ import annotations

import logging
from typing import Dict

import pytest

from grove.dispatcher import Dispatcher, RuntimeContext


# ── RuntimeContext ────────────────────────────────────────────────────────


class TestRuntimeContextEnv:
    def test_env_get_returns_snapshotted_value(self):
        ctx = RuntimeContext(env={"FOO": "bar"}, config={})
        assert ctx.env_get("FOO") == "bar"

    def test_env_get_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get("MISSING", "fallback") == "fallback"

    def test_env_get_returns_empty_string_default_when_absent_and_no_default(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get("MISSING") == ""

    def test_env_get_int_parses_numeric_string(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800"}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 1800

    def test_env_get_int_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 60

    def test_env_get_int_returns_default_when_empty_string(self):
        # Mirrors the legacy ``int(os.getenv("X", "1800"))`` pattern's
        # behavior on an empty value — fall back rather than ValueError.
        ctx = RuntimeContext(env={"TIMEOUT": ""}, config={})
        assert ctx.env_get_int("TIMEOUT", 60) == 60

    def test_env_get_int_returns_default_on_parse_failure(
        self, caplog: pytest.LogCaptureFixture
    ):
        ctx = RuntimeContext(env={"TIMEOUT": "not-a-number"}, config={})
        with caplog.at_level(logging.DEBUG, logger="grove.dispatcher"):
            value = ctx.env_get_int("TIMEOUT", 60)
        assert value == 60
        assert any("not-a-number" in r.getMessage() for r in caplog.records)

    def test_env_get_float_parses_decimal_string(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800.5"}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 1800.5

    def test_env_get_float_returns_default_when_missing(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 60.0

    def test_env_get_float_returns_default_on_parse_failure(self):
        ctx = RuntimeContext(env={"TIMEOUT": "abc"}, config={})
        assert ctx.env_get_float("TIMEOUT", 60.0) == 60.0


class TestRuntimeContextConfig:
    def test_config_get_walks_nested_path(self):
        ctx = RuntimeContext(
            env={},
            config={"memory": {"enabled": True, "store": {"path": "/x"}}},
        )
        assert ctx.config_get("memory", "enabled") is True
        assert ctx.config_get("memory", "store", "path") == "/x"

    def test_config_get_returns_default_when_path_missing(self):
        ctx = RuntimeContext(env={}, config={"memory": {}})
        assert ctx.config_get("memory", "absent", default="fallback") == "fallback"

    def test_config_get_returns_default_when_intermediate_is_non_mapping(self):
        # Walking past a non-dict value returns the default rather than raising.
        ctx = RuntimeContext(env={}, config={"memory": "not-a-dict"})
        assert ctx.config_get("memory", "enabled", default=False) is False

    def test_config_get_returns_none_when_no_default(self):
        ctx = RuntimeContext(env={}, config={})
        assert ctx.config_get("nonexistent") is None


class TestRuntimeContextImmutability:
    def test_runtime_context_is_frozen(self):
        ctx = RuntimeContext(env={}, config={})
        # dataclass(frozen=True) raises FrozenInstanceError on attribute set
        with pytest.raises(Exception):  # FrozenInstanceError, but be permissive
            ctx.env = {"NEW": "value"}  # type: ignore[misc]


# ── Dispatcher ────────────────────────────────────────────────────────────


class TestDispatcherConstruction:
    def test_dispatcher_captures_env_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GROVE_TEST_DISPATCHER_PROBE", "captured")
        d = Dispatcher()
        assert d.runtime_ctx.env_get("GROVE_TEST_DISPATCHER_PROBE") == "captured"

    def test_dispatcher_env_snapshot_is_frozen_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Env mutation after Dispatcher construction must NOT leak into
        # the snapshot — operators who edit env mid-session must restart.
        monkeypatch.setenv("GROVE_TEST_INITIAL", "first")
        d = Dispatcher()
        monkeypatch.setenv("GROVE_TEST_INITIAL", "second")
        assert d.runtime_ctx.env_get("GROVE_TEST_INITIAL") == "first"

    def test_dispatcher_captures_config_snapshot(self):
        d = Dispatcher()
        # The real config dict is captured; verify it's a dict (specific
        # contents depend on the operator's ~/.grove/config.yaml).
        assert isinstance(d.runtime_ctx.config, dict)

    def test_dispatcher_runtime_ctx_property_returns_same_instance(self):
        d = Dispatcher()
        assert d.runtime_ctx is d.runtime_ctx  # idempotent getter

    def test_build_runtime_context_returns_same_snapshot(self):
        d = Dispatcher()
        assert d.build_runtime_context() is d.runtime_ctx


class TestDispatcherGracefulDegradation:
    def test_dispatcher_handles_load_config_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        # If hermes_cli.config.load_config raises, the Dispatcher must
        # still construct with an empty config snapshot rather than
        # propagating the exception. The runtime context remains usable
        # for env reads even when config is unavailable.
        import hermes_cli.config as hcfg

        def _boom() -> Dict:
            raise RuntimeError("config broken")

        monkeypatch.setattr(hcfg, "load_config", _boom)
        with caplog.at_level(logging.WARNING, logger="grove.dispatcher"):
            d = Dispatcher()
        assert d.runtime_ctx.config == {}
        assert any("load_config" in r.getMessage() for r in caplog.records)

    def test_dispatcher_handles_non_dict_config(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # If load_config returns something other than a dict (corrupt
        # YAML deserializing to a list, etc.), the Dispatcher coerces
        # to an empty dict rather than letting an unexpected type
        # propagate into the Agent.
        import hermes_cli.config as hcfg
        monkeypatch.setattr(hcfg, "load_config", lambda: ["not", "a", "dict"])
        d = Dispatcher()
        assert d.runtime_ctx.config == {}


# ── AIAgent integration with runtime_ctx ──────────────────────────────────


class TestAIAgentRuntimeCtxInjection:
    """Smoke tests confirming AIAgent's _env_or / _config_load_or helpers
    route through runtime_ctx when injected, and fall back to direct
    substrate access when None (backward-compat path).

    These tests do NOT instantiate a full AIAgent (too heavy). They
    construct a bare AIAgent via ``object.__new__`` and set only the
    state the helpers read.
    """

    def _bare_agent(self, ctx=None):
        import run_agent
        agent = object.__new__(run_agent.AIAgent)
        agent._runtime_ctx = ctx
        return agent

    def test_env_or_reads_from_runtime_ctx_when_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ctx = RuntimeContext(env={"GROVE_PROBE": "from-ctx"}, config={})
        agent = self._bare_agent(ctx)
        # Set env to a different value to prove ctx wins
        monkeypatch.setenv("GROVE_PROBE", "from-env")
        assert agent._env_or("GROVE_PROBE") == "from-ctx"

    def test_env_or_falls_back_to_os_environ_when_ctx_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        agent = self._bare_agent(ctx=None)
        monkeypatch.setenv("GROVE_PROBE", "from-env")
        assert agent._env_or("GROVE_PROBE") == "from-env"

    def test_env_or_int_routes_through_ctx(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800"}, config={})
        agent = self._bare_agent(ctx)
        assert agent._env_or_int("TIMEOUT", 60) == 1800

    def test_env_or_int_fallback_on_unparseable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        agent = self._bare_agent(ctx=None)
        monkeypatch.setenv("TIMEOUT", "not-int")
        assert agent._env_or_int("TIMEOUT", 60) == 60

    def test_env_or_float_routes_through_ctx(self):
        ctx = RuntimeContext(env={"TIMEOUT": "1800.5"}, config={})
        agent = self._bare_agent(ctx)
        assert agent._env_or_float("TIMEOUT", 60.0) == 1800.5

    def test_config_load_or_routes_through_ctx_when_injected(self):
        ctx = RuntimeContext(env={}, config={"injected": True})
        agent = self._bare_agent(ctx)
        assert agent._config_load_or() == {"injected": True}

    def test_config_load_or_falls_back_to_live_load_when_ctx_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # When runtime_ctx is None, _config_load_or calls
        # hermes_cli.config.load_config directly.
        agent = self._bare_agent(ctx=None)
        import hermes_cli.config as hcfg
        monkeypatch.setattr(hcfg, "load_config", lambda: {"from": "live-load"})
        assert agent._config_load_or() == {"from": "live-load"}

    def test_config_load_or_handles_live_load_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        agent = self._bare_agent(ctx=None)
        import hermes_cli.config as hcfg

        def _boom():
            raise RuntimeError("config broken")
        monkeypatch.setattr(hcfg, "load_config", _boom)
        assert agent._config_load_or() == {}
