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


# ── the route ───────────────────────────────────────────────────────────────


async def handle_artifact(request: web.Request) -> web.Response:
    artifact_id = request.match_info["artifact_id"]
    if not _ID_RE.fullmatch(artifact_id):
        return _not_found()

    recorded = _lookup_artifact_path(request.app, artifact_id)
    if recorded is None:
        return _not_found()

    # CONTAINMENT canonical form — request-time strict resolve of the
    # ledger-recorded path. A file deleted after its emit is a 404, never 500.
    try:
        resolved = Path(recorded).resolve(strict=True)
    except (OSError, RuntimeError):
        return _not_found()

    roots: List[Path] = request.app["artifact_roots"]
    contained = False
    for root in roots:
        try:
            resolved.relative_to(root)
            contained = True
            break
        except ValueError:
            continue
    if not contained:
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


def register_artifact_routes(app: web.Application) -> None:
    """Resolve allowlist roots (loud per-root rejection) and register the
    read-only artifact route. Gated by portal_auth_middleware — the
    middleware's prefix set includes /artifact."""
    app["artifact_roots"] = resolve_artifact_roots()
    app["_artifact_index"] = {}
    app.router.add_get("/artifact/{artifact_id}", handle_artifact)
