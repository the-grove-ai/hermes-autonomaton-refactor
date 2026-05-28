"""Sprint 30 regression: escalate must reach the runtime API tools array.

The original Sprint 30 unit tests in test_escalate_tool.py exercise the
synthetic tool in isolation via an explicit `import tools.escalate_tool`
side-effect. That masked the runtime gap discovered 2026-05-28 during
live-driver verification: the escalate toolset name was registered (via
discover_builtin_tools) but neither in toolsets._GROVE_CORE_TOOLS nor in
the toolsets.TOOLSETS dict, AND its check_fn returned a falsy `{}`. The
toolset never reached _get_platform_tools(...)'s enabled_toolsets, never
reached the LLM's API tools array, and Sonnet correctly reported its
absence.

This integration test asserts the full chain — discovery -> platform
toolset enablement -> get_tool_definitions -> API tools array — without
any test-time import of tools.escalate_tool.
"""

from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions
from tools.registry import discover_builtin_tools, registry


def test_escalate_reaches_cli_api_tools_array():
    discover_builtin_tools()

    assert registry.is_toolset_available("escalate"), (
        "escalate toolset reports unavailable — check_escalate_requirements "
        "must return a truthy value (True), not a falsy empty dict"
    )

    cfg = load_config()
    enabled = _get_platform_tools(cfg, "cli")
    assert "escalate" in enabled, (
        f"escalate toolset missing from CLI enabled_toolsets after "
        f"_get_platform_tools — toolsets returned: {sorted(enabled)}"
    )

    defs = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
    names = {d["function"]["name"] for d in defs if "function" in d}
    assert "escalate" in names, (
        f"escalate tool missing from API tools array — Sprint 30 escalation "
        f"loop is broken at the LLM-visible surface. "
        f"Got {len(names)} tools: {sorted(names)}"
    )
