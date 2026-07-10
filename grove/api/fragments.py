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
    _ARTIFACT_PROPOSAL_TYPES,
    _as_str_list,
    _check_cellar_stale,
    _check_memory_stale,
    _check_wiki_stale,
    _fleet_index_rows,
    _fleet_presentation,
    _fleet_skill_records,
    _fleet_worker_registry,
    _fleet_zone_dirs,
    _ledger_terminal_dispositions,
    _list_fleet_units,
    _read_fleet_artifact,
    _read_page,
    _serialize_capability,
    pending_memory_proposal_items,
)
from grove.capability import CapabilityKind
from grove.capability_registry import load_capabilities
from grove.dock import _VALID_STATUSES, load_dock
from grove.eval.proposal_queue import _type_offers_approve
from grove.forge import feedback_store
from grove.eval.proposal_queue import read_all as read_all_proposals
from grove.red_pending_store import RED_PENDING_PROPOSAL_TYPE
from grove.api.red_nonce import nonce_key_from_app, red_nonce
from grove.wiki.index import MalformedWikiPage, _split_frontmatter
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
# fleet-ui-reconciliation-v1 C3: artifact types no longer reach the PENDING FEED
# (partitioned into the Fleet cross-link), so the feed's verbs branch is gone —
# but this helper is NOT feed-only: the promote-failure card
# (actions._forge_promote_error_card) re-renders the verb buttons on a
# re-tappable failure, so it stays.
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
    Move 2b), so both surfaces render byte-identical cards.

    fleet-ui-reconciliation-v1 C3 — artifact-pending types never reach this
    renderer (the feed partitions them into the Fleet cross-link card), so the
    feed's verb-iterating branch and the forge View-details link were
    clean-deleted. The disposition ROUTES are untouched — the C3 fleet
    component posts the same endpoints from the Fleet surface."""
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
    actions = _proposal_actions_html(
        pid, short_id, offers_approve=_type_offers_approve(ptype)
    )
    return (
        f'<div class="card" id="proposal-{short_id}">'
        f'<h4><span class="badge">{_esc(ptype)}</span></h4>'
        f'<p>{_esc(p.get("semantic_justification"))}</p>'
        f'<div class="meta">evidence: {_esc(ev_summary)}</div>'
        f'<div class="meta">created {_esc(p.get("created_at"))}</div>'
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


def _partition_proposals(proposals: list) -> tuple:
    """fleet-ui-reconciliation-v1 C3 — THE single-pass partition of the live
    queue: artifact-pending types (dispositioned in Fleet, mock screen D's
    cross-link) vs everything else (this page's cards). The pending page, the
    Proposals nav badge, and the cross-link count all derive from this one
    function, so badge N == rendered card N by construction (F3)."""
    artifact, other = [], []
    for p in proposals:
        (artifact if p.get("type") in _ARTIFACT_PROPOSAL_TYPES else other).append(p)
    return artifact, other


def _artifact_xlink_card(n: int) -> str:
    """The cross-link card that replaces artifact-pending cards in the queue
    (mock screen D): a count + a hash link into the Fleet review surface."""
    return (
        f'<div class="card xlink"><div class="grow">{n} fleet artifact(s) '
        f'awaiting review &mdash; dispositioned in '
        f'<a href="/portal#fragments/fleet/">Fleet</a>, not here.</div>'
        f'<span class="nav-badge hot">{n}</span></div>'
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

    fleet-ui-reconciliation-v1 C3 — ONE review surface: artifact-pending
    proposals (forge/fleet) are partitioned OUT of the card feed into a single
    cross-link card pointing at Fleet, where their disposition lives. The
    partition is presentation-only: the queue, read_all(), and the disposition
    routes are untouched.
    """
    proposals = [p.to_dict() for p in read_all_proposals()]
    # proposal-sort-v1 — render-only newest-first sort. created_at is ISO 8601 UTC on
    # every proposal, so a lexical sort IS chronological. read_all's append-order
    # contract (proposal_queue.py) is UNTOUCHED — this sorts only the local render
    # copy. A missing/empty created_at sorts LAST under reverse=True (unknown-age
    # proposals sink to the bottom). Matches the fleet viewer's newest-first order.
    proposals.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    artifact, proposals = _partition_proposals(proposals)
    memory_items = pending_memory_proposal_items()
    grouped = request.query.get("view") == "grouped"

    parts = ['<div id="proposals-listing">', _proposals_view_toggle(grouped)]
    if artifact:
        parts.append(_artifact_xlink_card(len(artifact)))
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


# fleet-artifact-legibility-v1 C3 — the fleet-unit context panel (mock screen
# A right panel). Read-only, worker-agnostic: everything comes from the C2
# unit join, the runtime-synthesized meta.json, GENERIC frontmatter key
# conventions, the feedback store, and the kaizen ledger — never a worker's
# JSON schema. Absent sources render NO section (never an empty shell).

MAX_HISTORY_ENTRIES = 20

# Generic lineage key CONVENTIONS — any worker using these gets lineage; no
# worker-name or JSON-schema branching. Payload-internal lineage (a schema's
# own object, e.g. a source_article block) deliberately does NOT render.
_META_LINEAGE_KEYS = ("source_name", "source_path")
_FRONTMATTER_LINEAGE_KEYS = ("source_brief", "source", "source_path")


def _cx_event(ts_label: str, body: str) -> str:
    return f'<div class="cx-ev"><div class="t">{ts_label}</div>{body}</div>'


def _context_fleet(entity_id: str) -> str:
    """Context for one fleet unit: UNIT / LINEAGE / GOALS SERVED / HISTORY.
    ``entity_id`` is ``{producer}/{unit_key}`` — the context route's ``.+``
    pattern already carries slash-bearing ids (cellar-page precedent)."""
    producer, _, unit_key = entity_id.partition("/")
    cap = _fleet_skill_records().get(producer)
    if cap is None or not unit_key:
        return '<p class="placeholder">Context unavailable — unit not found.</p>'
    unit = None
    for u in _list_fleet_units(cap):
        if u.get("unit_id") == unit_key or u.get("filename") == unit_key:
            unit = u
            break
    if unit is None:
        return '<p class="placeholder">Context unavailable — unit not found.</p>'

    parts = []
    # ---- UNIT (always: the C2 join is the unit's existence) ---------------
    parts.append(
        f'<h3>Unit</h3><dl class="model-detail">'
        f'<dt>Worker</dt><dd>{_esc(producer)}</dd>'
        f'<dt>State</dt><dd>{_state_chip(unit["governance_state"])}</dd>'
        f'<dt>Revision</dt><dd>{unit.get("revision_count", 0)}</dd>'
        f'<dt>Generated</dt><dd>{_esc(_relative_age(unit["mtime"]))}</dd>'
        f'<dt>unit_id</dt><dd class="mono">{_esc(unit["unit_id"])}</dd></dl>'
    )

    # ---- size-gated payload read (C1 gates; over-gate → UNIT+HISTORY only)
    text, src = _unit_primary_file(cap, unit, limit=_PARSE_SIZE_CAP + 1)
    payload = None
    fm = None
    if src is not None and len(text) <= _PARSE_SIZE_CAP:
        sfx = src.suffix.lower()
        if sfx == ".json":
            try:
                data = json.loads(text)
                payload = data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                payload = None
        elif sfx == ".md" and text.startswith("---"):
            split = _split_frontmatter(text)
            if split is not None:
                try:
                    fm_data = yaml.safe_load(split[0])
                    fm = fm_data if isinstance(fm_data, dict) else None
                except yaml.YAMLError:
                    fm = None

    # ---- LINEAGE (structurally-guaranteed sources ONLY) --------------------
    meta = {}
    fn = unit.get("filename")
    if fn:
        try:
            _z, staging, _c, _p = _fleet_zone_dirs(cap)
            d = staging / fn
            if d.is_dir():
                meta = _package_meta(d)
        except KeyError:
            meta = {}
    lineage_srcs = []
    for key in _META_LINEAGE_KEYS:
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            lineage_srcs.append(v)
            break  # source_name preferred; source_path is its fallback spelling
    for key in _FRONTMATTER_LINEAGE_KEYS:
        v = (fm or {}).get(key)
        if isinstance(v, str) and v.strip():
            lineage_srcs.append(v)
            break
    if lineage_srcs:
        evs = "".join(_cx_event("source", _esc(v)) for v in lineage_srcs)
        evs += _cx_event(
            _esc(_relative_age(unit["mtime"])),
            f"generated by {_esc(producer)}",
        )
        parts.append(f'<h3>Lineage</h3><div class="cx-tl">{evs}</div>')

    # ---- GOALS SERVED (generic dock_goal_refs key; EXISTING goal cards) ---
    refs = []
    if payload is not None:
        refs = _as_str_list(payload.get("dock_goal_refs"))
    elif fm is not None:
        refs = _as_str_list(fm.get("dock_goal_refs"))
    if refs:
        goals = _goals_by_id()
        cards = "".join(
            _goal_card(goals[r]) if r in goals
            else f'<div class="card"><div class="meta">unknown goal: '
                 f'{_esc(r)}</div></div>'
            for r in refs
        )
        parts.append(f'<h3>Goals served</h3>{cards}')

    # ---- HISTORY (staged event + feedback revisions + ledger terminal) ----
    dated = [(unit["mtime"],
              f'staged &rarr; {_esc(_state_meta(unit["governance_state"])[0])}')]
    reg = _fleet_worker_registry().get(cap.id)
    wid = reg[0] if reg else None
    entry = feedback_store.read(wid, unit["unit_id"]) if wid else None
    if entry:
        for h in (entry.get("history") or [])[-MAX_HISTORY_ENTRIES:]:
            note = _prose_html(h.get("revision_note", ""), False)
            dated.append((h.get("ts") or "", f"revision guidance: {note}"))
    dated.sort(key=lambda e: e[0])  # ISO strings — lexical IS chronological
    rows = [
        _cx_event(_esc(_relative_age(ts)) if ts else "", body)
        for ts, body in dated[:MAX_HISTORY_ENTRIES]
    ]
    disp = _ledger_terminal_dispositions().get(unit["unit_id"])
    if disp:  # undated in the ledger helper — always the terminal row
        rows.append(_cx_event("", f"terminal disposition: {_esc(disp)}"))
    parts.append(
        f'<h3>History</h3><div class="cx-tl">'
        f'{"".join(rows[:MAX_HISTORY_ENTRIES])}</div>'
    )

    return "".join(parts)


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
    elif entity_type == "fleet":
        # fleet-artifact-legibility-v1 C3 — entity_id = producer/unit_key.
        body = _context_fleet(entity_id)
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
# readers (_fleet_index_rows / _fleet_skill_records / _read_fleet_artifact),
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


# fleet-ui-reconciliation-v1 C2 — status board + outline nav presentation
# helpers. All data comes from portal._fleet_index_rows() (one pass, F3); these
# helpers only PRESENT it. State order is the review lifecycle.

_STATE_ORDER = (
    ("needs_review", "needs review"),
    ("revision_requested", "redrafting"),
    ("promoted", "promoted"),
    ("rejected", "rejected"),
    ("legacy", "legacy"),
)

_PRODUCER_MODE = "action_surface_publish"


def _humanize_cadence(cadence) -> str:
    """Presentation-only humanizer for the registry's common cron forms
    (``*/N * * * *`` → every N min; ``M H * * *`` → daily HH:MM). Anything
    else renders as the raw expression — never guessed."""
    if not cadence:
        return "no cadence"
    m = re.match(r"^\*/(\d+) \* \* \* \*$", cadence)
    if m:
        return f"every {m.group(1)} min"
    m = re.match(r"^(\d{1,2}) (\d{1,2}) \* \* \*$", cadence)
    if m:
        return f"daily {int(m.group(2)):02d}:{int(m.group(1)):02d}"
    return cadence


def _producer_meta_line(row: dict) -> str:
    """The worker-card meta line: role · cadence · last run (status-qualified).
    A skill with no registry entry says so rather than fabricating a schedule."""
    parts = ["Producer"]
    worker = row.get("worker")
    if worker:
        parts.append(_humanize_cadence(worker.get("cadence")))
        if not worker.get("enabled"):
            parts.append("disabled")
    else:
        parts.append("no worker registered")
    last = row.get("last_run")
    if last and last.get("ts"):
        qual = ""
        if last.get("status") == "failed":
            qual = " (failed)"
        elif last.get("status") == "no_work":
            qual = " (no work)"
        parts.append(f"last run {_relative_age(last['ts'])}{qual}")
    # C4 — lifecycle lineage hint, from STRUCTURAL provenance only (P1): a
    # named flywheel parent, or the agent_proposed promotion path. Today's
    # records are all operator_authored → no line renders.
    if row.get("parent_id"):
        parts.append(f"promoted from {row['parent_id']}")
    elif row.get("provenance") == "agent_proposed":
        parts.append("promoted from skill")
    return " &middot; ".join(_esc(p) for p in parts)


def _observer_ingest_line(row: dict) -> str:
    """The observer strip's freshness line — from the wiki ingest ledger
    (newest ingested SOURCE mtime), or an honest 'no ingest recorded'."""
    if row.get("last_ingest"):
        n = row.get("ingested_count") or 0
        return (f"last ingest {_esc(_relative_age(row['last_ingest']))} &middot; "
                f"{n} ingested")
    return "no ingest recorded"


def _state_pills(row: dict) -> str:
    """The state-pill row: needs_review ALWAYS renders (zero-styled at 0);
    other states render only when nonzero. Every pill deep-links to the
    producer's queue on that state's tab (C4)."""
    counts = row.get("state_counts") or {}
    base = f'/portal#fragments/fleet/{_esc(row["name"])}/'
    pills = []
    for state, label in _STATE_ORDER:
        n = counts.get(state, 0)
        if state != "needs_review" and not n:
            continue
        zero = " zero" if not n else ""
        pills.append(
            f'<a class="state-pill{zero}" href="{base}?state={state}">'
            f'<span class="dot dot-{state}"></span>'
            f'<span class="n">{n}</span> {label}</a>'
        )
    return f'<div class="state-row">{"".join(pills)}</div>'


async def handle_fleet_overview(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/`` — the fleet STATUS BOARD (C2, mock
    screen A): a wide grid of producer cards (zone badge, schedule/last-run
    meta, state pills) with an observer strip below. Producers vs observers
    split on the capability's ``approval_handoff.mode`` passthrough."""
    rows = _fleet_index_rows()
    producers = [r for r in rows if r.get("mode") == _PRODUCER_MODE]
    observers = [r for r in rows if r.get("mode") != _PRODUCER_MODE]
    total_needs = sum(r["needs_review_count"] for r in rows)

    if not rows:
        return _html_fragment(
            '<div class="content wide"><h2>Fleet</h2>'
            '<p class="placeholder">No fleet skills registered.</p></div>'
        )

    cards = "".join(
        f'<div class="card worker-card">'
        f'<h3><a href="/portal#fragments/fleet/{_esc(r["name"])}/">'
        f'{_esc(r["name"])}</a> {_fleet_zone_badge(r["zone"])}</h3>'
        f'<div class="meta">{_producer_meta_line(r)}</div>'
        f'{_state_pills(r)}'
        f'</div>'
        for r in producers
    )
    obs_cards = "".join(
        f'<div class="card"><div class="grow"><h3>'
        f'<a href="/portal#fragments/fleet/{_esc(r["name"])}/">'
        f'{_esc(r["name"])}</a> {_fleet_zone_badge(r["zone"])}</h3></div>'
        f'<span class="mono">{_observer_ingest_line(r)}</span></div>'
        for r in observers
    )
    obs_strip = (
        f'<div class="observer-strip">'
        f'<div class="obs-strip-label">Observers &mdash; ingest only, '
        f'no review queue</div>{obs_cards}</div>'
        if observers else ""
    )
    sub = (f'{len(producers)} producer(s) &middot; {len(observers)} '
           f'observer(s) &middot; {total_needs} unit(s) awaiting review')
    return _html_fragment(
        f'<div class="content wide"><h2>Fleet</h2>'
        f'<p class="page-sub">{sub}</p>'
        f'<div class="board">{cards}</div>{obs_strip}</div>'
    )


# ---------------------------------------------------------------------------
# Fleet outline nav (fleet-ui-reconciliation-v1 C2)
# ---------------------------------------------------------------------------


def _nav_badge(n: int, *, hot_when_positive: bool = True) -> str:
    hot = " hot" if (n and hot_when_positive) else ""
    return f'<span class="nav-badge{hot}">{n}</span>'


def _nav_toggle_attr() -> str:
    """The expand/collapse onclick shared by outline nodes: toggles .open on
    the node (twist rotation) and on its sibling .kids block."""
    return ('onclick="this.classList.toggle(\'open\');'
            'var k=this.nextElementSibling;'
            'if(k&&k.classList.contains(\'kids\')){k.classList.toggle(\'open\')}"')


async def handle_fleet_nav(request: web.Request) -> web.Response:
    """``GET /portal/fragments/nav/fleet`` — the data-driven Fleet outline for
    the sidebar (C2). Same one-pass join as the fleet index (F3, no N+1). The
    Fleet badge counts needs_review units ONLY. Producer nodes expand to
    nonzero state rows (hash links into the queue); observers group below,
    count-free, with the P2 ingest age when the ledger has one. Expansion
    state is server-rendered (root open; a producer opens when it has units
    needing review) and resets on refresh."""
    rows = _fleet_index_rows()
    producers = [r for r in rows if r.get("mode") == _PRODUCER_MODE]
    observers = [r for r in rows if r.get("mode") != _PRODUCER_MODE]
    total_needs = sum(r["needs_review_count"] for r in rows)

    # The root LABEL navigates to the board WITHOUT toggling (stopPropagation —
    # C4: with expansion persistence, a label click that also collapsed the
    # outline would stick closed); the twist / row background still toggles.
    parts = [
        f'<div class="nav-node open" data-nav-key="fleet" {_nav_toggle_attr()}>'
        f'<span class="twist">&#9654;</span>'
        f'<a href="/portal#fragments/fleet/" '
        f'onclick="event.stopPropagation()">Fleet</a>'
        f'{_nav_badge(total_needs)}</div>',
        '<div class="kids open">',
    ]
    for r in producers:
        name = _esc(r["name"])
        needs = r["needs_review_count"]
        counts = r.get("state_counts") or {}
        opened = " open" if needs else ""
        parts.append(
            f'<div class="nav-node lvl1{opened}" data-nav-key="{name}" '
            f'{_nav_toggle_attr()}>'
            f'<span class="twist">&#9654;</span> {name}'
            f'{_nav_badge(needs)}</div>'
        )
        state_rows = "".join(
            f'<a class="nav-item lvl2" '
            f'href="/portal#fragments/fleet/{name}/?state={state}">'
            f'<span class="dot dot-{state}"></span> {label}'
            f'{_nav_badge(counts[state], hot_when_positive=(state == "needs_review"))}'
            f'</a>'
            for state, label in _STATE_ORDER if counts.get(state)
        )
        parts.append(f'<div class="kids{opened}">{state_rows}</div>')
    if observers:
        parts.append('<div class="obs-label">Observers</div>')
        for r in observers:
            age = (f'<span class="mono">{_esc(_relative_age(r["last_ingest"]))}</span>'
                   if r.get("last_ingest") else "")
            parts.append(
                f'<a class="nav-item lvl1 observer" '
                f'href="/portal#fragments/fleet/{_esc(r["name"])}/">'
                f'{_esc(r["name"])}{age}</a>'
            )
    parts.append('</div>')
    return _html_fragment("".join(parts))


async def handle_proposals_nav(request: web.Request) -> web.Response:
    """``GET /portal/fragments/nav/proposals`` — the Proposals nav item + badge
    (C3). The badge counts EXACTLY what the pending page renders as cards —
    non-artifact live proposals + pending memory items — via the same
    ``_partition_proposals`` the page uses (F3: badge N == card N by
    construction). Refreshed by the ``proposal-disposition`` HX-Trigger event."""
    _artifact, other = _partition_proposals(
        [p.to_dict() for p in read_all_proposals()]
    )
    n = len(other) + len(pending_memory_proposal_items())
    return _html_fragment(
        f'<a class="nav-count-link" '
        f'href="/portal#fragments/proposals/pending">Proposals'
        f'{_nav_badge(n)}</a>'
    )


def _default_queue_state(counts: dict):
    """The default queue tab: needs_review when nonzero, else the first nonzero
    state in lifecycle order (C4 rule). None when the producer has no units."""
    if counts.get("needs_review"):
        return "needs_review"
    for state, _label in _STATE_ORDER:
        if counts.get(state):
            return state
    return None


def _queue_tabs(name: str, counts: dict, active) -> str:
    """The state-queue tab row (C4, mock screen B): nonzero states only (the C2
    nonzero-only ruling), each a dot + label + count. Tabs are hash links
    carrying ``?state=`` — a tab switch is one router dispatch, and the tab is
    deep-linkable (nav state rows + board pills land here)."""
    tabs = []
    for state, label in _STATE_ORDER:
        n = counts.get(state, 0)
        if not n:
            continue
        cls = " active" if state == active else ""
        tabs.append(
            f'<a class="queue-tab{cls}" '
            f'href="/portal#fragments/fleet/{_esc(name)}/?state={state}">'
            f'<span class="dot dot-{state}"></span> {label} &middot; {n}</a>'
        )
    return f'<div class="queue-tabs">{"".join(tabs)}</div>' if tabs else ""


async def handle_fleet_skill_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/{skill_name}/`` — one producer's inbox as an
    in-shell fragment. C4: state-queue tabs (mock screen B) — ``?state=``
    selects the tab server-side; the unit list renders that state only. An
    absent/invalid/zero-count ``state`` falls back to the default-tab rule.
    Unknown skill -> 404 fragment."""
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
    counts: dict = {}
    for u in units:
        counts[u["governance_state"]] = counts.get(u["governance_state"], 0) + 1
    needs = counts.get("needs_review", 0)
    pill_cls = "pending-pill" + ("" if needs else " zero")
    header = (
        f'<div class="inbox-head"><h2>{_esc(skill_name)} '
        f'{_fleet_zone_badge(cap.zone.value)}</h2>'
        f'<span class="{pill_cls}">{needs} needs review</span></div>'
    )
    requested = request.query.get("state")
    active = requested if counts.get(requested) else _default_queue_state(counts)
    tabs = _queue_tabs(skill_name, counts, active)
    if units:
        shown = [u for u in units if u["governance_state"] == active]
        cards = "".join(_review_card(u, remote_sink, cap=cap) for u in shown)
    else:
        cards = ('<div class="card"><p class="placeholder">No artifacts yet — '
                 'this producer is idle.</p></div>')
    return _html_fragment(
        f'<div class="content">{_fleet_breadcrumb(skill_name)}{header}{tabs}'
        f'{cards}</div>'
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
    unit = _find_fleet_unit(cap, filename=filename)
    if unit is not None:
        # C4 (mock screen C) — title row carries the state chip (+ rev chip when
        # revised); page-sub carries worker · generated ts · unit_id.
        rc = unit.get("revision_count", 0)
        rev_chip = f' <span class="state-chip">rev {rc}</span>' if rc else ""
        header = (
            f"<h2>{_esc(filename)} {_state_chip(unit['governance_state'])}"
            f"{rev_chip}</h2>"
            f'<p class="page-sub">{_esc(skill_name)} &middot; generated '
            f'{_esc(_relative_age(unit["mtime"]))} &middot; unit_id '
            f'<span class="mono">{_esc(unit["unit_id"])}</span></p>'
        )
    else:
        # No C2 unit resolves (e.g. a stray non-unit file) — keep the plain
        # two-state badge line rather than fabricating unit metadata.
        header = (
            f"<h2>{_esc(filename)}</h2>"
            f'<p class="meta">{_esc(skill_name)} &middot; '
            f'{_fleet_state_badge(state)}</p>'
        )
    body = (
        f'<div class="content">'
        f"{_fleet_breadcrumb(skill_name, filename)}{header}{content}</div>"
    )
    # Mount 2 (C1) — the disposition dock lands in #right-panel via the OOB swap
    # pattern (same mechanic as the cellar detail's context OOB). No unit → no
    # dock: a canonical-only or unresolvable file has nothing to disposition.
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


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C2 — generic package rendering (mock screen B).
#
# A staged-dir unit enumerates its non-meta files into tabs (per-suffix
# rendering); order and title come from the DECLARATION
# (presentation.package.order / title_from_meta), never a worker name. The
# forge-specific body builder (_forge_slug_body) retired into this path.
# ---------------------------------------------------------------------------

MAX_PACKAGE_TABS = 8
_FILE_SUFFIX_ALLOWLIST = (".md", ".json", ".txt")


def _render_file_html(text: str, suffix: str) -> str:
    """Per-suffix file rendering: .md → frontmatter-strip + _render_md; .json →
    the C1 structured facts/pretty path; .txt → escaped <pre>. Callers enforce
    the allowlist + size gate BEFORE this runs."""
    if suffix == ".md":
        body = text
        if body.startswith("---"):
            split = _split_frontmatter(body)
            if split is not None:
                body = split[1]
        return f'<article class="cellar-body">{_render_md(body)}</article>'
    if suffix == ".json":
        return _fleet_json_card(text)
    return f"<pre>{_esc(text)}</pre>"


def _package_files(d: Path, order: list) -> tuple:
    """``(tab_files, strip_entries)`` for a staged package dir. Declared order
    first, remaining non-meta files alphabetical (F6, enforced BEFORE any
    render): at most MAX_PACKAGE_TABS tabs; off-allowlist or over-1MB files go
    to the metadata strip — zero DOM content injection for them. Strip entries
    are ``(name, size, reason)``."""
    files = sorted(
        (f for f in d.iterdir() if f.is_file() and f.name != "meta.json"),
        key=lambda f: f.name,
    )
    by_name = {f.name: f for f in files}
    ordered = [by_name[n] for n in order if n in by_name]
    ordered += [f for f in files if f.name not in order]
    tabs, strip = [], []
    for f in ordered:
        size = f.stat().st_size
        if f.suffix.lower() not in _FILE_SUFFIX_ALLOWLIST:
            strip.append((f.name, size, "unsupported type"))
        elif size > _PARSE_SIZE_CAP:
            strip.append((f.name, size, "exceeds the 1MB render cap"))
        elif len(tabs) >= MAX_PACKAGE_TABS:
            strip.append((f.name, size, "tab overflow"))
        else:
            tabs.append(f)
    return tabs, strip


def _package_strip_html(strip: list) -> str:
    """The metadata strip for unrendered files: filename + size + reason. No
    per-file raw link — package-inner files have no read route (the artifact
    resolver serves single path components only); inventing one would be a new
    read surface, out of C2 scope."""
    if not strip:
        return ""
    rows = "".join(
        f'<span class="tag">{_esc(name)} &middot; {size:,} B &middot; '
        f'{_esc(reason)}</span>'
        for name, size, reason in strip
    )
    return f'<div class="rc-facts pkg-strip">{rows}</div>'


def _package_tabs_html(tab_files: list, unit_key: str) -> str:
    """Client-side tabs: one button + pane per renderable file (single
    dispatch — panes render server-side, the toggle is DOM-only). Default tab
    = first ordered file."""
    switch = (
        "var p=this.closest('.pkg'),k=this.dataset.pane;"
        "p.querySelectorAll('.ftab').forEach(function(t)"
        "{t.classList.toggle('on',t.dataset.pane===k)});"
        "p.querySelectorAll('.fpane').forEach(function(x)"
        "{x.classList.toggle('on',x.dataset.pane===k)});"
    )
    tabs, panes = [], []
    for i, f in enumerate(tab_files):
        on = " on" if i == 0 else ""
        tabs.append(
            f'<button type="button" class="ftab{on}" data-pane="{i}" '
            f'onclick="{switch}">{_esc(f.name)}</button>'
        )
        text = f.read_text(encoding="utf-8", errors="replace")
        panes.append(
            f'<div class="fpane{on}" data-pane="{i}">'
            f'{_render_file_html(text, f.suffix.lower())}</div>'
        )
    return (
        f'<div class="pkg"><div class="ftabs">{"".join(tabs)}</div>'
        f'{"".join(panes)}</div>'
    )


def _package_meta(d: Path) -> dict:
    """The unit's ``meta.json`` as a dict (``{}`` when absent/malformed —
    presentation-side; the write paths already fail loud)."""
    p = d / "meta.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _publish_affordance(cap: Any, unit_key: str, meta: dict,
                        title_keys: list) -> str:
    """The remote-publish action card for a SINK-published unit visited without
    a driving ``?pid`` (the include_publish=(pid is None) ruling, preserved).
    Gate: meta parsed and every title_from_meta key present — else a visible
    'Publish unavailable' notice (a missing machine field like the row id is
    caught fail-loud by the publish action itself)."""
    if meta and all(meta.get(k) for k in title_keys or []):
        return render_forge_publish_card(unit_key)
    return (
        f'<div class="card" id="forge-publish-{_esc(unit_key)}">'
        f'<h4>Publish unavailable</h4>'
        f'<div class="meta error">meta.json is missing or incomplete '
        f'&mdash; cannot publish without the package identity.</div></div>'
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
    ``/portal#fragments/fleet/forge-jobsearch/{slug}/`` (the generic unit
    fragment — fleet-artifact-legibility-v1 C2 retired the forge-specific
    body; the legacy /portal/fragments/forge/ route stays as an alias)."""
    slug = quote(request.match_info["slug"], safe="")
    raise web.HTTPFound(f"/portal#fragments/fleet/forge-jobsearch/{slug}/")


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


def _disposition_bar(pid: str, remote_sink: bool, revision_count: int = 0,
                     card_target: bool = False) -> str:
    """The producer-agnostic disposition bar (evolves the C1a ``_disposition_actions_div``):
    stacked full-width Promote / "Suggest revision…" / Reject, plus the progressively-
    disclosed feedback block. The SAME pid-keyed routes both proposal types use.

    ``remote_sink`` selects the Promote consequence copy ("publish to Drive" for forge,
    "ingest to wiki" for an mv-sink).

    fleet-artifact-legibility-v1 C4 (D6 fix) — ``card_target=True`` (Mount 1)
    posts with ``?mount=card`` and targets ``closest .review-card`` outerHTML:
    a SUCCESS swaps the whole card to its post-disposition transient (mock
    screen C). ``card_target=False`` (the Mount-2 dock) keeps the legacy
    ``#disp-result-*`` innerHTML target byte-identically. Failures (4xx/5xx)
    never swap in EITHER mode — the OOB #alert-banner fires and the per-unit
    ``#disp-result-*`` div carries the visible failure note, so the original
    card and its live verbs survive a failed tap (re-tap contract). The
    colon-free ``_short_id`` DOM id is load-bearing (the C1a Andon)."""
    pe = _esc(pid)
    short = _short_id(pid)
    rev_id = "rev-" + short
    result = "disp-result-" + short
    if card_target:
        qs = "?mount=card"
        target_attrs = 'hx-target="closest .review-card" hx-swap="outerHTML"'
    else:
        qs = ""
        target_attrs = f'hx-target="#{result}" hx-swap="innerHTML"'
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
<button class="btn btn-approve btn-promote" hx-post="/portal/actions/proposals/{pe}/promote{qs}" \
{target_attrs} \
hx-confirm="Promote this draft — {consequence} and resolve the unit?">\
Promote &mdash; {_esc(consequence)}</button>\
<button class="btn btn-revise" type="button" \
onclick="this.closest('.disposition-bar').querySelector('.feedback-block').classList.toggle('open')">\
Suggest revision&hellip;</button>\
<button class="btn btn-reject btn-reject-s" hx-post="/portal/actions/proposals/{pe}/reject{qs}" \
{target_attrs} \
hx-confirm="Reject this draft — archive it and dismiss the proposal?">Reject</button>\
<div class="feedback-block">\
<textarea id="{rev_id}" name="revision_text" class="revision-text" rows="3" \
placeholder="Revision guidance for the next draft (what to change)."></textarea>\
<div class="feedback-row">\
<button class="btn btn-approve" hx-post="/portal/actions/proposals/{pe}/suggest_revision{qs}" \
{target_attrs} hx-include="#{rev_id}">Send guidance &amp; redraft</button>\
<button class="btn btn-secondary" type="button" \
onclick="this.closest('.feedback-block').classList.remove('open')">Cancel</button>\
</div>\
<div class="revision-counter">{_esc(counter)}</div>\
</div>\
<div id="{result}"></div>\
</div>"""


def _revised_disclosure(unit: dict) -> str:
    """The REVISED disclosure — a redraft (revision_count>0) quotes the operator's
    latest directive so the card announces why it re-drafted."""
    if unit.get("revision_count", 0) > 0 and unit.get("directive_echo"):
        return f'<div class="revised-disclosure">{_esc(unit["directive_echo"])}</div>'
    return ""


def _unit_footer(unit: dict, remote_sink: bool, card_target: bool = False) -> str:
    """The per-state footer: the disposition bar (needs_review), the banked-guidance
    in-flight note (revision_requested), or nothing (terminal / legacy).
    ``card_target`` threads the C4 Mount-1 card-swap wiring through to the bar."""
    state = unit["governance_state"]
    if state == "needs_review" and unit.get("proposal_id"):
        return _disposition_bar(unit["proposal_id"], remote_sink,
                                unit.get("revision_count", 0),
                                card_target=card_target)
    if state == "revision_requested":
        note = unit.get("directive_echo") or ""
        quoted = f' <em>{_esc(note)}</em>' if note else ""
        return (f'<div class="inflight-note">Guidance banked; redrafts next '
                f'cycle.{quoted}</div>')
    return ""  # promoted / rejected (dimmed) / legacy (list-only) — no actions


def _unit_primary_file(cap: Any, unit: dict, limit: int = 2000) -> tuple:
    """``(text, source Path | None)`` for a unit's primary content — the staged
    dir's declaration-preferred content file (C2: ``presentation.package
    .order[0]`` first, alphabetical fallback), or the canonical/flat file. The
    GATE-A D2 disk-read path, retained; C1 surfaces WHICH file was read so the
    renderer can branch on its suffix. Best-effort: ``("", None)`` when nothing
    resolves (a read-side view must not 500 on a stray file)."""
    try:
        _z, staging, canonical, _p = _fleet_zone_dirs(cap)
        fn = unit.get("filename")
        if fn:
            for base in (staging, canonical):
                cand = base / fn
                if cand.is_file():
                    return (
                        cand.read_text(encoding="utf-8", errors="replace")[:limit],
                        cand,
                    )
            d = staging / fn
            if d.is_dir():
                pres, _err = _fleet_presentation(cap)
                order = ((pres or {}).get("package") or {}).get("order") or []
                for name in order:
                    cand = d / name
                    if cand.is_file():
                        return (
                            cand.read_text(
                                encoding="utf-8", errors="replace")[:limit],
                            cand,
                        )
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.name != "meta.json":
                        return (
                            f.read_text(encoding="utf-8", errors="replace")[:limit],
                            f,
                        )
    except OSError:
        pass
    return "", None


def _unit_preview(cap: Any, unit: dict, limit: int = 2000) -> str:
    """Text-only view of :func:`_unit_primary_file` (kept for the unit fragment)."""
    return _unit_primary_file(cap, unit, limit)[0]


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C1 — schema-declared card bodies (mock A + D).
#
# render_unit_card_body() replaces the raw-bytes preview inside the review
# card: a JSON artifact renders through its capability record's
# terminal_artifact.presentation declaration (headline / fact chips /
# collection preview); an undeclared JSON renders the honest fallback (key
# facts + teaching hint); .md renders a frontmatter-stripped _render_md
# excerpt. ZERO worker names in this logic — everything keys on the
# declaration and the file suffix. Every declared value is _esc'd unless the
# entry declares md:true, which routes through _render_md (nh3 mandatory).
# ---------------------------------------------------------------------------

# F3 bounds — enforced BEFORE any loop / render call.
MAX_RENDER_ITEMS = 50       # collection preview hard cap, whatever is declared
MAX_PROSE_BYTES = 4096      # per-string cap before any _esc/_render_md call
_PARSE_SIZE_CAP = 1_000_000  # bytes; a larger payload is never json.loads'd


def _prose_spec(entry) -> tuple:
    """Normalize a prose entry (``str`` | ``{path, md}``) → ``(path, md)``."""
    if isinstance(entry, dict):
        return entry.get("path", ""), bool(entry.get("md"))
    return entry, False


def _field_path(data, path):
    """Resolve a dot-path over nested dicts. None on any miss — never raises."""
    cur = data
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _prose_html(value, md: bool) -> str:
    """One declared prose value → safe HTML: clamp to MAX_PROSE_BYTES first,
    then ``_esc`` (or ``_render_md`` when md:true — nh3 stays mandatory). A
    clipped value carries a VISIBLE truncation marker."""
    text = str(value)
    clipped = len(text) > MAX_PROSE_BYTES
    text = text[:MAX_PROSE_BYTES]
    body = _render_md(text) if md else _esc(text)
    marker = '<span class="meta"> &hellip; truncated</span>' if clipped else ""
    return body + marker


def _pres_notice(detail: str) -> str:
    """The dim inline degradation notice (F2) — same pattern for a missing
    declared field and for a load-time-malformed declaration."""
    return (
        f'<div class="meta pres-notice">&#9888; presentation: {_esc(detail)} '
        f'&mdash; raw view available</div>'
    )


def _raw_payload_link(unit: dict) -> str:
    """The raw-payload disclosure — a hash LINK to the existing unit fragment
    (not an embedded <details>: a list view of cards must not carry up-to-1MB
    payloads in its DOM; the link is one line and already routed)."""
    producer, fn = unit.get("producer"), unit.get("filename")
    if not (producer and fn):
        return ""
    return (
        f'<a class="raw-link" href="/portal#fragments/fleet/'
        f'{_esc(producer)}/{_esc(fn)}/">View raw payload &#9656;</a>'
    )


def _fact_chips_html(facts: list, data: dict, notices: list) -> str:
    """Declared fact chips: scalar → ``label · value``; list → count; dict →
    field count. A missing path degrades to a notice, never a crash."""
    chips = []
    for f in facts:
        path = f.get("path", "")
        label = f.get("label") or str(path).rsplit(".", 1)[-1]
        val = _field_path(data, path)
        if val is None:
            notices.append(path)
            continue
        if isinstance(val, list):
            shown = str(len(val))
        elif isinstance(val, dict):
            shown = f"{len(val)} field(s)"
        else:
            shown = str(val)[:120]
        chips.append(f'<span class="tag">{_esc(label)} &middot; {_esc(shown)}</span>')
    return f'<div class="rc-facts">{"".join(chips)}</div>' if chips else ""


def _collection_html(coll: dict, data: dict, unit: dict, notices: list) -> str:
    """The declared collection preview: preview_count items (hard-capped at
    MAX_RENDER_ITEMS), each item_title + item_prose excerpts, then a visible
    "+N more" line. String-item collections render each string as prose."""
    items = _field_path(data, coll.get("path", ""))
    if not isinstance(items, list):
        notices.append(coll.get("path", "collection"))
        return ""
    if not items:
        return ""
    n_show = min(int(coll.get("preview_count", 2)), MAX_RENDER_ITEMS, len(items))
    rows = []
    for item in items[:n_show]:
        if isinstance(item, str):
            rows.append(f'<div class="coll-item"><p>{_prose_html(item, False)}</p></div>')
            continue
        if not isinstance(item, dict):
            continue
        title = ""
        title_path = coll.get("item_title")
        if title_path:
            tval = _field_path(item, title_path)
            if tval is None:
                notices.append(f'{coll.get("path")}[].{title_path}')
            else:
                title = f"<b>{_esc(str(tval)[:200])}</b>"
        prose_bits = []
        for entry in coll.get("item_prose", []) or []:
            path, md = _prose_spec(entry)
            val = _field_path(item, path)
            if not isinstance(val, str) or not val.strip():
                notices.append(f'{coll.get("path")}[].{path}')
                continue
            prose_bits.append(f"<p>{_prose_html(val, md)}</p>")
        rows.append(f'<div class="coll-item">{title}{"".join(prose_bits)}</div>')
    more = ""
    if len(items) > n_show:
        more = f'<div class="coll-more">+ {len(items) - n_show} more</div>'
    return f'<div class="coll">{"".join(rows)}{more}</div>'


def _declared_card_html(pres: dict, data: dict, unit: dict) -> str:
    """Render a JSON payload through its presentation declaration. Per-element
    degradation (F2): each missing/mistyped declared path falls back and adds
    ONE deduplicated inline notice; the rest of the card still renders."""
    parts: list = []
    notices: list = []

    def prose_block(entry, cls):
        path, md = _prose_spec(entry)
        val = _field_path(data, path)
        if not isinstance(val, str) or not val.strip():
            notices.append(path)
            return ""
        return f'<p class="{cls}">{_prose_html(val, md)}</p>'

    if "headline" in pres:
        parts.append(prose_block(pres["headline"], "rc-headline"))
    if "facts" in pres:
        parts.append(_fact_chips_html(pres["facts"], data, notices))
    if "collection" in pres:
        parts.append(_collection_html(pres["collection"], data, unit, notices))
    if "body" in pres:
        parts.append(prose_block(pres["body"], "rc-headline"))
    deduped: list = []
    for n in notices:
        if n and n not in deduped:
            deduped.append(n)
    notice_html = "".join(
        _pres_notice(f"field '{n}' not found") for n in deduped[:5]
    )
    return "".join(p for p in parts if p) + notice_html + _raw_payload_link(unit)


def _md_fallback_html(text: str, unit: dict) -> str:
    """Undeclared ``.md`` — a frontmatter-stripped, clamped ``_render_md``
    excerpt with the existing Show-full-draft toggle."""
    body = text
    if body.startswith("---"):
        split = _split_frontmatter(body)
        if split is not None:
            body = split[1]
    clipped = len(body) > MAX_PROSE_BYTES
    html = _render_md(body[:MAX_PROSE_BYTES])
    marker = '<div class="meta"> &hellip; truncated</div>' if clipped else ""
    return (
        f'<div class="draft-preview">{html}{marker}</div>'
        f'<button class="btn btn-secondary preview-toggle" type="button" '
        f'''onclick="this.previousElementSibling.classList.toggle('expanded');'''
        f'''this.textContent=this.previousElementSibling.classList.contains('expanded')?'''
        f''''Collapse':'Show full draft'">Show full draft</button>'''
        + _raw_payload_link(unit)
    )


def _json_fallback_html(data: dict, unit: dict) -> str:
    """Undeclared JSON — the honest fallback (mock D): top-level key facts +
    the teaching hint naming terminal_artifact.presentation + the raw link."""
    chips = []
    for key, val in list(data.items())[:8]:
        if isinstance(val, list):
            shown = str(len(val))
        elif isinstance(val, dict):
            shown = f"{len(val)} field(s)"
        else:
            shown = str(val)[:60]
        chips.append(f'<span class="tag">{_esc(key)} &middot; {_esc(shown)}</span>')
    facts = f'<div class="rc-facts">{"".join(chips)}</div>' if chips else ""
    hint = (
        f'<p class="rc-headline meta">Structured payload &mdash; {len(data)} '
        f'top-level key(s). Declare <span class="mono">terminal_artifact'
        f'.presentation</span> in this skill&rsquo;s capability record to '
        f'render prose here.</p>'
    )
    return facts + hint + _raw_payload_link(unit)


def render_unit_card_body(
    unit: dict, presentation, payload_text: str, *,
    filename: str = "", presentation_error: str = "",
) -> str:
    """The review card's content block (C1). Branches on the SOURCE FILE's
    suffix and the presentation declaration only — never on a worker name.
    ``payload_text`` is the disk read (up to the parse cap + 1); ``filename``
    is the actual file read (a nested unit's dir name has no suffix)."""
    malformed = (
        _pres_notice(f"declaration malformed ({presentation_error})")
        if presentation_error else ""
    )
    suffix = Path(filename or unit.get("filename") or "").suffix.lower()
    if suffix != ".json":
        return _md_fallback_html(payload_text, unit) + malformed
    if len(payload_text) > _PARSE_SIZE_CAP:
        return (
            _pres_notice("payload exceeds the 1MB parse cap")
            + _raw_payload_link(unit) + malformed
        )
    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return (
            f'<div class="meta error">Malformed JSON: {_esc(str(exc))}</div>'
            + _raw_payload_link(unit) + malformed
        )
    if not isinstance(data, dict):
        return (
            f'<p class="rc-headline meta">JSON {_esc(type(data).__name__)} '
            f'payload.</p>' + _raw_payload_link(unit) + malformed
        )
    if isinstance(presentation, dict) and presentation:
        return _declared_card_html(presentation, data, unit) + malformed
    return _json_fallback_html(data, unit) + malformed


def _draft_preview_block(cap: Any, unit: dict) -> str:
    """C1 — the schema-declared card body (mock A/D), replacing the raw-bytes
    preview. The disk-read path (:func:`_unit_primary_file`) is retained; the
    read is sized to the JSON parse cap so a declared payload parses whole.
    Empty when no content resolves."""
    if cap is None:
        return ""
    text, src = _unit_primary_file(cap, unit, limit=_PARSE_SIZE_CAP + 1)
    if not text:
        return ""
    presentation, presentation_error = _fleet_presentation(cap)
    return render_unit_card_body(
        unit, presentation, text,
        filename=(src.name if src is not None else ""),
        presentation_error=presentation_error or "",
    )


def _review_card(unit: dict, remote_sink: bool, cap: Any = None,
                 footer_override=None) -> str:
    """Mount 1 — one unit as a ``.review-card`` (state rail + header chip + meta +
    REVISED disclosure + declared/preview body + per-state footer). Terminals dim
    (``.card-resolved``); legacy is list-only.

    fleet-artifact-legibility-v1 C3 — the card body drives the CONTEXT panel:
    a click loads ``/portal/fragments/context/fleet/{producer}/{unit_id}`` into
    ``#right-panel``. The hx-trigger EVENT FILTER excludes clicks originating
    inside interactive elements (the title link, disposition verbs, textarea) —
    plain hx attrs, no new JS; the title <a> keeps its own navigation to the
    unit fragment. A successful context load marks the card ``.ctx-on``.

    C4 (D6 fix) — Mount-1 verbs target THIS card (outerHTML) and a success
    swaps in the post-disposition transient (``render_disposition_transient``
    passes ``footer_override`` = the slim result strip in place of the bar)."""
    state = unit["governance_state"]
    _label, rail, _chip = _state_meta(state)
    rc = unit.get("revision_count", 0)
    title = unit.get("filename") or unit["unit_id"]
    ver = f'draft v{rc + 1} &middot; ' if rc > 0 else ""
    dim = " card-resolved" if state in ("promoted", "rejected") else ""
    anchor = _short_id(unit.get("proposal_id") or unit["unit_id"])
    producer = unit.get("producer") or ""
    # Title navigates to the unit fragment. HASH-ONLY href (not the canonical
    # /portal#... form): the vendored htmx cancels the default of any bubbled
    # anchor click whose href does not START with '#' (shouldCancel runs
    # BEFORE the trigger filter), and this anchor lives inside the card's
    # context trigger. '#'-prefixed hrefs are exempt, navigate natively, and
    # the router dispatches them identically.
    if producer and title:
        title_html = (
            f'<a class="title" href="#fragments/fleet/'
            f'{_esc(producer)}/{_esc(title)}/">{_esc(title)}</a>'
        )
    else:
        title_html = f'<span class="title">{_esc(title)}</span>'
    ctx = ""
    if producer and unit.get("unit_id"):
        cue = (
            "if(event.detail.successful){document.querySelectorAll("
            "'.review-card.ctx-on').forEach(function(c){c.classList.remove"
            "('ctx-on')});this.classList.add('ctx-on')}"
        )
        ctx = (
            f' hx-get="/portal/fragments/context/fleet/{_esc(producer)}/'
            f'{_esc(unit["unit_id"])}" hx-target="#right-panel" '
            f'hx-swap="outerHTML" hx-trigger="click[!event.target.closest('
            f"'a,button,textarea,select,.disposition-bar'"
            f')]" hx-on::after-request="{cue}" style="cursor:pointer"'
        )
    head = (
        f'<div class="review-head">{title_html}'
        f'{_state_chip(state)}'
        f'<span class="head-meta">{ver}{_esc(_relative_age(unit["mtime"]))}</span></div>'
    )
    footer = (footer_override if footer_override is not None
              else _unit_footer(unit, remote_sink, card_target=True))
    return (
        f'<div class="card review-card {rail}{dim}" id="review-{anchor}"{ctx}>'
        f'{head}{_revised_disclosure(unit)}{_draft_preview_block(cap, unit)}'
        f'{footer}</div>'
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


# ---------------------------------------------------------------------------
# fleet-artifact-legibility-v1 C4 — the fleet-shaped disposition transient
# (D6 fix, mock screen C). A Mount-1 verb SUCCESS swaps the whole review card
# to its post-disposition render: chip flipped, body preserved, bar replaced
# by a slim result strip. Rendered HERE with the other card renderers;
# actions.py handlers CALL it (the render_forge_publish_card precedent).
# ---------------------------------------------------------------------------

_TRANSIENT_STATE = {
    "promote": "promoted",
    "reject": "rejected",
    "suggest_revision": "revision_requested",
}


def _disposition_strip(state: str, message: str, link_href=None,
                       link_label=None, link_external: bool = False,
                       echo=None) -> str:
    """The slim post-disposition result strip: state dot + message + an
    optional destination link, plus the C3-era directive echo for a
    suggest_revision. NO hx attributes — the strip is inert presentation."""
    dim = " dim" if state == "rejected" else ""
    link = ""
    if link_href and link_label:
        ext = ' target="_blank" rel="noopener"' if link_external else ""
        link = (f'<a class="lnk" href="{_esc(link_href)}"{ext}>'
                f'{_esc(link_label)} &#9656;</a>')
    echo_html = (f'<div class="revised-disclosure">{_esc(echo)}</div>'
                 if echo else "")
    return (
        f'<div class="disp-strip rail-{state}{dim}">'
        f'<span class="dot dot-{state}"></span> {_esc(message)}{link}</div>'
        f'{echo_html}'
    )


def render_disposition_transient(payload, disposition: str, *, message: str,
                                 link_href=None, link_label=None,
                                 link_external: bool = False,
                                 echo=None) -> str:
    """The post-disposition CARD for a Mount-1 verb success (D6 fix).

    Resolves the unit from the C2 join AFTER the handler's side effects ran
    (promote → topology/ledger shows the terminal; suggest_revision → the
    feedback store shows revision_requested); a vanished unit (reject archives
    the staged dir) falls back to an identity-only synthesis from the proposal
    payload — the strip carries the outcome either way. The card stays in
    place until the next queue fetch (mock C); counts refresh via the existing
    nav HX-Trigger header. Worker-agnostic: skill resolution is by the
    payload's skill_id, never a name literal."""
    pl = payload or {}
    skill_id = pl.get("skill_id")
    unit_key = pl.get("unit_id") or pl.get("row_id") or pl.get("slug")
    state = _TRANSIENT_STATE.get(disposition, disposition)
    cap = None
    producer = ""
    for name, c in _fleet_skill_records().items():
        if c.id == skill_id:
            cap, producer = c, name
            break
    unit = None
    if cap is not None and unit_key:
        for u in _list_fleet_units(cap):
            if u.get("unit_id") == unit_key or u.get("filename") == unit_key:
                unit = u
                break
    if unit is None:
        unit = {"unit_id": unit_key or "", "producer": producer,
                "governance_state": state, "revision_count": 0,
                "mtime": "", "filename": pl.get("slug") or ""}
    unit = dict(unit, governance_state=state)
    strip = _disposition_strip(state, message, link_href, link_label,
                               link_external, echo)
    return _review_card(unit, remote_sink=False, cap=cap,
                        footer_override=strip)


def _sink_skill_record(canonical_dir: str):
    """``(skill_name, cap)`` for the fleet record whose declared canonical sink
    is *canonical_dir* — the SINK-derived resolver (never a worker name). None
    when no record declares that sink."""
    for name, cap in _fleet_skill_records().items():
        try:
            if cap.governance["write_zone"]["canonical_dir"] == canonical_dir:
                return name, cap
        except (KeyError, TypeError):
            continue
    return None


async def handle_forge_slug_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/forge/{slug}/`` — LEGACY ALIAS (kept because
    previously-sent deep links and the fleet-ui C1 302 target land here). The
    fleet-artifact-legibility-v1 C2 generic package renderer serves it; the
    owning skill is resolved by SINK (canonical_dir == "forge" — the sanctioned
    sink discriminator, not a worker name). New emitters use the generic
    ``/portal/fragments/fleet/{skill}/{unit}/`` route."""
    resolved = _sink_skill_record("forge")
    if resolved is None:
        return _html_fragment(
            '<div class="error-card"><h3>404 — no remote-publish sink</h3>'
            '<p class="placeholder">No fleet record declares the forge '
            'sink.</p></div>', status=404,
        )
    skill_name, cap = resolved
    return _render_unit_detail(
        cap, skill_name, request.match_info["slug"], request.query.get("pid")
    )


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


def _render_unit_detail(cap: Any, skill: str, unit_name: str, pid) -> web.Response:
    """The generic unit DETAIL fragment (fleet-artifact-legibility-v1 C2, mock
    screen B) — shared by the generic route and the legacy forge alias.

    Package units (staged dirs) render declaration-ordered file tabs with
    per-suffix rendering + the metadata strip for overflow/blocked files;
    single-file units render suffix-correct through the same gates. Title and
    subtitle come from ``presentation.package`` + ``meta.json`` — never a
    worker name. The Mount-2 disposition dock rides the OOB ``#right-panel``
    swap; a remote-publish sink visited WITHOUT a driving ``?pid`` keeps the
    Publish affordance (the include_publish=(pid is None) ruling)."""
    unit = _find_fleet_unit(cap, proposal_id=pid, filename=unit_name)
    if unit is not None and unit.get("filename"):
        unit_name = unit["filename"]
    try:
        _z, staging, canonical, _p = _fleet_zone_dirs(cap)
    except KeyError:
        staging = canonical = None
    remote_sink = False
    if staging is not None:
        remote_sink = cap.governance["write_zone"]["canonical_dir"] == "forge"

    pres, _pres_err = _fleet_presentation(cap)
    pkg = (pres or {}).get("package") or {}
    order = pkg.get("order") or []
    title_keys = pkg.get("title_from_meta") or []

    d = (staging / unit_name) if staging is not None else None
    single = None
    if d is None or not d.is_dir():
        d = None
        for base in (staging, canonical):
            if base is not None and (base / unit_name).is_file():
                single = base / unit_name
                break
    if d is None and single is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — unit not found</h3>'
            f'<p class="placeholder">{_esc(skill)}/{_esc(unit_name)}</p></div>',
            status=404)

    # Title / subtitle — declaration + meta driven, unit_id fallback.
    meta = _package_meta(d) if d is not None else {}
    title_bits = [str(meta[k]) for k in title_keys if meta.get(k)]
    title = " &mdash; ".join(_esc(b) for b in title_bits) or _esc(
        (unit or {}).get("unit_id") or unit_name)
    if d is not None:
        tab_files, strip = _package_files(d, order)
        n_docs = len(tab_files) + len(strip)
        sub_tail = f'{n_docs} document(s) + meta'
        content = _package_tabs_html(tab_files, unit_name) if tab_files else (
            '<p class="placeholder">No renderable content files.</p>')
        content += _package_strip_html(strip)
    else:
        size = single.stat().st_size
        suffix = single.suffix.lower()
        sub_tail = "1 document"
        if suffix not in _FILE_SUFFIX_ALLOWLIST:
            content = _package_strip_html([(single.name, size, "unsupported type")])
        elif size > _PARSE_SIZE_CAP:
            content = _package_strip_html(
                [(single.name, size, "exceeds the 1MB render cap")])
        else:
            text = single.read_text(encoding="utf-8", errors="replace")
            content = _render_file_html(text, suffix)
    generated = (f' &middot; generated {_esc(_relative_age(unit["mtime"]))}'
                 if unit else "")
    header = (
        f'<h2>{title} {_fleet_zone_badge(cap.zone.value)}</h2>'
        f'<p class="page-sub">{_esc(skill)}{generated} &middot; {sub_tail}</p>'
    )

    # The remote-publish affordance (pid-less visits only — preserved ruling).
    publish = ""
    if remote_sink and pid is None and d is not None:
        publish = _publish_affordance(cap, unit_name, meta, title_keys)

    # Mount-2 disposition dock → OOB #right-panel (its native habitat).
    if unit is not None:
        dock = _disposition_dock(unit, remote_sink, skill)
    elif pid:
        # Unit not resolvable (e.g. proposal already gone) but a pid is present —
        # the bare bar in a dock shell so disposition still works.
        dock = (f'<div class="disposition-dock rail-needs_review">'
                f'{_disposition_bar(pid, remote_sink)}</div>')
    else:
        dock = None
    if dock is not None:
        return _html_fragment(
            f'<div class="content">{header}{publish}{content}</div>'
            f'<div id="right-panel" class="right-panel" '
            f'hx-swap-oob="true">{dock}</div>'
        )
    # FAIL LOUD (forge-review-surface-v1 posture preserved): reading is fine,
    # but a missing disposition affordance is announced, never silent.
    notice = ('<div class="meta error" id="unit-disposition-none">'
              'disposition unavailable — no open proposal for this unit</div>')
    return _html_fragment(
        f'<div class="content">{header}{publish}{content}{notice}</div>'
    )


async def handle_fleet_unit_fragment(request: web.Request) -> web.Response:
    """``GET /portal/fragments/fleet/{skill_name}/{unit}/`` — the generic unit
    detail (packages + single files). Unknown skill -> 404 fragment."""
    skill = request.match_info["skill_name"]
    cap = _fleet_skill_records().get(skill)
    if cap is None:
        return _html_fragment(
            f'<div class="error-card"><h3>404 — unknown fleet skill</h3>'
            f'<p class="placeholder">{_esc(skill)}</p></div>', status=404)
    return _render_unit_detail(
        cap, skill, request.match_info["unit"], request.query.get("pid")
    )


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
    # fleet-artifact-legibility-v1 C2 — LEGACY ALIAS: previously-sent forge deep
    # links land here; the generic package renderer serves it (sink-resolved).
    # New emitters use the generic /portal/fragments/fleet/{skill}/{unit}/ route.
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
    # fleet-ui-reconciliation-v1 C2 — the data-driven Fleet outline for the
    # sidebar (loaded on shell boot + on the fleet-disposition refresh event).
    app.router.add_get("/portal/fragments/nav/fleet", handle_fleet_nav)
    # fleet-ui-reconciliation-v1 C3 — the Proposals nav badge (non-artifact
    # count, same partition as the pending page; proposal-disposition refresh).
    app.router.add_get("/portal/fragments/nav/proposals", handle_proposals_nav)
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
