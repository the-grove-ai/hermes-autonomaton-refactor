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
    ``/portal#fragments/...`` without doubling the separator. Pure sync —
    reads the already-loaded config dict only (A4: no file/network I/O).
    """
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
        f"The Operator Portal is at {base_url}/portal. Link the operator to "
        "rendered substrate artifacts using the templates below.\n"
        "\n"
        "Link templates (Markdown [text](url) — the # routes through the portal shell):\n"
        f"- Cellar page: [{{title}}]({base_url}/portal#fragments/cellar/pages/{{page_id}})\n"
        f"- Proposals: [{{count}} pending]({base_url}/portal#fragments/proposals/pending)\n"
        f"- Dock goals: [View goals]({base_url}/portal#fragments/dock/goals)\n"
        f"- Composition: [View mesh]({base_url}/portal#fragments/composition/panel)\n"
        f"- Dashboard: [Dashboard]({base_url}/portal#fragments/dashboard/overview)\n"
        f"- Search: [Search: {{query}}]({base_url}/portal#fragments/search?q={{query}})\n"
        "\n"
        "Rules (ALWAYS follow):\n"
        "- ALWAYS include a portal link when you write to or read from the cellar.\n"
        "- ALWAYS include the review queue link when you mention pending proposals.\n"
        "- ALWAYS include the goals link when you reference dock goals or strategy.\n"
        "- ALWAYS include the composition link when you discuss connected nodes.\n"
        "- Format: standard Markdown [text](url). Works on Telegram, web, CLI."
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

    Resolution precedence: explicit ``base_url`` arg → ``resolve_portal_
    base_url(config)`` → ``resolve_portal_base_url(context["config"])``.
    The closure returns ``None`` only if resolution yields nothing usable
    (the composer treats that as a skip) — not a normal path, since the
    resolver always carries a default.
    """
    # One-element cache: empty until the first call resolves the URL.
    resolved_cache: list[Optional[str]] = []

    def _provider(context: Dict[str, Any]) -> Optional[SectionResult]:
        if not resolved_cache:
            candidate = base_url
            if not candidate:
                cfg = config if config is not None else context.get("config")
                candidate = resolve_portal_base_url(cfg)
            cleaned = (candidate or "").strip().rstrip("/")
            resolved_cache.append(cleaned or None)

        resolved = resolved_cache[0]
        if not resolved:
            return None
        return SectionResult(label=_SECTION_LABEL, text=_render_section(resolved))

    return _provider
