"""artifact-identity-v1 C2 — the read-only portal artifact route.

``GET /artifact/<id>`` resolves a 16-hex artifact ID to the file the
``artifact_written`` ledger event recorded for it, and serves that file
under strict containment. READ-ONLY — no mutation endpoints.

Two canonical-path forms, deliberately NOT unified (Phase 1 canon):

* IDENTITY form — expanduser+abspath, never realpath
  (grove.artifact_identity): what the ledger records and the cellar hashes.
* CONTAINMENT form — ``resolve(strict=True)``: what this route compares
  against the resolved allowlist roots at request time.

The route resolves the ledger-recorded path for CONTAINMENT ONLY; it never
re-derives identity from a resolved path.

Structural no-walking guarantee: the id→path mapping comes from the ledger
index alone; the only disk touch outside the ledger read is the resolved
path open (plus the cellar cross-ref glob over the wiki pages tree, which
serves nothing).
"""

from __future__ import annotations

import html as _html_mod
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import markdown
import nh3
from aiohttp import web

from grove.wiki.links import cellar_page_id
from hermes_constants import get_hermes_home, get_wiki_path

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# ── pinned sanitization profile — LOCAL to this route ───────────────────────
# Deliberately narrower than the cellar's _render_md profile (no span/div/img,
# no class/id attributes). The cellar profile is NOT modified here —
# sanitization-profile-parity is banked debt. http/https only: data: and
# javascript: URIs cannot survive.
_ARTIFACT_MD_TAGS = {
    "p", "a", "b", "i", "strong", "em",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "code", "pre", "blockquote",
    "table", "tr", "td", "th",
}
_ARTIFACT_MD_ATTRS = {"a": {"href"}}
_ARTIFACT_URL_SCHEMES = {"http", "https"}

# Plain-text suffixes served inline as text/plain. Everything not here and
# not .md — including .html/.htm/.svg, which are ACTIVE-content risks — is an
# attachment download, never rendered.
_TEXT_SUFFIXES = {
    ".txt", ".json", ".jsonl", ".yaml", ".yml", ".log", ".csv", ".tsv",
}

# Every /artifact/ response — 200s and 404s alike — carries these.
_ARTIFACT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


# ── allowlist roots ─────────────────────────────────────────────────────────


def _declared_artifact_roots(config: Optional[Dict[str, Any]] = None) -> List[str]:
    """The config-declared root list (portal.artifact_roots in the sovereign
    config, the portal.base_url precedent). Absent/malformed key → the
    default: the hermes home, where governed writes overwhelmingly land."""
    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    cfg = config or {}
    portal_cfg = cfg.get("portal")
    if isinstance(portal_cfg, dict):
        declared = portal_cfg.get("artifact_roots")
        if isinstance(declared, list) and declared:
            return [str(r) for r in declared]
    return [str(get_hermes_home())]


def resolve_artifact_roots(
    config: Optional[Dict[str, Any]] = None,
) -> List[Path]:
    """Resolve each declared root at startup: expanduser + resolve(strict=True).

    A root that fails strict resolve is REJECTED LOUDLY — logged at ERROR and
    excluded — never silently skipped into a smaller allowlist. Zero surviving
    roots is itself loud: the route will 404 everything.
    """
    resolved: List[Path] = []
    for raw in _declared_artifact_roots(config):
        try:
            root = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            logger.error(
                "[artifacts] artifact root %r REJECTED (strict resolve failed: "
                "%r) — excluded from the allowlist. Fix the portal."
                "artifact_roots entry or create the directory.", raw, exc,
            )
            continue
        resolved.append(root)
    if not resolved:
        logger.error(
            "[artifacts] NO artifact roots survived resolution — every "
            "/artifact/ request will 404. Check portal.artifact_roots."
        )
    return resolved


# ── lazy id→path ledger index ───────────────────────────────────────────────


def _scan_ledger_index() -> Dict[str, str]:
    """Build id→path from every artifact_written event across all session
    ledgers (the flywheel_cli dir-glob precedent: tolerant per-line parse,
    a malformed line or unreadable file never aborts the scan)."""
    from grove.kaizen_ledger import default_ledger_dir

    index: Dict[str, str] = {}
    ledger_dir = default_ledger_dir()
    if not ledger_dir.is_dir():
        return index
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event_type") != "artifact_written":
                        continue
                    aid = event.get("artifact_id")
                    apath = event.get("path")
                    if isinstance(aid, str) and isinstance(apath, str):
                        index[aid] = apath
        except OSError:
            continue
    return index


def _lookup_artifact_path(app: web.Application, artifact_id: str) -> Optional[str]:
    """Lazy index lookup, refreshed on miss before 404 — an artifact written
    after the last scan is found by the rescan; a genuinely unknown id costs
    one rescan and returns None."""
    index: Dict[str, str] = app["_artifact_index"]
    hit = index.get(artifact_id)
    if hit is not None:
        return hit
    fresh = _scan_ledger_index()
    index.clear()
    index.update(fresh)
    return index.get(artifact_id)


# ── response helpers — headers on EVERY response class ──────────────────────


def _respond(
    body: bytes | str,
    *,
    status: int = 200,
    content_type: str = "text/plain",
    charset: Optional[str] = "utf-8",
    attachment_name: Optional[str] = None,
) -> web.Response:
    headers = dict(_ARTIFACT_HEADERS)
    if attachment_name is not None:
        # Filename sanitized to a conservative token set — never raw user path.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", attachment_name) or "artifact"
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    if isinstance(body, str):
        return web.Response(
            text=body, status=status, content_type=content_type,
            charset=charset, headers=headers,
        )
    return web.Response(
        body=body, status=status, content_type=content_type, headers=headers,
    )


def _not_found() -> web.Response:
    # One uniform 404 for malformed id / unknown id / vanished file /
    # containment refusal — the response does not disclose which.
    return _respond("Not found.\n", status=404)


# ── cellar cross-ref ────────────────────────────────────────────────────────


def _cellar_page_for(artifact_id: str) -> Optional[str]:
    """The cellar page_id whose filename hash matches id[:8], if any — the
    existing ``*-<hash>.md`` filename convention (wiki/pipeline._write_page).
    Serves nothing; used only to render the "ingested as" cross-ref line."""
    pages_dir = get_wiki_path() / "pages"
    if not pages_dir.is_dir():
        return None
    short = artifact_id[:8]
    for match in sorted(pages_dir.rglob(f"*-{short}.md")):
        return cellar_page_id(str(match.relative_to(pages_dir)))
    return None


# ── lineage + recency reads (artifact-continuation-v1 P3) ───────────────────


def _scan_artifact_events() -> List[dict]:
    """Every artifact_written event across all session ledgers, in file/line
    order (the flywheel dir-glob precedent — tolerant per-line parse)."""
    from grove.kaizen_ledger import default_ledger_dir

    events: List[dict] = []
    ledger_dir = default_ledger_dir()
    if not ledger_dir.is_dir():
        return events
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event_type") == "artifact_written":
                        events.append(event)
        except OSError:
            continue
    return events


def _lineage_for(artifact_id: str) -> tuple:
    """(parents, children) for an artifact id, ledger-derived and
    read-resilient: a legacy event without parent_artifact_ids contributes
    nothing; a malformed field is skipped, never an error. Each side is a
    list of (id, display_name) pairs, deduped, insertion-ordered."""
    parents: Dict[str, str] = {}
    children: Dict[str, str] = {}
    by_id: Dict[str, str] = {}
    events = _scan_artifact_events()
    for event in events:
        aid = event.get("artifact_id")
        path = event.get("path")
        if isinstance(aid, str) and isinstance(path, str):
            by_id[aid] = path
    for event in events:
        aid = event.get("artifact_id")
        raw_parents = event.get("parent_artifact_ids")
        if not isinstance(raw_parents, list):
            continue  # legacy event — no lineage contribution
        if aid == artifact_id:
            for pid in raw_parents:
                if isinstance(pid, str) and pid and pid not in parents:
                    parents[pid] = Path(by_id.get(pid, pid)).name
        if artifact_id in raw_parents and isinstance(aid, str) and aid:
            if aid not in children:
                children[aid] = Path(by_id.get(aid, aid)).name
    return list(parents.items()), list(children.items())


_RECENT_ARTIFACTS_CAP = 8


def _recent_artifacts(exclude_id: Optional[str] = None) -> List[tuple]:
    """The last N distinct artifacts (id, basename), newest first, for the
    compose-with target select. Ledger-derived; capped small."""
    seen: Dict[str, str] = {}
    for event in _scan_artifact_events():
        aid = event.get("artifact_id")
        path = event.get("path")
        if isinstance(aid, str) and isinstance(path, str) and aid != exclude_id:
            seen[aid] = Path(path).name  # later events win (newest state)
    items = list(seen.items())
    items.reverse()  # later file/line position ≈ newer → first here
    return items[:_RECENT_ARTIFACTS_CAP]


# ── the route ───────────────────────────────────────────────────────────────


def _resolve_contained(app: web.Application, artifact_id: str) -> Optional[Path]:
    """Shared id→contained-path resolution (raw route + in-shell fragment):
    16-hex validation, ledger lookup (lazy index), request-time
    ``resolve(strict=True)``, allowlist-root containment. Returns the resolved
    path, or ``None`` for every refusal class — malformed id, unknown id,
    vanished file, containment escape — so both consumers stay uniform-404."""
    if not _ID_RE.fullmatch(artifact_id):
        return None

    recorded = _lookup_artifact_path(app, artifact_id)
    if recorded is None:
        return None

    # CONTAINMENT canonical form — request-time strict resolve of the
    # ledger-recorded path. A file deleted after its emit is a 404, never 500.
    try:
        resolved = Path(recorded).resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    roots: List[Path] = app["artifact_roots"]
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


async def handle_artifact(request: web.Request) -> web.Response:
    artifact_id = request.match_info["artifact_id"]
    resolved = _resolve_contained(request.app, artifact_id)
    if resolved is None:
        return _not_found()

    suffix = resolved.suffix.lower()
    try:
        raw = resolved.read_bytes()
    except OSError:
        return _not_found()  # vanished between resolve and read

    if suffix == ".md":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _respond(raw, content_type="application/octet-stream",
                            charset=None, attachment_name=resolved.name)
        rendered = nh3.clean(
            markdown.markdown(text, extensions=["fenced_code", "tables"]),
            tags=_ARTIFACT_MD_TAGS,
            attributes=_ARTIFACT_MD_ATTRS,
            url_schemes=_ARTIFACT_URL_SCHEMES,
        )
        page = _cellar_page_for(artifact_id)
        crossref = ""
        if page is not None:
            esc_page = _html_mod.escape(page, quote=True)
            crossref = (
                f'<p class="crossref">ingested as '
                f'<a href="/portal#fragments/cellar/pages/{esc_page}">'
                f'{esc_page}</a></p>'
            )
        esc_id = _html_mod.escape(artifact_id)
        frame = (
            "<!DOCTYPE html><html><head>"
            f"<title>artifact {esc_id}</title>"
            "</head><body>"
            f"<h1>artifact {esc_id}</h1>{crossref}<hr>"
            f"{rendered}"
            "</body></html>"
        )
        return _respond(frame, content_type="text/html")

    if suffix in _TEXT_SUFFIXES:
        try:
            return _respond(raw.decode("utf-8"), content_type="text/plain")
        except UnicodeDecodeError:
            pass  # declared-text file with binary content → attachment

    # Binary, .html/.htm/.svg, and anything else: attachment, never inline.
    return _respond(raw, content_type="application/octet-stream",
                    charset=None, attachment_name=resolved.name)


# ── in-shell fragment (artifact-continuation-v1 C1) ─────────────────────────


def _fragment_not_found() -> web.Response:
    """Uniform 404 fragment — one body for malformed / unknown / vanished /
    containment-refused (no oracle, no input reflection)."""
    from grove.api.fragments import _html_fragment

    return _html_fragment(
        '<div class="error-card"><h3>Not found</h3>'
        '<p>No such artifact.</p></div>',
        status=404,
    )


async def handle_artifact_fragment(request: web.Request) -> web.Response:
    """GET /portal/fragments/artifact/{id} — the in-shell artifact view.

    Same resolution + containment as the raw route (shared helper — never a
    parallel path). Markdown renders inside a persistent model-content
    demarcation container using the PINNED artifact profile plus an
    unconditional anchor rewrite (nh3-native ``link_rel`` +
    ``set_tag_attribute_values``: every surviving anchor carries
    rel="noopener noreferrer" target="_blank"). Non-md classes render
    metadata + a raw-route link only — no inline content in-shell; the
    hardened raw endpoint is the isolation surface for those."""
    from grove.api.fragments import _html_fragment

    artifact_id = request.match_info["artifact_id"]
    resolved = _resolve_contained(request.app, artifact_id)
    if resolved is None:
        return _fragment_not_found()

    esc_id = _html_mod.escape(artifact_id)
    esc_name = _html_mod.escape(resolved.name)
    raw_link = (
        f'<p class="meta"><a href="/artifact/{esc_id}" '
        f'rel="noopener noreferrer" target="_blank">open raw</a></p>'
    )

    if resolved.suffix.lower() != ".md":
        # Metadata-only view: basename + type + raw link. No inline bytes.
        suffix = _html_mod.escape(resolved.suffix.lower() or "(none)")
        markup = (
            f'<article id="artifact-detail">'
            f'<h2>artifact {esc_id}</h2>'
            f'<p class="meta">{esc_name} &middot; type {suffix} &middot; '
            f'served as download only</p>'
            f'{raw_link}'
            f'</article>'
        )
        return _html_fragment(markup)

    try:
        text = resolved.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        # Vanished between resolve and read, or declared-md binary content —
        # fall to the uniform 404 (never inline undecodable bytes in-shell).
        return _fragment_not_found()

    rendered = nh3.clean(
        markdown.markdown(text, extensions=["fenced_code", "tables"]),
        tags=_ARTIFACT_MD_TAGS,
        attributes=_ARTIFACT_MD_ATTRS,
        url_schemes=_ARTIFACT_URL_SCHEMES,
        # Unconditional anchor rewrite, sanitizer-native (atomic with the
        # clean — no post-parse mutation gap): every surviving anchor opens
        # outside the shell and never carries the opener reference.
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )

    page = _cellar_page_for(artifact_id)
    crossref = ""
    if page is not None:
        esc_page = _html_mod.escape(page, quote=True)
        crossref = (
            f'<p class="crossref">ingested as '
            f'<a href="/portal#fragments/cellar/pages/{esc_page}">'
            f'{esc_page}</a></p>'
        )

    markup = (
        f'<article id="artifact-detail">'
        f'<h2>artifact {esc_id}</h2>'
        f'<p class="meta">{esc_name}</p>'
        f'{crossref}{raw_link}'
        f'<div class="model-content">'
        f'<div class="model-content-label">model-generated content</div>'
        f'<div class="page-body">{rendered}</div>'
        f'</div>'
        # P3 — lineage + verb panel mount OUTSIDE the model-content
        # demarcation: these are SHELL controls and ledger-derived facts,
        # never adjacent to model-prose ambiguity.
        f'{_lineage_html(artifact_id)}'
        f'{_verb_panel_html(artifact_id)}'
        f'</article>'
    )
    return _html_fragment(markup)


def _lineage_html(artifact_id: str) -> str:
    """Ledger-derived lineage section (arc acceptance: visible lineage).
    Read-resilient: no lineage → empty string, never an error."""
    try:
        parents, children = _lineage_for(artifact_id)
    except Exception as exc:  # noqa: BLE001 — render-side resilience
        logger.warning("[artifacts] lineage read failed (section omitted): %r", exc)
        return ""
    if not parents and not children:
        return ""

    def _links(pairs):
        return ", ".join(
            f'<a href="/portal#fragments/artifact/{_html_mod.escape(aid)}">'
            f'{_html_mod.escape(name)}</a>'
            for aid, name in pairs
        )

    rows = ""
    if parents:
        rows += f'<p class="meta">derived from: {_links(parents)}</p>'
    if children:
        rows += f'<p class="meta">continuations: {_links(children)}</p>'
    return f'<div class="lineage">{rows}</div>'


def _verb_panel_html(artifact_id: str) -> str:
    """The continuation verb panel — refine + compose-with. Shell controls
    (template-locked markup, system-derived values only); posts land in
    #verb-result below the panel."""
    esc_id = _html_mod.escape(artifact_id)
    try:
        recent = _recent_artifacts(exclude_id=artifact_id)
    except Exception as exc:  # noqa: BLE001 — render-side resilience
        logger.warning(
            "[artifacts] recent-artifacts read failed (compose select "
            "renders empty): %r", exc,
        )
        recent = []
    options = "".join(
        f'<option value="{_html_mod.escape(aid)}">'
        f'{_html_mod.escape(name)} ({_html_mod.escape(aid[:8])})</option>'
        for aid, name in recent
    )
    compose_select = (
        f'<select name="target_id">{options}</select>' if options
        else '<p class="meta">(no other artifacts in the ledger yet)</p>'
    )
    return (
        f'<div class="verb-panel">'
        f'<h3>Continue</h3>'
        f'<form hx-post="/portal/actions/artifact/{esc_id}/refine" '
        f'hx-target="#verb-result" hx-swap="innerHTML">'
        f'<textarea name="instruction" rows="3" '
        f'placeholder="Refine this artifact..."></textarea>'
        f'<button type="submit">Refine</button>'
        f'</form>'
        f'<form hx-post="/portal/actions/artifact/{esc_id}/compose" '
        f'hx-target="#verb-result" hx-swap="innerHTML">'
        f'<textarea name="instruction" rows="3" '
        f'placeholder="Compose with the selected artifact..."></textarea>'
        f'{compose_select}'
        f'<button type="submit">Compose</button>'
        f'</form>'
        f'<div id="verb-result"></div>'
        f'</div>'
    )


# ── continuation verbs (artifact-continuation-v1 P3) ────────────────────────

# GATE-B cond. 5 — small in-flight cap on PORTAL-ORIGINATED turns. The
# pending store stays uncapped; only concurrent turn minting is bounded.
# Guarded by a threading lock: dispatch runs in executor threads.
_MAX_INFLIGHT_TURNS = 2
_inflight_lock = threading.Lock()
_inflight_turns = 0


def _acquire_turn_slot() -> bool:
    global _inflight_turns
    with _inflight_lock:
        if _inflight_turns >= _MAX_INFLIGHT_TURNS:
            return False
        _inflight_turns += 1
        return True


def _release_turn_slot() -> None:
    global _inflight_turns
    with _inflight_lock:
        _inflight_turns = max(0, _inflight_turns - 1)


def _verb_error(message: str, status: int) -> web.Response:
    from grove.api.fragments import _html_fragment

    return _html_fragment(
        f'<div class="error-card"><h3>Not dispatched</h3>'
        f'<p>{_html_mod.escape(message)}</p></div>',
        status=status,
    )


async def _handle_continuation_verb(
    request: web.Request, parent_ids: List[str], instruction: str,
) -> web.Response:
    """Shared verb body: validate parents against the ledger (GATE-B cond. 4;
    400, the turn never mints), enforce the in-flight cap (cond. 5; 429),
    dispatch off-loop, render the template-locked result fragment."""
    import asyncio
    from functools import partial

    from grove.api.fragments import _html_fragment

    if not instruction.strip():
        return _verb_error("Instruction text is required.", 400)
    # POST-time parent validation — unknown/stale id → 400, never a dispatch.
    index = _scan_ledger_index()
    for pid in parent_ids:
        if not _ID_RE.fullmatch(pid) or pid not in index:
            return _verb_error(
                "Unknown artifact id — it is not in the ledger. Reload the "
                "artifact and try again.", 400,
            )

    if not _acquire_turn_slot():
        return _verb_error(
            "Too many continuation turns in flight — wait for one to finish "
            "and try again.", 429,
        )
    try:
        from grove.continuation import dispatch_continuation_turn

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            partial(dispatch_continuation_turn, instruction, parent_ids),
        )
    except Exception as exc:  # noqa: BLE001 — loud, never a blank card
        logger.warning("[artifacts] continuation dispatch failed: %r", exc)
        return _verb_error(
            "The continuation turn failed to run — see the gateway log.", 500,
        )
    finally:
        _release_turn_slot()

    # Template-locked result fragment: response text INSIDE a model-content
    # container; artifact links hash-route; pending items link to the
    # pending fragment. Values system-derived only.
    text = _html_mod.escape(result.get("response_text") or "(no response)")
    links = "".join(
        f'<p class="meta">Artifact: '
        f'<a href="/portal#fragments/artifact/{_html_mod.escape(aid)}">'
        f'{_html_mod.escape(aid)}</a></p>'
        for aid in result.get("artifact_ids_written") or []
    )
    pending = ""
    if result.get("pending_items"):
        n = len(result["pending_items"])
        pending = (
            f'<p class="meta">{n} action(s) await your approval — '
            f'<a href="/portal#fragments/proposals/pending">review pending</a>'
            f'</p>'
        )
    return _html_fragment(
        f'<div class="verb-outcome">'
        f'<div class="model-content">'
        f'<div class="model-content-label">model-generated content</div>'
        f'<div class="page-body">{text}</div>'
        f'</div>'
        f'{links}{pending}'
        f'</div>'
    )


async def handle_artifact_refine(request: web.Request) -> web.Response:
    artifact_id = request.match_info["artifact_id"]
    form = await request.post()
    instruction = str(form.get("instruction", ""))
    return await _handle_continuation_verb(request, [artifact_id], instruction)


async def handle_artifact_compose(request: web.Request) -> web.Response:
    artifact_id = request.match_info["artifact_id"]
    form = await request.post()
    instruction = str(form.get("instruction", ""))
    target_id = str(form.get("target_id", ""))
    return await _handle_continuation_verb(
        request, [artifact_id, target_id], instruction,
    )


def register_artifact_routes(app: web.Application) -> None:
    """Resolve allowlist roots (loud per-root rejection) and register the
    artifact routes: the hardened raw route, the in-shell fragment, and the
    continuation verb POSTs. All gated by portal_auth_middleware — the
    prefix set covers /artifact and /portal."""
    app["artifact_roots"] = resolve_artifact_roots()
    app["_artifact_index"] = {}
    app.router.add_get("/artifact/{artifact_id}", handle_artifact)
    # artifact-continuation-v1 C1 — in-shell view; the shell's generic hash
    # router maps #fragments/artifact/<id> onto this path.
    app.router.add_get(
        "/portal/fragments/artifact/{artifact_id}", handle_artifact_fragment
    )
    # artifact-continuation-v1 P3 — continuation verbs. POST-only (a GET is
    # 405 by router construction; test-pinned per GATE-B cond. 2).
    app.router.add_post(
        "/portal/actions/artifact/{artifact_id}/refine", handle_artifact_refine
    )
    app.router.add_post(
        "/portal/actions/artifact/{artifact_id}/compose", handle_artifact_compose
    )
