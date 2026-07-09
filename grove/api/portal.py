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
from typing import Any, Dict, Optional

import yaml
from aiohttp import web

# Substrate readers — imported at module load so a missing reader fails the
# gateway loudly at startup, not lazily on first request.
from grove.capability import CapabilityKind, LifecycleState, Zone
from grove.capability_registry import load_capabilities
from grove.cellar import CellarIndex
from grove.composition.declaration import (
    _load_zone_map_from_schema,
    get_composition_status,
)
from grove.dock import load_dock
from grove.eval import proposal_queue
from grove.eval.proposal_queue import read_all as read_all_proposals
from grove.memory.digest import MemoryProposalHandler
from grove.memory.digest import _read_records as read_memory_records
from grove.memory.record import DECAY_RATES
from grove.memory.store import MemoryStore
from grove.wiki.index import MalformedWikiPage, WikiIndex, _split_frontmatter
from grove.wiki.links import cellar_page_id
from grove.wiki.watcher import ingest_file
from grove.zones import _resolve_schema_path
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
    # Both the JSON substrate API (/api/substrate/) and the HTML portal
    # (/portal, /portal/static, /portal/fragments) get the same localhost /
    # Tailscale-mesh gate. Everything else (chat, health, OpenAI-compat) passes.
    if not (request.path.startswith("/api/substrate/")
            or request.path.startswith("/portal")):
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
    logger.info(
        "[portal] init substrate: wiki_path=%s cellar_dir=%s",
        get_wiki_path(), get_hermes_home(),
    )
    app["wiki_index"] = WikiIndex(wiki_root=get_wiki_path())
    app["cellar_index"] = CellarIndex(cellar_dir=get_hermes_home())

    # MemoryStore.__init__ calls rebuild_index() (graceful: malformed JSONL
    # lines are logged and skipped) — _index is populated on construction.
    app["memory_store"] = MemoryStore(base_dir=get_hermes_home())

    # Zone map (R2″): pre-create the holder before the app starts so the lazy
    # first build in _get_zone_map mutates it IN PLACE rather than calling
    # app.__setitem__ on a started app (which aiohttp deprecates). Populated +
    # mtime-recorded on first /composition/nodes request, like the wiki index.
    app["_zone_map"] = {}

    app["_substrate_mtimes"] = {
        "wiki": _path_mtime(get_wiki_path() / "pages"),
        "memory": _path_mtime(app["memory_store"].log_path),
    }

    # propose-approve-deadlock-v1 Phase 1b-i — the process-level pending-RED store
    # singleton, so the portal approve handler and the render-side orphan check
    # reach the SAME store any Dispatcher stored the proposal into. In-memory
    # only; not a tool; no agent surface. (Render/approve wiring is 1b-ii.)
    from grove.red_pending_store import get_red_pending_store

    app["red_pending_store"] = get_red_pending_store()


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


def _get_zone_map(app: web.Application) -> dict:
    """Return the cached ``{sanitized_tool_key: zone_string}`` zone map,
    rebuilding it only when ``zones.schema.yaml``'s mtime changes.

    Mirrors the ``_check_wiki_stale`` mtime-staleness pattern so the schema is
    read ONCE per change, never per request (C3). The map is stored on app
    state (``app["_zone_map"]``) keyed against ``_substrate_mtimes["zones"]``.

    A2 (fail loud): a missing or unreadable schema raises — from
    ``_resolve_schema_path`` (ANDON A1 message) or the ``open()`` inside
    ``_load_zone_map_from_schema``. The granted-zone column is load-bearing for
    the authority-inversion view, so an empty map is never silently served.
    """
    schema_path = _resolve_schema_path(None)
    current = _path_mtime(schema_path)
    mtimes = app["_substrate_mtimes"]
    if "zones" not in mtimes or current != mtimes["zones"]:
        fresh = _load_zone_map_from_schema(schema_path)
        # Mutate the pre-created holder in place (see init_substrate_singletons)
        # so we never call app.__setitem__ on a started app.
        app["_zone_map"].clear()
        app["_zone_map"].update(fresh)
        mtimes["zones"] = current
    return app["_zone_map"]


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
        # Recursive: canonical pages are nested in per-source_type subdirs
        # (dock_goal/, scout_digest/, session_compacted/, ...). Matches
        # WikiIndex's **/*.md so the listing agrees with /search and detail.
        for path in sorted(pages_dir.glob("**/*.md")):
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
                # page_id is the path relative to pages_dir without .md (posix
                # slashes) — unique across subdirs, and equal to the /search
                # wiki source_path minus ".md".
                "page_id": path.relative_to(pages_dir).with_suffix("").as_posix(),
                "title": meta.get("title"),
                "source_type": meta.get("source_type"),
                "topics": _as_str_list(meta.get("topics")),
                "key_entities": _as_str_list(meta.get("key_entities")),
                "dock_goal_refs": _as_str_list(meta.get("dock_goal_refs")),
                "confidence": confidence,
                "source": meta.get("source"),
            })
    else:
        logger.warning("[portal] cellar pages directory does not exist: %s", pages_dir)
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


def _memory_proposals_path() -> Path:
    """Resolve ``~/.grove/memory_proposals.jsonl`` — the detector's crystallization
    staging file, distinct from the routing queue ``proposals.jsonl``."""
    return Path(get_hermes_home()) / "memory_proposals.jsonl"


def _memory_proposal_content(proposal: dict) -> str:
    """Content preview for a memory_context proposal, action-agnostic.

    ``create``/``supersede`` carry the text in ``proposed_record.content``;
    ``deprecate``/``graduate`` carry a flat ``content`` (no proposed_record).
    """
    rec = proposal.get("proposed_record")
    if isinstance(rec, dict) and rec.get("content"):
        return str(rec["content"])
    return str(proposal.get("content") or "")


def pending_memory_proposal_items() -> list:
    """Project pending memory_context crystallizations into review-queue items.

    The portal's review surface unifies two backing files: routing proposals
    (``proposals.jsonl``) and memory crystallizations (``memory_proposals.jsonl``).
    Each detector record is ``{session_id, status, timestamp, proposal}``; we
    filter to ``status == "pending"`` and project into a JSON-safe item that
    sits beside ``RoutingProposal.to_dict()`` in the combined list. The
    operator-facing summary reuses the existing ``MemoryProposalHandler``
    renderer so the portal and the CLI digest read identically.
    """
    items: list = []
    for rec in read_memory_records(_memory_proposals_path()):
        if rec.get("status") != "pending" or "proposal" not in rec:
            continue
        proposal = rec["proposal"]
        session_id = rec.get("session_id", "")
        # P4 — mint the SAME content-addressable id the digest mints
        # (digest._disposition_envelope), so the portal's action button and
        # the kaizen ledger agree on proposal_id. evidence parity is exact:
        # ``(session_id,)`` when present, else ``()``.
        evidence = (session_id,) if session_id else ()
        pid = proposal_queue.compute_proposal_id(
            type=proposal_queue.PROPOSAL_TYPE_MEMORY_CONTEXT,
            payload=proposal,
            evidence=evidence,
        )
        items.append({
            "proposal_id": pid,
            "type": "memory_context",
            "action": proposal.get("action", "create"),
            "content_preview": _memory_proposal_content(proposal),
            "semantic_justification": MemoryProposalHandler.summary_renderer(proposal),
            "created_at": rec.get("timestamp"),
        })
    return items


async def handle_proposals_pending(request: web.Request) -> web.Response:
    """List pending Kaizen proposals (the review queue).

    Unifies routing proposals (``proposals.jsonl``) and memory crystallizations
    (``memory_proposals.jsonl``) — the portal showed 0 while the agent reported
    59 because it read only the routing file (Sprint P3.1).
    """
    proposals = read_all_proposals()
    data = [p.to_dict() for p in proposals]  # to_dict() is JSON-safe by construction
    data.extend(pending_memory_proposal_items())
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
# Ingest endpoint (Sprint R1, compaction-ingest-contract-v1)
# ---------------------------------------------------------------------------


def _build_portal_url(
    page: Any, config: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """portal-link-reliability-v1 (P2) — a ready-made cellar deep link for a
    freshly ingested page, or ``None`` when no base URL resolves (I2).

    page_id parity with ``wiki/provider._format_result`` is MANDATORY (I3):
    ``page.path`` from ``ingest_file`` is ABSOLUTE
    (``get_wiki_path()/pages/<source_type>/<slug>-<hash>.md``), so the id is
    derived RELATIVE to the pages root — the exact contract
    ``handle_cellar_page_detail`` (``pages_dir`` at line 333) and the cellar
    listing (line 316) use. ``config`` is the resident snapshot when the caller
    has one; ``None`` falls back to ``load_config()`` inside
    ``resolve_portal_base_url`` (tolerable on the infrequent ingest path).
    """
    from grove.prompt.portal_links import resolve_portal_base_url

    base = resolve_portal_base_url(config=config)
    if not base:
        return None
    pages_root = get_wiki_path() / "pages"
    page_id = cellar_page_id(page.path.relative_to(pages_root))
    return f"{base}/portal#fragments/cellar/pages/{page_id}"


async def handle_ingest(request: web.Request) -> web.Response:
    """``POST /api/substrate/ingest`` — compact one source file into the cellar.

    Body: ``{"path": "<absolute filepath>"}``. The producer's terminal act — a
    fleet skill (or any caller) posts the path it just wrote, and the file
    compacts through the SAME :func:`grove.wiki.watcher.ingest_file` gate the
    CLI and scanner use. Idempotent by the mtime ledger: re-posting an unchanged
    file returns ``ingested: false`` and writes no duplicate.

    Fail loud: a glob-matched file whose shape is malformed raises inside
    ``ingest_file`` and surfaces as a 500 (the adapter names the defect) — it is
    never swallowed. Auth (loopback + Tailscale) is applied upstream by
    ``portal_auth_middleware``. governance_state stays null in the standard
    envelope (R1 scope; the write vocabulary is R1.5/P4).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"error": "bad_request", "detail": "body must be JSON"}, status=400
        )
    raw = body.get("path") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return web.json_response(
            {"error": "bad_request", "detail": "missing required string 'path'"},
            status=400,
        )
    path = Path(raw)
    if not path.is_file():
        return web.json_response(
            {"error": "not_found", "detail": f"no such file: {raw}"}, status=404
        )

    page = ingest_file(path)
    if page is None:
        return _envelope({"ingested": False, "path": str(path)})
    # portal-link-reliability-v1 (P2) — enrich with a ready-made cellar deep
    # link. Prefer the app's resident config snapshot; None falls back to
    # load_config() (Decision 2, documented deviation — ingest is infrequent).
    portal_url = _build_portal_url(page, config=request.app.get("config"))
    envelope = {
        "ingested": True,
        "source_type": page.source_type,
        "source": page.source,
        "title": page.title,
        "page_path": str(page.path),
    }
    if portal_url:
        envelope["portal_url"] = portal_url
    return _envelope(envelope)


# ---------------------------------------------------------------------------
# Composition endpoint (R2″ node-compositor-view-v1)
# ---------------------------------------------------------------------------


async def handle_composition_nodes(request: web.Request) -> web.Response:
    """``GET /api/substrate/composition/nodes`` — live GRV-004 composition state.

    Reports every composed node AND every dark MCP server, each tool carrying
    both its declared ``proposed_zone`` and the engine's ``granted_zone`` (the
    authority inversion). Reads runtime module globals via
    :func:`get_composition_status`, NOT the static ``compose-with.json`` snapshot
    (I2).

    Both inputs to the accessor are resolved HERE, outside the engine's MCP
    ``_lock``: the zone map from the mtime-cached :func:`_get_zone_map` (C3) and
    the ``mcp_servers`` config from a single ``_load_mcp_config()`` read (cheap,
    but kept off the lock path per C3). The accessor then holds the lock only for
    its five dict-snapshot copies (C1).
    """
    zone_map = _get_zone_map(request.app)
    # Lazy import: keep the heavy tools.mcp_tool subsystem off portal.py's
    # import path. _load_mcp_config returns {} when no config is present.
    from tools.mcp_tool import _load_mcp_config

    mcp_servers_config = _load_mcp_config()
    nodes = get_composition_status(
        mcp_servers_config=mcp_servers_config,
        zone_map=zone_map,
    )
    return _envelope(nodes, count=len(nodes))


# ---------------------------------------------------------------------------
# Fleet artifact endpoints (fleet-artifact-viewer-v1)
# ---------------------------------------------------------------------------
#
# Serve the RAW fleet skill outputs (~/.grove/{scout,researcher,drafter,
# cultivator}/) — distinct from the compacted cellar pages the cellar endpoints
# serve. Fleet membership is structural: a kind=skill capability record carrying
# a governance block (structural-review-gate-v1) IS a fleet skill; its
# write_zone declares the staging/canonical dirs and its terminal_artifact the
# filename pattern. The reader helpers are shared with the P2 portal fragments.


def _fleet_skill_records() -> Dict[str, Any]:
    """``{skill_name: Capability}`` for every kind=skill record carrying a
    governance block (the fleet capabilities). ``skill_name`` is the record id's
    trailing segment (``skill.fleet.scout`` -> ``scout``)."""
    out: Dict[str, Any] = {}
    for cid, cap in load_capabilities().items():
        if cap.kind == CapabilityKind.SKILL and cap.governance:
            out[cid.rsplit(".", 1)[-1]] = cap
    return out


def _fleet_zone_dirs(cap: Any) -> tuple:
    """``(zone_str, staging_dir: Path, canonical_dir: Path, pattern: str)`` for a
    fleet capability. Dirs resolve relative to ``$GROVE_HOME``. Raises
    ``KeyError`` if the governance block omits write_zone dirs (a malformed
    record — surfaced, never silently defaulted)."""
    grove = Path(get_hermes_home())
    wz = cap.governance["write_zone"]
    staging = grove / wz["staging_dir"]
    canonical = grove / wz["canonical_dir"]
    pattern = (
        (cap.governance.get("emission_preconditions") or {})
        .get("terminal_artifact", {})
        .get("path_pattern", "*")
    )
    return cap.zone.value, staging, canonical, pattern


def _fleet_mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# fleet-review-unification-v1 C2 — four-state artifact disposition (read-side).
#
# Replaces the two-state topology flag with a governance_state joined from:
#   * filesystem topology — AUTHORITATIVE for mv-sink producers (drafter,
#     cultivator): canonical presence = promoted.
#   * the live proposal store (open artifact proposals) — needs_review.
#   * the per-(worker, unit_id) feedback store — revision_requested / rejected
#     (won't-converge) + the revision_count / directive echo disclosure.
#   * the kaizen_disposition ledger — AUTHORITATIVE for the remote-publish sink
#     (forge → Drive), whose staged dir LINGERS post-publish so the filesystem
#     cannot show 'promoted'. Sink-authority rule (GATE-B): canonical_sink=="forge"
#     → ledger-authoritative terminals; else filesystem-authoritative.
#
# TOPOLOGICAL SUPREMACY — reconcile on read: an open proposal whose artifact
# already sits in canonical (out-of-band mv) is auto-closed promoted_out_of_band;
# an open proposal whose artifact was archived out of band (no staged, no
# canonical) with no live revision is auto-closed rejected_out_of_band.
# ---------------------------------------------------------------------------

_ARTIFACT_PROPOSAL_TYPES = (
    proposal_queue.PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
    proposal_queue.PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
)


def _artifact_unit_id(payload: Optional[dict]) -> Optional[str]:
    """The stable unit identity a proposal/ledger event keys on: unit_id (file
    producer) → row_id (forge) → slug (last resort)."""
    pl = payload or {}
    return pl.get("unit_id") or pl.get("row_id") or pl.get("slug")


def _open_artifact_proposals(skill_id: str) -> Dict[str, Any]:
    """``{unit_id -> live proposal}`` for this skill's OPEN artifact proposals
    (forge_artifact_pending + fleet_artifact_pending). read_all() returns only live
    proposals; terminals are popped into the ledger."""
    out: Dict[str, Any] = {}
    for p in read_all_proposals():
        if getattr(p, "type", None) not in _ARTIFACT_PROPOSAL_TYPES:
            continue
        if (p.payload or {}).get("skill_id") != skill_id:
            continue
        uid = _artifact_unit_id(p.payload)
        if uid:
            out[uid] = p
    return out


def _feedback_units(worker: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """``{unit_id -> {count, terminal_skip, latest_note, mtime}}`` from the worker's
    feedback store (``~/.grove/<worker>/.feedback/*.json``). Empty when worker is
    None / the dir is absent. A corrupt entry is skipped — a read-side view must
    not 500 on one bad file (the write paths already fail loud)."""
    if not worker:
        return {}
    fbdir = Path(get_hermes_home()) / worker / ".feedback"
    if not fbdir.is_dir():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for fp in fbdir.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        hist = d.get("history") or []
        out[fp.stem] = {
            "count": int(d.get("count", 0)),
            "terminal_skip": bool(d.get("terminal_skip")),
            "latest_note": (hist[-1].get("revision_note") if hist else None),
            "mtime": fp.stat().st_mtime,
        }
    return out


def _ledger_terminal_dispositions() -> Dict[str, str]:
    """``{unit_id -> 'applied'|'rejected'}`` from the kaizen_disposition ledger, for
    artifact proposals — the remote-publish sink's terminal source of truth. Keyed on
    the unit identity the disposition's ``applied_result`` carries (C2 enriches
    promote/reject with unit_id + slug); a reject's ``archive_path`` slug is the
    fallback key. Later events win (the last disposition is authoritative)."""
    ledger_dir = Path(get_hermes_home()) / ".kaizen_ledger"
    out: Dict[str, str] = {}
    if not ledger_dir.is_dir():
        return out
    for lf in sorted(ledger_dir.glob("*.jsonl")):
        try:
            lines = lf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event_type") != "kaizen_disposition":
                continue
            if ev.get("proposal_type") not in _ARTIFACT_PROPOSAL_TYPES:
                continue
            disp = ev.get("disposition")
            if disp not in ("applied", "rejected"):
                continue  # suggest_revision is not a terminal
            ar = ev.get("applied_result") or {}
            uid = ar.get("unit_id") or ar.get("slug")
            if not uid and ar.get("archive_path"):
                base = Path(str(ar["archive_path"])).name  # <slug>-<utc-ts>
                uid = base.rsplit("-", 1)[0] if "-" in base else base
            if uid:
                out[uid] = disp
    return out


def _reverse_pattern(filename: str, pattern: str) -> str:
    """Recover a unit_id from a flat canonical filename by stripping the
    ``terminal_artifact`` pattern's fixed prefix/suffix (the ``*`` is the unit_id):
    ``draft-*.md`` over ``draft-moon-bot.md`` → ``moon-bot``. A pattern without a
    ``*`` (or a name that does not fit) falls back to the filename stem."""
    if "*" not in pattern:
        return Path(filename).stem
    pre, suf = pattern.split("*", 1)
    s = filename
    if pre and s.startswith(pre):
        s = s[len(pre):]
    if suf and s.endswith(suf):
        s = s[: -len(suf)]
    return s


def _unit_from_meta(meta_path: Path, dirname: str) -> str:
    """The staged unit's unit_id from its synthesized/self-authored ``meta.json``
    (unit_id → row_id → slug), falling back to the dir name."""
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(m, dict):
            return m.get("unit_id") or m.get("row_id") or m.get("slug") or dirname
    except (json.JSONDecodeError, OSError):
        pass
    return dirname


def _resolve_unit_state(uid, producer, staged, canon, feedback, open_props,
                        ledger, remote_sink, autoclose) -> Optional[dict]:
    """Resolve ONE unit to its four-state governance_state + payload, applying
    topological supremacy (filesystem-first) and the sink-authority rule, and
    queuing any out-of-band proposal auto-close. Returns the artifact payload dict
    (with a private ``_mt`` sort key) or None."""
    fb = feedback.get(uid)
    prop = open_props.get(uid)
    rc = fb["count"] if fb else 0

    def row(state, mt, filename=None, size=None, include_prop=False):
        r = {
            "unit_id": uid,
            "producer": producer,
            "governance_state": state,
            "revision_count": rc,
            "mtime": _fleet_mtime_iso(mt),
            "_mt": mt,
        }
        if rc > 0 and fb and fb["latest_note"]:
            r["directive_echo"] = fb["latest_note"]
        if filename is not None:
            r["filename"] = filename
        if size is not None:
            r["size"] = size
        if include_prop and prop is not None:
            r["proposal_id"] = prop.proposal_id
            r["proposal_type"] = prop.type
        return r

    # (1) TOPOLOGICAL SUPREMACY — canonical presence wins (mv-sink promoted).
    if uid in canon:
        mt, fn, p = canon[uid]
        if prop is not None:  # open proposal + already-canonical = out-of-band mv
            autoclose.append((prop, "promoted_out_of_band",
                              {"unit_id": uid, "reconciled": "canonical_present"}))
        return row("promoted", mt, filename=fn, size=p.stat().st_size)

    # (2) Remote-publish sink (forge) — ledger authoritative for the terminal; the
    #     staged dir may linger post-publish, so the ledger wins over it.
    if remote_sink and uid in ledger:
        disp = ledger[uid]
        mt = staged[uid][0] if uid in staged else (fb["mtime"] if fb else 0.0)
        fn = staged[uid][1] if uid in staged else None
        if prop is not None:
            autoclose.append((
                prop,
                "promoted_out_of_band" if disp == "applied" else "rejected_out_of_band",
                {"unit_id": uid, "reconciled": "ledger_terminal"},
            ))
        return row("promoted" if disp == "applied" else "rejected", mt, filename=fn)

    # (3) Staged draft present.
    if uid in staged:
        mt, fn, _ud = staged[uid]
        if prop is not None:
            return row("needs_review", mt, filename=fn, include_prop=True)
        return row("legacy", mt, filename=fn)  # grandfathered: staged, no proposal

    # (4) No staged, no canonical. An open proposal here = out-of-band archive.
    if prop is not None:
        mt = fb["mtime"] if fb else 0.0
        if fb and not fb["terminal_skip"]:
            return row("revision_requested", mt)  # redraft pending; superseded on redraft
        autoclose.append((prop, "rejected_out_of_band",
                          {"unit_id": uid, "reconciled": "artifact_archived_oob"}))
        return row("rejected", mt)

    # (5) No artifact, no proposal — feedback-only unit (the redraft window /
    #     won't-converge). A plain Reject leaves no feedback trace and is not listed.
    if fb:
        return row("rejected" if fb["terminal_skip"] else "revision_requested",
                   fb["mtime"])
    return None


def _list_fleet_units(cap: Any) -> list:
    """The C2 four-state disposition list for one fleet skill, newest-first. Joins
    filesystem topology + proposal store + feedback store + (remote sink) ledger,
    reconciling on read. Auto-closes out-of-band proposals as a side effect (the
    single read-side write; Verdict A proves it does not drift the forge flow)."""
    skill_id = cap.id
    producer = skill_id.rsplit(".", 1)[-1]
    _zone, staging, canonical, pattern = _fleet_zone_dirs(cap)
    canonical_sink = cap.governance["write_zone"]["canonical_dir"]
    remote_sink = canonical_sink == "forge"
    from grove.api.actions import _worker_id_for_skill

    worker = _worker_id_for_skill(skill_id)
    has_pending = staging.resolve() != canonical.resolve()

    open_props = _open_artifact_proposals(skill_id)
    feedback = _feedback_units(worker)
    ledger = _ledger_terminal_dispositions() if remote_sink else {}

    # staged units — nested pending_review/<unit>/meta.json (C1b-2), plus any flat
    # legacy files (grandfathered pre-nesting).
    staged: Dict[str, tuple] = {}
    if has_pending and staging.is_dir():
        for meta_path in staging.glob("*/meta.json"):
            ud = meta_path.parent
            uid = _unit_from_meta(meta_path, ud.name)
            staged[uid] = (meta_path.stat().st_mtime, ud.name, ud)
        for p in staging.glob(pattern):  # flat legacy (non-recursive)
            if p.is_file():
                staged.setdefault(
                    _reverse_pattern(p.name, pattern),
                    (p.stat().st_mtime, p.name, p),
                )

    # canonical artifacts — flat files (mv-sink promoted).
    canon: Dict[str, tuple] = {}
    if canonical.is_dir():
        for p in canonical.glob(pattern):  # non-recursive: skips pending_review/
            if p.is_file():
                canon[_reverse_pattern(p.name, pattern)] = (p.stat().st_mtime, p.name, p)

    autoclose: list = []
    uids = set(staged) | set(canon) | set(feedback) | set(open_props) | set(ledger)
    rows = []
    for uid in uids:
        rec = _resolve_unit_state(uid, producer, staged, canon, feedback,
                                  open_props, ledger, remote_sink, autoclose)
        if rec is not None:
            rows.append(rec)

    # reconcile-on-read WRITE — auto-close out-of-band proposals to the terminal the
    # filesystem/ledger already reflects (idempotent; a re-read finds them gone).
    for prop, status, applied_result in autoclose:
        proposal_queue.finalize_proposal_state(prop.proposal_id, status, applied_result)

    rows.sort(key=lambda r: r["_mt"], reverse=True)
    for r in rows:
        r.pop("_mt", None)
    return rows


def _fleet_presentation(cap: Any) -> tuple:
    """``(presentation dict | None, presentation_error str | None)`` from the
    capability's terminal_artifact block (fleet-artifact-legibility-v1 C1).
    Loader-validated: an error means the declaration is treated as absent."""
    ta = (
        ((cap.governance or {}).get("emission_preconditions") or {})
        .get("terminal_artifact") or {}
    )
    err = ta.get("presentation_error")
    pres = ta.get("presentation") if not err else None
    return (pres if isinstance(pres, dict) else None), err


def _fleet_worker_registry() -> Dict[str, tuple]:
    """``{capability skill_id -> (worker_id, WorkerConfig)}`` from the operational
    registry (config/fleet_workers.yaml). A missing/malformed registry is logged
    LOUD and yields ``{}`` — the read-side index stays up with ``worker: null``
    rows (the same commanded-skip parity as a malformed capability record)."""
    from grove.fleet.config import load_fleet_workers
    from grove.fleet.errors import FleetWorkerAndon

    try:
        workers = load_fleet_workers()
    except FleetWorkerAndon as exc:
        logger.warning("[portal] fleet worker registry unreadable: %s", exc)
        return {}
    return {cfg.skill: (wid, cfg) for wid, cfg in workers.items()}


def _worker_last_run(worker_id: str) -> Optional[dict]:
    """``{ts, status}`` from the worker's newest terminal-state event, or None.

    The event bus at ``$GROVE_HOME/fleet/<id>/events/`` is the persisted run
    record (every run writes one before exit). File mtime selects the newest
    event FILE; the reported ``ts`` comes from the event body — never fabricated
    from artifact mtimes. An unreadable newest event is logged and yields None."""
    from grove.fleet.paths import events_dir

    d = events_dir(worker_id)
    if not d.is_dir():
        return None
    files = [p for p in d.glob("*.json") if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        ev = json.loads(newest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[portal] unreadable terminal event %s/%s: %r",
            worker_id, newest.name, exc,
        )
        return None
    if not isinstance(ev, dict):
        return None
    return {"ts": ev.get("ts"), "status": ev.get("status")}


def _ingest_ledger() -> Dict[str, float]:
    """The wiki watcher's idempotency ledger (source path -> source mtime at
    ingest), or ``{}`` when absent/unreadable (logged)."""
    path = get_wiki_path() / ".index" / "ingest_state.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[portal] wiki ingest ledger unreadable: %r", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _fleet_index_rows() -> list:
    """The fleet index rows — ONE pass over the capability records joining the
    C2 unit disposition, the operational worker registry, the terminal-event
    bus, and the wiki ingest ledger (fleet-ui-reconciliation-v1 C2). DATA ONLY
    (F6): no HTML, no layout hints — the JSON API, the outline nav, and the
    status board all inherit this row."""
    from collections import Counter

    records = _fleet_skill_records()
    registry = _fleet_worker_registry()
    ledger = _ingest_ledger()
    rows = []
    for name in sorted(records):
        cap = records[name]
        try:
            units = _list_fleet_units(cap)
            _zone, _staging, canonical, _pattern = _fleet_zone_dirs(cap)
        except KeyError as exc:
            # Parity with the cellar listing's commanded skip: one malformed
            # record must not blank the whole index. Logged loud, not swallowed.
            logger.warning(
                "[portal] fleet skill %s has a malformed governance block, "
                "skipping: %r", name, exc,
            )
            continue
        counts = Counter(u["governance_state"] for u in units)
        mode = ((cap.governance.get("approval_handoff") or {}).get("mode"))
        presentation, presentation_error = _fleet_presentation(cap)
        worker = None
        last_run = None
        reg = registry.get(cap.id)
        if reg is not None:
            wid, cfg = reg
            worker = {
                "id": wid,
                "enabled": cfg.enabled,
                "cadence": cfg.cadence,
                "quiet_hours": cfg.quiet_hours,
            }
            last_run = _worker_last_run(wid)
        # Observer freshness — max ledger mtime + entry count under the skill's
        # canonical dir. The ledger value is the SOURCE file's mtime recorded at
        # ingest ("newest source successfully ingested"), not ingest wall-clock.
        prefix = str(canonical) + "/"
        ingested = [v for k, v in ledger.items()
                    if k.startswith(prefix) and isinstance(v, (int, float))]
        rows.append({
            "name": name,
            "zone": cap.zone.value,
            "mode": mode,
            # C4 — structural promotion provenance (Skill Flywheel lineage):
            # lifecycle.provenance + lineage.parent_id, passthrough verbatim.
            # Today every fleet record is operator_authored/parent-less, so
            # renderers show no lineage line — honest, never name-inferred.
            "provenance": cap.lifecycle.provenance.value,
            "parent_id": cap.lineage.parent_id,
            # fleet-artifact-legibility-v1 C1 — the presentation declaration,
            # verbatim (data only: field paths and labels, never HTML).
            "presentation": presentation,
            "presentation_error": presentation_error,
            "worker": worker,
            "last_run": last_run,
            "last_ingest": _fleet_mtime_iso(max(ingested)) if ingested else None,
            "ingested_count": len(ingested),
            "artifact_count": len(units),
            "latest_mtime": units[0]["mtime"] if units else None,
            "needs_review_count": counts.get("needs_review", 0),
            "state_counts": dict(counts),
        })
    return rows


async def handle_fleet_index(request: web.Request) -> web.Response:
    """``GET /api/substrate/fleet/`` — one row per fleet skill: the C2 four-state
    disposition aggregation plus the C2-passthrough operational fields (mode /
    worker schedule / last_run / ingest freshness)."""
    skills = _fleet_index_rows()
    return _envelope({"skills": skills}, count=len(skills))


async def handle_fleet_skill(request: web.Request) -> web.Response:
    """``GET /api/substrate/fleet/{skill_name}/`` — the C2 four-state disposition
    list for one fleet skill, newest-first. Unknown skill -> 404."""
    skill_name = request.match_info["skill_name"]
    cap = _fleet_skill_records().get(skill_name)
    if cap is None:
        return web.json_response(
            {"error": "not_found", "detail": f"unknown fleet skill: {skill_name}"},
            status=404,
        )
    return _envelope({"artifacts": _list_fleet_units(cap)})


def _resolve_fleet_artifact(cap: Any, filename: str) -> Optional[tuple]:
    """Resolve ``filename`` to ``(path, governance_state)`` for a fleet skill,
    honoring the Yellow-zone lookup order (``pending_review/`` first, then the
    canonical dir). Returns ``None`` when neither holds the file OR a containment
    guard rejects a candidate that resolves outside its intended dir. Shared by
    the JSON content endpoint and the P2 portal artifact page (single resolver,
    no duplicated lookup)."""
    _zone, staging, canonical, _pattern = _fleet_zone_dirs(cap)
    search: list = []
    if staging.resolve() != canonical.resolve():
        search.append((staging, "pending_review"))  # pending_review first
    search.append((canonical, "canonical"))
    for base, state in search:
        candidate = base / filename
        try:
            candidate.resolve().relative_to(base.resolve())
        except ValueError:
            continue  # traversal-guarded out
        if candidate.is_file():
            return candidate, state
    return None


async def handle_fleet_artifact(request: web.Request) -> web.Response:
    """``GET /api/substrate/fleet/{skill_name}/{filename}`` — raw artifact content
    with the appropriate Content-Type.

    Lookup order for a Yellow-zone skill: ``pending_review/`` first, then the
    canonical dir, else 404. ``.md`` renders to sanitized HTML via the P2
    markdown renderer; ``.json`` returns the file verbatim as application/json;
    anything else falls back to text/plain. The ``{filename}`` route segment
    cannot carry a slash, and a containment guard rejects any candidate that
    resolves outside its intended dir.
    """
    skill_name = request.match_info["skill_name"]
    filename = request.match_info["filename"]
    cap = _fleet_skill_records().get(skill_name)
    if cap is None:
        return web.json_response(
            {"error": "not_found", "detail": f"unknown fleet skill: {skill_name}"},
            status=404,
        )
    read = _read_fleet_artifact(cap, filename)
    if read is None:
        return web.json_response(
            {"error": "not_found",
             "detail": f"artifact {filename} not found for skill {skill_name}"},
            status=404,
        )
    raw, suffix, _state = read
    if suffix == ".md":
        # Lazy import: grove.api.fragments imports this module, so a top-level
        # import would be circular. The renderer is only needed at request time.
        from grove.api.fragments import _render_md
        return web.Response(text=_render_md(raw), content_type="text/html")
    if suffix == ".json":
        return web.Response(text=raw, content_type="application/json")
    return web.Response(text=raw, content_type="text/plain")


def _read_fleet_artifact(cap: Any, filename: str) -> Optional[tuple]:
    """Read a fleet artifact through the shared resolver: ``(raw, suffix, state)``
    or ``None`` if not found. The single content reader both the JSON endpoint and
    the P2 portal page funnel through — neither re-scans the filesystem."""
    resolved = _resolve_fleet_artifact(cap, filename)
    if resolved is None:
        return None
    target, state = resolved
    return target.read_text(encoding="utf-8"), target.suffix, state


def _read_forge_slug(slug: str) -> Optional[dict]:
    """Read a forge ``pending_review/<slug>/`` draft dir (forge-jobsearch-v1).

    The forge stages a DIRECTORY (``resume.md`` + ``cover-letter.md`` + a
    ``meta.json`` sidecar carrying ``{row_id, company, role, slug}``), unlike the
    single-file fleet artifacts. Returns
    ``{"slug","meta","meta_error","resume_md","cover_md"}`` or ``None`` when the
    dir or either draft is absent. Path-safe: a ``slug`` that escapes the forge
    staging dir (traversal) resolves to ``None`` (fail-closed)."""
    staging = (Path(get_hermes_home()) / "forge" / "pending_review").resolve()
    slug_dir = (staging / slug).resolve()
    if not slug_dir.is_relative_to(staging) or not slug_dir.is_dir():
        return None
    resume, cover = slug_dir / "resume.md", slug_dir / "cover-letter.md"
    if not resume.is_file() or not cover.is_file():
        return None
    meta: Optional[dict] = None
    meta_error: Optional[str] = None
    meta_path = slug_dir / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            meta_error = f"meta.json is unreadable: {exc}"
    else:
        meta_error = "meta.json not found in the slug dir"
    return {
        "slug": slug,
        "meta": meta,
        "meta_error": meta_error,
        "resume_md": resume.read_text(encoding="utf-8"),
        "cover_md": cover.read_text(encoding="utf-8"),
    }


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
    # {page_id:.+} allows the subdir-qualified page_id (e.g. dock_goal/foo) to
    # carry slashes. The handler's containment guard blocks path traversal.
    app.router.add_get("/api/substrate/cellar/pages/{page_id:.+}", handle_cellar_page_detail)
    # Phase 3 — memory, dock, proposals, skills
    app.router.add_get("/api/substrate/memory/records", handle_memory_records)
    app.router.add_get("/api/substrate/dock/goals", handle_dock_goals)
    app.router.add_get("/api/substrate/proposals/pending", handle_proposals_pending)
    app.router.add_get("/api/substrate/skills/", handle_skills)
    # Phase 4 — search
    app.router.add_get("/api/substrate/search", handle_search)
    # Phase 5 — meta
    app.router.add_get("/api/substrate/meta", handle_meta)
    # R1 (compaction-ingest-contract-v1) — ingest write endpoint
    app.router.add_post("/api/substrate/ingest", handle_ingest)
    # R2″ (node-compositor-view-v1) — live composition state
    app.router.add_get("/api/substrate/composition/nodes", handle_composition_nodes)
    # fleet-artifact-viewer-v1 — raw fleet skill artifacts (index, per-skill
    # list, artifact content). Registration order is unambiguous — the three
    # patterns differ in segment count / trailing slash.
    app.router.add_get("/api/substrate/fleet/", handle_fleet_index)
    app.router.add_get("/api/substrate/fleet/{skill_name}/", handle_fleet_skill)
    app.router.add_get("/api/substrate/fleet/{skill_name}/{filename}", handle_fleet_artifact)
