"""Plugin-side tests for the web search provider layer.

After the hermes-severance, Tavily is the sole bundled web provider. This
suite covers:

- The tavily plugin instantiates and self-reports the expected
  capabilities + ABC-derived defaults.
- ``is_available()`` reflects env-var presence.
- The web_search_registry resolves the active provider in the documented
  scenarios (explicit config wins ignoring availability, fallback walk
  filtered by availability, unknown name falls back, no-creds → None).
- Plugin response shapes match the legacy bit-for-bit contract.

Per the dev skill: these tests use *real* imports from the plugin module —
no mocking of the provider class itself — so the test catches drift in the
ABC interface, the registry, and the plugin glue layer simultaneously.
"""
from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip Tavily env vars so is_available() returns False."""
    for k in (
        "TAVILY_API_KEY",
        "TAVILY_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)


def _ensure_plugins_loaded() -> None:
    """Idempotently load plugins so the registry is populated.

    Sprint 53 — supplies a Dispatcher-style ToolRegistry on first call.
    """
    from hermes_cli.plugins import (
        _ensure_plugins_discovered,
        discover_plugins as _discover_plugins,
        get_plugin_manager,
    )
    mgr = get_plugin_manager()
    if mgr._registry is None:
        from tools.registry import ToolRegistry, register_builtin_tools
        reg = ToolRegistry()
        register_builtin_tools(reg)
        _discover_plugins(registry=reg)
    else:
        _ensure_plugins_discovered()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean web-provider env."""
    _clear_web_env(monkeypatch)


# ---------------------------------------------------------------------------
# Discovery + capability flags
# ---------------------------------------------------------------------------


class TestBundledPluginsRegister:
    """The sole bundled web plugin (tavily) discovers and registers correctly."""

    def test_only_tavily_present_in_registry(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import list_providers

        names = sorted(p.name for p in list_providers())
        assert names == ["tavily"]

    def test_tavily_capability_flags_match_spec(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider("tavily")
        assert provider is not None, "plugin 'tavily' not registered"
        # tavily: search + extract + crawl.
        assert provider.supports_search() is True
        assert provider.supports_extract() is True
        assert provider.supports_crawl() is True

    def test_tavily_has_name_and_display_name(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider("tavily")
        assert provider is not None
        assert provider.name == "tavily"
        assert provider.display_name  # any non-empty string

    def test_tavily_has_setup_schema(self) -> None:
        """``get_setup_schema()`` returns a dict the picker can consume."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider("tavily")
        assert provider is not None
        schema = provider.get_setup_schema()
        assert isinstance(schema, dict)
        assert "name" in schema
        assert "env_vars" in schema


# ---------------------------------------------------------------------------
# is_available() behavior
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """The tavily plugin's ``is_available()`` returns False without env config."""

    def test_tavily_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        assert p.is_available() is False
        monkeypatch.setenv("TAVILY_API_KEY", "real")
        assert p.is_available() is True


# ---------------------------------------------------------------------------
# Registry resolution semantics (Option B — conservative smart fallback)
# ---------------------------------------------------------------------------


class TestRegistryResolution:
    """``_resolve()`` follows explicit-config + availability-filtered fallback."""

    def test_explicit_configured_provider_returned_even_when_unavailable(
        self,
    ) -> None:
        """Explicit ``web.search_backend`` wins regardless of is_available().

        Without availability filtering on the explicit path, the dispatcher
        would silently switch backends; with this check the dispatcher
        surfaces a precise "TAVILY_API_KEY is not set" error instead.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        # No TAVILY_API_KEY (fixture cleared it).
        result = _resolve("tavily", capability="search")
        assert result is not None
        assert result.name == "tavily"
        # Confirm it's the unavailable one — dispatcher will surface
        # a typed credential-missing error to the caller.
        assert result.is_available() is False

    def test_unknown_configured_name_falls_back_to_available_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Typo / uninstalled plugin → walk legacy preference, pick available."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        monkeypatch.setenv("TAVILY_API_KEY", "real")
        result = _resolve("not-a-real-provider", capability="search")
        # The unknown name shouldn't return None when tavily is available.
        assert result is not None
        assert result.name == "tavily"
        assert result.is_available() is True

    def test_no_config_no_credentials_returns_none(
        self,
    ) -> None:
        """No backend configured AND no available providers → None.

        Tavily requires an API key; with none present the resolver returns
        None and the dispatcher surfaces a "set up a provider" error.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        result = _resolve(None, capability="search")
        assert result is None


# ---------------------------------------------------------------------------
# Sync-vs-async extract detection
# ---------------------------------------------------------------------------


class TestAsyncExtractDispatch:
    """The dispatcher detects async vs sync extract methods correctly."""

    def test_tavily_extract_is_sync(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        assert inspect.iscoroutinefunction(p.extract) is False


# ---------------------------------------------------------------------------
# Error response shape (preserved bit-for-bit from legacy)
# ---------------------------------------------------------------------------


class TestErrorResponseShapes:
    """When credentials are missing, the plugin returns typed errors, not raises."""

    def test_tavily_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_tavily_crawl_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        result = p.crawl("https://example.com")
        assert isinstance(result, dict)
        assert "results" in result
        assert isinstance(result["results"], list)
        if result["results"]:
            assert "error" in result["results"][0]
