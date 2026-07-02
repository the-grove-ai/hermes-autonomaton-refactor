"""R7 (browser-read-surface-v1) — fail-loud duplicate-server-key guard.

Interim guard from mcp-server-id-roundtrip-v1: two configured mcp_servers whose
sanitized names collide before the first underscore derive the same admission key,
so one server's tools would silently admit/attribute under the other's id. The
guard reuses the SAME derivation as live admission (sanitize + _mcp_server_of) so
it predicts real collisions rather than approximating them.
"""

from __future__ import annotations

import pytest

from grove.context_budget import _mcp_server_of
from tools.mcp_tool import (
    _assert_no_derived_key_collision,
    _derived_admission_key,
    sanitize_mcp_name_component,
    McpServerKeyCollision,
)


def test_current_live_config_passes():
    # {notion, grove-browser} -> {notion, grove}: no collision (today's config).
    _assert_no_derived_key_collision({"notion": {}, "grove-browser": {}})


def test_future_grove_social_collides_and_fails_loud():
    # grove-browser and grove-social both sanitize to grove_* -> derive "grove".
    with pytest.raises(McpServerKeyCollision) as exc:
        _assert_no_derived_key_collision({"grove-browser": {}, "grove-social": {}})
    assert "grove" in str(exc.value)
    assert "grove-browser" in str(exc.value) and "grove-social" in str(exc.value)


def test_derived_key_values():
    assert _derived_admission_key("notion") == "notion"
    assert _derived_admission_key("grove-browser") == "grove"
    assert _derived_admission_key("grove-social") == "grove"


@pytest.mark.parametrize("server", ["notion", "grove-browser", "grove-social", "some_other_node"])
def test_guard_derivation_matches_real_admission(server):
    """Drift guard: the key the guard derives MUST equal what _mcp_server_of
    returns for a real registered tool name (mcp_<sanitized-server>_<tool>), the
    exact form _convert_mcp_schema produces. If these ever diverge, the guard
    would mispredict live collisions."""
    real_tool = f"mcp_{sanitize_mcp_name_component(server)}_{sanitize_mcp_name_component('browser_search')}"
    assert _derived_admission_key(server) == _mcp_server_of(real_tool)


def test_single_server_never_collides():
    _assert_no_derived_key_collision({"notion": {}})
    _assert_no_derived_key_collision({})


def test_collision_propagates_through_load_mcp_config(monkeypatch):
    """The chokepoint's broad `except Exception: return {}` must NOT swallow a
    collision — it fails loud instead of silently degrading to an empty set."""
    import hermes_cli.config as hc
    from tools import mcp_tool

    monkeypatch.setattr(
        hc, "load_config",
        lambda *a, **k: {"mcp_servers": {"grove-browser": {"url": "x"}, "grove-social": {"url": "y"}}},
    )
    with pytest.raises(McpServerKeyCollision):
        mcp_tool._load_mcp_config()


def test_valid_config_loads_through_chokepoint(monkeypatch):
    import hermes_cli.config as hc
    from tools import mcp_tool

    monkeypatch.setattr(
        hc, "load_config",
        lambda *a, **k: {"mcp_servers": {"notion": {"url": "n"}, "grove-browser": {"url": "b"}}},
    )
    out = mcp_tool._load_mcp_config()
    assert set(out) == {"notion", "grove-browser"}  # no collision -> loads normally
