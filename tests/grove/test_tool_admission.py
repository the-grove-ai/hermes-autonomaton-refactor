import pytest
from unittest.mock import MagicMock, patch
from grove.tool_admission import get_admitted_tools


def _make_cap(cap_id, tools, platform="all"):
    cap = MagicMock()
    cap.id = cap_id
    cap.platform = platform
    cap.bindings = MagicMock()
    cap.bindings.tools = tools
    return cap


def _make_registry(tool_names):
    reg = MagicMock()
    reg._tools = {name: MagicMock() for name in tool_names}
    return reg


CAPS = {
    "web": _make_cap("web", ["web_search", "web_extract"]),
    "discord": _make_cap("discord", ["discord"], platform=["discord"]),
    "terminal": _make_cap("terminal", ["terminal"]),
}


def test_all_platform_excludes_platform_restricted_tools():
    reg = _make_registry(["web_search", "web_extract", "discord", "terminal"])
    with patch("grove.tool_admission.load_capabilities", return_value=CAPS):
        result = get_admitted_tools(reg, "telegram", {})
    assert "web_search" in result
    assert "web_extract" in result
    assert "terminal" in result
    assert "discord" not in result  # platform: [discord] only


def test_discord_platform_admits_discord_tools():
    reg = _make_registry(["web_search", "discord"])
    with patch("grove.tool_admission.load_capabilities", return_value=CAPS):
        result = get_admitted_tools(reg, "discord", {})
    assert "discord" in result
    assert "web_search" in result  # platform: all still included


def test_empty_config_returns_all_admitted():
    reg = _make_registry(["web_search"])
    with patch("grove.tool_admission.load_capabilities", return_value={"web": CAPS["web"]}):
        result = get_admitted_tools(reg, "telegram", {})
    assert result == {"web_search", "web_extract"}


def test_user_opt_out_removes_tool():
    reg = _make_registry(["web_search", "web_extract"])
    with patch("grove.tool_admission.load_capabilities", return_value={"web": CAPS["web"]}):
        result = get_admitted_tools(reg, "telegram", {
            "blocked_tools": {"telegram": ["web_extract"]}
        })
    assert "web_search" in result
    assert "web_extract" not in result


def test_legacy_toolset_name_warns_and_expands():
    reg = _make_registry(["web_search"])
    with patch("grove.tool_admission.load_capabilities", return_value={}), \
         patch("grove.tool_admission._build_legacy_map", return_value={"hermes-cli": ["web_search", "terminal"]}):
        import logging
        with patch.object(logging.getLogger("grove.tool_admission"), "warning") as mock_warn:
            result = get_admitted_tools(reg, "telegram", {
                "platform_toolsets": {"telegram": ["hermes-cli"]}
            })
        mock_warn.assert_called_once()
        assert "migrate" in mock_warn.call_args[0][0].lower()
    assert "web_search" in result
    assert "terminal" in result


def test_capability_load_failure_raises():
    reg = _make_registry([])
    with patch("grove.tool_admission.load_capabilities", side_effect=RuntimeError("bad caps")):
        with pytest.raises(RuntimeError):
            get_admitted_tools(reg, "telegram", {})
