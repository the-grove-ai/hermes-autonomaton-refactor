"""cellar-search-tool-v1 — on-demand search over the living cellar.

The agent's ONLY path to query the living-cellar pages (WikiIndex over
``$GROVE_WIKI_PATH/pages``). It closes the fabrication seam: instead of
improvising a cellar/portal URL on a miss, the agent calls this tool and either
relays a real, ready-made portal link (byte-identical to the turn-start injection
link) or reports a structured honest negative ("not in the local cellar").

Acquisition mirrors session_search: the WikiIndex is lazily constructed (it
self-resolves ``$GROVE_WIKI_PATH/pages``) and injectable for tests. Read-only /
Green (classified in zones.schema.yaml) — runs with no approval pause.
"""

from __future__ import annotations

import json

from tools.registry import tool_error

CELLAR_SEARCH_SCHEMA = {
    "name": "cellar_search",
    "description": (
        "Search the local living cellar (your compacted canonical knowledge "
        "pages) for research/knowledge on a topic. Returns matching pages with "
        "title, source type, a snippet, and a ready-made portal link to each "
        "page. If nothing matches, returns an empty result you can report "
        "honestly as 'not in the local cellar' — do NOT invent a cellar or portal "
        "URL. Use this whenever the operator asks you to find or link local "
        "research/knowledge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search the cellar for (a topic or phrase).",
            },
            "limit": {
                "type": "integer",
                "description": "Max pages to return (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


def cellar_search(query: str, limit: int = 5, index=None, base_url=None) -> str:
    """Search the living cellar. Returns a JSON string.

    * malformed (empty/blank query) → ``{"error": ...}`` — a malformed call, NOT
      an honest negative (a blank query must never masquerade as a clean miss).
    * valid query, no match → ``{"results": [], "cellar_searched": true, "count": 0}``
    * valid query, matches → each result carries title, source_type, snippet, and
      (when a portal base URL resolves) ``portal_url`` from the shared builder.
    """
    if not isinstance(query, str) or not query.strip():
        return tool_error(
            "cellar_search requires a non-empty 'query'. A blank query is a "
            "malformed call, not an empty-cellar result."
        )

    if index is None:
        from grove.wiki.index import WikiIndex

        index = WikiIndex()  # self-resolves $GROVE_WIKI_PATH/pages
    if base_url is None:
        from grove.prompt.portal_links import resolve_portal_base_url

        base_url = (resolve_portal_base_url() or "").strip()

    from grove.wiki.links import cellar_page_portal_link

    try:
        k = int(limit) if limit else 5
    except (TypeError, ValueError):
        k = 5
    hits = index.query(query.strip(), k=k)

    results = []
    for r in hits:
        item = {
            "title": r.title,
            "source_type": r.source_type,
            "snippet": r.snippet,
        }
        if base_url:
            item["portal_url"] = cellar_page_portal_link(r.source_path, base_url)
        results.append(item)

    return json.dumps(
        {"results": results, "cellar_searched": True, "count": len(results)},
        ensure_ascii=False,
    )


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools."""
    reg.register(
        name="cellar_search",
        toolset="session_search",
        schema=CELLAR_SEARCH_SCHEMA,
        handler=lambda args, **kw: cellar_search(
            query=args.get("query") or "",
            limit=args.get("limit", 5),
            index=kw.get("index"),
        ),
        emoji="📚",
    )
