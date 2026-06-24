import pytest
from unittest.mock import patch, MagicMock


def test_dispatcher_stores_platform():
    from grove.dispatcher import Dispatcher
    d = Dispatcher(platform="telegram")
    assert d._platform == "telegram"


def test_dispatcher_platform_defaults_to_cli():
    from grove.dispatcher import Dispatcher
    d = Dispatcher()
    assert d._platform == "cli"


def test_inject_core_tools_param_removed():
    """inject_core_tools is gone — passing it must raise TypeError."""
    from grove.dispatcher import Dispatcher
    with pytest.raises(TypeError):
        Dispatcher(inject_core_tools=True)


def test_get_authorized_tools_calls_get_admitted_tools():
    from grove.dispatcher import Dispatcher
    d = Dispatcher(platform="telegram")

    admitted = {"web_search", "terminal"}
    fake_defs = [
        {"type": "function", "function": {"name": "web_search"}},
        {"type": "function", "function": {"name": "terminal"}},
        {"type": "function", "function": {"name": "discord"}},  # not admitted
    ]

    with patch("grove.tool_admission.get_admitted_tools", return_value=admitted), \
         patch("model_tools.get_tool_definitions", return_value=fake_defs):
        result = d.get_authorized_tools()

    names = {t["function"]["name"] for t in result}
    assert "web_search" in names
    assert "terminal" in names
    assert "discord" not in names


def test_enabled_toolsets_secondary_filter_restricts_admitted():
    """enabled_toolsets acts as an intersection filter when provided."""
    from grove.dispatcher import Dispatcher
    d = Dispatcher(platform="telegram")

    admitted = {"web_search", "terminal", "memory"}
    fake_defs = [
        {"type": "function", "function": {"name": "web_search"}},
        {"type": "function", "function": {"name": "terminal"}},
        {"type": "function", "function": {"name": "memory"}},
    ]

    with patch("grove.tool_admission.get_admitted_tools", return_value=admitted), \
         patch("model_tools.get_tool_definitions", return_value=fake_defs), \
         patch("toolsets.resolve_toolset", return_value=["memory"]):
        result = d.get_authorized_tools(enabled_toolsets=["memory"])

    names = {t["function"]["name"] for t in result}
    assert "memory" in names
    assert "web_search" not in names
    assert "terminal" not in names


def test_get_authorized_tools_respects_disabled_toolsets():
    from grove.dispatcher import Dispatcher
    from unittest.mock import patch
    d = Dispatcher(platform="telegram")

    admitted = {"web_search", "terminal", "patch"}
    fake_defs = [
        {"type": "function", "function": {"name": "web_search"}},
        {"type": "function", "function": {"name": "terminal"}},
        {"type": "function", "function": {"name": "patch"}},
    ]

    with patch("grove.tool_admission.get_admitted_tools", return_value=admitted), \
         patch("model_tools.get_tool_definitions", return_value=fake_defs), \
         patch("toolsets.resolve_toolset", return_value=["patch"]):
        result = d.get_authorized_tools(disabled_toolsets=["file"])

    names = {t["function"]["name"] for t in result}
    assert "web_search" in names
    assert "terminal" in names
    assert "patch" not in names
