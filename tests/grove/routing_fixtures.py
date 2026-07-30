"""Relocated v1→v2 routing fixture translator (hermes-severance-v1).

The product module ``grove/config/routing_migrate.py`` was deleted at severance
(no v1 instance state exists anywhere; GRV-001 v2.0 is the only shape the loaders
accept). Its ``to_v2_split`` transform was the sole reusable v1→v2 fixture builder
shared across the routing test suite, so it is preserved here as a TEST-ONLY
helper. The census in :func:`_build_split` remains the single source of the split
(no caller hand-sorts keys), exactly as it was in the product tool.
"""
from __future__ import annotations

from io import StringIO
from typing import Any, Dict, Tuple, Union

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

_V2 = "2.0"

# ── census tables (verbatim from the retired grove/config/routing_migrate.py) ──
_OPERATIONAL_FROM_ROUTING = (
    "default_tier",
    "tier_preferences",
    "model_facts",
    "routing_rules",
    "telemetry",
    "provider_routing",
)
_OPERATIONAL_TOPLEVEL = ("pattern_cache", "goal_attachment")
_AUTHORITY_TOPLEVEL = ("tier_budgets",)
_DROPPED_FROM_ROUTING = ("zone_overrides",)


class MigrationError(RuntimeError):
    """The v1 doc cannot be split (not a mapping / no ``routing:`` block)."""


def _ruamel() -> YAML:
    """Round-trip parser tuned to the routing file layout, so the comment-dense
    config survives the split without reflow."""
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y


def _build_split(v1_doc: Any) -> Tuple[CommentedMap, CommentedMap, Dict[str, str]]:
    """Return (operational_doc, authority_doc, disposition) from a loaded v1 doc.

    ``disposition`` maps each censused v1 key to ``"operational"`` /
    ``"authority"`` / ``"dropped"`` — only keys actually present in v1 appear.
    """
    if not isinstance(v1_doc, dict):
        raise MigrationError("v1 routing config did not parse to a mapping")
    routing = v1_doc.get("routing")
    if not isinstance(routing, dict):
        raise MigrationError(
            "v1 routing config has no top-level 'routing:' mapping — not a v1 "
            "file (already migrated, or malformed)"
        )

    operational: CommentedMap = CommentedMap()
    operational["schema_version"] = _V2
    operational["surface_class"] = "in_scope"
    operational["writable_on"] = "autonomous_loop"

    authority: CommentedMap = CommentedMap()
    authority["schema_version"] = _V2
    authority["surface_class"] = "scope_defining"
    authority["writable_on"] = "operator_authenticated"
    authority["default_zone"] = "red"

    disposition: Dict[str, str] = {}

    for key in _OPERATIONAL_FROM_ROUTING:
        if key in routing:
            operational[key] = routing[key]  # moves the value node + its comments
            disposition[key] = "operational"
    for key in _OPERATIONAL_TOPLEVEL:
        if key in v1_doc:
            operational[key] = v1_doc[key]
            disposition[key] = "operational"

    # escalation.threshold → authority.escalation_threshold (flatten). The rest
    # of the v1 ``escalation`` block (prose ``description``) is not load-bearing
    # and is not carried.
    esc = routing.get("escalation")
    if isinstance(esc, dict) and "threshold" in esc:
        authority["escalation_threshold"] = esc["threshold"]
        disposition["escalation_threshold"] = "authority"
    for key in _AUTHORITY_TOPLEVEL:
        if key in v1_doc:
            authority[key] = v1_doc[key]
            disposition[key] = "authority"
    if "escalation_policy" in routing:
        authority["escalation_policy"] = routing["escalation_policy"]
        disposition["escalation_policy"] = "authority"

    for key in _DROPPED_FROM_ROUTING:
        if key in routing:
            disposition[key] = "dropped"

    return operational, authority, disposition


def _dump_to_str(doc: Any, yaml_rt: YAML) -> str:
    """Serialize a split doc to a YAML string via the same ruamel writer the
    migrator used on disk — so fixtures emit byte-comparable v2."""
    buf = StringIO()
    yaml_rt.dump(doc, buf)
    return buf.getvalue()


def to_v2_split(v1: Union[str, Dict[str, Any]]) -> Tuple[str, str]:
    """The single reusable v1→v2 split transform (relocated fixture helper).

    Accepts a loaded v1 mapping (a top-level ``routing:`` block) or a v1 YAML
    string, and returns the ``(operational_yaml, authority_yaml)`` text pair,
    each serialized through the ruamel writer. Only keys actually present in the
    input are carried; ``zone_overrides`` is dropped (§VII).
    """
    yaml_rt = _ruamel()
    doc = yaml_rt.load(v1) if isinstance(v1, str) else v1
    operational, authority, _disposition = _build_split(doc)
    return _dump_to_str(operational, yaml_rt), _dump_to_str(authority, yaml_rt)
