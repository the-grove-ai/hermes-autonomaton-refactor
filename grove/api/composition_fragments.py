"""Operator Portal — Composition panel HTML fragment (R2″ node-compositor-view-v1).

Renders the LIVE GRV-004 composition state as an HTMX panel: which nodes the
engine composes, their tools, the authority inversion between each tool's
node-proposed zone and the engine-granted zone, and per-node health. Consumes
the SAME accessor as the P-series JSON API (``get_composition_status``) and the
SAME mtime-cached zone map (``portal._get_zone_map``), so the panel and the JSON
endpoint never diverge.

Design invariants (held by this module):
  I1 — GENERIC RENDERING. One card template, driven entirely by the accessor
       payload. No conditional keys on a specific server/node name; the only
       branch is data-driven on ``is_composed`` (composed vs. dark node).
  I3 — AUTHORITY INVERSION VISIBLE. Every tool shows its ``proposed_zone`` and
       ``granted_zone``. When they differ, the row renders ``proposed → granted``
       with the granted zone as the colored primary badge; when they match, a
       single granted badge (no redundant duplication).
  I4 — DARK NODES VISIBLE. Configured MCP servers without a GRV-004 declaration
       render as a muted card carrying "No GRV-004 declaration", so the operator
       sees the full mesh footprint, not just declared nodes.

CSS REUSE. Health "dots" are realized as the existing ``badge-green`` /
``badge-yellow`` / ``badge-red`` chips, and rows reuse ``card`` / ``meta`` /
``tag`` / ``badge`` — ``style.css`` is not modified by this sprint. ``_esc`` and
``_html_fragment`` are imported from grove.api.fragments so escaping and the
text/html response envelope match every other fragment.
"""

from __future__ import annotations

import logging

from aiohttp import web

from grove.api.fragments import _ZONE_BADGE, _esc, _html_fragment
from grove.api.portal import _get_zone_map
from grove.composition.declaration import get_composition_status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Render helpers (pure functions of the accessor payload — no node-name logic)
# ---------------------------------------------------------------------------


def _health_badge(node: dict) -> str:
    """Health indicator as a colored badge (the "dot + label" the SPEC calls
    for, realized with the existing badge-* classes).

    healthy + connected  -> green  "Connected"
    healthy + not conn.   -> neutral "Configured"  (truthful: green "Connected"
                              would misreport a configured-but-unconnected server)
    connect_failed        -> red    "<failure type>" (reauth / unreachable)
    breaker_open          -> yellow "Circuit breaker open"
    """
    health = node.get("health")
    if health == "connect_failed":
        label = node.get("connect_failure_type") or "connect failed"
        return f'<span class="badge badge-red">{_esc(label)}</span>'
    if health == "breaker_open":
        return '<span class="badge badge-yellow">Circuit breaker open</span>'
    # healthy
    if node.get("is_connected"):
        return '<span class="badge badge-green">Connected</span>'
    return '<span class="badge">Configured</span>'


def _tool_row(tool: dict) -> str:
    """One tool row showing the authority inversion (I3).

    proposed != granted -> ``proposed → [granted badge]`` (engine overrode)
    proposed == granted -> single granted badge
    granted is None     -> proposed badge + "no engine rule" (no schema entry;
                            the proposal is unmatched, not silently dropped)
    """
    name = tool.get("name")
    proposed = tool.get("proposed_zone")
    granted = tool.get("granted_zone")

    if granted is None:
        zone_html = (
            f'<span class="badge {_ZONE_BADGE.get(proposed, "")}">{_esc(proposed)}</span> '
            f'<span class="meta">proposed &middot; no engine rule</span>'
        )
    elif proposed != granted:
        zone_html = (
            f'<span class="tag">{_esc(proposed)}</span>'
            f'<span class="zone-arrow"> &rarr; </span>'
            f'<span class="badge {_ZONE_BADGE.get(granted, "")}">{_esc(granted)}</span>'
        )
    else:
        zone_html = f'<span class="badge {_ZONE_BADGE.get(granted, "")}">{_esc(granted)}</span>'

    return (
        f'<li class="tool-row">'
        f'<span class="tool-name">{_esc(name)}</span> {zone_html}'
        f'</li>'
    )


def _node_card(node: dict) -> str:
    """One composition card. Data-driven; the only branch is ``is_composed``
    (composed node vs. dark node), never a specific node name (I1)."""
    is_composed = node.get("is_composed")
    # Identity: declared node_id for composed nodes; the raw server_name for
    # dark nodes (which have no declaration).
    identity = node.get("node_id") if is_composed else node.get("server_name")
    card_cls = "card composition-card" if is_composed else "card composition-card dark-node"

    header = f'<h4>{_esc(identity)} '
    if is_composed:
        version = node.get("version")
        proto = node.get("grv_standard")
        if version:
            header += f'<span class="badge">v{_esc(version)}</span> '
        if proto:
            header += f'<span class="badge">{_esc(proto)}</span> '
    header += _health_badge(node) + "</h4>"

    parts = [f'<div class="{card_cls}">', header]

    url = node.get("url")
    if url:
        parts.append(f'<div class="meta">{_esc(url)}</div>')

    if is_composed:
        tools = node.get("tools") or []
        if tools:
            parts.append('<ul class="tool-list listing">')
            parts.extend(_tool_row(t) for t in tools)
            parts.append("</ul>")
        else:
            parts.append('<div class="meta">No tools declared.</div>')
    else:
        # I4 — the operator sees the dark node and why it is dark.
        parts.append('<div class="meta dark-note">No GRV-004 declaration</div>')

    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Panel route
# ---------------------------------------------------------------------------


async def handle_composition_panel(request: web.Request) -> web.Response:
    """Render the composition panel: composed nodes first (by node_id), then
    dark nodes (by server_name).

    Reads runtime state via ``get_composition_status`` (I2: live globals, not
    compose-with.json). Both accessor inputs are resolved outside the engine
    lock — the mtime-cached zone map (C3) and a single mcp_servers config read —
    exactly as the JSON endpoint does.
    """
    zone_map = _get_zone_map(request.app)
    # Lazy import keeps the heavy tools.mcp_tool subsystem off this module's
    # import path (mirrors handle_composition_nodes). {} when no config present.
    from tools.mcp_tool import _load_mcp_config

    mcp_servers_config = _load_mcp_config()
    nodes = get_composition_status(
        mcp_servers_config=mcp_servers_config,
        zone_map=zone_map,
    )

    composed = sorted(
        (n for n in nodes if n.get("is_composed")),
        key=lambda n: (n.get("node_id") or "").lower(),
    )
    dark = sorted(
        (n for n in nodes if not n.get("is_composed")),
        key=lambda n: (n.get("server_name") or "").lower(),
    )
    ordered = composed + dark

    parts = ['<div id="composition-panel">', "<h2>Composition</h2>"]
    if not ordered:
        parts.append('<p class="placeholder">No MCP servers configured.</p>')
    else:
        parts.extend(_node_card(n) for n in ordered)
    parts.append("</div>")
    return _html_fragment("".join(parts))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_composition_routes(app: web.Application) -> None:
    """Register the composition panel fragment route."""
    app.router.add_get("/portal/fragments/composition/panel", handle_composition_panel)
