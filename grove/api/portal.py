"""Operator Portal substrate API — handlers, auth, and substrate singletons.

Sprint P1 (portal-api-scaffold-v1). Eight read-only GET endpoints on the
existing aiohttp gateway. JSON-only (HTML fragments ship in P2). Substrate
readers are app-level singletons built once at startup and refreshed by an
mtime staleness check, so no per-request index rebuild.

NO SILENT DEGRADATION. The only commanded defensive read is malformed
per-page YAML frontmatter in the cellar listing (logged + skipped so one
bad page does not blank the whole listing). Everything else surfaces.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from aiohttp import web

# Substrate readers — imported at module load so a missing reader fails the
# gateway loudly at startup, not lazily on first request.
from grove.capability import CapabilityKind, LifecycleState, Zone
from grove.capability_registry import load_capabilities
from grove.cellar import CellarIndex
from grove.dock import load_dock
from grove.eval import proposal_queue
from grove.eval.proposal_queue import read_all as read_all_proposals
from grove.memory.record import DECAY_RATES
from grove.memory.store import MemoryStore
from grove.wiki.index import MalformedWikiPage, WikiIndex, _split_frontmatter
from hermes_constants import get_hermes_home, get_wiki_path

logger = logging.getLogger(__name__)

# Tailscale assigns mesh addresses from the 100.64.0.0/10 CGNAT block.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK = ("127.0.0.1", "::1")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


@web.middleware
async def portal_auth_middleware(request: web.Request, handler):
    # PROXY NOTE: This checks request.remote directly. If a reverse proxy is
    # ever placed in front of the gateway, request.remote will be the proxy's
    # IP. Update this middleware to parse X-Forwarded-For / X-Real-IP in that
    # case.
    if not request.path.startswith("/api/substrate/"):
        return await handler(request)
    remote = request.remote
    if remote in _LOOPBACK:
        return await handler(request)
    if remote and remote.startswith("100."):
        try:
            if ipaddress.ip_address(remote) in _TAILSCALE_CGNAT:
                return await handler(request)
        except ValueError:
            pass
    return web.json_response(
        {"error": "forbidden",
         "detail": "Access restricted to localhost or Tailscale mesh"},
        status=403,
    )


# ---------------------------------------------------------------------------
# Substrate singletons + staleness checks
# ---------------------------------------------------------------------------


def _path_mtime(path: Path) -> float:
    """mtime of a file or directory; 0.0 when it does not exist yet — a real
    empty-substrate state (e.g. no pages authored), not a swallowed error."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def init_substrate_singletons(app: web.Application) -> None:
    """Construct each substrate reader once and attach it to app state.

    The wiki and cellar FTS indices are built LAZILY (on first search), not
    here. WikiIndex.build_index() is fail-loud by design — a single malformed
    page aborts the build — so building eagerly at startup would let one bad
    page crash the whole gateway (chat, Telegram, existing API). Deferring the
    build keeps that failure contained to the /search request, where it
    surfaces as a 500 with the offending page named. (SPEC amendment, GATE 2.)
    """
    app["wiki_index"] = WikiIndex(wiki_root=get_wiki_path())
    app["cellar_index"] = CellarIndex(cellar_dir=get_hermes_home())

    # MemoryStore.__init__ calls rebuild_index() (graceful: malformed JSONL
    # lines are logged and skipped) — _index is populated on construction.
    app["memory_store"] = MemoryStore(base_dir=get_hermes_home())

    app["_substrate_mtimes"] = {
        "wiki": _path_mtime(get_wiki_path() / "pages"),
        "memory": _path_mtime(app["memory_store"].log_path),
    }


def _check_wiki_stale(app: web.Application) -> None:
    """Ensure the wiki FTS index exists and is fresh. Called only by /search —
    the cellar listing/detail read files directly and never touch this index.

    A malformed page makes build_index()/update_index() raise MalformedWikiPage;
    that propagates to the /search handler as a 500 naming the bad page (fail
    loud, contained to the request)."""
    idx = app["wiki_index"]
    pages_dir = get_wiki_path() / "pages"
    current = _path_mtime(pages_dir)
    if not idx.index_path.exists():
        idx.build_index()  # lazy first build (deferred from startup)
        app["_substrate_mtimes"]["wiki"] = current
    elif current != app["_substrate_mtimes"].get("wiki"):
        # A directory's mtime changes when pages are added/removed but NOT when
        # a page is edited in place. update_index() re-checks per-file mtimes,
        # so in-place edits are caught on the next add/remove or restart —
        # acceptable staleness for a read-only metadata API.
        idx.update_index()
        app["_substrate_mtimes"]["wiki"] = current


def _check_memory_stale(app: web.Application) -> None:
    log_path = app["memory_store"].log_path
    current = _path_mtime(log_path)
    if current != app["_substrate_mtimes"].get("memory"):
        app["memory_store"].rebuild_index()
        app["_substrate_mtimes"]["memory"] = current


def _check_cellar_stale(app: web.Application) -> None:
    # The cellar is a multi-file corpus with no single backing file to stat, so
    # the mtime-cache pattern used for wiki/memory does not fit. CellarIndex's
    # own _ensure_fresh() does an incremental per-file mtime refresh (and a lazy
    # first build); query() also calls it internally, so this is belt-and-
    # suspenders for any non-query reader.
    app["cellar_index"]._ensure_fresh()


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(data: Any, count: Optional[int] = None) -> web.Response:
    """Wrap a payload in the canonical response envelope. ``governance_state``
    is null in P1; it becomes load-bearing in Sprint P4."""
    return web.json_response({
        "data": data,
        "meta": {
            "governance_state": None,
            "timestamp": _iso_now(),
            "count": count,
        },
    })


def _json_safe(obj: Any) -> Any:
    """Round-trip a payload through json with a str() fallback so non-JSON
    scalars (YAML-native dates, Path objects) serialize. For payloads with NO
    enums only — capabilities use _serialize_capability (enum -> .value), since
    str() on an enum would leak ``ClassName.MEMBER`` instead of the value."""
    return json.loads(json.dumps(obj, default=str))


def _serialize_capability(cap: Any) -> dict:
    """Serialize a Capability dataclass with recursive enum -> value conversion.

    dataclasses.asdict() recurses nested dataclasses but leaves Enum objects in
    place (kind, zone, and nested lifecycle.state / lifecycle.provenance /
    trigger.disclosure / context.disclosure / context.dock_composition /
    failure.fallback / tier_rule.validation.strategy) — none JSON-serializable.
    The default hook maps any Enum to its .value, anything else to str()."""
    raw = dataclasses.asdict(cap)
    return json.loads(json.dumps(
        raw, default=lambda o: o.value if isinstance(o, Enum) else str(o)
    ))


# ---------------------------------------------------------------------------
# Cellar endpoints (Phase 2)
# ---------------------------------------------------------------------------


def _as_str_list(value) -> list:
    """Lenient list coercion for listing metadata — never raises (a listing
    degrades, it does not crash on one odd field)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _read_page(path: Path) -> tuple:
    """Return ``(frontmatter_dict, body)`` for one page.

    Raises ``FileNotFoundError`` if the file vanished (e.g. deleted between the
    directory scan and the read), ``MalformedWikiPage`` if there is no
    terminated ``---`` frontmatter block or it is not a mapping, or
    ``yaml.YAMLError`` if the frontmatter will not parse. Callers decide
    whether that is a skip (listing) or a hard error (detail).
    """
    text = path.read_text(encoding="utf-8")
    fm_str, body = _split_frontmatter(text)  # raises MalformedWikiPage
    meta = yaml.safe_load(fm_str)            # raises yaml.YAMLError
    if not isinstance(meta, dict):
        raise MalformedWikiPage(f"frontmatter in {path.name} is not a mapping")
    return meta, body


async def handle_cellar_pages(request: web.Request) -> web.Response:
    """List canonical cellar pages with frontmatter metadata.

    A direct filesystem scan — deliberately independent of the wiki FTS index,
    so a malformed page is skipped here (commanded) rather than 500'ing the
    listing via the fail-loud index build.
    """
    pages_dir = get_wiki_path() / "pages"
    pages: list = []
    if pages_dir.is_dir():
        for path in sorted(pages_dir.glob("*.md")):
            try:
                meta, _body = _read_page(path)
            except FileNotFoundError:
                logger.warning(
                    "[portal] page vanished during scan, skipping: %s", path.name
                )
                continue
            except (yaml.YAMLError, MalformedWikiPage) as exc:
                # COMMANDED defensive read: one malformed page must not blank
                # the whole listing. Log loudly and skip; serve N-1 pages.
                logger.warning(
                    "[portal] malformed frontmatter, skipping %s: %r",
                    path.name, exc,
                )
                continue
            confidence = meta.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                pass
            pages.append({
                "page_id": path.stem,
                "title": meta.get("title"),
                "source_type": meta.get("source_type"),
                "topics": _as_str_list(meta.get("topics")),
                "key_entities": _as_str_list(meta.get("key_entities")),
                "dock_goal_refs": _as_str_list(meta.get("dock_goal_refs")),
                "confidence": confidence,
                "source": meta.get("source"),
            })
    return _envelope(pages, count=len(pages))


async def handle_cellar_page_detail(request: web.Request) -> web.Response:
    """Return a single cellar page: full frontmatter + markdown body."""
    page_id = request.match_info["page_id"]
    pages_dir = get_wiki_path() / "pages"
    path = pages_dir / f"{page_id}.md"
    # Containment guard — the {page_id} route already forbids slashes, but
    # refuse anything that resolves outside the pages directory.
    try:
        path.resolve().relative_to(pages_dir.resolve())
    except ValueError:
        return web.json_response(
            {"error": "not_found", "detail": f"Page {page_id} not found"},
            status=404,
        )
    try:
        meta, body = _read_page(path)
    except FileNotFoundError:
        return web.json_response(
            {"error": "not_found", "detail": f"Page {page_id} not found"},
            status=404,
        )
    except (yaml.YAMLError, MalformedWikiPage) as exc:
        # A detail request for a specific page that cannot parse is a real
        # failure, not a skip — surface it. No retry, no degradation.
        logger.warning("[portal] detail parse failure for %s: %r", page_id, exc)
        return web.json_response(
            {"error": "parse_error",
             "detail": "Page frontmatter is malformed or file is being written"},
            status=500,
        )
    # Frontmatter may carry YAML-native scalars (e.g. dates) that json cannot
    # serialize; coerce to JSON-safe values for this raw passthrough payload.
    return _envelope({"frontmatter": _json_safe(meta), "body": body, "page_id": page_id})


# ---------------------------------------------------------------------------
# Memory / Dock / Proposals / Skills endpoints (Phase 3)
# ---------------------------------------------------------------------------


async def handle_memory_records(request: web.Request) -> web.Response:
    """List ACTIVE memory records.

    projected_records() holds all statuses (active | superseded | deprecated |
    graduated); the endpoint serves only active — superseded/graduated records
    are suppressed from serving surfaces by design. (SPEC clarification: the
    endpoint is 'active records', and _index is not active-only.) Uses the
    public projected_records() accessor rather than the private _index attr.
    MemoryRecord has no enum fields — dataclasses.asdict() is JSON-clean.
    """
    _check_memory_stale(request.app)
    store = request.app["memory_store"]
    records = [
        dataclasses.asdict(rec)
        for rec in store.projected_records().values()
        if rec.status == "active"
    ]
    return _envelope(records, count=len(records))


async def handle_dock_goals(request: web.Request) -> web.Response:
    """List Dock goals, or null data when the Dock is not installed."""
    dock = load_dock()
    if dock is None:
        return _envelope(None, count=0)
    goals = []
    for g in dock.goals:
        goals.append({
            "id": g.id,
            "name": g.name,
            "vector": g.vector,
            "status": g.status,
            "definition_of_done": g.definition_of_done,
            "keywords": list(g.keywords),
            "unlocked_skills": list(g.unlocked_skills),
            "extra": g.extra,  # passthrough YAML — JSON-coerced below
        })
    # extra may carry YAML-native dates (deadline, milestones); coerce to safe.
    return _envelope(_json_safe(goals), count=len(goals))


async def handle_proposals_pending(request: web.Request) -> web.Response:
    """List pending Kaizen proposals (the review queue)."""
    proposals = read_all_proposals()
    data = [p.to_dict() for p in proposals]  # to_dict() is JSON-safe by construction
    return _envelope(data, count=len(data))


async def handle_skills(request: web.Request) -> web.Response:
    """List capability records of kind=skill."""
    caps = load_capabilities()
    skills = [
        _serialize_capability(cap)
        for cap in caps.values()
        if cap.kind == CapabilityKind.SKILL
    ]
    return _envelope(skills, count=len(skills))


# ---------------------------------------------------------------------------
# Search endpoint (Phase 4)
# ---------------------------------------------------------------------------


async def handle_search(request: web.Request) -> web.Response:
    """FTS5 search across wiki + cellar.

    Results are PARTITIONED, never merged: bm25 scores are corpus-relative and
    mathematically incomparable across the two indices, so a single ranked list
    would be meaningless. The consumer sees ``{"wiki": [...], "cellar": [...]}``.
    """
    q = request.query.get("q", "")
    if not q.strip():
        # Empty query is a normal no-op, not an error.
        return _envelope({"wiki": [], "cellar": []}, count=0)
    try:
        k = int(request.query.get("k", "10"))
    except ValueError:
        return web.json_response(
            {"error": "bad_request", "detail": "k must be an integer"},
            status=400,
        )
    k = max(1, min(k, 50))  # cap at 50; floor at 1
    source_type = request.query.get("source_type")

    # Lazy first-build + freshness. A malformed wiki page makes the build fail
    # loud — surface it as a contained 500 naming the page, not a gateway crash.
    try:
        _check_wiki_stale(request.app)
    except MalformedWikiPage as exc:
        logger.error("[portal] wiki index build failed during search: %r", exc)
        return web.json_response(
            {"error": "index_error", "detail": f"Wiki index build failed: {exc}"},
            status=500,
        )
    _check_cellar_stale(request.app)

    # Freshness already ensured above, so query with ensure_fresh=False.
    wiki = request.app["wiki_index"].query(
        text=q, k=k, source_type=source_type, ensure_fresh=False
    )
    cellar = request.app["cellar_index"].query(text=q, k=k)

    return _envelope(
        {
            "wiki": [dataclasses.asdict(r) for r in wiki],
            "cellar": [dataclasses.asdict(r) for r in cellar],
        },
        count=len(wiki) + len(cellar),
    )


# ---------------------------------------------------------------------------
# Meta endpoint (Phase 5)
# ---------------------------------------------------------------------------


async def handle_meta(request: web.Request) -> web.Response:
    """System metadata + enum vocabularies.

    Every enumeration is sourced from its module's source-of-truth constants so
    the contract can never drift from the runtime. proposal_queue has no single
    collection constant, so introspect its PROPOSAL_TYPE_* names (the private
    _LEGACY_ROUTING_TYPE has a different prefix and is excluded).
    """
    proposal_types = sorted(
        getattr(proposal_queue, name)
        for name in dir(proposal_queue)
        if name.startswith("PROPOSAL_TYPE_")
    )
    return _envelope({
        "node": {
            "version": "0.1.0",
            "protocol": "GRV-001",
        },
        "enumerations": {
            "lifecycle_states": [s.value for s in LifecycleState],
            "zone_classes": [z.value for z in Zone],
            "proposal_types": proposal_types,
            "memory_entity_types": list(DECAY_RATES.keys()),
            "capability_kinds": [k.value for k in CapabilityKind],
        },
    })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_portal_routes(app: web.Application) -> None:
    """Register all ``/api/substrate/`` routes.

    Handlers land incrementally across Sprint P1 phases: cellar (Phase 2);
    memory, dock, proposals, skills (Phase 3); search (Phase 4); meta
    (Phase 5).
    """
    # Phase 2 — cellar
    app.router.add_get("/api/substrate/cellar/pages", handle_cellar_pages)
    app.router.add_get("/api/substrate/cellar/pages/{page_id}", handle_cellar_page_detail)
    # Phase 3 — memory, dock, proposals, skills
    app.router.add_get("/api/substrate/memory/records", handle_memory_records)
    app.router.add_get("/api/substrate/dock/goals", handle_dock_goals)
    app.router.add_get("/api/substrate/proposals/pending", handle_proposals_pending)
    app.router.add_get("/api/substrate/skills/", handle_skills)
    # Phase 4 — search
    app.router.add_get("/api/substrate/search", handle_search)
    # Phase 5 — meta
    app.router.add_get("/api/substrate/meta", handle_meta)
