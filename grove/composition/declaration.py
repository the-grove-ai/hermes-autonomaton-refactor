"""GRV-004 node declaration fetch — engine-composer-v1 Phase 1.

The engine (hermes-gateway) composes external capability nodes (e.g.
grove-browser) over the existing ``mcp_servers`` registry. Before a node's
tools are dialed in, the engine fetches the node's SERVED GRV-004
declaration and reads it as a *proposal*.

Authority inversion (Invariant 1): the declaration is advisory only. The
engine's ``zones.schema.yaml`` is the sole zone authority — a tool's
``proposed_zone`` here is honored ONLY if a matching engine rule agrees
(``grove/zones.py``). No node-authored policy enters core. This module
never classifies and never grants; it only fetches and parses.

Dark-node constraint: a failed fetch (timeout, HTTP error, malformed JSON,
missing required fields) is NOT an error — the node still connects and its
tools still dispatch under engine authority. The node is simply excluded
from composeWith derivation (Phase 4) until a declaration is cached. Fail
soft here; fail closed at the zone gate.

Wire->field mapping (observed against the live grove-browser declaration at
http://100.80.12.118:8830/.well-known/grove-autonomaton on 2026-06-27):

    NodeDeclaration.node_id        <- raw["edge"]["node"]
    NodeDeclaration.version        <- raw["version"]
    NodeDeclaration.grv_standard   <- raw["protocol"]              (e.g. "GRV-004")
    NodeDeclaration.proposed_tools <- [{"name": t["id"],
                                        "proposed_zone": t["zone"]}
                                       for t in raw["edge"]["tools"]]

A usable declaration requires ``protocol`` and ``edge.tools`` (the
authority-proposal surface). Absent either, the document is not a dialable
GRV-004 edge and the fetch returns None. ``version`` and ``edge.node`` fall
back to ``"unknown"`` without failing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

__all__ = [
    "NodeDeclaration",
    "fetch_node_declaration",
    "derive_compose_with",
    "publish_compose_with",
]

_WELL_KNOWN_PATH = "/.well-known/grove-autonomaton"


@dataclass(frozen=True)
class NodeDeclaration:
    """A composed node's SERVED GRV-004 declaration — a zone PROPOSAL.

    Frozen: a declaration is an immutable snapshot taken at connect. It is
    never mutated; a node that changes its declaration requires an engine
    restart to be re-read (see the lifecycle note in ``tools/mcp_tool.py``).
    """

    node_id: str
    version: str
    grv_standard: str
    proposed_tools: list[dict]   # each: {"name": str, "proposed_zone": str}
    raw: dict                    # full JSON response, verbatim


def _derive_declaration_url(base_url: str) -> str:
    """Strip any path from ``base_url`` and append the well-known path.

    ``http://host:8830/mcp`` -> ``http://host:8830/.well-known/grove-autonomaton``.
    """
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, _WELL_KNOWN_PATH, "", ""))


def _parse_declaration(
    payload: object, fallback_node_id: Optional[str] = None,
) -> Optional[NodeDeclaration]:
    """Map a raw declaration dict to a NodeDeclaration, or None if unusable.

    Required: ``protocol`` and a list-valued ``edge.tools`` (the
    authority-proposal surface). ``version`` falls back to ``"unknown"``.

    ``node_id`` resolves ``edge.node`` first; absent that, ``fallback_node_id``
    (the local ``mcp_servers`` config key, e.g. ``"grove-browser"``); absent
    both, ``"unknown"``.
    """
    if not isinstance(payload, dict):
        return None
    protocol = payload.get("protocol")
    edge = payload.get("edge")
    if not protocol or not isinstance(edge, dict):
        return None
    raw_tools = edge.get("tools")
    if not isinstance(raw_tools, list):
        return None
    proposed_tools: list[dict] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("id")
        if not name:
            continue
        proposed_tools.append({
            "name": name,
            "proposed_zone": tool.get("zone"),
        })
    return NodeDeclaration(
        node_id=str(edge.get("node") or fallback_node_id or "unknown"),
        version=str(payload.get("version") or "unknown"),
        grv_standard=str(protocol),
        proposed_tools=proposed_tools,
        raw=payload,
    )


async def fetch_node_declaration(
    base_url: str,
    declaration_url: Optional[str] = None,
    timeout: float = 5.0,
    fallback_node_id: Optional[str] = None,
) -> Optional[NodeDeclaration]:
    """Fetch and parse a composed node's GRV-004 declaration.

    Args:
        base_url: the node's MCP edge URL (e.g. the ``url`` from its
            ``mcp_servers`` config entry). Its path is stripped to derive
            the well-known location unless ``declaration_url`` is given.
        declaration_url: explicit declaration URL override. When provided,
            used verbatim (no derivation).
        timeout: per-request timeout in seconds.
        fallback_node_id: node_id to use when the declaration omits
            ``edge.node`` — the caller passes its local ``mcp_servers``
            config key (e.g. ``"grove-browser"``).

    Returns:
        A NodeDeclaration on success, or None on ANY failure. Never raises
        and never blocks MCP connect (the dark-node degradation path).
    """
    url = declaration_url or _derive_declaration_url(base_url)
    try:
        import aiohttp

        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        declaration = _parse_declaration(payload, fallback_node_id=fallback_node_id)
        if declaration is None:
            logger.warning(
                "Declaration fetch failed for %s: malformed or non-GRV-004 "
                "payload (missing protocol/edge.tools)",
                base_url,
            )
            return None
        logger.info(
            "Declaration fetched for %s: %d proposed tools",
            declaration.node_id,
            len(declaration.proposed_tools),
        )
        return declaration
    except Exception as exc:  # noqa: BLE001 — dark-node: any failure -> None
        logger.warning("Declaration fetch failed for %s: %r", base_url, exc)
        return None


# ── composeWith derivation (engine-composer-v1 Phase 4) ──────────────────────
#
# The engine publishes a DERIVED composeWith (Invariant 2): the list reflects
# only nodes it is actually composing AND that are currently health-passing —
# never the static config intent. This is the inverse direction from the node's
# own served composeWith; here the ENGINE attests which capability providers are
# live behind it. Written to ~/.grove/compose-with.json — distinct from
# mcp-children.json (the stdio-subprocess PID registry consumed by the orphan
# reaper, tools/mcp_tool.py:2231); the two are never conflated.


def _breaker_failed(server_name: str) -> bool:
    """True iff either MCP breaker currently marks ``server_name`` failed.

    Mirrors the live breaker semantics in tools/mcp_tool.py:
      * connect-level — name present in ``_server_connect_failed`` (read via the
        public accessor ``get_connect_failures()``, mcp_tool.py:1783).
      * per-call circuit breaker OPEN — error count >= threshold AND still inside
        the cooldown window: the same open-test the tool handler uses to
        short-circuit (mcp_tool.py:2653-2657).
    """
    import time

    from tools import mcp_tool as _m

    if server_name in _m.get_connect_failures():
        return True
    count = _m._server_error_counts.get(server_name, 0)
    if count >= _m._CIRCUIT_BREAKER_THRESHOLD:
        opened_at = _m._server_breaker_opened_at.get(server_name, 0.0)
        if (time.monotonic() - opened_at) < _m._CIRCUIT_BREAKER_COOLDOWN_SEC:
            return True
    return False


def _server_url(server_name: str, declaration: "NodeDeclaration") -> Optional[str]:
    """The dialed URL for ``server_name``.

    Primary: the live MCPServerTask config (``tools/mcp_tool._servers[name].
    _config``, set at mcp_tool.py:1487; ``"url"`` key per ``_is_http`` :1009).
    Fallback: the node's self-declared ``edge.endpoint`` from the cached
    declaration. Returns None if neither is available.
    """
    from tools import mcp_tool as _m

    server = _m._servers.get(server_name)
    if server is not None:
        cfg = getattr(server, "_config", None) or {}
        url = cfg.get("url")
        if url:
            return url
    edge = declaration.raw.get("edge")
    if isinstance(edge, dict) and edge.get("endpoint"):
        return edge.get("endpoint")
    return None


def derive_compose_with(composed_nodes: dict) -> list[dict]:
    """Derive the composeWith publication from live, health-passing nodes.

    Invariant 2: a node is included iff it (a) has a cached NodeDeclaration in
    ``composed_nodes`` AND (b) is NOT marked failed by either MCP breaker. A
    declared-but-failed node (health-failing) and a dark node (no declaration,
    absent from ``composed_nodes``) are both excluded — the published list is the
    intersection of "declared" and "currently healthy", never the static config.
    """
    result: list[dict] = []
    for server_name, decl in composed_nodes.items():
        if _breaker_failed(server_name):
            continue  # health-failing — excluded (Invariant 2)
        result.append({
            "node_id": decl.node_id,
            "version": decl.version,
            "grv_standard": decl.grv_standard,
            "tools": decl.proposed_tools,
            "url": _server_url(server_name, decl),
        })
    return result


def publish_compose_with(
    composed_nodes: Optional[dict] = None,
    path: Optional[Path] = None,
) -> list[dict]:
    """Derive and write the composeWith publication to ~/.grove/compose-with.json.

    Writes ``[]`` when no health-passing composed node exists. Returns the
    derived list. ``composed_nodes`` defaults to the live
    ``tools/mcp_tool._composed_nodes``; ``path`` is overridable for tests.

    REFRESH GAP (R2 prime): invoked once after initial connect
    (``register_mcp_servers``). Breaker-state changes AFTER that write — a node
    going unhealthy mid-session, or recovering past its cooldown — are NOT
    reflected until the next process start. Dynamic re-publication on breaker
    transitions is deferred; the single post-connect write is sufficient for R2.
    """
    if composed_nodes is None:
        from tools import mcp_tool as _m

        composed_nodes = _m._composed_nodes
    if path is None:
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "compose-with.json"
    derived = derive_compose_with(composed_nodes)
    path.write_text(json.dumps(derived, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "composeWith published: %d health-passing node(s) -> %s",
        len(derived), path,
    )
    return derived
