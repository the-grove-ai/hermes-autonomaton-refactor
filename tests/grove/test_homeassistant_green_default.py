"""operator-mutable-admission-v1 Phase 2 — homeassistant_read green default.

homeassistant_read governs GREEN reads (ha_list_entities / ha_list_services) but
was intent-gated to system_admin — the wrong repo default for a green read. The
green-reads-are-offered principle (matched to x_search: green, proactive,
always:true) de-gates it: offered on every classified turn AND the unknown core,
like read_file / search_files / web_extract / x_search.
"""
from __future__ import annotations

from grove.context_budget import _registry_allowed_names, reset_caps_index_cache

_HA_TOOLS = ("ha_list_entities", "ha_list_services")


def _offered(intent):
    reset_caps_index_cache()
    names, _ = _registry_allowed_names(intent, "moderate", current_tier=None)
    return names


def test_homeassistant_read_offered_on_non_system_admin_intents():
    for intent in ("conversation", "research", "creative_writing"):
        offered = _offered(intent)
        for tool in _HA_TOOLS:
            assert tool in offered, f"{tool} must be offered on {intent!r} (green read)"


def test_homeassistant_read_rides_the_unknown_core():
    # A green always:true read is admitted even on the unknown/fallback turn.
    offered = _offered("unknown")
    for tool in _HA_TOOLS:
        assert tool in offered, f"{tool} must ride the unknown core (green read)"
