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
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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
    _fleet_skill_records,
    _fleet_zone_dirs,
    _list_fleet_artifacts,
    _list_fleet_units,
    _read_fleet_artifact,
    _read_forge_slug,
    _read_page,
    _serialize_capability,
    pending_memory_proposal_items,
)
from grove.capability import CapabilityKind
from grove.capability_registry import load_capabilities
from grove.dock import _VALID_STATUSES, load_dock
from grove.eval.proposal_queue import PROPOSAL_VERBS, _type_offers_approve
from grove.eval.proposal_queue import read_all as read_all_proposals
from grove.red_pending_store import RED_PENDING_PROPOSAL_TYPE
from grove.api.red_nonce import nonce_key_from_app, red_nonce
from grove.wiki.index import MalformedWikiPage
from grove.wiki.links import cellar_page_id
from hermes_constants import get_hermes_home, get_wiki_path

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


def render_alert_banner(
    message: str, *, status: int | None = None, detail: str | None = None,
) -> str:
    """OOB fragment that drives the persistent ``#alert-banner`` slot
    NON-DESTRUCTIVELY (portal-action-error-surfacing-v1 P2).

    Returns a ``<div id="alert-banner" hx-swap-oob="true">`` replacement carrying
    one alert. It targets the banner slot ONLY — never ``#center-panel`` — so
    surfacing an action failure never wipes what the operator is reading. P3's
    ``_loud_action_failure`` appends this to failure responses; the base
    template's ``htmx:responseError`` handler lifts its content into the live
    banner (htmx does not swap 4xx bodies itself). Every interpolated value passes
    through ``_esc``.

    Carries a ``.alert-dismiss`` control (portal-action-error-surfacing-v1 P3.5) —
    the base template's DELEGATED click listener clears + hides the banner on tap
    (survives innerHTML replacement). No auto-timeout — an error banner only
    clears on manual dismiss or a subsequent successful action."""
    status_txt = f"{_esc(status)}: " if status is not None else ""
    detail_html = f'<p class="alert-detail">{_esc(detail)}</p>' if detail else ""
    return (
        f'<div id="alert-banner" class="alert-banner" role="alert" '
        f'aria-live="assertive" hx-swap-oob="true">'
        f'<div class="alert alert-error">'
        f'<button type="button" class="alert-dismiss" aria-label="Dismiss">'
        f'&times;</button>'
        f'<strong>Action failed.</strong> {status_txt}{_esc(message)}'
        f'{detail_html}'
        f'</div>'
        f'</div>'
    )


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
    # Optional ?source_type= filter (the portal_links deep links use it).
    # Sanitize to alphanumerics + underscore ONLY — strip ., /, \\ and any
    # other path character. The value is used solely for an equality match
    # below, but hardening it defensively means a crafted value can never
    # carry path semantics (Gemini guardrail: path-traversal). Sanitized-empty
    # means no filter (existing behavior, full listing).
    source_type_filter = request.query.get("source_type")
    if source_type_filter:
        source_type_filter = re.sub(r"[^a-zA-Z0-9_]", "", source_type_filter) or None
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
            page_id = cellar_page_id(rel)
            source_type = meta.get("source_type")
            # Apply the source_type filter (when set) — only pages whose
            # frontmatter source_type matches survive; absent filter = all.
            if source_type_filter and source_type != source_type_filter:
                continue
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
            # fleet-ui-reconciliation-v1 C1 — hash anchor, not hx-get+push: the
            # shell's hash router is the single dispatcher/history actor (F1).
            parts.append(
                f'<li{conf_attr}>'
                f'<a href="/portal#fragments/cellar/pages/{_esc(p["page_id"])}">'
                f'{_esc(p["title"])}</a> '
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


def _status_select_html(goal) -> str:
    """The P4 status toggle. Options are driven by ``_VALID_STATUSES`` (sorted)
    so the dropdown inherits the loader's taxonomy automatically — if the set
    ever expands, the toggle follows (PM ruling, Sprint P4). A current status
    somehow off-set renders as a leading disabled option so the operator sees
    the truth, while the writer is never asked to persist an invalid value."""
    options: list[str] = []
    current = goal.status
    if current not in _VALID_STATUSES:
        options.append(
            f'<option value="{_esc(current)}" disabled selected>'
            f'{_esc(current)} (unknown)</option>'
        )
    for status in sorted(_VALID_STATUSES):
        sel = " selected" if status == current else ""
        options.append(
            f'<option value="{_esc(status)}"{sel}>{_esc(status)}</option>'
        )
    gid = _esc(goal.id)
    return (
        f'<div class="goal-actions">'
        f'<select name="status" '
        f'hx-patch="/portal/actions/dock/goals/{gid}" '
        f'hx-target="#goal-{gid}" hx-swap="outerHTML">'
        f'{"".join(options)}'
        f'</select>'
        f'</div>'
    )


def render_goal_card(goal) -> str:
    """One Dock goal card. Shared by the listing (:func:`handle_dock_goals`) and
    the PATCH response (``actions.handle_dock_goal_update``) so the swapped-in
    card is byte-identical to the listed one."""
    keywords = "".join(f'<span class="tag">{_esc(k)}</span>' for k in goal.keywords)
    return (
        f'<div class="card" id="goal-{_esc(goal.id)}" {_ctx_attrs("dock", goal.id)}>'
        f'<h4>{_esc(goal.name)} '
        f'<span class="badge">{_esc(goal.vector)}</span> '
        f'<span class="badge">{_esc(goal.status)}</span></h4>'
        f'<div class="meta">{_esc(goal.definition_of_done)}</div>'
        f'<div>{keywords}</div>'
        f'{_milestones_html(goal.extra)}'
        f'{_status_select_html(goal)}'
        f'</div>'
    )


async def handle_dock_goals(request: web.Request) -> web.Response:
    """List Dock goals as cards, or a 'not installed' message when absent.

    A malformed/incompatible dock.yaml (load_dock raises ValueError per the
    Architectural Prime Directive) surfaces as a readable error fragment in the
    panel rather than a raw 500 — consistent with the portal's "visible error
    fragment, never a blank panel" rule. The operator sees the exact reason
    (e.g. unsupported version, or goal entries missing required keys)."""
    try:
        dock = load_dock()
    except ValueError as exc:
        logger.warning("[portal] dock manifest unreadable: %r", exc)
        return _html_fragment(
            f'<div id="dock-listing"><div class="error-card">'
            f"<h3>Dock manifest unreadable</h3>"
            f"<p>{_esc(str(exc))}</p>"
            f'<p class="meta">The portal reads grove Dock v1 '
            f"(version: 1; goals with name / vector / status / "
            f"definition_of_done / keywords / context_sources / "
            f"unlocked_skills). Reconcile ~/.grove/dock/dock.yaml to that "
            f"schema to restore the panel.</p></div></div>"
        )
    if dock is None:
        return _html_fragment(
            '<div id="dock-listing"><p class="placeholder">'
            'Dock not installed — no goals are configured.</p></div>'
        )
    parts = ['<div id="dock-listing">']
    if not dock.goals:
        parts.append('<p class="placeholder">The Dock has no goals.</p>')
    for g in dock.goals:
        parts.append(render_goal_card(g))
    parts.append('</div>')
    return _html_fragment("".join(parts))


def _short_id(proposal_id: str) -> str:
    """First 12 chars of the hash tail — the stable DOM id for a proposal card.
    Mirrors ``RoutingProposal.short_id`` so routing and memory cards share one
    convention."""
    return proposal_id.split(":")[-1][:12]


def _proposal_actions_html(
    proposal_id: str, short_id: str, *, offers_approve: bool = True
) -> str:
    """The approve/reject/dismiss button row. The full ``proposal_id`` rides the
    hx-post URL; the ``short_id`` targets the card for outerHTML replacement.

    ``offers_approve`` (portal-action-error-surfacing-v1 P3.6) omits the Approve
    button for a render-only type whose approve dead-ends at ``_handler_for``
    (e.g. ``portal_action_failure``) — mirroring the in-chat push gate, one
    resolver (``_type_offers_approve``). Reject + Dismiss always stay: the portal
    reject/dismiss path dequeues + records disposition WITHOUT ``_handler_for``,
    so both are honored for every type."""
    pid = _esc(proposal_id)
    approve = (
        f'<button class="btn btn-approve" '
        f'hx-post="/portal/actions/proposals/{pid}/approve" '
        f'hx-target="#proposal-{short_id}" hx-swap="outerHTML" '
        f'hx-confirm="Approve this proposal?">Approve</button>'
    ) if offers_approve else ""
    return (
        f'<div class="proposal-actions">'
        f'{approve}'
        f'<button class="btn btn-reject" '
        f'hx-post="/portal/actions/proposals/{pid}/reject" '
        f'hx-target="#proposal-{short_id}" hx-swap="outerHTML">Reject</button>'
        f'<button class="btn btn-dismiss" '
        f'hx-post="/portal/actions/proposals/{pid}/dismiss" '
        f'hx-target="#proposal-{short_id}" hx-swap="outerHTML">Dismiss</button>'
        f'</div>'
    )


def _render_red_proposal_card(request, full_pid: str, short_id: str) -> str:
    """RED ``.env`` proposal card — propose-approve-deadlock-v1 Phase 1b-ii.

    Pulls the MASKED operator-facing description from the in-memory store
    singleton (``request.app["red_pending_store"]``); the secret value is NEVER
    rendered. If the payload is gone (orphan — durable queue row survived a
    restart, in-memory payload did not) render EXPIRED with a Dismiss affordance,
    NOT a live approve. Otherwise render a two-step approve form carrying the
    ``approve``-step CSRF nonce. Not batchable (single-id action row)."""
    store = request.app.get("red_pending_store")
    bare = full_pid.split(":", 1)[1] if ":" in full_pid else full_pid
    masked = store.masked_description(bare) if store is not None else None
    badge = _ZONE_BADGE.get("red", "badge")
    pid = _esc(full_pid)

    if masked is None:
        # ORPHAN → EXPIRED. Dismiss removes the stale durable row (no approve).
        return (
            f'<div class="card card-expired" id="proposal-{short_id}">'
            f'<h4><span class="badge {badge}">RED</span> '
            f'<span class="badge">expired</span></h4>'
            f'<p>This pending .env change is no longer available — the gateway '
            f'restarted and pending proposals are session-scoped. Re-propose to '
            f'apply it.</p>'
            f'<div class="proposal-actions">'
            f'<button class="btn btn-dismiss" '
            f'hx-post="/portal/actions/proposals/{pid}/dismiss" '
            f'hx-target="#proposal-{short_id}" hx-swap="outerHTML">Dismiss</button>'
            f'</div>'
            f'</div>'
        )

    nonce = red_nonce(full_pid, "approve", nonce_key_from_app(request.app))
    # red-action-store-pending-v1 Phase B — OPAQUE_DYNAMIC_EFFECT affordance. When
    # the classifier could NOT statically resolve the effect (command substitution,
    # unparseable, dynamic targets), warn that approval authorizes the INTENT to run
    # the string, not a guaranteed outcome. Legible proposals carry no warning.
    opaque = store.is_opaque(bare) if store is not None else False
    opaque_warning = (
        '<div class="meta meta-opaque">⚠ OPAQUE dynamic command — effect not '
        'statically resolved. Approving authorizes the intent to run this string, '
        'not a guaranteed outcome.</div>'
        if opaque else ""
    )
    # Two-step approve: this POST /approve returns a Confirm card (no mint); the
    # Confirm card's POST /confirm performs the write. hx-vals carries the nonce.
    # red-action-store-pending-v1 Phase B — per-action-type title (governance write /
    # privileged shell / secret access / opaque command / generic), derived from the
    # stored effect. Generalizes the former hardwired "RED — governance write".
    title = store.card_title(bare) if store is not None else "RED — action"
    return (
        f'<div class="card card-red" id="proposal-{short_id}">'
        f'<h4><span class="badge {badge}">{_esc(title)}</span></h4>'
        f'<p>{_esc(masked)}</p>'
        f'{opaque_warning}'
        f'<div class="meta">value: •••• (masked)</div>'
        f'<div class="proposal-actions">'
        f'<button class="btn btn-approve" '
        f'hx-post="/portal/actions/proposals/{pid}/approve" '
        f'hx-vals=\'{{"nonce": "{_esc(nonce)}"}}\' '
        f'hx-target="#proposal-{short_id}" hx-swap="outerHTML" '
        f'hx-confirm="Approve a RED .env write? You will confirm once more.">'
        f'Approve</button>'
        f'<button class="btn btn-reject" '
        f'hx-post="/portal/actions/proposals/{pid}/reject" '
        f'hx-target="#proposal-{short_id}" hx-swap="outerHTML">Reject</button>'
        f'</div>'
        f'</div>'
    )


# fleet-pipeline-v1 P2 — verb-bearing proposal types render their OWN action set
# by iterating the type's verb tuple (PROPOSAL_VERBS), not the generic
# approve/reject/dismiss. verb -> (route template, label, css, confirm-or-None).
# Adding a verb (e.g. "suggest_revision") is one dict entry + one tuple element.
_PROPOSAL_VERB_ROUTES = {
    "promote": (
        "/portal/actions/proposals/{pid}/promote", "Promote", "btn-approve",
        "Promote this draft — publish to Drive and update the row?",
    ),
    "reject": (
        "/portal/actions/proposals/{pid}/reject", "Reject", "btn-reject", None,
    ),
}


def _verb_actions_html(proposal_id: str, short_id: str, verbs) -> str:
    """Render action buttons by iterating a proposal type's verb set. Unknown
    verbs (shaped-for but not yet routed, e.g. suggest_revision) are skipped."""
    pid = _esc(proposal_id)
    buttons = []
    for verb in verbs:
        spec = _PROPOSAL_VERB_ROUTES.get(verb)
        if spec is None:
            continue
        route, label, css, confirm = spec
        confirm_attr = f' hx-confirm="{_esc(confirm)}"' if confirm else ""
        buttons.append(
            f'<button class="btn {css}" '
            f'hx-post="{route.format(pid=pid)}" '
            f'hx-target="#proposal-{short_id}" hx-swap="outerHTML"'
            f"{confirm_attr}>{label}</button>"
        )
    return f'<div class="proposal-actions">{"".join(buttons)}</div>'


def _proposal_card_html(request: web.Request, p: dict) -> str:
    """Render ONE routing/kaizen proposal card — RED bespoke or generic. Shared by
    the flat feed and the grouped-by-proposer view (proposal-proposer-attribution-v1
    Move 2b), so both surfaces render byte-identical cards."""
    pid = p.get("proposal_id", "")
    short_id = _short_id(pid)
    ptype = p.get("type")
    # propose-approve-deadlock-v1 Phase 1b-ii — a RED .env proposal renders a bespoke
    # masked/two-step card, never the generic one-tap row.
    if ptype == RED_PENDING_PROPOSAL_TYPE:
        return _render_red_proposal_card(request, pid, short_id)
    evidence = p.get("evidence")
    if isinstance(evidence, dict):
        ev_summary = ", ".join(f"{k}: {v}" for k, v in list(evidence.items())[:6])
    elif isinstance(evidence, (list, tuple)):
        ev_summary = f"{len(evidence)} item(s)"
    else:
        ev_summary = str(evidence) if evidence else ""
    # Verb-bearing types (P2) render their own action set; everything else keeps the
    # generic approve/reject/dismiss row.
    verbs = PROPOSAL_VERBS.get(ptype)
    actions = (
        _verb_actions_html(pid, short_id, verbs) if verbs
        else _proposal_actions_html(
            pid, short_id, offers_approve=_type_offers_approve(ptype)
        )
    )
    # forge-review-surface-v1 P2 (M2) — verb-bearing (forge) cards carry a "View
    # details" link; a missing slug degrades in place, never crashes the feed.
    view_html = ""
    if verbs:
        slug = (p.get("payload") or {}).get("slug")
        if slug:
            # Hash anchor (C1): the ?pid rides the hash and becomes a real query
            # string when the router's htmx.ajax GET dispatches it.
            href = f"/portal#fragments/forge/{quote(str(slug))}/?pid={quote(str(pid))}"
            view_html = (
                f'<div class="meta"><a href="{_esc(href)}">View details</a></div>'
            )
        else:
            view_html = (
                '<div class="meta error">view unavailable — missing payload.slug</div>'
            )
    return (
        f'<div class="card" id="proposal-{short_id}">'
        f'<h4><span class="badge">{_esc(ptype)}</span></h4>'
        f'<p>{_esc(p.get("semantic_justification"))}</p>'
        f'<div class="meta">evidence: {_esc(ev_summary)}</div>'
        f'<div class="meta">created {_esc(p.get("created_at"))}</div>'
        f'{view_html}'
        f'{actions}'
        f'</div>'
    )


def _memory_card_html(m: dict) -> str:
    """Render ONE memory-crystallization card (shared by both views)."""
    pid = m.get("proposal_id", "")
    short_id = _short_id(pid)
    return (
        f'<div class="card" id="proposal-{short_id}">'
        f'<h4><span class="badge">{_esc(m.get("type"))}</span> '
        f'<span class="badge">{_esc(m.get("action"))}</span></h4>'
        f'<p>{_esc(m.get("semantic_justification"))}</p>'
        f'<div class="meta">created {_esc(m.get("created_at"))}</div>'
        f'{_proposal_actions_html(pid, short_id, offers_approve=_type_offers_approve(m.get("type")))}'
        f'</div>'
    )


def _proposals_view_toggle(grouped: bool) -> str:
    """proposal-proposer-attribution-v1 Move 2b — the flat <-> grouped view toggle.
    Both views COEXIST; the flat newest-first feed (proposal-sort-v1) is the default."""
    base = "/portal/fragments/proposals/pending"
    flat_active = "" if grouped else " active"
    grp_active = " active" if grouped else ""
    return (
        '<div class="view-toggle">'
        f'<a class="toggle{flat_active}" hx-get="{base}" '
        'hx-target="#proposals-listing" hx-swap="outerHTML">Newest first</a> '
        f'<a class="toggle{grp_active}" hx-get="{base}?view=grouped" '
        'hx-target="#proposals-listing" hx-swap="outerHTML">By proposer</a>'
        '</div>'
    )


async def handle_proposals_pending(request: web.Request) -> web.Response:
    """List pending Kaizen proposals as cards with approve/reject/dismiss (P4).

    Two COEXISTING views (proposal-proposer-attribution-v1 Move 2b): the default
    FLAT newest-first feed (proposal-sort-v1), and ``?view=grouped`` — per-proposer
    sections, groups ordered by their most-recent proposal, newest-first within
    (inherits the flat sort). Unifies routing proposals (``proposals.jsonl``) and
    memory crystallizations (``memory_proposals.jsonl``).
    """
    proposals = [p.to_dict() for p in read_all_proposals()]
    # proposal-sort-v1 — render-only newest-first sort. created_at is ISO 8601 UTC on
    # every proposal, so a lexical sort IS chronological. read_all's append-order
    # contract (proposal_queue.py) is UNTOUCHED — this sorts only the local render
    # copy. A missing/empty created_at sorts LAST under reverse=True (unknown-age
    # proposals sink to the bottom). Matches the fleet viewer's newest-first order.
    proposals.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    memory_items = pending_memory_proposal_items()
    grouped = request.query.get("view") == "grouped"

    parts = ['<div id="proposals-listing">', _proposals_view_toggle(grouped)]
    if not proposals and not memory_items:
        parts.append(
            '<p class="placeholder">No pending proposals — the system has '
            'nothing to recommend changing.</p>'
        )
    elif grouped:
        # Bucket by proposer (proposals already newest-first). Groups ordered by
        # their MOST-RECENT proposal (each group's first item, since the input is
        # sorted); within a group newest-first is inherited. "unattributed" (legacy)
        # and "governance" (RED) are just proposers → their own sections.
        groups: dict = {}
        for p in proposals:
            groups.setdefault(p.get("proposer") or "unattributed", []).append(p)
        ordered = sorted(
            groups.items(),
            key=lambda kv: (kv[1][0].get("created_at") or "") if kv[1] else "",
            reverse=True,
        )
        for proposer, plist in ordered:
            parts.append(
                f'<section class="proposer-group" data-proposer="{_esc(proposer)}">'
                f'<h3 class="proposer-head">{_esc(proposer)} '
                f'<span class="count">({len(plist)})</span></h3>'
            )
            for p in plist:
                parts.append(_proposal_card_html(request, p))
            parts.append('</section>')
        if memory_items:
            parts.append(
                '<section class="proposer-group" data-proposer="memory">'
                '<h3 class="proposer-head">memory</h3>'
            )
            for m in memory_items:
                parts.append(_memory_card_html(m))
            parts.append('</section>')
    else:
        for p in proposals:
            parts.append(_proposal_card_html(request, p))
        for m in memory_items:
            parts.append(_memory_card_html(m))
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
    turn refreshes this sidebar via the page's OOB swap). Hash anchor (C1) —
    the shell's hash router dispatches it."""
    return (
        f'<li><a href="/portal#fragments/cellar/pages/{_esc(page_id)}">'
        f'{_esc(title)}</a></li>'
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
        f'<a href="/portal#fragments/cellar/pages/{_esc(page_id)}">'
        f'{_esc(r.title or page_id)}</a>'
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
# Routing model-swap page (portal-model-swap-v1)
# ---------------------------------------------------------------------------

# telemetry-tier-decoupling-v1: the operator manages every model-bound tier from
# the portal — T1/T2/T3 plus the now-separate Telemetry tier (and any future
# operator-added tier). The list is read LIVE from tier_preferences rather than
# hardcoded, so adding a tier in routing.config.yaml surfaces a card with no code
# change. Handler-backed tiers (T0 pattern_cache) carry no model and get no card.
_TIER_SORT_ORDER = {"Telemetry": 0, "T1": 1, "T2": 2, "T3": 3}


def _swappable_tiers() -> tuple[str, ...]:
    """Return the model-bound tiers from tier_preferences, ordered for the portal
    swap panel: Telemetry first (the classifier), then T1-T3 in canonical order,
    then any operator-added tiers alphabetically. Excludes handler-backed tiers
    (T0 pattern_cache) that carry no model field."""
    prefs = _live_tier_preferences()
    tiers = [
        name for name, entry in prefs.items()
        if isinstance(entry, dict) and "model" in entry
    ]
    tiers.sort(key=lambda t: (_TIER_SORT_ORDER.get(t, 100), t))
    return tuple(tiers)


def _live_tier_preferences() -> dict:
    """Re-read tier_preferences from the operator routing.config.yaml (N2).

    Read path, so ``yaml.safe_load`` (comments irrelevant once parsed). Returns
    the tier->entry mapping, or ``{}`` when the file is absent/malformed. The
    portal renders ``previous_model`` from here — the live router's frozen
    TierConfig does not carry it, so the file is the source of truth for the
    card and for the post-swap re-render."""
    path = Path(get_hermes_home()) / "routing.config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    tier_prefs = ((data or {}).get("routing") or {}).get("tier_preferences")
    return tier_prefs if isinstance(tier_prefs, dict) else {}


def _model_options_html(catalog: list, current_slug) -> str:
    """``<option>`` set for the model dropdown, current model selected. A current
    slug that is off-catalog renders as a selected leading option so the operator
    sees the truth (mirrors the dock status-select idiom)."""
    slugs = {m["slug"] for m in catalog}
    options: list[str] = []
    if current_slug and current_slug not in slugs:
        options.append(
            f'<option value="{_esc(current_slug)}" selected>'
            f'{_esc(current_slug)} (not in catalog)</option>'
        )
    for m in catalog:
        sel = " selected" if m["slug"] == current_slug else ""
        options.append(
            f'<option value="{_esc(m["slug"])}"{sel}>{_esc(m["display_name"])}</option>'
        )
    return "".join(options)


def render_tier_card(tier: str, config, catalog: list, error: str | None = None) -> str:
    """One routing-tier card — a self-contained ``<div id="tier-{tier}">`` so
    HTMX swaps it alone (N1). Shows the current model (catalog display name), a
    display-only cost, a model dropdown, a Swap button, and a Revert button shown
    only when ``previous_model`` is on record (AC-6). Every interpolated value
    passes through ``_esc`` (C4). ``error``, when set, renders inline so a failed
    swap keeps the card and shows why (C3)."""
    config = config or {}
    current = config.get("model")
    previous = config.get("previous_model")
    by_slug = {m["slug"]: m for m in catalog}
    entry = by_slug.get(current)
    display = entry["display_name"] if entry else (current or "(unbound)")
    if entry:
        cost = (
            f'${entry["input_cost_per_mtok"]} in / '
            f'${entry["output_cost_per_mtok"]} out per Mtok (display-only)'
        )
    else:
        cost = "cost unknown — model not in catalog"

    tier_e = _esc(tier)
    revert_btn = ""
    if previous:
        prev_disp = by_slug.get(previous, {}).get("display_name", previous)
        revert_btn = (
            f'<button type="button" class="btn btn-secondary" '
            f'hx-post="/portal/actions/routing/revert" hx-include="closest form" '
            f'hx-target="#tier-{tier_e}" hx-swap="outerHTML">'
            f'Revert to {_esc(prev_disp)}</button>'
        )
    error_html = f'<div class="meta error">{_esc(error)}</div>' if error else ""

    return (
        f'<div class="card" id="tier-{tier_e}">'
        f'<h4>{tier_e} <span class="badge">{_esc(display)}</span></h4>'
        f'<div class="meta">{_esc(cost)}</div>'
        f'<form class="tier-form">'
        f'<input type="hidden" name="tier" value="{tier_e}">'
        f'<select name="model_slug" '
        f'hx-get="/portal/fragments/routing/model" hx-trigger="change" '
        f'hx-target="#right-panel" hx-swap="outerHTML">'
        f'{_model_options_html(catalog, current)}</select>'
        f'<button type="button" class="btn" '
        f'hx-post="/portal/actions/routing/swap" hx-include="closest form" '
        f'hx-target="#tier-{tier_e}" hx-swap="outerHTML">Swap</button>'
        f'{revert_btn}'
        f'</form>'
        f'{error_html}'
        f'</div>'
    )


def render_routing_fragment(config, catalog: list) -> str:
    """The model-routing cards as a self-contained fragment for ``#center-panel``.

    Wrapped in ``<div id="routing-panel">`` so the portal nav can swap the whole
    panel, while each tier card keeps its own ``#tier-{tier}`` id for in-place
    swap (the swap/revert handlers target those directly). ``config`` is the
    tier_preferences mapping. Tiers are read live via ``_swappable_tiers()``
    (Telemetry + T1-T3 + any operator-added). This is the single source of the
    card markup — ``render_routing_page`` wraps it for the standalone page (A2).
    """
    cards = "".join(
        render_tier_card(t, (config or {}).get(t), catalog)
        for t in _swappable_tiers()
    )
    return (
        '<div id="routing-panel">'
        "<h2>Tier model bindings</h2>"
        '<p class="meta">Swap the model bound to a tier. The change is validated '
        "and hot-reloaded; the next turn uses the new model. Costs are "
        "display-only heuristics.</p>"
        f'<div class="tier-cards">{cards}</div>'
        "</div>"
    )


def render_routing_page(config, catalog: list) -> str:
    """The standalone ``/portal/routing`` page — the routing fragment in a full
    HTML shell that loads the same stylesheet and HTMX runtime as the portal SPA.
    Kept for direct full-page access; the in-shell path loads
    ``render_routing_fragment`` via the Models nav item."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Operator Portal — Model Routing</title>\n"
        '<link rel="stylesheet" href="/portal/static/style.css">\n'
        '<script src="/portal/static/htmx.min.js" defer></script>\n'
        "</head>\n<body>\n"
        '<header class="topbar"><div class="brand">grove-autonomaton '
        '<span class="brand-sub">Model Routing</span></div></header>\n'
        '<main class="layout"><section class="center-panel">'
        f"{render_routing_fragment(config, catalog)}"
        "</section></main>\n</body>\n</html>\n"
    )


async def handle_routing_page(request: web.Request) -> web.Response:
    """Serve the standalone ``/portal/routing`` page (full HTML, opened directly
    in a browser). Reads the live tier_preferences and the model catalog."""
    from grove.config.model_catalog import load_catalog

    return web.Response(
        text=render_routing_page(_live_tier_preferences(), load_catalog()),
        content_type="text/html",
    )


async def handle_routing_panel(request: web.Request) -> web.Response:
    """The Models nav panel — routing cards as a fragment for ``#center-panel``.

    Same content as ``/portal/routing`` minus the standalone shell, so the portal
    SPA loads it like every other category. Reachable via the hash router at
    ``/portal#fragments/routing/panel``.
    """
    from grove.config.model_catalog import load_catalog

    return _html_fragment(
        render_routing_fragment(_live_tier_preferences(), load_catalog())
    )


def render_model_context(slug: str, catalog: list) -> str:
    """The ``#right-panel`` detail for one catalog model — display name, slug,
    provider, display-only cost, notes, and which tiers currently bind it.

    Returned as a full ``<div id="right-panel">`` for an outerHTML swap (the same
    convention as ``handle_context``). Every value is ``_esc``'d (C4). Lets the
    operator read cost/notes before committing a swap.
    """
    entry = next((m for m in catalog if m.get("slug") == slug), None)
    bound = [
        t for t, e in (_live_tier_preferences() or {}).items()
        if isinstance(e, dict) and e.get("model") == slug
    ]
    bound_html = (
        f'<div class="meta">Currently bound to: {_esc(", ".join(sorted(bound)))}</div>'
        if bound else '<div class="meta">Not currently bound to any tier.</div>'
    )
    if entry is None:
        body = (
            f"<h3>{_esc(slug) or 'No model selected'}</h3>"
            f'<div class="meta error">Not in the model catalog.</div>'
            f"{bound_html}"
        )
    else:
        body = (
            f'<h3>{_esc(entry["display_name"])}</h3>'
            f"<div class=\"meta\"><code>{_esc(slug)}</code></div>"
            f'<dl class="model-detail">'
            f"<dt>Provider</dt><dd>{_esc(entry.get('provider'))}</dd>"
            f"<dt>Input</dt><dd>${_esc(entry.get('input_cost_per_mtok'))} / Mtok</dd>"
            f"<dt>Output</dt><dd>${_esc(entry.get('output_cost_per_mtok'))} / Mtok</dd>"
            f"</dl>"
            f"<p class=\"meta\">{_esc(entry.get('notes') or '')}</p>"
            f'<p class="meta">Costs are display-only heuristics.</p>'
            f"{bound_html}"
        )
    return f'<div id="right-panel" class="right-panel">{body}</div>'


async def handle_model_context(request: web.Request) -> web.Response:
    """Right-panel detail for the model named by ``?model_slug``.

    Loaded when the operator changes a tier's model dropdown (HTMX sends the
    select's value as the ``model_slug`` query param), so cost/provider/notes are
    visible before the swap is committed.
    """
    from grove.config.model_catalog import load_catalog

    slug = request.query.get("model_slug", "")
    return _html_fragment(render_model_context(slug, load_catalog()))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fleet fragments (fleet-artifact-viewer-v1; in-shell since
# fleet-ui-reconciliation-v1 C1)
# ---------------------------------------------------------------------------
#
# Fleet renders IN-SHELL: ``/portal/fragments/fleet/...`` fragments the shell's
# hash router dispatches into #center-panel. A Telegram deep link is the hash
# form ``/portal#fragments/fleet/...`` — the shell loads first, then the router
# dispatches, so the operator always lands in the full styled portal. The
# legacy standalone paths (``/portal/fleet/...``) 302 to the hash URLs so
# previously-sent links keep landing. Readers are the shared portal.py fleet
# readers (_fleet_skill_records / _list_fleet_artifacts / _read_fleet_artifact),
# never the filesystem.


def _fleet_breadcrumb(skill: str = "", filename: str = "") -> str:
    """``Fleet > {skill} > {filename}`` — each ancestor an in-shell hash link
    (the hash router dispatches it into #center-panel), the leaf plain."""
    crumbs = ['<a href="/portal#fragments/fleet/">Fleet</a>']
    if skill:
        crumbs.append(
            f'<a href="/portal#fragments/fleet/{_esc(skill)}/">{_esc(skill)}</a>'
        )
    if filename:
        crumbs.append(_esc(filename))
    return f'<p class="meta breadcrumb">{" &rsaquo; ".join(crumbs)}</p>'


def _fleet_zone_badge(zone: str) -> str:
    return f'<span class="badge {_ZONE_BADGE.get(zone, "badge")}">{_esc(zone)}</span>'


def _fleet_state_badge(state: str) -> str:
    """canonical -> green, pending_review -> yellow ('pending review' label)."""
    if state == "pending_review":
        return '<span class="badge badge-yellow">pending review</span>'
    if state == "canonical":
        return '<span class="badge badge-green">canonical</span>'
    return f'<span class="badge">{_esc(state)}</span>'


def _fleet_json_card(raw: str) -> str:
    """Structured card for a JSON artifact: top-level keys with scalar values
    inline (lists/objects summarized), plus the full raw JSON in a collapsible
    <details>. A parse failure surfaces the error and the raw text (fail loud,
    never a blank card)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (
            '<div class="card"><h4>Malformed JSON</h4>'
            f'<div class="meta error">{_esc(str(exc))}</div>'
            f"<pre>{_esc(raw)}</pre></div>"
        )
    rows = []
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                summary = f"{{{len(val)} field(s)}}"
            elif isinstance(val, list):
                summary = f"[{len(val)} item(s)]"
            else:
                summary = str(val)
            rows.append(
                f"<dt>{_esc(key)}</dt><dd>{_esc(summary)}</dd>"
            )
        fields = f'<dl class="model-detail">{"".join(rows)}</dl>' if rows else (
            '<p class="meta">Empty object.</p>'
        )
    else:
        fields = f'<p class="meta">Top-level JSON is a {_esc(type(data).__name__)}.</p>'
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    return (
        f'<div class="card">{fields}</div>'
        f"<details><summary>Raw JSON</summary><pre>{_esc(pretty)}</pre></details>"
    )


async def handle_fleet_overview(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/`` — the fleet overview fragment: one card
    per skill, in the WIDE content primitive (C1; the board layout lands in C2)."""
    records = _fleet_skill_records()
    cards = []
    for name in sorted(records):
        cap = records[name]
        try:
            arts = _list_fleet_artifacts(cap)
        except KeyError:
            # Parity with the API index: skip a malformed record, don't blank all.
            logger.warning("[portal] fleet skill %s malformed, skipping card", name)
            continue
        latest = arts[0]["mtime"] if arts else "—"
        cards.append(
            f'<div class="card"><h4>'
            f'<a href="/portal#fragments/fleet/{_esc(name)}/">{_esc(name)}</a> '
            f'{_fleet_zone_badge(cap.zone.value)}</h4>'
            f'<div class="meta">{len(arts)} artifact(s) &middot; '
            f'latest {_esc(latest)}</div></div>'
        )
    body = "<h2>Fleet artifacts</h2>" + (
        "".join(cards) if cards
        else '<p class="placeholder">No fleet skills with artifacts yet.</p>'
    )
    return _html_fragment(f'<div class="content wide">{body}</div>')


async def handle_fleet_skill_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/{skill_name}/`` — one producer's inbox as an
    in-shell fragment. Unknown skill -> 404 fragment."""
    skill_name = request.match_info["skill_name"]
    cap = _fleet_skill_records().get(skill_name)
    if cap is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — not found</h3>'
            f'<p class="placeholder">Unknown fleet skill: {_esc(skill_name)}</p></div>',
            status=404,
        )
    # fleet-review-unification-v1 C3 — the producer INBOX (Mount 1): C2 four-state
    # units as .review-cards with the inline disposition component. The Promote
    # consequence copy is sink-derived (forge → Drive; mv-sink → wiki).
    units = _list_fleet_units(cap)
    remote_sink = cap.governance["write_zone"]["canonical_dir"] == "forge"
    needs = sum(1 for u in units if u["governance_state"] == "needs_review")
    pill_cls = "pending-pill" + ("" if needs else " zero")
    header = (
        f'<div class="inbox-head"><h2>{_esc(skill_name)} '
        f'{_fleet_zone_badge(cap.zone.value)}</h2>'
        f'<span class="{pill_cls}">{needs} needs review</span></div>'
    )
    if units:
        cards = "".join(_review_card(u, remote_sink, cap=cap) for u in units)
    else:
        cards = ('<div class="card"><p class="placeholder">No artifacts yet — '
                 'this producer is idle.</p></div>')
    return _html_fragment(
        f'<div class="content">{_fleet_breadcrumb(skill_name)}{header}{cards}</div>'
    )


async def handle_fleet_artifact_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/{skill_name}/{filename}`` — the full artifact
    view as an in-shell fragment: rendered markdown for ``.md``, a structured card
    + collapsible raw for ``.json``. When the artifact resolves to a C2 unit, the
    Mount-2 disposition dock rides an OOB ``#right-panel`` swap into its native
    300px habitat (C1). Unknown skill or missing artifact -> 404 fragment."""
    skill_name = request.match_info["skill_name"]
    filename = request.match_info["filename"]
    cap = _fleet_skill_records().get(skill_name)
    read = _read_fleet_artifact(cap, filename) if cap is not None else None
    if read is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — not found</h3>'
            f'<p class="placeholder">Artifact not found: '
            f'{_esc(skill_name)}/{_esc(filename)}</p></div>',
            status=404,
        )
    raw, suffix, state = read
    if suffix == ".md":
        content = f'<article class="cellar-body">{_render_md(raw)}</article>'
    elif suffix == ".json":
        content = _fleet_json_card(raw)
    else:
        content = f"<pre>{_esc(raw)}</pre>"
    header = (
        f"<h2>{_esc(filename)}</h2>"
        f'<p class="meta">{_esc(skill_name)} &middot; {_fleet_state_badge(state)}</p>'
    )
    body = (
        f'<div class="content">'
        f"{_fleet_breadcrumb(skill_name, filename)}{header}{content}</div>"
    )
    # Mount 2 (C1) — the disposition dock lands in #right-panel via the OOB swap
    # pattern (same mechanic as the cellar detail's context OOB). No unit → no
    # dock: a canonical-only or unresolvable file has nothing to disposition.
    unit = _find_fleet_unit(cap, filename=filename)
    oob = ""
    if unit is not None:
        remote_sink = cap.governance["write_zone"]["canonical_dir"] == "forge"
        oob = (
            f'<div id="right-panel" class="right-panel" hx-swap-oob="true">'
            f'{_disposition_dock(unit, remote_sink, skill_name)}</div>'
        )
    return _html_fragment(body + oob)


def render_forge_publish_card(
    slug: str, *, published: bool = False, error: str | None = None,
    folder_link: str | None = None,
) -> str:
    """The forge Publish card — a self-contained ``<div id="forge-publish-{slug}">``
    so HTMX swaps it alone (mirrors render_tier_card). Default state shows a
    Publish button; ``error`` renders inline and KEEPS the button (retry-safe —
    the orchestrator's exists-guard makes a re-tap idempotent); ``published``
    renders the terminal success state (folder link + Status->Drafted), no
    button. Every interpolated value passes through ``_esc``."""
    slug_e = _esc(slug)
    if published:
        link = _esc(folder_link or "")
        return (
            f'<div class="card card-resolved" id="forge-publish-{slug_e}">'
            f'<h4>Published <span class="badge badge-green">Drafted</span></h4>'
            f'<div class="meta">Application package created; the Notion row was '
            f'updated (Status &rarr; Drafted).</div>'
            f'<div class="meta"><a href="{link}" target="_blank" rel="noopener">'
            f'Open the Drive folder</a></div></div>'
        )
    error_html = ""
    if error:
        error_html = f'<div class="meta error">{_esc(error)}</div>'
    if folder_link:
        error_html += (
            f'<div class="meta"><a href="{_esc(folder_link)}" target="_blank" '
            f'rel="noopener">Drive folder (already created)</a></div>'
        )
    return (
        f'<div class="card" id="forge-publish-{slug_e}">'
        f'<h4>Publish application package</h4>'
        f'<div class="meta">Creates a Drive folder with the two Docs, writes the '
        f'link to the Notion row, and flips Status to Drafted.</div>'
        f'{error_html}'
        f'<button type="button" class="btn" '
        f'hx-post="/portal/actions/forge/{slug_e}/publish" '
        f'hx-target="#forge-publish-{slug_e}" hx-swap="outerHTML">Publish</button>'
        f'</div>'
    )


def _forge_slug_body(
    slug: str, read: dict, *, pid: str | None = None, include_publish: bool = True
) -> str:
    """Inner HTML for a forge draft dir — the h2 + zone badge + subtitle, an
    OPTIONAL Publish card, and the resume.md / cover-letter.md rendered-markdown
    articles. Sole consumer is ``handle_forge_slug_fragment`` (C1 retired the
    standalone slug-dir page), which passes ``include_publish = pid is None`` —
    the Publish card renders for bare visits (the retired page's affordance),
    and is omitted when a ``?pid`` drives disposition. ``pid`` itself is NOT
    consumed here."""
    meta = read["meta"]
    if meta and all(meta.get(k) for k in ("row_id", "company", "role")):
        publish = render_forge_publish_card(slug)
        subtitle = f'{_esc(meta.get("company", ""))} &mdash; {_esc(meta.get("role", ""))}'
    else:
        why = read["meta_error"] or "meta.json is missing row_id/company/role"
        publish = (
            f'<div class="card" id="forge-publish-{_esc(slug)}">'
            f'<h4>Publish unavailable</h4>'
            f'<div class="meta error">{_esc(why)} — cannot publish without the '
            f'row identity.</div></div>'
        )
        subtitle = "(meta.json incomplete)"
    publish_html = publish if include_publish else ""
    return (
        f'<h2>{_esc(slug)} {_fleet_zone_badge("yellow")}</h2>'
        f'<p class="meta">forge-jobsearch &middot; {subtitle}</p>'
        f"{publish_html}"
        f"<h3>resume.md</h3>"
        f'<article class="cellar-body">{_render_md(read["resume_md"])}</article>'
        f"<h3>cover-letter.md</h3>"
        f'<article class="cellar-body">{_render_md(read["cover_md"])}</article>'
    )


# ---------------------------------------------------------------------------
# Legacy standalone-path redirects (fleet-ui-reconciliation-v1 C1).
#
# The ``/portal/fleet/...`` standalone pages are RETIRED; fleet renders in-shell.
# These 302s keep every previously-sent deep link (Telegram push notes, skill
# handoff messages) landing on the same content — now inside the shell via the
# hash router. The fragment tail mirrors the legacy tail 1:1, except the forge
# slug dir, whose in-shell home is the existing forge fragment.
# ---------------------------------------------------------------------------


async def handle_fleet_overview_redirect(request: web.Request) -> web.Response:
    """302 ``/portal/fleet/`` → ``/portal#fragments/fleet/``."""
    raise web.HTTPFound("/portal#fragments/fleet/")


async def handle_fleet_skill_redirect(request: web.Request) -> web.Response:
    """302 ``/portal/fleet/{skill}/`` → ``/portal#fragments/fleet/{skill}/``."""
    skill = quote(request.match_info["skill_name"], safe="")
    raise web.HTTPFound(f"/portal#fragments/fleet/{skill}/")


async def handle_fleet_artifact_redirect(request: web.Request) -> web.Response:
    """302 ``/portal/fleet/{skill}/{filename}`` →
    ``/portal#fragments/fleet/{skill}/{filename}``."""
    skill = quote(request.match_info["skill_name"], safe="")
    filename = quote(request.match_info["filename"], safe="")
    raise web.HTTPFound(f"/portal#fragments/fleet/{skill}/{filename}")


async def handle_forge_slug_redirect(request: web.Request) -> web.Response:
    """302 ``/portal/fleet/forge-jobsearch/{slug}/`` →
    ``/portal#fragments/forge/{slug}/`` (the existing in-shell forge fragment —
    the slug-DIR viewer's body builder is shared, so content parity holds)."""
    slug = quote(request.match_info["slug"], safe="")
    raise web.HTTPFound(f"/portal#fragments/forge/{slug}/")


# ---------------------------------------------------------------------------
# fleet-review-unification-v1 C3 — the Action Surface disposition component.
# Producer-agnostic; rendered identically at both mounts (inbox card footer +
# detail dock). Tokens/CSS from style.css (the C3 block); verbs POST the EXISTING
# promote/reject/suggest_revision routes for BOTH proposal types (no write-path
# change). State → rail/chip label from _STATE_META.
# ---------------------------------------------------------------------------

_STATE_META = {
    "needs_review":       ("needs review", "rail-needs_review", "chip-needs_review"),
    "revision_requested": ("redrafting", "rail-revision_requested", "chip-revision_requested"),
    "promoted":           ("promoted", "rail-promoted", "chip-promoted"),
    "rejected":           ("rejected", "rail-rejected", "chip-rejected"),
    "legacy":             ("legacy", "rail-legacy", "chip-legacy"),
}
_REVISION_MAX = 3  # mirrors grove.api.actions._REVISION_MAX (the N-breaker copy)


def _state_meta(state: str) -> tuple:
    return _STATE_META.get(state, (state, "rail-legacy", "chip-legacy"))


def _relative_age(iso: str) -> str:
    """A compact relative age ('just now' / 'N min ago' / 'N h ago' / 'yesterday' /
    'N days ago') from a C2 ISO-8601 mtime — the mock's timestamp style. Falls back to
    the raw string on a parse miss: a display helper must never raise."""
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return iso
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 172800:
        return "yesterday"
    return f"{int(secs // 86400)} days ago"


def _state_chip(state: str) -> str:
    label, _rail, chip = _state_meta(state)
    return (f'<span class="state-chip {chip}"><span class="dot"></span>'
            f'{_esc(label)}</span>')


def _disposition_bar(pid: str, remote_sink: bool, revision_count: int = 0) -> str:
    """The producer-agnostic disposition bar (evolves the C1a ``_disposition_actions_div``):
    stacked full-width Promote / "Suggest revision…" / Reject, plus the progressively-
    disclosed feedback block. The SAME pid-keyed routes both proposal types use.

    ``remote_sink`` selects the Promote consequence copy ("publish to Drive" for forge,
    "ingest to wiki" for an mv-sink). Verb responses land in a per-unit ``#disp-result-*``
    div (innerHTML) — the ``_resolved_card`` on success, a VISIBLE failure message on a
    non-2xx (the routes' OOB #alert-banner also fires). The colon-free ``_short_id`` DOM
    id is load-bearing: ``compute_proposal_id`` yields ``sha256:<hex>`` and a ':' in a CSS
    #id selector breaks ``hx-include`` (the C1a Andon). hx-post keeps the raw pid (URL-legal)."""
    pe = _esc(pid)
    short = _short_id(pid)
    rev_id = "rev-" + short
    result = "disp-result-" + short
    consequence = "publish to Drive" if remote_sink else "ingest to wiki"
    counter = (f"Revision {revision_count + 1} of {_REVISION_MAX} — after "
               f"{_REVISION_MAX} marked won't-converge.")
    on_after = (
        "if(!event.detail.successful){"
        f"var r=document.getElementById('{result}');"
        "if(r){r.textContent='Disposition failed — see the alert above; "
        "you are still on the draft.'}}"
    )
    return f"""<div class="disposition-bar" hx-on::after-request="{on_after}">\
<button class="btn btn-approve btn-promote" hx-post="/portal/actions/proposals/{pe}/promote" \
hx-target="#{result}" hx-swap="innerHTML" \
hx-confirm="Promote this draft — {consequence} and resolve the unit?">\
Promote &mdash; {_esc(consequence)}</button>\
<button class="btn btn-revise" type="button" \
onclick="this.closest('.disposition-bar').querySelector('.feedback-block').classList.toggle('open')">\
Suggest revision&hellip;</button>\
<button class="btn btn-reject btn-reject-s" hx-post="/portal/actions/proposals/{pe}/reject" \
hx-target="#{result}" hx-swap="innerHTML" \
hx-confirm="Reject this draft — archive it and dismiss the proposal?">Reject</button>\
<div class="feedback-block">\
<textarea id="{rev_id}" name="revision_text" class="revision-text" rows="3" \
placeholder="Revision guidance for the next draft (what to change)."></textarea>\
<div class="feedback-row">\
<button class="btn btn-approve" hx-post="/portal/actions/proposals/{pe}/suggest_revision" \
hx-target="#{result}" hx-swap="innerHTML" hx-include="#{rev_id}">Send guidance &amp; redraft</button>\
<button class="btn btn-secondary" type="button" \
onclick="this.closest('.feedback-block').classList.remove('open')">Cancel</button>\
</div>\
<div class="revision-counter">{_esc(counter)}</div>\
</div>\
<div id="{result}"></div>\
</div>"""


def _disposition_actions_div(pid: str, producer: str = "forge") -> str:
    """C1a compat shim — the bare disposition bar for the forge in-shell fragment.
    fleet-review-unification-v1 C3 folded this into ``_disposition_bar``; forge is a
    remote-publish sink."""
    return _disposition_bar(pid, remote_sink=(producer == "forge"))


def _revised_disclosure(unit: dict) -> str:
    """The REVISED disclosure — a redraft (revision_count>0) quotes the operator's
    latest directive so the card announces why it re-drafted."""
    if unit.get("revision_count", 0) > 0 and unit.get("directive_echo"):
        return f'<div class="revised-disclosure">{_esc(unit["directive_echo"])}</div>'
    return ""


def _unit_footer(unit: dict, remote_sink: bool) -> str:
    """The per-state footer: the disposition bar (needs_review), the banked-guidance
    in-flight note (revision_requested), or nothing (terminal / legacy)."""
    state = unit["governance_state"]
    if state == "needs_review" and unit.get("proposal_id"):
        return _disposition_bar(unit["proposal_id"], remote_sink,
                                unit.get("revision_count", 0))
    if state == "revision_requested":
        note = unit.get("directive_echo") or ""
        quoted = f' <em>{_esc(note)}</em>' if note else ""
        return (f'<div class="inflight-note">Guidance banked; redrafts next '
                f'cycle.{quoted}</div>')
    return ""  # promoted / rejected (dimmed) / legacy (list-only) — no actions


def _unit_preview(cap: Any, unit: dict, limit: int = 2000) -> str:
    """A bounded first-chars preview of a unit's primary content — the staged dir's
    first non-meta content file, or the canonical/flat file. Best-effort: returns ''
    when nothing resolves (a read-side view must not 500 on a stray file)."""
    try:
        _z, staging, canonical, _p = _fleet_zone_dirs(cap)
        fn = unit.get("filename")
        if fn:
            for base in (staging, canonical):
                cand = base / fn
                if cand.is_file():
                    return cand.read_text(encoding="utf-8", errors="replace")[:limit]
            d = staging / fn
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.name != "meta.json":
                        return f.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        pass
    return ""


def _draft_preview_block(cap: Any, unit: dict) -> str:
    """The 3-line clamped content preview + an in-place "Show full draft" expand
    (toggles ``.expanded``, lifting the CSS line-clamp over the bounded read). Empty
    when no content resolves."""
    if cap is None:
        return ""
    text = _unit_preview(cap, unit)
    if not text:
        return ""
    return (
        f'<p class="draft-preview">{_esc(text)}</p>'
        f'<button class="btn btn-secondary preview-toggle" type="button" '
        f'''onclick="this.previousElementSibling.classList.toggle('expanded');'''
        f'''this.textContent=this.previousElementSibling.classList.contains('expanded')?'''
        f''''Collapse':'Show full draft'">Show full draft</button>'''
    )


def _review_card(unit: dict, remote_sink: bool, cap: Any = None) -> str:
    """Mount 1 — one unit as a ``.review-card`` (state rail + header chip + meta +
    REVISED disclosure + 3-line clamped preview + per-state footer). Terminals dim
    (``.card-resolved``); legacy is list-only."""
    state = unit["governance_state"]
    _label, rail, _chip = _state_meta(state)
    rc = unit.get("revision_count", 0)
    title = unit.get("filename") or unit["unit_id"]
    ver = f'draft v{rc + 1} &middot; ' if rc > 0 else ""
    dim = " card-resolved" if state in ("promoted", "rejected") else ""
    anchor = _short_id(unit.get("proposal_id") or unit["unit_id"])
    head = (
        f'<div class="review-head"><span class="title">{_esc(title)}</span>'
        f'{_state_chip(state)}'
        f'<span class="head-meta">{ver}{_esc(_relative_age(unit["mtime"]))}</span></div>'
    )
    return (
        f'<div class="card review-card {rail}{dim}" id="review-{anchor}">'
        f'{head}{_revised_disclosure(unit)}{_draft_preview_block(cap, unit)}'
        f'{_unit_footer(unit, remote_sink)}</div>'
    )


def _disposition_dock(unit: dict, remote_sink: bool, producer: str) -> str:
    """Mount 2 — the sticky ``.disposition-dock`` for a detail view: state-colored top
    border, a ``.dock-meta`` header, and the same disposition bar (or in-flight note).
    Works for forge AND file-producer detail fragments."""
    state = unit["governance_state"]
    _label, rail, _chip = _state_meta(state)
    rc = unit.get("revision_count", 0)
    ver = f'draft v{rc + 1} &middot; ' if rc > 0 else ""
    meta = (
        f'<div class="dock-meta"><span class="unit">'
        f'{_esc(unit.get("filename") or unit["unit_id"])}</span> {_state_chip(state)}'
        f'<br>{ver}{_esc(_relative_age(unit["mtime"]))} &middot; {_esc(producer)}</div>'
    )
    return (
        f'<div class="disposition-dock {rail}">{meta}'
        f'{_revised_disclosure(unit)}{_unit_footer(unit, remote_sink)}</div>'
    )


async def handle_forge_slug_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/forge/{slug}/`` — the forge draft as an in-shell
    fragment for ``#center-panel``. Since fleet-ui-reconciliation-v1 C1 this is
    ALSO the destination of the retired standalone slug-dir page (via 302), so it
    carries that page's Publish card whenever no ``?pid`` drives disposition
    (``include_publish = pid is None`` — the pid-driven proposals path is
    unchanged, Publish omitted as before).

    ``?pid=`` drives disposition (P2/M3). The Mount-2 disposition dock rides an
    OOB ``#right-panel`` swap (C1 — its native 300px habitat) when a C2 unit or a
    pid resolves. FAIL LOUD: neither → the draft still renders (reading is fine)
    but a VISIBLE "disposition unavailable — no pid" notice stays inline in the
    body, never a silent omission. Missing / unreadable slug dir -> 404."""
    slug = request.match_info["slug"]
    read = _read_forge_slug(slug)
    if read is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — forge draft not found</h3>'
            f'<p class="placeholder">No forge draft dir: {_esc(slug)}</p></div>',
            status=404,
        )
    pid = request.query.get("pid")
    body = _forge_slug_body(slug, read, pid=pid, include_publish=(pid is None))
    # fleet-review-unification-v1 C3 — Mount 2: forge adopts the sticky disposition
    # DOCK (state rail + dock-meta + the same bar), driven by the C2 unit. The route
    # URL is unchanged. pid gate + fail loud preserved.
    cap = _fleet_skill_records().get("forge-jobsearch")
    unit = _find_fleet_unit(cap, proposal_id=pid, filename=slug) if cap else None
    if unit is not None:
        dock = _disposition_dock(unit, remote_sink=True, producer="forge-jobsearch")
    elif pid:
        # C2 unit not resolvable (e.g. proposal already gone) but a pid is present —
        # render the bare bar in a dock shell so disposition still works.
        dock = (f'<div class="disposition-dock rail-needs_review">'
                f'{_disposition_bar(pid, remote_sink=True)}</div>')
    else:
        dock = None
    if dock is not None:
        oob = (f'<div id="right-panel" class="right-panel" hx-swap-oob="true">'
               f'{dock}</div>')
        return _html_fragment(f'<div class="content">{body}</div>{oob}')
    notice = ('<div class="meta error" id="forge-kaizen-none">'
              'disposition unavailable — no pid</div>')
    return _html_fragment(f'<div class="content">{body}{notice}</div>')


def _find_fleet_unit(cap: Any, *, proposal_id: str | None = None,
                     filename: str | None = None) -> Optional[dict]:
    """Find one C2 unit for *cap* by its open proposal_id or by its filename/dir name.
    None when neither matches (or cap is None)."""
    if cap is None:
        return None
    for u in _list_fleet_units(cap):
        if proposal_id and u.get("proposal_id") == proposal_id:
            return u
        if filename and u.get("filename") == filename:
            return u
    return None


async def handle_fleet_unit_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/{skill_name}/{unit}/`` — Mount 2 for a FILE
    producer: the unit's staged content + the C2 disposition dock, for the context
    sidebar. The generic sibling of the forge fragment (drafter/cultivator detail).
    Unknown skill / unit -> 404 fragment."""
    skill = request.match_info["skill_name"]
    unit_name = request.match_info["unit"]
    cap = _fleet_skill_records().get(skill)
    if cap is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — unknown fleet skill</h3>'
            f'<p class="placeholder">{_esc(skill)}</p></div>', status=404)
    unit = _find_fleet_unit(cap, filename=unit_name)
    if unit is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — unit not found</h3>'
            f'<p class="placeholder">{_esc(skill)}/{_esc(unit_name)}</p></div>',
            status=404)
    remote_sink = cap.governance["write_zone"]["canonical_dir"] == "forge"
    dock = _disposition_dock(unit, remote_sink, skill)
    content = _unit_preview(cap, unit, limit=100000)
    body = (
        f'<h2>{_esc(unit_name)} {_fleet_zone_badge(cap.zone.value)}</h2>'
        + (f'<article class="cellar-body">{_render_md(content)}</article>'
           if content else '<p class="placeholder">No staged content.</p>')
    )
    return _html_fragment(dock + body)


async def handle_portal_slash_redirect(request: web.Request) -> web.Response:
    """Redirect the trailing-slash ``/portal/`` to the canonical ``/portal`` shell.

    The shell route is registered at ``/portal`` (no trailing slash) and aiohttp
    does not auto-match the slash variant, so a user typing ``/portal/`` used to
    get a bare 404. Canonicalize it here (fleet-artifact-viewer-v1 smoke)."""
    raise web.HTTPFound("/portal")


def register_fragment_routes(app: web.Application) -> None:
    """Register the portal shell + ``/portal/fragments/*`` routes.

    Handlers land incrementally across Sprint P2 phases: shell (Phase 1);
    cellar (Phase 2); memory/dock/proposals/skills (Phase 3); context
    (Phase 4); search (Phase 5).
    """
    # Phase 1 — shell
    app.router.add_get("/portal", handle_portal_shell)
    # fleet-artifact-viewer-v1 — canonicalize the trailing-slash variant.
    app.router.add_get("/portal/", handle_portal_slash_redirect)
    # Phase 2 — cellar (listing + detail). {page_id:.+} carries the subdir-
    # qualified id (e.g. dock_goal/foo); the handler's containment guard blocks
    # path traversal.
    app.router.add_get("/portal/fragments/cellar/pages", handle_cellar_listing)
    app.router.add_get("/portal/fragments/cellar/pages/{page_id:.+}", handle_cellar_detail)
    # Phase 3 — memory, dock, proposals, skills
    app.router.add_get("/portal/fragments/memory/records", handle_memory_records)
    app.router.add_get("/portal/fragments/dock/goals", handle_dock_goals)
    app.router.add_get("/portal/fragments/proposals/pending", handle_proposals_pending)
    # forge-review-surface-v1 P1 — the forge draft body as an in-shell fragment
    # for load into #center-panel. Since C1 it is ALSO the 302 target of the
    # retired standalone /portal/fleet/forge-jobsearch/{slug}/ page (shared load
    # path _read_forge_slug, shared body builder _forge_slug_body).
    app.router.add_get("/portal/fragments/forge/{slug}/", handle_forge_slug_fragment)
    # fleet-review-unification-v1 C3 — Mount 2 for file producers: the generic unit
    # detail fragment (staged content + disposition dock). Forge keeps its own route
    # above; this serves drafter/cultivator/etc.
    app.router.add_get(
        "/portal/fragments/fleet/{skill_name}/{unit}/", handle_fleet_unit_fragment
    )
    app.router.add_get("/portal/fragments/skills/", handle_skills)
    # Phase 4 — context sidebar. {entity_id:.+} carries slash-bearing cellar
    # page_ids; entity_type is a single non-slash segment.
    app.router.add_get(
        "/portal/fragments/context/{entity_type}/{entity_id:.+}", handle_context
    )
    # Phase 5 — search
    app.router.add_get("/portal/fragments/search", handle_search)
    # portal-model-swap-v1 — standalone model-routing page (full HTML)
    app.router.add_get("/portal/routing", handle_routing_page)
    # portal-models-nav-v1 — Models nav panel (fragment for #center-panel)
    app.router.add_get("/portal/fragments/routing/panel", handle_routing_panel)
    # portal-models-nav-v1 — model detail for the right-panel (?model_slug=...)
    app.router.add_get("/portal/fragments/routing/model", handle_model_context)
    # fleet-ui-reconciliation-v1 C1 — fleet renders IN-SHELL: hash-routed
    # fragments (the two-segment unit fragment is registered above). The
    # overview registers at the bare dir path; the skill/artifact patterns
    # cannot collide with it (aiohttp {var} segments are non-empty).
    app.router.add_get("/portal/fragments/fleet/", handle_fleet_overview)
    app.router.add_get(
        "/portal/fragments/fleet/{skill_name}/", handle_fleet_skill_fragment
    )
    app.router.add_get(
        "/portal/fragments/fleet/{skill_name}/{filename}",
        handle_fleet_artifact_fragment,
    )
    # Legacy standalone paths 302 → hash URLs (previously-sent deep links).
    # The forge slug-dir redirect is registered before the generic single-file
    # patterns so the literal path wins (same ordering the pages used).
    app.router.add_get("/portal/fleet/", handle_fleet_overview_redirect)
    app.router.add_get(
        "/portal/fleet/forge-jobsearch/{slug}/", handle_forge_slug_redirect
    )
    app.router.add_get("/portal/fleet/{skill_name}/", handle_fleet_skill_redirect)
    app.router.add_get(
        "/portal/fleet/{skill_name}/{filename}", handle_fleet_artifact_redirect
    )
