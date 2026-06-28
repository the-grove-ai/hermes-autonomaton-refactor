"""portal-link-provider-v1 — Operator Portal deep-link composition.

The agent writes to the substrate (cellar pages, proposals, Dock goals,
fleet output) but the operator has no direct path from a conversation to
the rendered artifact. This module supplies the base-URL resolver (Phase 2)
and the PromptComposer provider (Phase 3) that inject portal deep-link
templates into the agent's context, so the agent naturally includes
clickable Markdown links when it references substrate content.

Hash routing (I5): every deep link uses ``{base_url}/portal#fragments/...``
(note the ``#``, not ``/``). The shell loads first, then the hash-router JS
in ``index.html`` dispatches an ``htmx.ajax`` GET to the fragment route —
so the operator always lands in the full styled portal, never a raw
unstyled fragment.

NO file or network I/O here (A4): the resolver reads the already-loaded
config dict only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from grove.prompt.composer import SectionProvider, SectionResult

# Sensible default when neither sovereign config nor the api_server block
# names a reachable address — the loopback portal on its conventional port.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8642

# 0.0.0.0 is a bind-any wildcard, not a dialable destination. When the
# gateway binds 0.0.0.0 (Tailscale exposure), a client must reach it via a
# concrete address; the safe local substitute is loopback.
_UNREACHABLE_BIND = "0.0.0.0"


def resolve_portal_base_url(config: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the Operator Portal base URL from sovereign config (I2).

    Resolution order:
      1. ``config["portal"]["base_url"]`` — the operator-set value (the
         Tailscale mesh address in production).
      2. Derive ``http://{host}:{port}`` from
         ``config["platforms"]["api_server"]``. The unreachable bind host
         ``0.0.0.0`` maps to ``127.0.0.1`` (a client cannot dial 0.0.0.0).
      3. Sensible default ``http://127.0.0.1:8642``.

    The trailing slash is stripped so callers append
    ``/portal#fragments/...`` without doubling the separator.

    ``config`` semantics:
      * An explicit dict (even ``{}``) is used as-is — pure, no I/O.
      * ``None`` means "read the real sovereign config yourself": the
        FULL config is loaded via ``hermes_cli.config.load_config()``.
        This is the production path — the Dispatcher hands the composer
        only the ``prompt`` sub-block, which lacks the top-level
        ``portal`` / ``platforms`` keys, so a partial dict would silently
        resolve to the loopback default. ``None`` reaches past that.
    """
    if config is None:
        # Load the full sovereign config (~/.grove/config.yaml, merged +
        # mtime-cached). NOT the prompt sub-block — portal.base_url and
        # platforms.api_server live at the top level.
        from hermes_cli.config import load_config

        config = load_config()
    cfg = config or {}

    # (1) Operator-set base URL wins outright.
    portal_cfg = cfg.get("portal")
    if isinstance(portal_cfg, dict):
        base_url = portal_cfg.get("base_url")
        if base_url and str(base_url).strip():
            return str(base_url).strip().rstrip("/")

    # (2) Derive from the api_server bind address, falling through to the
    # (3) defaults for any missing piece.
    host = _DEFAULT_HOST
    port: Any = _DEFAULT_PORT
    platforms = cfg.get("platforms")
    if isinstance(platforms, dict):
        api_server = platforms.get("api_server")
        if isinstance(api_server, dict):
            raw_host = api_server.get("host")
            if raw_host and str(raw_host).strip():
                host = str(raw_host).strip()
            raw_port = api_server.get("port")
            if raw_port:
                port = raw_port

    if host == _UNREACHABLE_BIND:
        host = _DEFAULT_HOST

    return f"http://{host}:{port}".rstrip("/")


_SECTION_LABEL = "portal_links"


def _render_section(base_url: str) -> str:
    """Render the portal deep-link guidance section.

    Every link uses ``{base_url}/portal#fragments/...`` (I5: ``#``, not
    ``/``) so the operator lands in the full styled shell. The text MUST
    stay under 300 tokens (I4); ``{title}`` / ``{page_id}`` / ``{count}`` /
    ``{query}`` are placeholders the agent fills per reference.
    """
    return (
        "## Portal Deep Links\n"
        f"Portal: {base_url}/portal. Hand the operator a link to any rendered "
        "artifact (Markdown; the # routes through the shell):\n"
        f"- Cellar page: [{{title}}]({base_url}/portal#fragments/cellar/pages/{{page_id}})\n"
        f"- Cellar by type: [Research]({base_url}/portal#fragments/cellar/pages?source_type=research)"
        " (swap research → scout/drafter/dock/notes)\n"
        f"- Proposals: [{{count}} pending]({base_url}/portal#fragments/proposals/pending)\n"
        f"- Dock goals: [View goals]({base_url}/portal#fragments/dock/goals)\n"
        f"- Composition: [View mesh]({base_url}/portal#fragments/composition/panel)\n"
        f"- Dashboard: [Dashboard]({base_url}/portal#fragments/dashboard/overview)\n"
        f"- Search: [Search: {{query}}]({base_url}/portal#fragments/search?q={{query}})\n"
        "\n"
        "Rules (ALWAYS follow):\n"
        "- ALWAYS link the cellar page(s) when you read or write the cellar.\n"
        "- ALWAYS link the review queue when you mention pending proposals.\n"
        "- ALWAYS link goals when you reference dock goals or strategy.\n"
        "- ALWAYS link composition when you discuss connected nodes."
    )


def build_portal_links_provider(
    base_url: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> SectionProvider:
    """Factory returning the portal-links section provider (Phase 3).

    Mirrors the ``preamble.py`` factory pattern: the factory captures the
    ``base_url`` / ``config`` at composer-build time and returns a closure
    the composer calls per turn. The base URL is resolved ONCE on first
    call (it is session-stable — the bind address does not change
    mid-session) and cached; subsequent turns reuse it.

    Resolution precedence:
      1. explicit ``base_url`` arg (an operator override) → use it.
      2. explicit ``config`` dict (a FULL config injected for tests /
         embedding) → ``resolve_portal_base_url(config)``.
      3. neither → ``resolve_portal_base_url()`` with NO args, so the
         resolver loads the FULL sovereign config itself. The composer
         must NOT hand us the ``prompt`` sub-block — it lacks
         ``portal`` / ``platforms`` and would resolve to the loopback
         default (the base_url-empty bug this factory was fixed for).

    The closure returns ``None`` only if resolution yields nothing usable
    (the composer treats that as a skip) — not a normal path, since the
    resolver always carries a default.
    """
    # One-element cache: empty until the first call resolves the URL.
    resolved_cache: list[Optional[str]] = []

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        if not resolved_cache:
            if base_url:
                candidate = base_url
            elif config is not None:
                candidate = resolve_portal_base_url(config)
            else:
                # Production path: read the FULL sovereign config (load_config),
                # NOT the prompt sub-block the composer holds.
                candidate = resolve_portal_base_url()
            cleaned = (candidate or "").strip().rstrip("/")
            resolved_cache.append(cleaned or None)

        resolved = resolved_cache[0]
        if not resolved:
            return None
        return SectionResult(label=_SECTION_LABEL, text=_render_section(resolved))

    return _provider
