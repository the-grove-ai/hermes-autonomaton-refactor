"""
grove/tool_admission.py — capability record as sole tool-admission authority.

Replaces the enabled_toolsets / _get_platform_tools() path. Called by
Dispatcher.get_authorized_tools() to determine which tools a platform
surface receives this session.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Set

from grove.capability_registry import load_capabilities

if TYPE_CHECKING:
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _build_legacy_map() -> dict[str, list[str]]:
    """Build composite-toolset-name → tool-name mapping from toolsets.py.

    Used only to translate old platform_toolsets config values like
    'hermes-cli' or 'hermes-telegram'. The composite declarations in
    toolsets.py are preserved (ANDON A1 — run_agent.py:3465 reads
    'google-workspace' toolset from live registry). DELETE this function
    when operator configs are fully migrated to capability IDs.
    """
    try:
        from toolsets import TOOLSETS
        return {
            name: list(defn.get("tools", []))
            for name, defn in TOOLSETS.items()
            if name.startswith("hermes-")
        }
    except Exception as exc:
        logger.warning("tool_admission: could not build legacy toolset map: %r", exc)
        return {}


def get_admitted_tools(
    registry: "ToolRegistry",
    platform: str,
    user_config: dict,
) -> Set[str]:
    """Return the set of tool names admitted for *platform* this session.

    Authority chain (applied in order):
    1. Capability records filtered by platform field.
    2. Legacy toolset-name shim — translates old hermes-* composite names
       in config.platform_toolsets with a deprecation warning.
    3. User opt-in via extra_capabilities[platform].
    4. User opt-out via blocked_tools[platform].

    Raises on capability registry load failure (Fail Loud — a broken
    registry must surface, not silently yield an empty tool surface).
    """
    caps = load_capabilities()  # raises on failure — intentional

    # ── 1. Capability-record filter ──────────────────────────────────────
    admitted: Set[str] = set()
    for cap in caps.values():
        p = cap.platform
        if p == "all" or platform in p:
            admitted.update(cap.bindings.tools)

    # ── 2. Legacy config shim ────────────────────────────────────────────
    legacy_names = (user_config.get("platform_toolsets") or {}).get(platform) or []
    if not isinstance(legacy_names, list):
        legacy_names = [str(legacy_names)]

    if legacy_names:
        legacy_map = _build_legacy_map()
        for ts_name in legacy_names:
            if ts_name in legacy_map:
                logger.warning(
                    "tool_admission: platform_toolsets uses legacy toolset "
                    "name %r — migrate config.yaml to capability IDs.",
                    ts_name,
                )
                admitted.update(legacy_map[ts_name])

    # ── 3. User opt-in ───────────────────────────────────────────────────
    for cap_id in (user_config.get("extra_capabilities") or {}).get(platform, []):
        if cap_id in caps:
            admitted.update(caps[cap_id].bindings.tools)
        else:
            logger.warning(
                "tool_admission: extra_capabilities references unknown "
                "capability id %r — skipping.", cap_id
            )

    # ── 4. User opt-out ──────────────────────────────────────────────────
    admitted -= set((user_config.get("blocked_tools") or {}).get(platform, []))

    return admitted
