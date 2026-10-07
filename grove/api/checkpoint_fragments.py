"""Operator Portal — Checkpoints (Setup ▸ Advanced, demo mode only).

Save the node's records at one moment; put a saved moment back. The page is a
thin surface over ``grove.checkpoints``: it decides nothing, and it writes
nothing to any goal's records. A restore is asked for here and carried out
when the gateway restarts, which this page then triggers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web

from grove import checkpoints

logger = logging.getLogger(__name__)


def _demo() -> bool:
    try:
        from grove.api.actions import _demo_tokenless_approve
        return bool(_demo_tokenless_approve())
    except Exception:  # noqa: BLE001 — unknown means not in demo mode
        return False


def _restart_gateway() -> None:
    """End this process so the service manager starts it again; the restore
    is applied on the way up. Scheduled a moment later so the reply is sent."""
    import asyncio
    import os
    import signal

    asyncio.get_running_loop().call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)


def _when(value: Any) -> str:
    return str(value or "")[:19].replace("T", " ") + (" UTC" if value else "")


def checkpoints_page_html(note: str = "", confirm: Optional[str] = None,
                          error: str = "") -> str:
    from grove.api.fragments import _esc

    head = ('<header class="sc-header"><div class="sc-eyebrow sc-event">SETUP · ADVANCED</div>'
            '<h1>Checkpoints</h1><p>Save the node\'s records at one moment, and put that '
            'moment back to rehearse from it. Nothing is ever deleted: a restore first moves '
            'the current records to a dated archive folder. No record is altered, and every '
            'record keeps the time it was made.</p></header>')
    if not _demo():
        return (f'<div class="sc" id="checkpoints-page">{head}<div class="sc-panel"><p>'
                f'Checkpoints are available in demo mode only.</p></div></div>')
    banner = ""
    if error:
        banner += f'<div class="sc-panel"><p class="meta error">{_esc(error)}</p></div>'
    if note:
        banner += f'<div class="sc-panel"><p>{_esc(note)}</p></div>'
    waiting = checkpoints.pending_restore()
    if waiting:
        banner += (
            f'<div class="sc-panel" hx-get="/portal/fragments/checkpoints/" '
            f'hx-trigger="every 6s" hx-target="#checkpoints-page" hx-swap="outerHTML">'
            f'<p>Restoring <strong>{_esc(waiting.get("name"))}</strong>. The gateway is '
            f'restarting; this page shows the result when it is back.</p></div>')
    busy = checkpoints.in_flight()
    state = (
        '<div class="sc-panel"><h3>Right now</h3>'
        + ('<p>Nothing is in flight. A checkpoint can be saved or restored.</p>' if not busy else
           '<p>A checkpoint cannot be saved or restored while work is under way:</p><ul>'
           + "".join(f"<li>{_esc(r)}</li>" for r in busy) + "</ul>")
        + '</div>')
    save = (
        '<div class="sc-panel"><h3>Save a checkpoint</h3>'
        '<form class="tier-form" hx-post="/portal/actions/checkpoints/save" '
        'hx-target="#checkpoints-page" hx-swap="outerHTML">'
        '<input type="text" name="name" placeholder="name, such as before-month-3" required>'
        '<input type="text" name="note" placeholder="a note for yourself">'
        f'<button type="submit" class="btn"{" disabled" if busy else ""}>Save</button></form>'
        '<p class="sc-foot">Every store is copied together: turn records, sessions and their '
        'chain anchors, decision logs, the Kaizen ledger, kegs, proposals waiting, the Dock, '
        'signatures in force, learned phrases, the work queue and the backlog folders. Not part '
        'of a checkpoint: which model each tier is bound to, model prices and routing rules. '
        'A restore leaves those as they are.</p>'
        '</div>')
    rows = ""
    for m in checkpoints.listing():
        name = str(m.get("name"))
        if m.get("unreadable"):
            rows += (f'<div class="sc-version"><strong>{_esc(name)}</strong> '
                     f'<span class="meta error">manifest unreadable</span></div>')
            continue
        goals = "; ".join(
            f'{g.get("goal")}: run {g.get("run")}, {g.get("decided")} decided, '
            f'{g.get("queued")} in the queue, backlog {(g.get("backlog") or {}).get("released")} '
            f'of {(g.get("backlog") or {}).get("items")} released'
            for g in (m.get("goals") or []))
        if confirm == name:
            control = (
                f'<div class="sc-note"><strong>Restore {_esc(name)}?</strong> The current '
                f'records are moved to an archive folder, not deleted. The gateway restarts, '
                f'which takes about half a minute, and the audit check runs on the way up.'
                f'</div><form class="tier-form" hx-post="/portal/actions/checkpoints/restore" '
                f'hx-target="#checkpoints-page" hx-swap="outerHTML">'
                f'<input type="hidden" name="name" value="{_esc(name)}">'
                f'<button type="submit" class="btn">Restore now</button>'
                f'<a class="btn btn-secondary" hx-get="/portal/fragments/checkpoints/" '
                f'hx-target="#checkpoints-page" hx-swap="outerHTML">Cancel</a></form>')
        else:
            control = (
                f'<button type="button" class="btn btn-secondary"{" disabled" if busy else ""} '
                f'hx-get="/portal/fragments/checkpoints/?confirm={_esc(name)}" '
                f'hx-target="#checkpoints-page" hx-swap="outerHTML">Restore…</button>')
        rows += (
            f'<div class="sc-version"><div class="sc-version-head"><strong>{_esc(name)}</strong>'
            f'<span class="sc-eyebrow">SAVED {_esc(_when(m.get("saved_at")))}</span></div>'
            + (f'<div>{_esc(m.get("note"))}</div>' if m.get("note") else "")
            + f'<div class="sc-note">{_esc(goals)}</div>{control}</div>')
    saved = (f'<div class="sc-panel"><h3>Saved checkpoints</h3><div class="sc-versions">'
             f'{rows or "<div class=sc-note>None saved yet.</div>"}</div></div>')
    last = checkpoints.last_restore()
    result = ""
    if last:
        check = last.get("audit_check") or {}
        ok = last.get("identical") and check.get("result") == "intact"
        problems = "".join(f"<li>{_esc(p)}</li>" for p in
                           (last.get("mismatches") or []) + (check.get("problems") or []))
        result = (
            f'<div class="sc-panel"><div class="sc-panel-head"><h3>Last restore</h3>'
            f'<span class="sc-status {"sc-status-signed" if ok else "sc-status-draft"}">'
            f'{"RESTORED · CHAIN INTACT" if ok else "NEEDS YOUR ATTENTION"}</span></div>'
            f'<div class="sc-rows">'
            f'<div class="sc-row"><span>Checkpoint</span><span class="sc-mono">'
            f'{_esc(last.get("name"))}, saved {_esc(_when(last.get("saved_at")))}</span></div>'
            f'<div class="sc-row"><span>Restored</span><span class="sc-mono">'
            f'{_esc(_when(last.get("restored_at")))}</span></div>'
            f'<div class="sc-row"><span>Identical to the checkpoint, file by file</span>'
            f'<span class="sc-mono">{"yes" if last.get("identical") else "NO"}</span></div>'
            f'<div class="sc-row"><span>Audit check</span><span class="sc-mono">'
            f'{_esc(str(check.get("result")).replace("_", " "))}'
            + (f' · {check.get("chained")} of {check.get("records")} records'
               if check.get("records") is not None else "")
            + '</span></div>'
            f'<div class="sc-row"><span>What was there before</span><span class="sc-mono">'
            f'{_esc(last.get("archive") or "—")}</span></div></div>'
            + (f'<ul>{problems}</ul>' if problems else "")
            + (f'<p class="meta error">{_esc(last.get("failed"))}</p>' if last.get("failed") else "")
            + '</div>')
    log = "".join(
        f'<div class="sc-row"><span>{_esc(_when(e.get("at")))} · '
        f'{_esc(str(e.get("action")).replace("_", " "))}</span><span class="sc-mono">'
        f'{_esc(e.get("name") or "")} <span class="sc-quiet">{_esc(e.get("surface") or "")}'
        f'</span></span></div>' for e in checkpoints.admin_log(limit=12))
    admin = (f'<div class="sc-panel"><h3>Admin log</h3><div class="sc-rows">'
             f'{log or "<div class=sc-note>Nothing yet.</div>"}</div><p class="sc-foot">Saves '
             f'and restores are logged here, outside every goal\'s records. They add nothing '
             f'to the audit chain and alter nothing in it.</p></div>')
    return (f'<div class="sc" id="checkpoints-page">{head}{banner}{state}{result}{save}{saved}'
            f'{admin}</div>')


async def handle_checkpoints_page(request: web.Request) -> web.Response:
    from grove.api.fragments import _html_fragment
    return _html_fragment(checkpoints_page_html(confirm=request.query.get("confirm")))


async def handle_checkpoint_save(request: web.Request) -> web.Response:
    from grove.api.fragments import _html_fragment

    if not _demo():
        return _html_fragment(checkpoints_page_html(), status=403)
    data = await request.post()
    try:
        saved = checkpoints.save(str(data.get("name") or ""), str(data.get("note") or ""),
                                 surface="portal")
    except checkpoints.CheckpointRefused as refused:
        return _html_fragment(checkpoints_page_html(error=str(refused)))
    return _html_fragment(checkpoints_page_html(note=f"Saved checkpoint {saved['name']}."))


async def handle_checkpoint_restore(request: web.Request) -> web.Response:
    from grove.api.fragments import _html_fragment

    if not _demo():
        return _html_fragment(checkpoints_page_html(), status=403)
    data = await request.post()
    try:
        checkpoints.request_restore(str(data.get("name") or ""), surface="portal")
    except checkpoints.CheckpointRefused as refused:
        return _html_fragment(checkpoints_page_html(error=str(refused)))
    logger.warning("[portal] checkpoint restore requested: %s — restarting the gateway",
                   data.get("name"))
    _restart_gateway()
    return _html_fragment(checkpoints_page_html())


def register_checkpoint_routes(app: web.Application) -> None:
    app.router.add_get("/portal/fragments/checkpoints/", handle_checkpoints_page)
    app.router.add_post("/portal/actions/checkpoints/save", handle_checkpoint_save)
    app.router.add_post("/portal/actions/checkpoints/restore", handle_checkpoint_restore)
