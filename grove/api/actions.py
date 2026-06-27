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

import logging

from aiohttp import web

from grove.api.fragments import (
    _esc,
    _html_fragment,
    _proposal_actions_html,
    _short_id,
    render_goal_card,
)
from grove.api.portal import _memory_proposals_path
from grove.dock import _VALID_STATUSES, load_dock
from grove.dock.writer import update_dock_goal_status
from grove.eval import proposal_queue
from grove.eval.proposal_queue import (
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

logger = logging.getLogger(__name__)

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


def _not_found_card(proposal_id: str) -> web.Response:
    return _html_fragment(
        f'<div class="card card-resolved">'
        f'<p>Proposal <code>{_esc(proposal_id)}</code> not found — it may have '
        f'already been resolved.</p></div>',
        status=404,
    )


# ---------------------------------------------------------------------------
# Routing proposals — the cli_approve sequence, verbatim
# ---------------------------------------------------------------------------


def _apply_routing(proposal, action: str, full_id: str, short_id: str, reason):
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
            return _html_fragment(
                f'<div class="card" id="proposal-{short_id}">'
                f'<h4><span class="badge">{_esc(type_label)}</span> '
                f'<span class="badge badge-yellow">refused</span></h4>'
                f'<p>{_esc(summary)}</p>'
                f'<div class="meta error">Cannot approve proposal type '
                f'{_esc(proposal.type)} from the portal.</div>'
                f'{_proposal_actions_html(full_id, short_id)}'
                f'</div>',
                status=422,
            )
        # B2 no-cluster-no-proposal gate — always on, scoped to rows that
        # declare requires_source_patterns (today: routing_adjustment).
        if handler.requires_source_patterns and not proposal.source_patterns:
            return _html_fragment(
                f'<div class="card" id="proposal-{short_id}">'
                f'<h4><span class="badge">{_esc(type_label)}</span> '
                f'<span class="badge badge-yellow">refused</span></h4>'
                f'<p>{_esc(summary)}</p>'
                f'<div class="meta error">Cannot approve: no source_patterns '
                f'(B2 no-cluster-no-proposal gate).</div>'
                f'{_proposal_actions_html(full_id, short_id)}'
                f'</div>',
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
        return _apply_routing(routing, action, proposal_id, short_id, reason)

    # Then memory crystallizations.
    store = request.app["memory_store"]
    resolved = _apply_memory(proposal_id, action, store, reason)
    if resolved is not None:
        return resolved

    return _not_found_card(proposal_id)


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
        return _html_fragment(
            f'<div class="card"><p class="error">Invalid status: '
            f'{_esc(str(status))}. Expected one of '
            f'{_esc(", ".join(sorted(_VALID_STATUSES)))}.</p></div>',
            status=400,
        )

    try:
        updated = update_dock_goal_status(goal_id, status)
    except FileNotFoundError:
        return _html_fragment(
            '<div class="card"><p class="error">Dock not installed — no '
            'dock.yaml to update.</p></div>',
            status=404,
        )

    if not updated:
        return _html_fragment(
            f'<div class="card"><p class="error">Goal '
            f'<code>{_esc(goal_id)}</code> not found.</p></div>',
            status=404,
        )

    # Re-load and render the fresh card so the swapped-in markup matches the
    # listing exactly (and reflects the value the loader actually accepted).
    dock = load_dock()
    goal = next((g for g in dock.goals if g.id == goal_id), None) if dock else None
    if goal is None:
        # The write succeeded but the goal vanished on reload — fail loud.
        return _html_fragment(
            f'<div class="card"><p class="error">Goal '
            f'<code>{_esc(goal_id)}</code> updated but could not be reloaded.'
            f'</p></div>',
            status=500,
        )
    return _html_fragment(render_goal_card(goal))


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
