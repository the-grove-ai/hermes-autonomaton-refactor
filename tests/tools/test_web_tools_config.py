"""Tests for web backend selection and availability checks.

Coverage:
  _get_backend() — backend selection logic with env var combinations.
  check_web_api_key() — unified availability check.
  Auxiliary-model resolution — check_auxiliary_model / summarizer re-resolution.

After the hermes-severance, Tavily is the sole bundled web provider; the
firecrawl / parallel / exa client-construction suites were removed along
with those plugins.
"""

import importlib
import json
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestAuxiliaryResolution:
    """Auxiliary-model availability re-resolves per call (not pinned to import)."""

    def test_nous_auth_token_respects_hermes_home_override(self, tmp_path):
        """Auth lookup should read from GROVE_HOME/auth.json, not ~/.grove/auth.json."""
        real_home = tmp_path / "real-home"
        (real_home / ".grove").mkdir(parents=True)

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "auth.json").write_text(json.dumps({
            "providers": {
                "nous": {
                    "access_token": "nous-token",
                }
            }
        }))

        with patch.dict(os.environ, {
            "HOME": str(real_home),
            "GROVE_HOME": str(hermes_home),
        }, clear=False):
            import tools.web_tools
            importlib.reload(tools.web_tools)
            assert tools.web_tools._read_nous_access_token() == "nous-token"

    def test_check_auxiliary_model_re_resolves_backend_each_call(self):
        """Availability checks should not be pinned to module import state."""
        import tools.web_tools

        # Simulate the pre-fix import-time cache slot for regression coverage.
        tools.web_tools.__dict__["_aux_async_client"] = None

        with patch(
            "tools.web_tools.get_async_text_auxiliary_client",
            side_effect=[(None, None), (MagicMock(base_url="https://api.openrouter.ai/v1"), "test-model")],
        ):
            assert tools.web_tools.check_auxiliary_model() is False
            assert tools.web_tools.check_auxiliary_model() is True

    @pytest.mark.asyncio
    async def test_summarizer_re_resolves_backend_after_initial_unavailable_state(self):
        """Summarization should pick up a backend that becomes available later in-process."""
        import tools.web_tools

        tools.web_tools.__dict__["_aux_async_client"] = None

        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="summary text"))]

        with patch(
            "tools.web_tools._resolve_web_extract_auxiliary",
            side_effect=[(None, None, {}), (MagicMock(base_url="https://api.openrouter.ai/v1"), "test-model", {})],
        ), patch(
            "tools.web_tools.async_call_llm",
            new=AsyncMock(return_value=response),
        ) as mock_async_call:
            assert tools.web_tools.check_auxiliary_model() is False
            result = await tools.web_tools._call_summarizer_llm(
                "Some content worth summarizing",
                "Source: https://example.com\n\n",
                None,
            )

        assert result == "summary text"
        mock_async_call.assert_awaited_once()


class TestBackendSelection:
    """Test suite for _get_backend() backend selection logic.

    The backend is configured via config.yaml (web.backend), set by
    ``hermes tools``. Tavily is the sole bundled backend; selection falls
    back to key-based detection for legacy / manual setups.
    """

    _ENV_KEYS = (
        "TAVILY_API_KEY",
    )

    def setup_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def teardown_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    # ── Config-based selection (web.backend in config.yaml) ───────────

    def test_config_tavily(self):
        """web.backend=tavily in config → 'tavily' regardless of other keys."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}):
            assert _get_backend() == "tavily"

    def test_config_tavily_overrides_env_keys(self):
        """web.backend=tavily in config → 'tavily' even with no matching key."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "tavily"}):
            assert _get_backend() == "tavily"

    def test_config_tavily_case_insensitive(self):
        """web.backend=Tavily (mixed case) → 'tavily'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "Tavily"}):
            assert _get_backend() == "tavily"

    # ── Fallback (no web.backend in config) ───────────────────────────

    def test_fallback_tavily_only_key(self):
        """Only TAVILY_API_KEY set → 'tavily'."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            assert _get_backend() == "tavily"

    def test_fallback_no_keys_defaults_to_tavily(self):
        """No keys, no config → 'tavily' (sole bundled backend; fails at dispatch)."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={}):
            assert _get_backend() == "tavily"

    def test_invalid_config_falls_through_to_fallback(self):
        """web.backend=invalid → ignored, uses key-based fallback → tavily."""
        from tools.web_tools import _get_backend
        with patch("tools.web_tools._load_web_config", return_value={"backend": "nonexistent"}), \
             patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            assert _get_backend() == "tavily"


class TestWebSearchSchema:
    """Test suite for web_search tool schema and handler wiring."""

    def test_schema_exposes_optional_limit(self):
        import tools.web_tools

        limit_schema = tools.web_tools.WEB_SEARCH_SCHEMA["parameters"]["properties"]["limit"]

        assert limit_schema["type"] == "integer"
        assert limit_schema["minimum"] == 1
        assert limit_schema["maximum"] == 100
        assert limit_schema["default"] == 5
        assert "limit" not in tools.web_tools.WEB_SEARCH_SCHEMA["parameters"]["required"]

    def test_registered_handler_passes_limit(self):
        import tools.web_tools

        from tools.registry import ToolRegistry, register_builtin_tools
        _reg = ToolRegistry()
        register_builtin_tools(_reg)
        entry = _reg.get_entry("web_search")
        with patch("tools.web_tools.web_search_tool", return_value='{"success": true}') as mock_search:
            result = entry.handler({"query": "site:example.com docs", "limit": 12})

        assert result == '{"success": true}'
        mock_search.assert_called_once_with("site:example.com docs", limit=12)

    def test_registered_handler_defaults_limit_to_five(self):
        import tools.web_tools

        from tools.registry import ToolRegistry, register_builtin_tools
        _reg = ToolRegistry()
        register_builtin_tools(_reg)
        entry = _reg.get_entry("web_search")
        with patch("tools.web_tools.web_search_tool", return_value='{"success": true}') as mock_search:
            result = entry.handler({"query": "docs"})

        assert result == '{"success": true}'
        mock_search.assert_called_once_with("docs", limit=5)

    def test_web_search_clamps_limit_before_backend_call(self):
        import tools.web_tools

        # The tool dispatcher resolves a provider from the registry and calls
        # provider.search(query, limit). Mock the provider lookup so we can
        # assert the limit is clamped before reaching the backend.
        fake_search = MagicMock(return_value={"success": True, "data": {"web": []}})
        fake_provider = MagicMock(
            name="TavilyWebSearchProvider",
            supports_search=MagicMock(return_value=True),
        )
        fake_provider.search = fake_search
        fake_provider.name = "tavily"

        with patch("tools.web_tools._get_search_backend", return_value="tavily"), \
             patch("agent.web_search_registry.get_provider", return_value=fake_provider), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch.object(tools.web_tools._debug, "log_call"), \
             patch.object(tools.web_tools._debug, "save"):
            result = json.loads(tools.web_tools.web_search_tool("docs", limit=500))

        assert result == {"success": True, "data": {"web": []}}
        fake_search.assert_called_once_with("docs", 100)


class TestWebSearchErrorHandling:
    """Test suite for web_search_tool() error responses."""

    def test_search_error_response_does_not_expose_diagnostics(self):
        import tools.web_tools

        # Mock the registry's get_provider to return a fake provider whose
        # .search() raises so we can verify error sanitization.
        fake_provider = MagicMock(
            name="TavilyWebSearchProvider",
            supports_search=MagicMock(return_value=True),
        )
        fake_provider.search.side_effect = RuntimeError("boom")
        fake_provider.name = "tavily"

        with patch("tools.web_tools._get_search_backend", return_value="tavily"), \
             patch("agent.web_search_registry.get_provider", return_value=fake_provider), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch.object(tools.web_tools._debug, "log_call") as mock_log_call, \
             patch.object(tools.web_tools._debug, "save"):
            result = json.loads(tools.web_tools.web_search_tool("test query", limit=3))

        assert result == {"error": "Error searching web: boom"}

        debug_payload = mock_log_call.call_args.args[1]
        assert debug_payload["error"] == "Error searching web: boom"
        assert "traceback" not in debug_payload["error"]
        assert "exception_type" not in debug_payload["error"]
        assert "config" not in result
        assert "exception_type" not in result
        assert "exception_chain" not in result
        assert "traceback" not in result


class TestCheckWebApiKey:
    """Test suite for check_web_api_key() availability check."""

    _ENV_KEYS = (
        "TAVILY_API_KEY",
    )

    def setup_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def teardown_method(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)

    def test_tavily_key_only(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            from tools.web_tools import check_web_api_key
            assert check_web_api_key() is True

    def test_no_keys_returns_false(self):
        from tools.web_tools import check_web_api_key
        assert check_web_api_key() is False


def test_web_requires_env_includes_tavily_key():
    from tools.web_tools import _web_requires_env

    assert "TAVILY_API_KEY" in _web_requires_env()
