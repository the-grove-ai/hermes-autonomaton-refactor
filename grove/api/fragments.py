"""Operator Portal — HTML fragment routes (Sprint P2, portal-knowledge-browser-v1).

The portal is a single HTMX page served same-origin from the existing aiohttp
gateway. ``GET /portal`` returns the shell; ``/portal/fragments/*`` return HTML
fragments that HTMX swaps into the shell's panels. These routes consume the
SAME substrate readers as the P1 ``/api/substrate/`` JSON API — the JSON
endpoints stay pure JSON (no content negotiation). Markdown bodies are rendered
server-side (``markdown``) and sanitized (``nh3``) before they reach a panel.

NO SILENT DEGRADATION. A missing reader fails the gateway loudly at import; a
malformed page surfaces as a visible error fragment, never a blank panel.
"""

from __future__ import annotations

import dataclasses
import html
import logging
from pathlib import Path

import markdown
import nh3
import yaml
from aiohttp import web

# Reuse P1's substrate readers/helpers verbatim so the portal and the JSON API
# never diverge in how they parse the substrate. _read_page raises
# FileNotFoundError / MalformedWikiPage / yaml.YAMLError; callers decide skip
# (listing) vs surface (detail), exactly as P1 does. _serialize_capability /
# _check_memory_stale are the same readers the JSON endpoints use.
from grove.api.portal import (
    _as_str_list,
    _check_cellar_stale,
    _check_memory_stale,
    _check_wiki_stale,
    _read_page,
    _serialize_capability,
)
from grove.capability import CapabilityKind
from grove.capability_registry import load_capabilities
from grove.dock import load_dock
from grove.eval.proposal_queue import read_all as read_all_proposals
from grove.wiki.index import MalformedWikiPage
from hermes_constants import get_wiki_path

logger = logging.getLogger(__name__)

# Repo root: grove/api/fragments.py -> grove/api -> grove -> <root>.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PORTAL_ASSETS = _REPO_ROOT / "gateway" / "assets" / "portal"

# ---------------------------------------------------------------------------
# Server-side markdown rendering + sanitization
# ---------------------------------------------------------------------------

# Allowed tags cover everything the markdown extensions emit (fenced code,
# tables, toc anchors) plus inline emphasis. Anything outside this set — most
# importantly <script> and event-handler-bearing elements — is stripped by nh3.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "blockquote", "pre", "code",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "strong", "em", "b", "i", "del", "sub", "sup",
    "span", "div", "img",
}

# nh3 attribute allowlist: a "*" key applies to every tag. ``class``/``id`` are
# safe presentational/anchor attributes (toc needs heading ids + anchor hrefs).
# href/src remain subject to nh3's URL-scheme scrubbing (no javascript:).
_ALLOWED_ATTRS = {
    "*": {"class", "id"},
    "a": {"href", "title", "id", "class"},
    "img": {"src", "alt", "title"},
    "td": {"align"},
    "th": {"align"},
    "code": {"class"},
    "pre": {"class"},
}


def _render_md(raw_markdown: str) -> str:
    """Render a markdown string to sanitized HTML.

    1. markdown -> HTML (fenced_code, tables, toc extensions).
    2. nh3.clean() strips any tag/attribute outside the allowlist — scripts and
       inline event handlers cannot survive. Sanitization is NEVER disabled; if
       legitimate formatting is stripped, widen the allowlist instead (A4).
    """
    html = markdown.markdown(
        raw_markdown or "",
        extensions=["fenced_code", "tables", "toc"],
    )
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)


# ---------------------------------------------------------------------------
# Shell route
# ---------------------------------------------------------------------------


async def handle_portal_shell(request: web.Request) -> web.Response:
    """Serve the portal HTML shell (the single-page HTMX application)."""
    index_path = _PORTAL_ASSETS / "index.html"
    return web.FileResponse(index_path)


# ---------------------------------------------------------------------------
# Fragment helpers
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    """HTML-escape any scalar for safe interpolation into a fragment (quotes
    too, so values are safe inside double-quoted attributes)."""
    return html.escape("" if value is None else str(value), quote=True)


def _tags_html(values: list) -> str:
    """Render a list of strings as <span class="tag"> chips (escaped)."""
    return "".join(f'<span class="tag">{_esc(v)}</span>' for v in values)


def _html_fragment(markup: str, status: int = 200) -> web.Response:
    """Wrap fragment markup in a text/html response (HTMX swaps the body in)."""
    return web.Response(text=markup, status=status, content_type="text/html")


# ---------------------------------------------------------------------------
# Cellar fragments (Phase 2)
# ---------------------------------------------------------------------------


async def handle_cellar_listing(request: web.Request) -> web.Response:
    """Render the cellar page listing, grouped by source_type subdirectory.

    A direct recursive filesystem scan (same as P1's handle_cellar_pages) so a
    malformed page is skipped here — commanded defensive read — rather than
    blanking the whole listing. Grouping key is the page's first path component
    (the per-source_type subdir, e.g. dock_goal/); pages sitting directly under
    pages/ fall back to their frontmatter source_type, else 'uncategorized'.
    """
    pages_dir = get_wiki_path() / "pages"
    groups: dict[str, list[dict]] = {}
    if pages_dir.is_dir():
        for path in sorted(pages_dir.glob("**/*.md")):
            try:
                meta, _body = _read_page(path)
            except FileNotFoundError:
                logger.warning(
                    "[portal] page vanished during scan, skipping: %s", path.name
                )
                continue
            except (yaml.YAMLError, MalformedWikiPage) as exc:
                # COMMANDED skip: one bad page must not blank the listing.
                logger.warning(
                    "[portal] malformed frontmatter, skipping %s: %r",
                    path.name, exc,
                )
                continue
            rel = path.relative_to(pages_dir)
            page_id = rel.with_suffix("").as_posix()
            source_type = meta.get("source_type")
            group = rel.parts[0] if len(rel.parts) > 1 else (source_type or "uncategorized")
            confidence = meta.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence = None
            groups.setdefault(group, []).append({
                "page_id": page_id,
                "title": meta.get("title") or page_id,
                "source_type": source_type,
                "topics": _as_str_list(meta.get("topics")),
                "confidence": confidence,
            })

    parts = ['<div id="cellar-listing">']
    if not groups:
        parts.append(
            '<p class="placeholder">No knowledge pages yet — the cellar is empty.</p>'
        )
    for group in sorted(groups):
        parts.append(f'<h3 class="group-header">{_esc(group)}</h3>')
        parts.append('<ul class="listing">')
        for p in sorted(groups[group], key=lambda d: (d["title"] or "").lower()):
            conf_attr = (
                f' data-confidence="{p["confidence"]:.2f}"'
                if p["confidence"] is not None else ""
            )
            badge = (
                f'<span class="badge badge-{_esc(p["source_type"])}">{_esc(p["source_type"])}</span>'
                if p["source_type"] else ""
            )
            parts.append(
                f'<li{conf_attr}>'
                f'<a hx-get="/portal/fragments/cellar/pages/{_esc(p["page_id"])}" '
                f'hx-target="#center-panel" hx-push-url="true">{_esc(p["title"])}</a> '
                f'{badge} {_tags_html(p["topics"])}'
                f'</li>'
            )
        parts.append('</ul>')
    parts.append('</div>')
    return _html_fragment("".join(parts))


def _frontmatter_dl(meta: dict) -> str:
    """Render selected frontmatter fields as a <dl> metadata header."""
    rows = [
        ("source_type", _esc(meta.get("source_type"))),
        ("topics", _tags_html(_as_str_list(meta.get("topics")))),
        ("entities", _tags_html(_as_str_list(meta.get("key_entities")))),
        ("confidence", _esc(meta.get("confidence"))),
        ("dock_goal_refs", _tags_html(_as_str_list(meta.get("dock_goal_refs")))),
    ]
    cells = "".join(
        f"<dt>{label}</dt><dd>{value or '&mdash;'}</dd>" for label, value in rows
    )
    return f'<dl class="frontmatter">{cells}</dl>'


async def handle_cellar_detail(request: web.Request) -> web.Response:
    """Render one cellar page: frontmatter header + server-rendered markdown.

    The body is rendered via _render_md() (markdown -> nh3-sanitized HTML). A
    nonexistent page is a 404 fragment; a malformed/partly-written page is a 500
    fragment — both surface (no silent blank). The htmx:responseError listener
    in the shell renders the generic error card on those non-200 statuses.
    """
    page_id = request.match_info["page_id"]
    pages_dir = get_wiki_path() / "pages"
    path = pages_dir / f"{page_id}.md"
    # Containment guard: the {page_id:.+} route permits slashes, so refuse any
    # resolved path that escapes the pages directory (path traversal).
    try:
        path.resolve().relative_to(pages_dir.resolve())
    except ValueError:
        return _html_fragment(
            f'<div class="error-card"><h3>Not found</h3>'
            f'<p>Page {_esc(page_id)} not found.</p></div>',
            status=404,
        )
    try:
        meta, body = _read_page(path)
    except FileNotFoundError:
        return _html_fragment(
            f'<div class="error-card"><h3>Not found</h3>'
            f'<p>Page {_esc(page_id)} not found.</p></div>',
            status=404,
        )
    except (yaml.YAMLError, MalformedWikiPage) as exc:
        # A detail request for a specific page that cannot parse is a real
        # failure, not a skip — surface it. No retry, no degradation.
        logger.warning("[portal] detail parse failure for %s: %r", page_id, exc)
        return _html_fragment(
            '<div class="error-card"><h3>Parse error</h3>'
            '<p>Page frontmatter is malformed or the file is being written.</p></div>',
            status=500,
        )

    title = meta.get("title") or page_id
    rendered = _render_md(body)
    # OOB swap: load the context sidebar for this page. The context endpoint
    # lands in Phase 4 — until then this fires a request that 404s, and the
    # sidebar keeps showing "Loading context..." (expected, not a defect).
    # The OOB div replaces #right-panel, then its load trigger fetches the
    # context. hx-swap="outerHTML" makes that fetch REPLACE this div with the
    # response's <div id="right-panel">, instead of the default innerHTML which
    # would nest a duplicate-id div. Both this OOB path and the Phase 3 cards
    # therefore swap #right-panel via outerHTML — one consistent mechanic.
    oob = (
        f'<div id="right-panel" hx-swap-oob="true" '
        f'hx-get="/portal/fragments/context/cellar/{_esc(page_id)}" '
        f'hx-trigger="load" hx-swap="outerHTML">'
        f'<div class="spinner">Loading context...</div>'
        f'</div>'
    )
    markup = (
        f'<article id="page-detail">'
        f'<h2>{_esc(title)}</h2>'
        f'{_frontmatter_dl(meta)}'
        f'<div class="page-body">{rendered}</div>'
        f'</article>'
        f'{oob}'
    )
    return _html_fragment(markup)


# ---------------------------------------------------------------------------
# Memory / Dock / Proposals / Skills fragments (Phase 3)
# ---------------------------------------------------------------------------


def _ctx_attrs(entity_type: str, entity_id) -> str:
    """HTMX attributes that load the context sidebar for an entity.

    Clicking a memory/dock/proposal/skill card updates ONLY the sidebar (the
    listing stays in #center-panel), so — unlike cellar detail, which targets
    #center-panel and needs an OOB side-effect — this targets #right-panel
    directly with an outerHTML swap. The context endpoint (Phase 4) returns a
    <div id="right-panel">; until it exists these 404 (same interim state as the
    cellar OOB swap), and the htmx:responseError listener surfaces it.
    """
    return (
        f'hx-get="/portal/fragments/context/{_esc(entity_type)}/{_esc(entity_id)}" '
        f'hx-target="#right-panel" hx-swap="outerHTML" style="cursor:pointer"'
    )


_ZONE_BADGE = {"green": "badge-green", "yellow": "badge-yellow", "red": "badge-red"}


async def handle_memory_records(request: web.Request) -> web.Response:
    """List ACTIVE memory records as cards (confidence bar, decay, dock link).

    Uses the same active-only projection as P1's JSON endpoint
    (projected_records() filtered to status == 'active').
    """
    _check_memory_stale(request.app)
    store = request.app["memory_store"]
    records = [
        dataclasses.asdict(rec)
        for rec in store.projected_records().values()
        if rec.status == "active"
    ]
    parts = ['<div id="memory-listing">']
    if not records:
        parts.append('<p class="placeholder">No active memory records.</p>')
    for rec in records:
        conf = rec.get("confidence")
        try:
            conf_f = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            conf_f = 0.0
        ref = rec.get("dock_goal_ref")
        ref_html = ""
        if ref:
            ref_html = (
                f' &middot; goal: <a {_ctx_attrs("dock", ref)}>{_esc(ref)}</a>'
            )
        parts.append(
            f'<div class="card">'
            f'<h4><a {_ctx_attrs("memory", rec.get("id"))}>'
            f'<span class="badge">{_esc(rec.get("entity_type"))}</span></a> '
            f'<span class="badge badge-green">{_esc(rec.get("status"))}</span></h4>'
            f'<p>{_esc(rec.get("content"))}</p>'
            f'<div class="confidence-track">'
            f'<div class="confidence-bar" style="width:{conf_f * 100:.0f}%"></div></div>'
            f'<div class="meta">confidence {_esc(conf)} &middot; '
            f'accessed {_esc(rec.get("access_count"))}&times; &middot; '
            f'decay {_esc(rec.get("decay_rate"))}{ref_html}</div>'
            f'</div>'
        )
    parts.append('</div>')
    return _html_fragment("".join(parts))


def _milestones_html(extra: dict) -> str:
    """Render goal milestones from the passthrough ``extra`` dict as tags.

    ``extra`` is passthrough YAML so milestones may be strings, or dicts with a
    name/title (+ optional status). Presentation only — coerce leniently."""
    milestones = extra.get("milestones") if isinstance(extra, dict) else None
    if not isinstance(milestones, (list, tuple)) or not milestones:
        return ""
    chips = []
    for m in milestones:
        if isinstance(m, dict):
            label = m.get("name") or m.get("title") or m.get("label") or str(m)
            status = m.get("status")
            label = f"{label} ({status})" if status else label
        else:
            label = str(m)
        chips.append(f'<span class="tag">{_esc(label)}</span>')
    return '<div class="meta">milestones: ' + "".join(chips) + "</div>"


async def handle_dock_goals(request: web.Request) -> web.Response:
    """List Dock goals as cards, or a 'not installed' message when absent."""
    dock = load_dock()
    if dock is None:
        return _html_fragment(
            '<div id="dock-listing"><p class="placeholder">'
            'Dock not installed — no goals are configured.</p></div>'
        )
    parts = ['<div id="dock-listing">']
    if not dock.goals:
        parts.append('<p class="placeholder">The Dock has no goals.</p>')
    for g in dock.goals:
        keywords = "".join(f'<span class="tag">{_esc(k)}</span>' for k in g.keywords)
        parts.append(
            f'<div class="card" {_ctx_attrs("dock", g.id)}>'
            f'<h4>{_esc(g.name)} '
            f'<span class="badge">{_esc(g.vector)}</span> '
            f'<span class="badge">{_esc(g.status)}</span></h4>'
            f'<div class="meta">{_esc(g.definition_of_done)}</div>'
            f'<div>{keywords}</div>'
            f'{_milestones_html(g.extra)}'
            f'</div>'
        )
    parts.append('</div>')
    return _html_fragment("".join(parts))


async def handle_proposals_pending(request: web.Request) -> web.Response:
    """List pending Kaizen proposals as read-only cards (approve/reject is P4)."""
    proposals = [p.to_dict() for p in read_all_proposals()]
    parts = ['<div id="proposals-listing">']
    if not proposals:
        parts.append(
            '<p class="placeholder">No pending proposals — the system has '
            'nothing to recommend changing.</p>'
        )
    for p in proposals:
        evidence = p.get("evidence")
        if isinstance(evidence, dict):
            ev_summary = ", ".join(f"{k}: {v}" for k, v in list(evidence.items())[:6])
        elif isinstance(evidence, (list, tuple)):
            ev_summary = f"{len(evidence)} item(s)"
        else:
            ev_summary = str(evidence) if evidence else ""
        parts.append(
            f'<div class="card">'
            f'<h4><span class="badge">{_esc(p.get("type"))}</span></h4>'
            f'<p>{_esc(p.get("semantic_justification"))}</p>'
            f'<div class="meta">evidence: {_esc(ev_summary)}</div>'
            f'<div class="meta">created {_esc(p.get("created_at"))}</div>'
            f'</div>'
        )
    parts.append('</div>')
    return _html_fragment("".join(parts))


async def handle_skills(request: web.Request) -> web.Response:
    """List capability records of kind=skill as cards with zone badges."""
    caps = load_capabilities()
    skills = [
        _serialize_capability(cap)
        for cap in caps.values()
        if cap.kind == CapabilityKind.SKILL
    ]
    skills.sort(key=lambda d: (d.get("id") or "").lower())
    parts = ['<div id="skills-listing">']
    if not skills:
        parts.append('<p class="placeholder">No skills registered.</p>')
    for s in skills:
        zone = s.get("zone")
        zone_cls = _ZONE_BADGE.get(zone, "")
        state = (s.get("lifecycle") or {}).get("state")
        trigger = s.get("trigger") or {}
        kw = "".join(
            f'<span class="tag">{_esc(k)}</span>' for k in (trigger.get("keywords") or [])
        )
        category = (s.get("skill") or {}).get("category")
        cat_html = f' <span class="tag">{_esc(category)}</span>' if category else ""
        parts.append(
            f'<div class="card" {_ctx_attrs("skill", s.get("id"))}>'
            f'<h4>{_esc(s.get("id"))} '
            f'<span class="badge {zone_cls}">{_esc(zone)}</span> '
            f'<span class="badge">{_esc(state)}</span></h4>'
            f'<div class="meta">triggers: {kw or "&mdash;"}{cat_html}</div>'
            f'</div>'
        )
    parts.append('</div>')
    return _html_fragment("".join(parts))


# ---------------------------------------------------------------------------
# Context sidebar (Phase 4)
# ---------------------------------------------------------------------------


def _scan_page_index() -> list[dict]:
    """Scan all cellar pages once, returning lightweight records for relating.

    Skips malformed pages (logged) — the same commanded defensive read as the
    listing; one bad page must not blank the context sidebar.
    """
    pages_dir = get_wiki_path() / "pages"
    out: list[dict] = []
    if not pages_dir.is_dir():
        return out
    for path in sorted(pages_dir.glob("**/*.md")):
        try:
            meta, _body = _read_page(path)
        except FileNotFoundError:
            continue
        except (yaml.YAMLError, MalformedWikiPage) as exc:
            logger.warning(
                "[portal] malformed frontmatter during context scan, skipping %s: %r",
                path.name, exc,
            )
            continue
        page_id = path.relative_to(pages_dir).with_suffix("").as_posix()
        out.append({
            "page_id": page_id,
            "title": meta.get("title") or page_id,
            "topics": set(_as_str_list(meta.get("topics"))),
            "dock_goal_refs": _as_str_list(meta.get("dock_goal_refs")),
        })
    return out


def _goals_by_id() -> dict:
    """Map goal id -> Goal from the installed Dock (empty when no Dock)."""
    dock = load_dock()
    return {g.id: g for g in dock.goals} if dock is not None else {}


def _goal_card(goal) -> str:
    """A small, clickable goal card for the context sidebar (pivots to that
    goal's own context on click)."""
    return (
        f'<div class="card" {_ctx_attrs("dock", goal.id)}>'
        f'<h4>{_esc(goal.name)} '
        f'<span class="badge">{_esc(goal.vector)}</span> '
        f'<span class="badge">{_esc(goal.status)}</span></h4>'
        f'<div class="meta">{_esc(goal.definition_of_done)}</div>'
        f'</div>'
    )


def _page_link(page_id: str, title: str) -> str:
    """A related-page link that loads the page into the center panel (which in
    turn refreshes this sidebar via the page's OOB swap)."""
    return (
        f'<li><a hx-get="/portal/fragments/cellar/pages/{_esc(page_id)}" '
        f'hx-target="#center-panel" hx-push-url="true">{_esc(title)}</a></li>'
    )


def _section(title: str, inner: str, empty_msg: str) -> str:
    body = inner if inner else f'<p class="placeholder">{empty_msg}</p>'
    return f'<h3>{_esc(title)}</h3>{body}'


def _context_cellar(entity_id: str) -> str:
    """Context for a cellar page: its goals + topic-related pages."""
    pages_dir = get_wiki_path() / "pages"
    path = pages_dir / f"{entity_id}.md"
    try:
        path.resolve().relative_to(pages_dir.resolve())
        meta, _body = _read_page(path)
    except (ValueError, FileNotFoundError):
        return '<p class="placeholder">Context unavailable — page not found.</p>'
    except (yaml.YAMLError, MalformedWikiPage):
        # Surface visibly in the sidebar rather than 500 — a 5xx here would trip
        # the shell's error listener and wipe the center panel the operator is
        # reading. The message is loud; the reading pane stays intact.
        return '<p class="placeholder">Context unavailable — page is unreadable.</p>'

    goals = _goals_by_id()
    refs = _as_str_list(meta.get("dock_goal_refs"))
    goal_cards = "".join(
        _goal_card(goals[r]) if r in goals
        else f'<div class="card"><div class="meta">unknown goal: {_esc(r)}</div></div>'
        for r in refs
    )

    my_topics = set(_as_str_list(meta.get("topics")))
    related = []
    if my_topics:
        for p in _scan_page_index():
            if p["page_id"] == entity_id:
                continue
            overlap = len(my_topics & p["topics"])
            if overlap:
                related.append((overlap, p["page_id"], p["title"]))
        related.sort(key=lambda t: (-t[0], t[2].lower()))
    page_links = "".join(_page_link(pid, title) for _ov, pid, title in related[:12])
    page_links = f'<ul class="listing">{page_links}</ul>' if page_links else ""

    return (
        _section("Related Goals", goal_cards, "No goals reference this page.")
        + _section("Related Pages", page_links, "No pages share this page's topics.")
    )


def _context_memory(app: web.Application, entity_id: str) -> str:
    """Context for a memory record: its associated Dock goal."""
    store = app.get("memory_store")
    rec = store.projected_records().get(entity_id) if store is not None else None
    if rec is None:
        return '<p class="placeholder">Context unavailable — record not found.</p>'
    ref = rec.dock_goal_ref
    if not ref:
        return _section("Associated Goal", "", "This record has no associated goal.")
    goals = _goals_by_id()
    if ref in goals:
        return _section("Associated Goal", _goal_card(goals[ref]), "")
    return _section(
        "Associated Goal",
        f'<div class="card"><div class="meta">unknown goal: {_esc(ref)}</div></div>',
        "",
    )


def _context_dock(app: web.Application, entity_id: str) -> str:
    """Context for a Dock goal: reverse lookup of pages + memory records."""
    # Knowledge pages whose dock_goal_refs include this goal.
    page_links = "".join(
        _page_link(p["page_id"], p["title"])
        for p in _scan_page_index()
        if entity_id in p["dock_goal_refs"]
    )
    page_links = f'<ul class="listing">{page_links}</ul>' if page_links else ""

    # Memory records whose dock_goal_ref matches this goal (active only).
    store = app.get("memory_store")
    mem_items = ""
    if store is not None:
        rows = [
            f'<li><span class="badge">{_esc(rec.entity_type)}</span> {_esc(rec.content)}</li>'
            for rec in store.projected_records().values()
            if rec.status == "active" and rec.dock_goal_ref == entity_id
        ]
        mem_items = f'<ul class="listing">{"".join(rows)}</ul>' if rows else ""

    return (
        _section("Knowledge Pages", page_links, "No pages reference this goal.")
        + _section("Memory Records", mem_items, "No memory records reference this goal.")
    )


async def handle_context(request: web.Request) -> web.Response:
    """Assemble the context sidebar for one substrate entity.

    Returns a full <div id="right-panel"> that consumers swap via outerHTML
    (cards) or the cellar OOB load (also outerHTML). Failures render a visible
    in-sidebar message rather than a 5xx, so the reading pane is never wiped.
    """
    entity_type = request.match_info["entity_type"]
    entity_id = request.match_info["entity_id"]
    if entity_type == "cellar":
        body = _context_cellar(entity_id)
    elif entity_type == "memory":
        body = _context_memory(request.app, entity_id)
    elif entity_type == "dock":
        body = _context_dock(request.app, entity_id)
    else:
        body = '<p class="placeholder">No context available.</p>'
    return _html_fragment(f'<div id="right-panel" class="right-panel">{body}</div>')


# ---------------------------------------------------------------------------
# Search (Phase 5)
# ---------------------------------------------------------------------------


def _wiki_result_html(r) -> str:
    """One Wiki Match: a clickable page link + rendered snippet. Wiki results
    ARE the browsable cellar pages (same wiki/pages corpus), so each loads its
    detail into #center-panel, which fires the OOB sidebar context swap."""
    page_id = Path(r.source_path).with_suffix("").as_posix()
    return (
        f'<li>'
        f'<a hx-get="/portal/fragments/cellar/pages/{_esc(page_id)}" '
        f'hx-target="#center-panel" hx-push-url="true">{_esc(r.title or page_id)}</a>'
        f'<div class="search-snippet">{_render_md(r.snippet)}</div>'
        f'</li>'
    )


def _cellar_result_html(r) -> str:
    """One Cellar Match: title + content_type badge + rendered snippet. The
    CellarIndex corpus is ~/.grove skill/identity/config/memory files, which
    have no browsable detail route in P2 — so these are not clickable (no broken
    links). A read-only browser for that corpus is future work."""
    return (
        f'<li>'
        f'<span class="result-title">{_esc(r.title or r.source_path)}</span> '
        f'<span class="badge">{_esc(r.content_type)}</span>'
        f'<div class="search-snippet">{_render_md(r.snippet)}</div>'
        f'</li>'
    )


async def handle_search(request: web.Request) -> web.Response:
    """FTS5 search across wiki + cellar, rendered as stacked partitioned
    sections. Results are NEVER merged — bm25 scores are corpus-relative and
    incomparable across the two indices (same invariant as the P1 JSON API)."""
    q = request.query.get("q", "")
    if not q.strip():
        return _html_fragment('<p class="placeholder">Enter a search term.</p>')
    try:
        k = int(request.query.get("k", "10"))
    except ValueError:
        k = 10
    k = max(1, min(k, 50))
    source_type = request.query.get("source_type")

    # Lazy first-build + freshness (same as P1). A malformed wiki page makes the
    # build fail loud — surface the offending page by name in a visible error
    # card. Rendered at 200 (not 500) so the message itself reaches the panel:
    # search targets #center-panel, and a 5xx would trip the shell's generic
    # error listener and discard this specific diagnostic.
    try:
        _check_wiki_stale(request.app)
    except MalformedWikiPage as exc:
        logger.error("[portal] wiki index build failed during search: %r", exc)
        return _html_fragment(
            f'<div class="error-card"><h3>Search index error</h3>'
            f'<p>Wiki index build failed: {_esc(exc)}</p></div>'
        )
    _check_cellar_stale(request.app)

    wiki = request.app["wiki_index"].query(
        text=q, k=k, source_type=source_type, ensure_fresh=False
    )
    cellar = request.app["cellar_index"].query(text=q, k=k)

    if not wiki and not cellar:
        return _html_fragment(
            f'<div id="search-results"><p class="placeholder">'
            f'No matches found for &ldquo;{_esc(q)}&rdquo;.</p></div>'
        )

    wiki_items = "".join(_wiki_result_html(r) for r in wiki)
    cellar_items = "".join(_cellar_result_html(r) for r in cellar)
    wiki_body = (
        f'<ol>{wiki_items}</ol>' if wiki_items
        else '<p class="placeholder">No wiki matches.</p>'
    )
    cellar_body = (
        f'<ol>{cellar_items}</ol>' if cellar_items
        else '<p class="placeholder">No cellar matches.</p>'
    )
    markup = (
        f'<div id="search-results">'
        f'<section class="search-partition"><h3>Wiki Matches</h3>{wiki_body}</section>'
        f'<hr>'
        f'<section class="search-partition"><h3>Cellar Matches</h3>{cellar_body}</section>'
        f'</div>'
    )
    return _html_fragment(markup)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_fragment_routes(app: web.Application) -> None:
    """Register the portal shell + ``/portal/fragments/*`` routes.

    Handlers land incrementally across Sprint P2 phases: shell (Phase 1);
    cellar (Phase 2); memory/dock/proposals/skills (Phase 3); context
    (Phase 4); search (Phase 5).
    """
    # Phase 1 — shell
    app.router.add_get("/portal", handle_portal_shell)
    # Phase 2 — cellar (listing + detail). {page_id:.+} carries the subdir-
    # qualified id (e.g. dock_goal/foo); the handler's containment guard blocks
    # path traversal.
    app.router.add_get("/portal/fragments/cellar/pages", handle_cellar_listing)
    app.router.add_get("/portal/fragments/cellar/pages/{page_id:.+}", handle_cellar_detail)
    # Phase 3 — memory, dock, proposals, skills
    app.router.add_get("/portal/fragments/memory/records", handle_memory_records)
    app.router.add_get("/portal/fragments/dock/goals", handle_dock_goals)
    app.router.add_get("/portal/fragments/proposals/pending", handle_proposals_pending)
    app.router.add_get("/portal/fragments/skills/", handle_skills)
    # Phase 4 — context sidebar. {entity_id:.+} carries slash-bearing cellar
    # page_ids; entity_type is a single non-slash segment.
    app.router.add_get(
        "/portal/fragments/context/{entity_type}/{entity_id:.+}", handle_context
    )
    # Phase 5 — search
    app.router.add_get("/portal/fragments/search", handle_search)
