"""Fleet manager — the ticker's per-tick fleet check (Phase 3).

Holds cross-tick state the 60s ticker cannot: the running worker handles and each
worker's last-dispatch time. One ``tick()`` does two defensive passes:

  1. REAP — poll every running handle, enforce its wall-clock, and on exit apply
     death observability: exit-0 + valid terminal event = done (success/no_work
     distinguished, the quiet paths); exit-0 + NO event = catastrophic -> Andon;
     nonzero exit -> Andon. Andons route to the observed-event bus.
  2. DISPATCH — for each enabled worker not already running, if cadence is due
     and it is outside quiet hours, resolve its input_state; work -> Popen the
     worker off-thread via the runner; no work -> quiet; a cold/failed resolve ->
     Andon (never blocks the tick, never silent-skips).

``tick()` is fully defensive — no single worker's failure stops the others or
the tick — so the ticker can call it OUTSIDE its ``except Exception: debug``
swallow with only a thin last-resort guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from grove.fleet import runner
from grove.fleet.cadence import cadence_due, in_quiet_hours
from grove.fleet.config import WorkerConfig, load_fleet_workers
from grove.fleet.errors import FleetWorkerAndon, OperatorActionRequired
from grove.fleet.observability import surface_fleet_andon
from grove.fleet.reap import enforce_wall_clock, remove_pidfile
from grove.fleet.resolvers import resolve_input_state
from grove.fleet.runner import WorkerHandle

logger = logging.getLogger(__name__)


def _review_mode_for_skill(skill_id: Optional[str]) -> Optional[str]:
    """The worker's ``approval_handoff.mode`` from its capability record (or None).

    fleet-review-unification-v1 — the SOLE ``approval_handoff.mode`` read in the
    codebase. Gates BOTH the operator-promote proposal emission (post-run reap) and
    the C1b-1 revision-directive fold (pre-run dispatch). ``action_surface_publish``
    is the producer-declaring value; as of C1b-2 forge, drafter, and cultivator all
    declare it (the review-unified producer set).
    """
    from grove.capability_registry import load_capabilities

    cap = load_capabilities().get(skill_id)
    gov = (cap.governance or {}) if cap is not None else {}
    return ((gov.get("approval_handoff") or {}).get("mode")) if isinstance(gov, dict) else None


def _canonical_sink_for_skill(skill_id: Optional[str]) -> Optional[str]:
    """The worker's ``governance.write_zone.canonical_dir`` from its capability record
    (or None). fleet-review-unification-v1 C1b-2 — the promote-dispatch + proposal-type
    routing key: ``forge`` → self-authored forge_artifact_pending (Drive publish);
    anything else → generic fleet_artifact_pending (mv → canonical, poller ingests)."""
    from grove.capability_registry import load_capabilities

    cap = load_capabilities().get(skill_id)
    gov = (cap.governance or {}) if cap is not None else {}
    return ((gov.get("write_zone") or {}).get("canonical_dir")) if isinstance(gov, dict) else None


class FleetManager:
    def __init__(self, loop: Optional[Any] = None, workers_path: Optional[Any] = None):
        self._loop = loop
        self._workers_path = workers_path
        self._running: Dict[str, WorkerHandle] = {}
        self._last_dispatch: Dict[str, datetime] = {}

    # ── public ───────────────────────────────────────────────────────────────

    def tick(self, now: Optional[datetime] = None) -> None:
        """One fleet tick. Never raises — surfaces failures via the bus."""
        now = now or datetime.now(timezone.utc)
        self._reap_running()
        self._maybe_dispatch(now)

    # ── reap / death observability ─────────────────────────────────────────────

    def _reap_running(self) -> None:
        for wid, handle in list(self._running.items()):
            try:
                self._reap_one(wid, handle)
            except Exception as exc:  # noqa: BLE001 — one reap must not stop the rest
                logger.error("[fleet.manager] reap of worker %s crashed: %r", wid, exc)
                self._running.pop(wid, None)  # drop the stuck handle

    def _reap_one(self, wid: str, handle: WorkerHandle) -> None:
        killed = enforce_wall_clock(handle)
        rc = handle.proc.poll()
        if rc is None:
            return  # still running within its window
        # Exited (naturally or via the wall-clock kill) — reap and classify.
        self._running.pop(wid, None)
        remove_pidfile(wid)
        event = self._read_event(handle.event_path)
        self._classify_terminal(wid, handle, rc, event, killed)

    @staticmethod
    def _read_event(event_path) -> Optional[Dict[str, Any]]:
        try:
            if event_path.exists():
                import json

                return json.loads(event_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a torn/missing event is handled as absent
            return None
        return None

    def _classify_terminal(self, wid, handle, rc, event, killed) -> None:
        run_id = handle.run_id
        if killed:
            surface_fleet_andon(
                wid,
                run_id,
                f"worker exceeded its wall-clock ({handle.wall_clock_secs}s) and "
                f"was killed",
                check="wall_clock_exceeded",
                loop=self._loop,
            )
            return
        if rc == 0:
            if event is None:
                # exit-0 with NO terminal event — the worker died without writing.
                surface_fleet_andon(
                    wid,
                    run_id,
                    "worker exited 0 but wrote NO terminal-state event — "
                    "catastrophic (died before its terminal write)",
                    check="catastrophic_no_event",
                    loop=self._loop,
                )
                return
            status = event.get("status")
            if status == "no_work":
                logger.info("[fleet.manager] worker %s run %s -> no_work", wid, run_id)
                return  # silent — nothing staged, nothing to promote
            if status == "success":
                logger.info("[fleet.manager] worker %s run %s -> success", wid, run_id)
                # fleet-pipeline-v1 P2 — the ONLY branch that emits an
                # approve-artifact proposal. no_work + every failure emit nothing.
                self._maybe_emit_artifact_proposal(wid, run_id, event)
                return
            # exit-0 but a non-terminal status — the worker exits nonzero on
            # failure, so this is anomalous; surface it.
            surface_fleet_andon(
                wid,
                run_id,
                f"worker reported status={status!r}: {event.get('detail')}",
                check=event.get("check") or "nonzero_exit",
                loop=self._loop,
            )
            return
        # Nonzero exit -> Andon; read the terminal event for the WHY.
        detail = (event or {}).get("detail") or f"exit code {rc}"
        check = (event or {}).get("check") or "nonzero_exit"
        surface_fleet_andon(
            wid, run_id, f"worker exited {rc}: {detail}", check=check, loop=self._loop
        )

    def _maybe_emit_artifact_proposal(self, wid: str, run_id: str, event: dict) -> None:
        """On a fleet worker SUCCESS, emit a forge_artifact_pending proposal so the
        operator can promote (publish) or reject the staged draft — but ONLY when
        the skill's approval_handoff is an action-surface publish (an ingest_post
        worker auto-ingests and needs no operator promote). Reads slug/row_id/
        fit_score OFF the event fields (never parsed from detail/paths). Defensive:
        an emit failure surfaces an Andon, never crashes the tick."""
        try:
            skill_id = event.get("skill")
            # fleet-review-unification-v1 C1a/C1b-1 — producer == skill_id;
            # approval_handoff.mode == "action_surface_publish" is the producer-
            # declaring gate. ``_review_mode_for_skill`` is the SOLE mode read (this
            # file) — the SAME helper gates the C1b-1 directive fold in
            # ``_maybe_dispatch_one``.
            if _review_mode_for_skill(skill_id) != "action_surface_publish":
                return  # ingest_post / other — no operator-promote proposal

            slug = event.get("slug")
            if not slug:
                surface_fleet_andon(
                    wid, run_id,
                    "success event carries no slug — cannot emit a promote proposal "
                    "for the staged draft",
                    check="event_missing_slug", loop=self._loop,
                )
                return

            from grove.eval.proposal_queue import (
                PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
                PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
                file_agentless,
            )

            # fleet-review-unification-v1 C1b-2 — proposal TYPE by canonical_sink.
            # A file producer (canonical_sink != "forge") emits the GENERIC
            # fleet_artifact_pending, keyed on the stable unit_id (no Notion row_id);
            # forge falls through to its byte-identical forge_artifact_pending path.
            canonical_sink = _canonical_sink_for_skill(skill_id)
            if canonical_sink != "forge":
                unit_id = event.get("unit_id") or slug
                payload = {
                    "slug": slug,
                    "unit_id": unit_id,
                    "skill_id": skill_id,
                    "canonical_sink": canonical_sink,
                }
                pid, appended = file_agentless(
                    type=PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING,
                    payload=payload,
                    evidence=(unit_id,),  # stable per-unit dedup key
                    justification="Draft staged for review: " + slug,
                    proposer=skill_id,  # proposal-proposer-attribution-v1
                )
                logger.info(
                    "[fleet.manager] emitted %s proposal %s (appended=%s) for %s",
                    PROPOSAL_TYPE_FLEET_ARTIFACT_PENDING, pid, appended, slug,
                )
                return

            row_id = event.get("row_id")
            fit_score = event.get("fit_score")
            payload = {
                "slug": slug,
                "row_id": row_id,
                "skill_id": skill_id,
                "fit_score": fit_score,
            }
            justification = "Draft staged for review: " + slug + (
                f" (fit {fit_score})" if fit_score is not None else ""
            )
            pid, appended = file_agentless(
                type=PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING,
                payload=payload,
                evidence=(row_id or slug,),  # stable per-unit dedup key
                justification=justification,
                proposer=skill_id,  # proposal-proposer-attribution-v1 (producer #1)
            )
            logger.info(
                "[fleet.manager] emitted %s proposal %s (appended=%s) for %s",
                PROPOSAL_TYPE_FORGE_ARTIFACT_PENDING, pid, appended, slug,
            )
        except Exception as exc:  # noqa: BLE001 — emit must never crash the tick
            surface_fleet_andon(
                wid, run_id, f"failed to emit artifact proposal: {exc}",
                check="artifact_emit_failed", loop=self._loop,
            )

    # ── dispatch ───────────────────────────────────────────────────────────────

    def _maybe_dispatch(self, now: datetime) -> None:
        try:
            workers = load_fleet_workers(self._workers_path)
        except FleetWorkerAndon as exc:
            surface_fleet_andon(
                "<registry>", "load", str(exc), check=exc.check, loop=self._loop
            )
            return
        except Exception as exc:  # noqa: BLE001 — a broken registry must not kill the tick
            surface_fleet_andon(
                "<registry>", "load", f"{type(exc).__name__}: {exc}",
                check="registry_error", loop=self._loop,
            )
            return

        for wid, cfg in workers.items():
            if not cfg.enabled or wid in self._running:
                continue
            try:
                self._maybe_dispatch_one(wid, cfg, now)
            except OperatorActionRequired as exc:
                # ensure_mcp_warm auth-dead halt (P3/P5). HONOR the broadcast flag:
                # broadcast=True is the loud-once operator alert; broadcast=False is
                # the latch-suppressed repeat (still recorded in logs + Kaizen, just
                # not re-pinged) — G5.
                surface_fleet_andon(
                    wid, "dispatch", str(exc), check=exc.check, loop=self._loop,
                    broadcast=exc.broadcast,
                )
            except FleetWorkerAndon as exc:
                # HONOR the broadcast flag: a breaker-open warm halt is broadcast=False
                # (G3 — no cadence storm), a genuine fault is broadcast=True. Existing
                # resolver Andons default broadcast=True (unchanged).
                surface_fleet_andon(
                    wid, "dispatch", str(exc), check=exc.check, loop=self._loop,
                    broadcast=exc.broadcast,
                )
            except Exception as exc:  # noqa: BLE001 — one worker's failure is isolated
                surface_fleet_andon(
                    wid, "dispatch", f"{type(exc).__name__}: {exc}",
                    check="resolver_failed", loop=self._loop,
                )

    def _maybe_dispatch_one(self, wid: str, cfg: WorkerConfig, now: datetime) -> None:
        if in_quiet_hours(cfg.quiet_hours):
            return
        if not cadence_due(cfg.cadence, self._last_dispatch.get(wid), now):
            return
        # fleet-mcp-warm-unification-v1 P5 — warm the resolver's MCP server ONCE per
        # dispatch (placed BEFORE resolve_input_state, so never per-RPC), so a
        # fleet-only cold window self-heals with NO interactive turn. Server derived
        # from input_state (locked ruling: no requires_mcp field; default 'notion').
        # The ordered check's Andons (OperatorActionRequired / FleetWorkerAndon, each
        # carrying broadcast) propagate to the dispatch surfacer above.
        target_server = cfg.input_state.get("server", "notion")
        self._ensure_mcp_warm_sync(target_server, wid)
        payload = resolve_input_state(cfg.input_state, wid)  # None -> no work; raises -> Andon
        if payload is None:
            return  # legitimate no_work — the quiet path
        # fleet-review-unification-v1 C1b-1 — the revision-directive fold, LIFTED here
        # from resolve_notion_query. AMENDMENT-gated: inject ONLY when the worker's
        # approval_handoff.mode == "action_surface_publish" (forge today) — NO injection
        # for ingest_post workers even if a feedback file exists for the unit. Ordering:
        # AFTER the resolver constructs its payload; payload is a flat dict, key
        # "revision_directive" exactly as before. Read the per-unit feedback store by
        # unit_id (== row_id for notion_query) — same files, same directive, forge-identical.
        if isinstance(payload, dict) and _review_mode_for_skill(cfg.skill) == "action_surface_publish":
            from grove.fleet.resolvers import _revision_directive

            _directive = _revision_directive(payload.get("unit_id"), wid)
            if _directive:
                payload["revision_directive"] = _directive
        handle = runner.dispatch(cfg, payload)
        self._running[wid] = handle
        self._last_dispatch[wid] = now
        logger.info("[fleet.manager] dispatched worker %s run %s", wid, handle.run_id)

    def _ensure_mcp_warm_sync(self, server_id: str, wid: str) -> None:
        """Drive the async ``ensure_mcp_warm`` from this SYNC ticker-thread call.

        In production the ticker thread holds ``self._loop`` (the gateway loop): the
        coroutine is scheduled onto it via ``run_coroutine_threadsafe`` and this thread
        blocks on ``.result()`` — so the ordered check's exceptions (OperatorActionRequired
        / FleetWorkerAndon) propagate straight into ``_maybe_dispatch``'s surfacer, exactly
        as a synchronous raise would. The MCP work itself hops to the dedicated MCP loop
        regardless of which loop runs the coroutine, so the loop choice is immaterial to
        correctness. Without a loop (out-of-band / tests) a fresh ``asyncio.run`` loop is
        used. Blocking is by design: only a genuinely COLD warm blocks (Check-4), and the
        plausibly-warm fast-path returns instantly with no RPC.
        """
        import asyncio

        from tools.mcp_tool import ensure_mcp_warm

        coro = ensure_mcp_warm(server_id, {"wid": wid})
        loop = self._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop).result()
        else:
            asyncio.run(coro)
