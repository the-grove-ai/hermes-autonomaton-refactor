"""Portal action routes — write endpoints for operator sovereignty actions.

Sprint P4 (portal-action-surface-v1). Every action here calls the SAME apply
logic the CLI and conversation surfaces use — there is no parallel governance
pipeline and no 202-async pattern (these are synchronous file writes):

* Routing proposals → ``grove.flywheel_cli`` (``_handler_for`` →
  ``apply_callback`` → ``remove`` → ``_record_kaizen_disposition``) — the exact
  ``cli_approve`` sequence.
* Memory proposals  → ``grove.memory.digest`` (``MemoryProposalHandler.apply``,
  ``_disposition_envelope``, ``_rewrite``) — the exact ``run_digest`` per-record
  sequence.
* Dock status       → ``grove.dock.writer.update_dock_goal_status`` (ruamel
  round-trip, comment-preserving).

The portal's auth middleware already gates ``/portal/*`` (loopback + Tailscale).

NO SILENT DEGRADATION. An unknown proposal id is a 404; an invalid dock status
is a 400; an apply/write failure surfaces with context — it is not swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiohttp import web

from grove.api.fragments import (
    _esc,
    _html_fragment,
    _live_tier_preferences,
    _proposal_actions_html,
    _short_id,
    _swappable_tiers,
    render_alert_banner,
    render_forge_publish_card,
    render_goal_card,
    render_tier_card,
)
from grove.api.portal import _memory_proposals_path, _read_forge_slug
from grove.forge import PublishError, feedback_store, publish_application_package
from grove.config.model_catalog import load_catalog
from grove.config.routing_writer import ConfigValidationError, get_writer
from grove.dock import _VALID_STATUSES, load_dock
from grove.dock.writer import update_dock_goal_status
from grove.eval import proposal_queue
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
    PROPOSAL_TYPE_MEMORY_CONTEXT,
    compute_proposal_id,
)
from grove.flywheel_cli import (
    _handler_for,
    _machine_config_path,
    _record_kaizen_disposition,
)
from grove.memory import digest
from grove.memory.digest import MemoryProposalHandler
from grove.notify import broadcast_to_operator
from hermes_constants import get_hermes_home
from tools import mcp_tool

logger = logging.getLogger(__name__)

# forge-jobsearch-v1 — the raw MCP tool name the gateway notion OAuth session
# advertises for a page-property write (sanitized registry name:
# mcp_notion_notion_update_page).
_NOTION_UPDATE_TOOL = "notion-update-page"

# Operator-facing disposition label (what the resolved card shows) per action.
_DISPOSITION_LABEL = {
    "approve": "approved",
    "reject": "rejected",
    "dismiss": "dismissed",
}

# Badge colour per shown disposition: green = applied, red = rejected, neutral
# (default badge) = dismissed.
_DISPOSITION_BADGE = {
    "approved": "badge badge-green",
    "rejected": "badge badge-red",
    "dismissed": "badge",
}


# ---------------------------------------------------------------------------
# Card fragments
# ---------------------------------------------------------------------------


def _resolved_card(short_id: str, type_label: str, disposition: str, summary: str) -> web.Response:
    """The post-action replacement card — no buttons, greyed out, kept visible
    so the operator sees what they just did."""
    badge_cls = _DISPOSITION_BADGE.get(disposition, "badge")
    return _html_fragment(
        f'<div class="card card-resolved" id="proposal-{short_id}">'
        f'<h4><span class="badge">{_esc(type_label)}</span> '
        f'<span class="{badge_cls}">{_esc(disposition)}</span></h4>'
        f'<p>{_esc(summary)}</p>'
        f'</div>'
    )


def _not_found_card_html(proposal_id: str) -> str:
    return (
        f'<div class="card card-resolved">'
        f'<p>Proposal <code>{_esc(proposal_id)}</code> not found — it may have '
        f'already been resolved.</p></div>'
    )


def _archive_forge_slug(proposal) -> Optional[str]:
    """Move a rejected forge draft OUT of pending_review into
    ``~/.grove/forge/.archive/<slug>-<ts>/`` (fleet-pipeline-v1 P3 / Gemini D+B1).

    One atomic ``rename`` within ``~/.grove`` (a single mount) both retains the
    trainable package AND clears the skip-already-staged marker (the one-level
    ``pending_review/*/meta.json`` glob no longer sees it), so the row becomes
    re-draftable. Returns the archive path, or None when the dir is already gone
    (published/removed). The CALLER archives BEFORE finalize so a crash between
    leaves the proposal live."""
    slug = (proposal.payload or {}).get("slug")
    if not slug:
        return None
    home = Path(get_hermes_home())
    src = home / "forge" / "pending_review" / slug
    if not src.is_dir():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = home / "forge" / ".archive" / f"{slug}-{ts}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)  # atomic within the one ~/.grove mount
    return str(dest)


# ---------------------------------------------------------------------------
# Loud disposition (portal-action-error-surfacing-v1 P3, Option A)
# ---------------------------------------------------------------------------


async def _loud_action_failure(
    inline_card_html: str,
    *,
    failure_class: str,
    action: str,
    message: str,
    status: int,
    detail: str | None = None,
    file_kaizen: bool = True,
) -> web.Response:
    """The shared loud-disposition path for a portal action failure (Option A).

    Runs BOTH side-effects BEFORE assembling the response so neither can prevent
    the return, then returns the handler's EXISTING failure status — 4xx/5xx
    UNCHANGED. Option A preserves HTTP honesty: a failed action still speaks a
    failure code to any monitor/curl; the operator-facing surface is the banner,
    not a status flip.

    Body = the inline card the handler already built PLUS the OOB ``#alert-banner``
    (render_alert_banner). On the 4xx/5xx htmx does not swap the body, so the
    original action card survives in the DOM for retry while the base template's
    ``responseError`` listener lifts the banner (P2) — the failure is unmissable.

    Fail-safe (the P1 reporter discipline applied to the helper): both
    side-effects run and are swallowed on error so the card + banner ALWAYS
    return. ``broadcast_to_operator`` is P1-internally-safe (never raises);
    ``file_agentless_proposal`` does file I/O and is wrapped here.

    ``file_kaizen`` gates only the Kaizen filing (the broadcast + banner always
    fire): a pure client-input failure with no conceivable structural fix can set
    it False so the review queue is not fed noise."""
    logger.error(
        "[portal.actions] %s failed (%s): %s", action, failure_class, message
    )

    # Side-effect 1 — reach the operator on every connected surface (P1 fail-safe).
    await broadcast_to_operator(f"Portal action '{action}' failed: {message}")

    # Side-effect 2 — file a Kaizen proposal so a RECURRING failure earns a
    # structural fix. Wrapped: a queue write must never block the card + banner.
    if file_kaizen:
        try:
            proposal_queue.file_agentless_proposal(
                failure_class=failure_class,
                action=action,
                evidence=failure_class,          # stable → dedup on (class, action)
                justification=message,           # excluded from id; ephemeral-safe
                instance={"detail": detail} if detail else None,
            )
        except Exception as exc:  # noqa: BLE001 — reporter path: log, never raise
            logger.error(
                "[portal.actions] kaizen filing failed for %s/%s: %r",
                action, failure_class, exc,
            )

    banner = render_alert_banner(message, status=status, detail=detail)
    return _html_fragment(inline_card_html + banner, status=status)


# ---------------------------------------------------------------------------
# Routing proposals — the cli_approve sequence, verbatim
# ---------------------------------------------------------------------------


async def _apply_routing(proposal, action: str, full_id: str, short_id: str, reason):
    """Apply a routing proposal action. Mirrors grove.flywheel_cli.cli_approve /
    cli_reject: approve runs the registry apply_callback + remove + disposition;
    reject/dismiss remove + record (routing has no soft-dismiss — dismiss is a
    rejection disposition, SPEC 1c)."""
    type_label = proposal.type
    summary = proposal.to_dict().get("semantic_justification") or ""

    if action == "approve":
        try:
            handler = _handler_for(proposal.type)
        except ValueError:
            msg = f"Cannot approve proposal type {proposal.type} from the portal."
            return await _loud_action_failure(
                f'<div class="card" id="proposal-{short_id}">'
                f'<h4><span class="badge">{_esc(type_label)}</span> '
                f'<span class="badge badge-yellow">refused</span></h4>'
                f'<p>{_esc(summary)}</p>'
                f'<div class="meta error">{_esc(msg)}</div>'
                f'{_proposal_actions_html(full_id, short_id)}'
                f'</div>',
                failure_class="proposal_type_not_approvable",
                action="proposal_approve",
                message=msg,
                status=422,
            )
        # B2 no-cluster-no-proposal gate — always on, scoped to rows that
        # declare requires_source_patterns (today: routing_adjustment).
        if handler.requires_source_patterns and not proposal.source_patterns:
            msg = "Cannot approve: no source_patterns (B2 no-cluster-no-proposal gate)."
            return await _loud_action_failure(
                f'<div class="card" id="proposal-{short_id}">'
                f'<h4><span class="badge">{_esc(type_label)}</span> '
                f'<span class="badge badge-yellow">refused</span></h4>'
                f'<p>{_esc(summary)}</p>'
                f'<div class="meta error">{_esc(msg)}</div>'
                f'{_proposal_actions_html(full_id, short_id)}'
                f'</div>',
                failure_class="proposal_missing_source_patterns",
                action="proposal_approve",
                message=msg,
                status=409,
            )
        target, applied = handler.apply_callback(
            proposal, machine_path=_machine_config_path()
        )
        proposal_queue.remove(proposal.proposal_id)
        _record_kaizen_disposition(
            proposal, disposition="applied", applied_result=applied
        )
        logger.info(
            "[portal.actions] routing proposal %s applied (%s%s)",
            proposal.proposal_id, handler.apply_label_prefix, target,
        )
        return _resolved_card(short_id, type_label, "approved", summary)

    # fleet-pipeline-v1 P3 — forge-type-aware reject: archive-then-clear. Move the
    # staged package OUT of pending_review (atomically clearing the skip-already-
    # staged marker AND retaining the trainable corpus), THEN finalize. Order is
    # load-bearing: archive BEFORE finalize, so a crash between them leaves the
    # proposal LIVE (re-rejectable), never a cleared-proposal-with-unarchived-dir.
    if proposal.type == PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING:
        archive_path = _archive_forge_slug(proposal)
        proposal_queue.finalize_proposal_state(
            proposal.proposal_id, "rejected",
            {"archive_path": archive_path}, reason=reason,
        )
        return _resolved_card(
            short_id, type_label, _DISPOSITION_LABEL[action], summary
        )

    # reject + dismiss both dequeue; dismiss records a rejection disposition
    # (routing has no distinct dismiss concept — SPEC 1c).
    proposal_queue.remove(proposal.proposal_id)
    _record_kaizen_disposition(
        proposal, disposition="rejected", reason=reason,
    )
    return _resolved_card(
        short_id, type_label, _DISPOSITION_LABEL[action], summary
    )


# ---------------------------------------------------------------------------
# Memory proposals — the run_digest per-record sequence, verbatim
# ---------------------------------------------------------------------------


def _apply_memory(proposal_id: str, action: str, store, reason):
    """Find the pending memory record whose computed proposal_id matches, apply
    the operator's action, and rewrite the file. Returns the resolved card, or
    None when no record matched (caller emits 404)."""
    path = _memory_proposals_path()
    records = digest._read_records(path)
    for rec in records:
        if rec.get("status") != "pending" or "proposal" not in rec:
            continue
        proposal = rec["proposal"]
        session_id = rec.get("session_id", "")
        evidence = (session_id,) if session_id else ()
        pid = compute_proposal_id(
            type=PROPOSAL_TYPE_MEMORY_CONTEXT, payload=proposal, evidence=evidence
        )
        if pid != proposal_id:
            continue

        short_id = _short_id(proposal_id)
        summary = MemoryProposalHandler.summary_renderer(proposal)

        if action == "approve":
            applied = MemoryProposalHandler(store).apply(proposal)
            rec["status"] = "approved"
            digest._rewrite(path, records)
            _record_kaizen_disposition(
                digest._disposition_envelope(proposal, session_id),
                disposition="applied",
                applied_result={"applied": bool(applied)},
            )
        elif action == "reject":
            rec["status"] = "rejected"
            digest._rewrite(path, records)
            _record_kaizen_disposition(
                digest._disposition_envelope(proposal, session_id),
                disposition="rejected",
                reason=reason
                or proposal.get("proposed_record", {}).get("justification"),
            )
        else:  # dismiss — SOFT. Status flips to "dismissed"; NO disposition
            # recorded (crystallization-cadence-v1 Gap 3 — dismiss is not a
            # rejection, so the detector's rejection memory is untouched).
            rec["status"] = "dismissed"
            digest._rewrite(path, records)

        return _resolved_card(
            short_id, "memory_context", _DISPOSITION_LABEL[action], summary
        )

    return None


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


async def _action_reason(request: web.Request):
    """Optional rejection reason — query param ``reason`` wins, else form body."""
    if request.query.get("reason"):
        return request.query["reason"]
    if request.content_type == "application/x-www-form-urlencoded":
        data = await request.post()
        if data.get("reason"):
            return str(data["reason"])
    return None


async def _dispatch_proposal_action(request: web.Request, action: str) -> web.Response:
    proposal_id = request.match_info["proposal_id"]
    short_id = _short_id(proposal_id)
    reason = await _action_reason(request)

    # Routing proposals first — the content-addressable id is unique across both
    # backing files, so a routing hit is unambiguous.
    routing = proposal_queue.read(proposal_id)
    if routing is not None:
        return await _apply_routing(routing, action, proposal_id, short_id, reason)

    # Then memory crystallizations.
    store = request.app["memory_store"]
    resolved = _apply_memory(proposal_id, action, store, reason)
    if resolved is not None:
        return resolved

    return await _loud_action_failure(
        _not_found_card_html(proposal_id),
        failure_class="proposal_not_found",
        action=f"proposal_{action}",
        message=(
            f"Proposal {proposal_id} not found — it may have already been resolved."
        ),
        status=404,
    )


async def handle_proposal_approve(request: web.Request) -> web.Response:
    return await _dispatch_proposal_action(request, "approve")


async def handle_proposal_reject(request: web.Request) -> web.Response:
    return await _dispatch_proposal_action(request, "reject")


async def handle_proposal_dismiss(request: web.Request) -> web.Response:
    return await _dispatch_proposal_action(request, "dismiss")


async def handle_dock_goal_update(request: web.Request) -> web.Response:
    """PATCH a Dock goal's status. Validates against the loader's closed status
    set (_VALID_STATUSES) and persists via the comment-preserving dock writer."""
    goal_id = request.match_info["goal_id"]

    if request.content_type == "application/json":
        body = await request.json()
        status = body.get("status")
    else:
        data = await request.post()
        status = data.get("status")

    if status not in _VALID_STATUSES:
        # file_kaizen=False — the dock status control is closed to _VALID_STATUSES,
        # so a 400 here is a hand-crafted request the UI cannot produce: no
        # structural fix exists, filing would only pollute the queue. Broadcast +
        # banner + log + 400 stay unconditional.
        return await _loud_action_failure(
            f'<div class="card"><p class="error">Invalid status: '
            f'{_esc(str(status))}. Expected one of '
            f'{_esc(", ".join(sorted(_VALID_STATUSES)))}.</p></div>',
            failure_class="dock_invalid_status",
            action="dock_update",
            message=f"Invalid dock status: {status!r}.",
            status=400,
            file_kaizen=False,
        )

    try:
        updated = update_dock_goal_status(goal_id, status)
    except FileNotFoundError:
        return await _loud_action_failure(
            '<div class="card"><p class="error">Dock not installed — no '
            'dock.yaml to update.</p></div>',
            failure_class="dock_not_installed",
            action="dock_update",
            message="Dock not installed — no dock.yaml to update.",
            status=404,
        )

    if not updated:
        return await _loud_action_failure(
            f'<div class="card"><p class="error">Goal '
            f'<code>{_esc(goal_id)}</code> not found.</p></div>',
            failure_class="dock_goal_not_found",
            action="dock_update",
            message=f"Dock goal {goal_id!r} not found.",
            status=404,
        )

    # Re-load and render the fresh card so the swapped-in markup matches the
    # listing exactly (and reflects the value the loader actually accepted).
    dock = load_dock()
    goal = next((g for g in dock.goals if g.id == goal_id), None) if dock else None
    if goal is None:
        # The write succeeded but the goal vanished on reload — fail loud. Status
        # STAYS 500 (Option A: a real server error keeps its honest code); the
        # banner lifts on 5xx exactly as on 4xx.
        return await _loud_action_failure(
            f'<div class="card"><p class="error">Goal '
            f'<code>{_esc(goal_id)}</code> updated but could not be reloaded.'
            f'</p></div>',
            failure_class="dock_reload_vanished",
            action="dock_update",
            message=f"Dock goal {goal_id!r} updated but vanished on reload.",
            status=500,
        )
    return _html_fragment(render_goal_card(goal))


# ---------------------------------------------------------------------------
# Routing tier model-swap (portal-model-swap-v1)
# ---------------------------------------------------------------------------


def _unknown_tier_card_html(tier: str) -> str:
    return (
        f'<div class="card"><p class="error">Unknown tier {_esc(tier)} — the '
        f'operator manages {_esc(", ".join(_swappable_tiers()))} from the portal.'
        f'</p></div>'
    )


async def handle_tier_model_swap(request: web.Request) -> web.Response:
    """Swap the model bound to a tier. Form body: ``tier`` (T1/T2/T3, R3) and
    ``model_slug`` (must be in the catalog). Calls the sole routing writer, then
    returns the re-rendered tier card reflecting the POST-write state (N2). An
    off-catalog slug or a ``ConfigValidationError`` re-renders the SAME card with
    the error inline — the card stays, no 500 (C3)."""
    data = await request.post()
    tier = str(data.get("tier") or "")
    model_slug = str(data.get("model_slug") or "")

    if tier not in _swappable_tiers():
        # file_kaizen=False — the UI only offers swappable tiers, so an unknown
        # tier is a hand-crafted request with no structural fix. Loud everywhere
        # (broadcast + banner + log + 400), just not queued.
        return await _loud_action_failure(
            _unknown_tier_card_html(tier),
            failure_class="tier_unknown",
            action="tier_swap",
            message=f"Unknown tier {tier!r}.",
            status=400,
            file_kaizen=False,
        )

    catalog = load_catalog()
    if model_slug not in {m["slug"] for m in catalog}:
        return await _loud_action_failure(
            render_tier_card(
                tier, _live_tier_preferences().get(tier), catalog,
                error=f"Model {model_slug!r} is not in the catalog.",
            ),
            failure_class="tier_model_off_catalog",
            action="tier_swap",
            message=f"Model {model_slug!r} is not in the catalog.",
            status=400,
        )

    try:
        await get_writer().swap_tier_model(tier, model_slug)
    except ConfigValidationError as exc:
        return await _loud_action_failure(
            render_tier_card(
                tier, _live_tier_preferences().get(tier), catalog, error=str(exc)
            ),
            failure_class="tier_config_invalid",
            action="tier_swap",
            message=str(exc),
            status=422,
        )

    logger.info("[portal.actions] tier %s swapped to %s", tier, model_slug)
    # N2 — render the live, post-write state (re-read after the writer committed).
    return _html_fragment(
        render_tier_card(tier, _live_tier_preferences().get(tier), catalog)
    )


async def handle_tier_model_revert(request: web.Request) -> web.Response:
    """Revert a tier to its ``previous_model`` — one-level undo (AC-6). Form body:
    ``tier``. Same write path and N2 re-read as swap; a ``ConfigValidationError``
    (e.g. no previous_model on record) re-renders the card with the error
    inline."""
    data = await request.post()
    tier = str(data.get("tier") or "")

    if tier not in _swappable_tiers():
        # file_kaizen=False — same as swap: an unknown tier is UI-impossible, so
        # no structural fix; loud everywhere but not queued.
        return await _loud_action_failure(
            _unknown_tier_card_html(tier),
            failure_class="tier_unknown",
            action="tier_revert",
            message=f"Unknown tier {tier!r}.",
            status=400,
            file_kaizen=False,
        )

    catalog = load_catalog()
    try:
        await get_writer().revert_tier_model(tier)
    except ConfigValidationError as exc:
        return await _loud_action_failure(
            render_tier_card(
                tier, _live_tier_preferences().get(tier), catalog, error=str(exc)
            ),
            failure_class="tier_config_invalid",
            action="tier_revert",
            message=str(exc),
            status=422,
        )

    logger.info("[portal.actions] tier %s reverted", tier)
    return _html_fragment(
        render_tier_card(tier, _live_tier_preferences().get(tier), catalog)
    )


# fleet-pipeline-v1 P3 — bounded publish. A hang beyond this becomes an in-process
# TimeoutError; the promote route then KEEPS the lease held (the run_in_executor
# thread survives wait_for cancel and would double-write if a re-tap started).
_FORGE_PUBLISH_TIMEOUT = 90.0


async def _forge_publish_core(slug: str, loop) -> dict:
    """Shared publish mechanics: meta.json -> Drive (contents-aware) -> Notion.
    DRIVE first (idempotent, contents-aware — never publishes a partial folder),
    NOTION last, and only AFTER Drive contents are verified complete.

    Returns a discriminated dict — ``{"ok": True, "folder_link", "row_id"}`` on
    success, or ``{"ok": False, "kind", "status", "message", "folder_link"?,
    "detail"?}`` on an EXPECTED failure. An UNEXPECTED exception propagates. The
    portal is a CONSUMER of the MCP substrate: a cold notion session fails LOUD,
    never connecting or waking the server. Shared by /publish (renders forge
    cards) and /promote (lease + finalize)."""
    read = _read_forge_slug(slug)
    if read is None:
        return {"ok": False, "kind": "forge_no_draft_dir", "status": 404,
                "message": f"No forge draft dir for {slug!r}."}
    meta = read["meta"]
    if not meta or not all(meta.get(k) for k in ("row_id", "company", "role")):
        why = read["meta_error"] or "meta.json is missing row_id/company/role"
        return {"ok": False, "kind": "forge_meta_invalid", "status": 400,
                "message": f"Cannot publish: {why}."}
    row_id, company, role = meta["row_id"], meta["company"], meta["role"]
    slug_dir = Path(get_hermes_home()) / "forge" / "pending_review" / slug
    resume_path = str(slug_dir / "resume.md")
    cover_path = str(slug_dir / "cover-letter.md")

    try:
        result = await loop.run_in_executor(
            None,
            lambda: publish_application_package(
                row_id, company, role, resume_path, cover_path
            ),
        )
    except PublishError as exc:
        return {"ok": False, "kind": "forge_drive_publish_error", "status": 422,
                "message": "Drive publish failed — no Notion write attempted.",
                "detail": json.dumps(exc.partial_state)}
    folder_link = result.get("folder_link")

    with mcp_tool._lock:
        server = mcp_tool._servers.get("notion")
    if server is None or not getattr(server, "session", None):
        return {"ok": False, "kind": "forge_notion_cold", "status": 400,
                "message": ("Drive package created. Notion MCP is cold — ping the "
                            "agent in chat to wake it, then tap Publish again."),
                "folder_link": folder_link}
    notion_call = mcp_tool._make_tool_handler(
        "notion", _NOTION_UPDATE_TOOL, mcp_tool._DEFAULT_TOOL_TIMEOUT
    )
    args = {
        "page_id": row_id,
        "command": "update_properties",
        "properties": {"Application Package": folder_link, "Status": "Drafted"},
    }
    raw = await loop.run_in_executor(None, lambda: notion_call(args))
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = {"error": f"unparseable notion response: {raw!r}"}
    if "error" in parsed:
        return {"ok": False, "kind": "forge_notion_update_error", "status": 400,
                "message": (f"Drive package created. Notion update failed: "
                            f"{parsed['error']}. Tap Publish again to retry."),
                "folder_link": folder_link}
    logger.info(
        "[portal.actions] forge %s published: folder=%s row=%s Status->Drafted",
        slug, folder_link, row_id,
    )
    return {"ok": True, "folder_link": folder_link, "row_id": row_id}


async def handle_forge_publish(request: web.Request) -> web.Response:
    """``POST /portal/actions/forge/{slug}/publish`` — the operator's Publish tap
    (forge-jobsearch-v1). Thin over :func:`_forge_publish_core`; renders the SAME
    forge card + inline error (4xx, never 500) on failure, success card on ok."""
    slug = request.match_info["slug"]
    res = await _forge_publish_core(slug, asyncio.get_running_loop())
    if res.get("ok"):
        return _html_fragment(
            render_forge_publish_card(slug, published=True, folder_link=res["folder_link"]),
            status=200,
        )
    if res["kind"] == "forge_drive_publish_error":
        card_error = ("Drive publish failed — no Notion write attempted. "
                      f"Partial state: {res['detail']}")
    else:
        card_error = res["message"]
    return await _loud_action_failure(
        render_forge_publish_card(slug, folder_link=res.get("folder_link"), error=card_error),
        failure_class=res["kind"], action="forge_publish",
        message=res["message"], status=res["status"], detail=res.get("detail"),
    )


def _forge_promote_error_card(proposal_id: str, short_id: str, ptype: str,
                              message: str, *, retappable: bool) -> str:
    """A promote-failure card. Re-renders the verb buttons ONLY when the draft is
    re-tappable (completed-failure cleared the lease); a held lease (timeout /
    in-flight) shows no buttons so the operator does not re-tap into the race."""
    actions = ""
    if retappable:
        from grove.api.fragments import _verb_actions_html
        from grove.eval.proposal_queue import PROPOSAL_VERBS
        actions = _verb_actions_html(proposal_id, short_id, PROPOSAL_VERBS.get(ptype, ()))
    return (
        f'<div class="card" id="proposal-{short_id}">'
        f'<h4><span class="badge">{_esc(ptype)}</span> '
        f'<span class="badge badge-yellow">error</span></h4>'
        f'<div class="meta error">{_esc(message)}</div>{actions}</div>'
    )


async def handle_forge_promote(request: web.Request) -> web.Response:
    """``POST /portal/actions/proposals/{proposal_id}/promote`` — the bespoke async
    Promote tap for a forge_artifact_pending proposal (fleet-pipeline-v1 P3).

    Sequence (Gemini 1c, verbatim): set_lease -> bounded publish -> finalize. The
    proposal is NEVER removed until finalize; there is no re-enqueue.

    TWO failure dispositions (Gemini 1a'): a COMPLETED failure (the executor future
    RETURNED) clears the lease — the record stays, re-tappable. A TIMEOUT (wait_for
    cancels the await but the run_in_executor thread STAYS LIVE) KEEPS the lease
    held — clearing it would let a re-tap double-write against the concurrent-racy
    Drive guard; only the startup sweep or a manual clear releases it."""
    proposal_id = request.match_info["proposal_id"]
    short_id = _short_id(proposal_id)

    proposal = proposal_queue.read(proposal_id)
    if proposal is None:
        return await _loud_action_failure(
            _not_found_card_html(proposal_id), failure_class="proposal_not_found",
            action="forge_promote", message="Proposal already resolved.", status=404,
        )
    ptype = proposal.type
    slug = (proposal.payload or {}).get("slug")
    if not slug:
        return await _loud_action_failure(
            _forge_promote_error_card(proposal_id, short_id, ptype,
                                      "Proposal carries no slug — cannot publish.",
                                      retappable=False),
            failure_class="forge_promote_no_slug", action="forge_promote",
            message="Proposal carries no slug — cannot publish.", status=400,
        )

    lease = proposal_queue.set_lease(proposal_id, holder="portal_promote")
    if lease == proposal_queue.LEASE_NOT_FOUND:
        return await _loud_action_failure(
            _not_found_card_html(proposal_id), failure_class="proposal_not_found",
            action="forge_promote", message="Proposal already resolved.", status=404,
        )
    if lease == proposal_queue.LEASE_ALREADY_HELD:
        return await _loud_action_failure(
            _forge_promote_error_card(proposal_id, short_id, ptype,
                                      "A publish is already in flight for this draft.",
                                      retappable=False),
            failure_class="forge_promote_in_flight", action="forge_promote",
            message="A publish is already in flight for this draft.", status=409,
        )

    loop = asyncio.get_running_loop()
    try:
        res = await asyncio.wait_for(
            _forge_publish_core(slug, loop), timeout=_FORGE_PUBLISH_TIMEOUT
        )
    except asyncio.TimeoutError:
        # TIMEOUT — executor thread STILL LIVE. KEEP the lease held.
        msg = ("Publish timed out — still processing. The lease is HELD; it will "
               "release on the next gateway restart (or a manual clear). Do NOT "
               "re-tap yet.")
        return await _loud_action_failure(
            _forge_promote_error_card(proposal_id, short_id, ptype, msg, retappable=False),
            failure_class="forge_promote_timeout", action="forge_promote",
            message=msg, status=504,
        )
    except Exception as exc:  # noqa: BLE001 — future RETURNED an error -> completed failure
        proposal_queue.clear_lease(proposal_id)
        msg = f"Publish failed unexpectedly: {exc}. Re-tap to retry."
        return await _loud_action_failure(
            _forge_promote_error_card(proposal_id, short_id, ptype, msg, retappable=True),
            failure_class="forge_promote_error", action="forge_promote",
            message=msg, status=500,
        )

    if not res.get("ok"):
        # COMPLETED failure (discriminated) — clear the lease, record untouched.
        proposal_queue.clear_lease(proposal_id)
        return await _loud_action_failure(
            _forge_promote_error_card(proposal_id, short_id, ptype, res["message"],
                                      retappable=True),
            failure_class=res["kind"], action="forge_promote",
            message=res["message"], status=res["status"], detail=res.get("detail"),
        )

    # SUCCESS — finalize (the single disposition path): remove + kaizen ledger.
    proposal_queue.finalize_proposal_state(
        proposal_id, "applied", {"folder_link": res["folder_link"]}
    )
    return _resolved_card(
        short_id, ptype, "promoted", f"Published — {res['folder_link']}"
    )


# ---------------------------------------------------------------------------
# suggest-revision-verb-v1 — the informed-path loop-back tap
# ---------------------------------------------------------------------------

# N-breaker: after this many operator revisions on one row, the row is marked
# won't-converge (terminal_skip) and excluded from re-selection — the placebo-
# livelock fix. A small module constant this sprint (no ~/.grove config touch).
_REVISION_MAX = 3


async def _suggest_revision_text(request: web.Request):
    """Parse ``revision_text`` from the request body (handle_dock_goal_update
    precedent: form-urlencoded or JSON). Returns the RAW text when it has
    non-whitespace content, else None — the caller Andons on None (never a silent
    400). RAW is preserved (the store keeps what the operator typed); only the
    presence check strips."""
    if request.content_type == "application/x-www-form-urlencoded":
        data = await request.post()
        raw = data.get("revision_text")
        if raw is not None and str(raw).strip():
            return str(raw)
    if request.content_type == "application/json":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed JSON is absent input -> Andon
            return None
        raw = body.get("revision_text") if isinstance(body, dict) else None
        if raw is not None and str(raw).strip():
            return str(raw)
    return None


def _forge_suggest_error_card(short_id: str, message: str) -> str:
    """A suggest-revision failure card (lands in ``#kaizen-result``). The textarea
    survives in the DOM (hx-target is ``#kaizen-result``, not the disposition div),
    so the operator can correct and re-submit — no re-render of the affordance."""
    return (
        f'<div class="card card-error" id="proposal-{short_id}">'
        f'<div class="meta error">{_esc(message)}</div></div>'
    )


def _write_archive_pending_marker(slug: str) -> None:
    """Write the ``.archive-pending`` intent marker into ``pending_review/<slug>/``.

    Written AFTER finalize, BEFORE ``_archive_forge_slug``: a crash landing between
    the marker and the archive leaves the marked dir for the P4 orphan-staged sweep
    to complete — the finalize-before-archive crash residual self-heals. A dotfile,
    so it never enters the ``_staged_row_ids`` ``*/meta.json`` glob."""
    slug_dir = Path(get_hermes_home()) / "forge" / "pending_review" / slug
    if slug_dir.is_dir():
        (slug_dir / ".archive-pending").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )


async def handle_forge_suggest_revision(request: web.Request) -> web.Response:
    """``POST /portal/actions/proposals/{proposal_id}/suggest_revision`` — the
    informed-path loop-back tap (suggest-revision-verb-v1). Accumulate the operator's
    free-text guidance into the Path-B feedback store, record a suggest_revision
    disposition, then archive the stale draft so the row re-drafts WITH the guidance
    next cadence.

    ORDERING is finalize-success-gated: store.write -> finalize -> marker -> archive.
    Archive runs ONLY if finalize succeeded; a finalize failure clears the lease and
    Andons WITHOUT archiving (the uncleared skip-marker keeps the row out of
    re-selection — never a feedback-blind re-draft). Fast local ops -> no
    timeout-split; a mid-request cancel clears the lease in the finally."""
    proposal_id = request.match_info["proposal_id"]
    short_id = _short_id(proposal_id)

    # (1) revision_text — FAIL LOUD on empty/whitespace/absent (never a silent 400).
    revision_text = await _suggest_revision_text(request)
    if not revision_text:
        msg = "Revision guidance is empty — enter what the next draft must change."
        return await _loud_action_failure(
            _forge_suggest_error_card(short_id, msg),
            failure_class="suggest_revision_empty", action="forge_suggest_revision",
            message=msg, status=400, file_kaizen=False,  # pure client input, no structural fix
        )

    # (2) resolve proposal + the pid->row_id JOIN — FAIL LOUD on missing.
    proposal = proposal_queue.read(proposal_id)
    if proposal is None:
        return await _loud_action_failure(
            _not_found_card_html(proposal_id), failure_class="proposal_not_found",
            action="forge_suggest_revision", message="Proposal already resolved.", status=404,
        )
    ptype = proposal.type
    slug = (proposal.payload or {}).get("slug")
    row_id = (proposal.payload or {}).get("row_id")
    if not row_id:
        msg = "Proposal carries no row_id — cannot store revision guidance."
        return await _loud_action_failure(
            _forge_suggest_error_card(short_id, msg),
            failure_class="suggest_revision_no_row_id", action="forge_suggest_revision",
            message=msg, status=400,
        )

    # (3) set_lease CAS — the double-tap guard.
    lease = proposal_queue.set_lease(proposal_id, holder="portal_suggest_revision")
    if lease == proposal_queue.LEASE_NOT_FOUND:
        return await _loud_action_failure(
            _not_found_card_html(proposal_id), failure_class="proposal_not_found",
            action="forge_suggest_revision", message="Proposal already resolved.", status=404,
        )
    if lease == proposal_queue.LEASE_ALREADY_HELD:
        msg = "A disposition is already in flight for this draft."
        return await _loud_action_failure(
            _forge_suggest_error_card(short_id, msg),
            failure_class="suggest_revision_in_flight", action="forge_suggest_revision",
            message=msg, status=409,
        )

    # (4) ORDERING — finalize-success-gated. Fast local ops.
    lease_released = False
    wont_converge = False
    try:
        # a. store durable FIRST (accumulate). Harmless pre-write: if finalize fails
        #    below, the skip-marker is NEVER cleared, so the row is not re-selected.
        entry = feedback_store.write(row_id, revision_text)
        # N-BREAKER — the tap crossing N terminally excludes the row (won't converge)
        # and fires a LOUD out-of-band Andon. The disposition still proceeds (the
        # current draft is recorded + archived); the row simply will not re-draft.
        wont_converge = int(entry.get("count", 0)) >= _REVISION_MAX
        if wont_converge:
            feedback_store.set_terminal_skip(row_id)
            wc_msg = (
                f"Revision limit reached ({entry.get('count')}) for row {row_id} "
                f"(slug {slug}) — marked WON'T-CONVERGE; it will no longer re-draft. "
                f"Manual attention needed."
            )
            logger.error("[portal.actions] suggest_revision won't-converge: %s", wc_msg)
            await broadcast_to_operator(wc_msg)
            try:
                proposal_queue.file_agentless_proposal(
                    failure_class="revision_wont_converge",
                    action="forge_suggest_revision", evidence=row_id, justification=wc_msg,
                )
            except Exception:  # noqa: BLE001 — reporter path, never blocks the disposition
                logger.error(
                    "[portal.actions] won't-converge kaizen filing failed", exc_info=True
                )

        # b. finalize (ledger audit + pop). If THIS fails -> NO archive.
        try:
            finalized = proposal_queue.finalize_proposal_state(
                proposal_id, "suggest_revision",
                {"row_id": row_id, "revision_note": revision_text},
            )
        except Exception as exc:  # noqa: BLE001 — finalize failure: no archive, clear lease, Andon
            proposal_queue.clear_lease(proposal_id)
            lease_released = True
            msg = (f"Recording the revision failed: {exc}. The draft was NOT archived; "
                   f"re-tap to retry.")
            return await _loud_action_failure(
                _forge_suggest_error_card(short_id, msg),
                failure_class="suggest_revision_finalize_error",
                action="forge_suggest_revision", message=msg, status=500,
            )
        if not finalized:
            # proposal vanished between read and finalize — no archive.
            proposal_queue.clear_lease(proposal_id)
            lease_released = True
            return await _loud_action_failure(
                _not_found_card_html(proposal_id), failure_class="proposal_not_found",
                action="forge_suggest_revision", message="Proposal already resolved.",
                status=404,
            )

        # c. archive-pending marker (into the slug dir) BEFORE the archive.
        _write_archive_pending_marker(slug)
        # d. archive LAST — clears the skip-marker so the row re-drafts WITH guidance.
        #    Best-effort post-finalize: the disposition is already durable (store +
        #    ledger + pop). A physical-rename glitch leaves the marker for the P4
        #    orphan sweep to complete — logged LOUD, never silent.
        try:
            _archive_forge_slug(proposal)
        except Exception:  # noqa: BLE001 — marker retained -> orphan sweep reconciles
            logger.error(
                "[portal.actions] suggest_revision archive failed post-finalize for "
                "slug=%s (row_id=%s); .archive-pending marker retained for the orphan "
                "sweep", slug, row_id, exc_info=True,
            )
    except asyncio.CancelledError:
        # operator closed the tab mid-request — release the lease, then re-raise.
        proposal_queue.clear_lease(proposal_id)
        lease_released = True
        raise
    finally:
        # belt-and-suspenders: a lease still held on any unexpected exit is cleared.
        # On SUCCESS finalize already popped the proposal, so this no-ops.
        if not lease_released:
            proposal_queue.clear_lease(proposal_id)

    # SUCCESS — resolved card. revision_note is passed RAW; _resolved_card HTML-
    # escapes the summary at render (store keeps raw). The won't-converge tap
    # succeeded (recorded + archived + terminally excluded) — a distinct card, not a
    # failure; the loud Andon fired out-of-band above.
    if wont_converge:
        return _resolved_card(
            short_id, ptype, "won't converge",
            f"Revision limit reached ({_REVISION_MAX}) — recorded and archived, but this "
            f"row is marked WON'T-CONVERGE and will no longer re-draft. Needs manual "
            f"attention.",
        )
    return _resolved_card(
        short_id, ptype, "revision requested",
        f"Revision guidance recorded — the row will re-draft with your notes: "
        f"{revision_text}",
    )


def _sweep_orphan_staged() -> list:
    """Archive ``pending_review/*/`` dirs carrying the ``.archive-pending`` marker —
    the finalize-before-archive crash residual (suggest-revision-verb-v1 P2/P4). A
    healthy or actively-staging draft lacks the marker and is NOT swept (the
    false-positive guard: bare 'staged dir + no proposal' is banned). Reuses
    ``_archive_forge_slug`` via a slug-carrying stand-in; idempotent (an
    already-archived / missing dir yields None). Startup-only (pre-ticker slot).
    Returns the swept slugs. Co-located with ``_archive_forge_slug`` (its
    dependency) so it is unit-testable without importing the gateway."""
    from types import SimpleNamespace

    pending = Path(get_hermes_home()) / "forge" / "pending_review"
    if not pending.is_dir():
        return []
    swept = []
    for slug_dir in sorted(pending.iterdir()):
        if not slug_dir.is_dir() or not (slug_dir / ".archive-pending").is_file():
            continue  # false-positive guard — only marked (crash-residual) dirs
        try:
            _archive_forge_slug(SimpleNamespace(payload={"slug": slug_dir.name}))
            swept.append(slug_dir.name)
        except Exception:  # noqa: BLE001 — one bad dir must not block startup
            logger.error(
                "[startup] orphan-staged sweep failed for %s", slug_dir.name, exc_info=True
            )
    return swept


def register_action_routes(app: web.Application) -> None:
    """Register the portal's write endpoints. Wired at gateway connect() time,
    after the read-only portal/fragment/dashboard routes. portal_auth_middleware
    already gates every /portal/* path."""
    app.router.add_post(
        "/portal/actions/proposals/{proposal_id}/approve", handle_proposal_approve
    )
    app.router.add_post(
        "/portal/actions/proposals/{proposal_id}/reject", handle_proposal_reject
    )
    app.router.add_post(
        "/portal/actions/proposals/{proposal_id}/dismiss", handle_proposal_dismiss
    )
    app.router.add_patch(
        "/portal/actions/dock/goals/{goal_id}", handle_dock_goal_update
    )
    # portal-model-swap-v1 — tier model swap + revert
    app.router.add_post("/portal/actions/routing/swap", handle_tier_model_swap)
    app.router.add_post("/portal/actions/routing/revert", handle_tier_model_revert)
    # forge-jobsearch-v1 — operator Publish tap (Drive-first, Notion-last)
    app.router.add_post("/portal/actions/forge/{slug}/publish", handle_forge_publish)
    # fleet-pipeline-v1 P3 — bespoke async Promote tap (set_lease -> bounded
    # publish -> finalize) for a forge_artifact_pending proposal.
    app.router.add_post(
        "/portal/actions/proposals/{proposal_id}/promote", handle_forge_promote
    )
    # suggest-revision-verb-v1 P2 — bespoke informed-path loop-back tap
    # (store.write -> finalize -> marker -> archive; finalize-success-gated).
    app.router.add_post(
        "/portal/actions/proposals/{proposal_id}/suggest_revision",
        handle_forge_suggest_revision,
    )
