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

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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

# fleet-event-reconciliation-v1 RC-1 — orphaned terminal events at most this
# old are re-classified through the live fold; older ones are named in the
# WARNING summary (the audit trail, gate ruling c) and marked, never carded.
_RECONCILE_WINDOW_DAYS = 7


def _classified_marker_path(event_path: Any) -> Path:
    """The sidecar marker beside a terminal event file (gate ruling a)."""
    return Path(str(event_path) + ".classified")


def _mark_classified(event_path: Any) -> None:
    """Mark a terminal event as classified (sidecar ``<event>.json.classified``).

    Written by BOTH the live-reap path and the reconciler so the two converge
    on one legibility story. The marker is an efficiency + Andon-noise
    mechanism, NOT the correctness wall — proposal emission is content-
    addressed and dedups on re-classify; the marker is what prevents a
    re-scan from re-firing ``surface_fleet_andon`` (which has no dedup).
    A marker-write failure logs and continues: the dedup wall holds.
    """
    try:
        _classified_marker_path(event_path).touch()
    except OSError as exc:
        logger.error(
            "[fleet.reconcile] could not write classified marker for %s: %r",
            event_path, exc,
        )


def _event_timestamp(event: Dict[str, Any], event_path: Path) -> datetime:
    """The event's authoritative timestamp (gate ruling c): the in-band ``ts``
    field; file mtime as fail-open fallback; ``now`` if both are unreadable —
    over-inclusion lands in the fold behind the dedup wall, silent skip is
    the sin."""
    raw = event.get("ts")
    if isinstance(raw, str):
        try:
            ts = datetime.fromisoformat(raw)
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(event_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


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
    def __init__(
        self,
        loop: Optional[Any] = None,
        workers_path: Optional[Any] = None,
        override_path: Optional[Any] = None,
    ):
        self._loop = loop
        self._workers_path = workers_path
        # fleet-hygiene-sweep P4 — the node-local enable-flag overlay path
        # (None → the real <GROVE_HOME>/fleet_workers.override.yaml at call
        # time). Passed EXPLICITLY into every load so overrides apply even when
        # _workers_path is an explicit (test) registry.
        self._override_path = override_path
        # Edge-trigger latch for the R-B3 fail-closed override state: the
        # reason string while in fail-closed, None while healthy. Fire ONE
        # Andon at onset, log recovery — no per-tick spam (the refusal-demotion
        # no-spam precedent).
        self._override_fail_reason: Optional[str] = None
        self._running: Dict[str, WorkerHandle] = {}
        self._last_dispatch: Dict[str, datetime] = {}
        # fleet-event-reconciliation-v1 (gate ruling e) — first-tick-as-boot:
        # the first reconciliation pass runs with source="boot", every later
        # one with source="tick" (the RC-2 stall tripwire).
        self._boot_reconciled = False

    # ── public ───────────────────────────────────────────────────────────────

    def tick(self, now: Optional[datetime] = None) -> None:
        """One fleet tick. Never raises — surfaces failures via the bus."""
        now = now or datetime.now(timezone.utc)
        self._reap_running()
        self._maybe_dispatch(now)
        # I1 — windowed publish digest at the TAIL of the tick (post-append: every
        # publish this tick already landed in the durable log during _reap_running).
        self._maybe_emit_publish_digest()
        # fleet-event-reconciliation-v1 RC-1 — orphaned-terminal-event pass at
        # the very tail: _reap_running above has already classified (and
        # marked) every live-handle exit this tick, so anything unclassified
        # here is a genuine orphan.
        self._maybe_reconcile_events()

    def _maybe_emit_publish_digest(self) -> None:
        """I1 (unattended-publish-legibility-v1 MOVE 4) — the windowed publish
        digest, HOSTED at the tail of the tick (not a dedicated racing tick). The
        digest owns its own telemetry-first failure posture (it catches and leaves
        the watermark unadvanced); this guard is the last line so a digest bug can
        never break the tick."""
        try:
            from grove.fleet.digest import emit_publish_digest

            emit_publish_digest(loop=self._loop)
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.error("[fleet.manager] publish digest host failed: %r", exc)

    # ── orphaned-terminal-event reconciliation (fleet-event-reconciliation-v1) ──

    def _maybe_reconcile_events(self) -> None:
        """Host the RC-1 reconciliation pass; a pass bug never breaks the tick."""
        source = "boot" if not self._boot_reconciled else "tick"
        self._boot_reconciled = True
        try:
            self._reconcile_events(source)
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.error(
                "[fleet.reconcile] %s reconciliation pass failed: %r", source, exc,
            )

    def _reconcile_events(self, source: str) -> None:
        """RC-1 — classify orphaned terminal events through the SAME live fold.

        A worker's terminal event reaches the operator only via the in-memory
        handle chain (``_reap_one`` → ``_classify_terminal`` →
        ``_maybe_emit_artifact_proposal``); a gateway restart severs the
        handle and, before this sprint, permanently orphaned the event (the
        260719/cf577af0 incident — a clean success event on disk, no card,
        for hours). This pass scans every ``<fleet>/<wid>/events/*.json``
        without a ``.classified`` sidecar and routes it through
        ``_classify_terminal`` with a stub handle
        (``SimpleNamespace(run_id=…, wall_clock_secs=None)``, ``killed=False``
        — the killed branch is unreachable for a reconciled event because a
        wall-clock kill is only ever decided by a LIVE ``_reap_one`` holding
        the real handle; an event on disk means the worker reached its own
        terminal write). Failed events Andon exactly like live reaps (gate
        ruling d corollary): restart-orphaned failures were exactly as
        invisible as successes.

        ORDERING PIN (gate ruling e condition): first-tick reconciliation
        must only ever see post-``sweep_orphans`` state. The boot sequence
        guarantees it — ``gateway/run.py:17425-17434`` runs ``sweep_orphans``
        BEFORE the cron ticker starts, so by the first ``tick()`` every
        stranded process group is dead and no orphan is mid-write. The
        mechanical wall pinned by test: any run_id still in
        ``self._running`` is SKIPPED (the ticker owns it; its event may be
        torn-mid-write or simply not yet reaped this tick — ``_reap_running``
        at the head of the tick handles it next pass).

        Window (gate ruling c): events at most ``_RECONCILE_WINDOW_DAYS`` old
        (in-band ``ts`` authoritative; mtime fail-open into the fold) ride
        the fold; older ones are named in the WARNING summary — that IS the
        audit trail — and marked. No new ledger event type. Double-classify
        is safe regardless: proposal emission is content-addressed (the
        dedup wall); the marker exists to stop re-scans and re-Andons.
        """
        from grove.fleet import paths as fleet_paths

        root = fleet_paths.fleet_root()
        if not root.is_dir():
            return
        live_run_ids = {h.run_id for h in self._running.values()}
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=_RECONCILE_WINDOW_DAYS
        )
        reconciled: list = []
        stale: list = []
        for event_path in sorted(root.glob("*/events/*.json")):
            if _classified_marker_path(event_path).exists():
                continue
            wid = event_path.parent.parent.name
            run_id = event_path.stem
            if run_id in live_run_ids:
                continue  # ordering pin — the ticker owns this run
            try:
                event = json.loads(event_path.read_text(encoding="utf-8"))
                if not isinstance(event, dict):
                    raise ValueError("terminal event is not a mapping")
            except Exception as exc:  # noqa: BLE001 — torn/unreadable orphan
                logger.warning(
                    "[fleet.reconcile] unreadable orphan event %s (%r) — "
                    "marked classified so it cannot re-scan; inspect manually",
                    event_path, exc,
                )
                _mark_classified(event_path)
                continue
            if _event_timestamp(event, event_path) < cutoff:
                stale.append(f"{wid}/{run_id}")
                _mark_classified(event_path)
                continue
            if source == "tick":
                # RC-2 tripwire — an orphan appearing under a LIVE ticker is
                # the reap-stall signature (the cf577af0 mystery), surfaced
                # at the journald-visible floor.
                logger.warning(
                    "[fleet.reconcile] RC-2 tripwire: run %s/%s completed "
                    "under a live ticker but was never reaped (event ts %s) "
                    "— reconciling now",
                    wid, run_id, event.get("ts"),
                )
            self._classify_terminal(
                wid,
                SimpleNamespace(run_id=run_id, wall_clock_secs=None),
                0,
                event,
                False,
            )
            _mark_classified(event_path)
            reconciled.append(f"{wid}/{run_id}")
        if reconciled or stale:
            logger.warning(
                "[fleet.reconcile] %s reconciliation: %d orphaned event(s) "
                "classified through the live fold: %s; %d stale (>%dd) "
                "marked trace-only: %s",
                source, len(reconciled), reconciled,
                len(stale), _RECONCILE_WINDOW_DAYS, stale,
            )

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
        # fleet-event-reconciliation-v1 (gate ruling a) — the live path writes
        # the same classified marker the reconciler writes: one legibility story.
        _mark_classified(handle.event_path)

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
                    # drafter-quality-checks-v1 P4 — the quality rider, read OFF
                    # the event fields (the canonical channel, same discipline as
                    # slug/unit_id above). Always-present-null precedent: null on
                    # every ungated producer's proposals.
                    "quality_score": event.get("quality_score"),
                    "rubric_version": event.get("rubric_version"),
                    "redraft_count": event.get("redraft_count"),
                    "evaluator_model": event.get("evaluator_model"),
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

            # forge-publish-meta-hotfix-v1 P1 — the emit-time meta defect surfaces
            # HERE, on the staged (success) event, not by discarding the run. A
            # stub meta.json (missing company/role/row_id) fires the loud operator
            # Andon via the existing surfacer (broadcast + andon_halt ledger leg)
            # so the defect is met at emit time, then STILL falls through to the
            # promote-proposal path so the draft is surfaced behind a defect marker
            # — inform disposition, never withhold work.
            meta_defect = event.get("meta_defect")
            if meta_defect:
                surface_fleet_andon(
                    wid, run_id,
                    f"forge draft {slug!r} staged with an INCOMPLETE meta.json "
                    f"({meta_defect}) — publish is endpoint-blocked until the "
                    f"missing field(s) are backfilled; the draft is staged for "
                    f"review with a defect marker",
                    check="forge_meta_incomplete", loop=self._loop,
                )

            # forge-unattended-publish-v1 P2 — hard-AND gate at the fire-point.
            # Conjunct 1 (mode == action_surface_publish) is already satisfied
            # (checked above). Conjunct 2 is the operator's overlay-armed
            # publication.unattended, read ONLY via publication_unattended_
            # authorized — the merged Capability never carries the field. ARMED
            # → fire the atomic Drive door directly and RETURN; the published
            # event replaces the proposal. UN-ARMED (absent/false, the shipped
            # default) → fall through to the existing proposal path, unchanged.
            # forge-publish-meta-hotfix-v1 P1 — a defect-marked draft NEVER takes
            # the unattended door (it would 400 at the endpoint and strand the
            # operator with no card); it always falls to the proposal path.
            from grove.capability_registry import publication_unattended_authorized

            if publication_unattended_authorized(skill_id) is True and not meta_defect:
                self._publish_unattended(wid, run_id, skill_id, event)
                return

            row_id = event.get("row_id")
            fit_score = event.get("fit_score")
            payload = {
                "slug": slug,
                "row_id": row_id,
                "skill_id": skill_id,
                "fit_score": fit_score,
            }
            # forge-publish-meta-hotfix-v1 P1 — the promote card's defect marker.
            # The forge payload is CONTENT-ADDRESSED (proposal_id hashes
            # type|payload|evidence), so an always-present key would fork the id
            # of every clean draft. Added ONLY when a defect exists — a clean
            # draft's payload stays byte-identical to the pre-sprint shape and its
            # proposal_id is unchanged.
            if meta_defect:
                payload["meta_defect"] = meta_defect
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

    def _publish_unattended(self, wid: str, run_id: str, skill_id: str, event: dict) -> None:
        """forge-unattended-publish-v1 P3 — fire the atomic Drive door for an
        ARMED forge worker's staged package, then make the local state coherent.

        DRIVE-ONLY (invariant 3): the door (``publish_application_package``)
        never speaks to Notion; the status flip stays portal-owned. This method
        imports and calls NOTHING Notion/MCP — physical isolation. The local
        coherence (canonicalize + archive) and the audit event are filesystem /
        memory-store writes only.

        Inputs (invariant 2): ``row_id`` is EVENT-sourced (the authoritative row
        identity); ``company`` / ``role`` are untrusted LABELS from meta.json;
        resume/cover are FIXED filenames jail-rooted in the staging slug dir. The
        door SELF-ACQUIRES its OAuth token — no token is passed.

        Ordering (publish-FIRST — never canonicalize before a confirmed publish):
          door → PublishError/token error → Andon (publish failed), STOP.
          → canonicalize + archive → failure → Andon carrying folder_link
            ("on Drive but local state stuck"), STOP.
          → memory audit event (FleetPublishedUnattended, honest provenance) →
            emit failure → Andon carrying folder_link (publish + coherence STAND,
            never unwound), STOP.
          → operator info event (carries folder_link — the publish-time link).
        """
        from pathlib import Path

        from hermes_constants import get_hermes_home
        from grove.forge.resolve import ResolvedForgePackage, resolve_forge_package

        slug = event.get("slug")  # non-empty (validated upstream)
        row_id = event.get("row_id")
        if not row_id:
            surface_fleet_andon(
                wid, run_id,
                f"unattended publish aborted for {slug!r} — success event carries "
                f"no row_id (the Drive/Notion row identity)",
                check="publish_no_row_id", loop=self._loop,
            )
            return

        home = Path(get_hermes_home())
        resolved = resolve_forge_package(home, slug)
        if not isinstance(resolved, ResolvedForgePackage):
            surface_fleet_andon(
                wid, run_id,
                f"unattended publish aborted for {slug!r} — cannot resolve staged "
                f"package: {resolved.reason}",
                check=f"publish_unresolved_{resolved.kind}", loop=self._loop,
            )
            return

        from grove.forge import publish_application_package
        from grove.forge.publish import PublishError

        # ── door publish (FIRST — the confirmed external effect) ──
        try:
            result = publish_application_package(
                str(row_id),
                resolved.company,
                resolved.role,
                resolved.resume_path,
                resolved.cover_path,
                operator_initiated=False,  # I4 — honest provenance: unattended ticker
            )
        except PublishError as exc:
            surface_fleet_andon(
                wid, run_id,
                f"unattended Drive publish FAILED for {slug!r}: {exc}",
                check="publish_failed", loop=self._loop,
                extra={"partial_state": getattr(exc, "partial_state", None)},
            )
            return
        except Exception as exc:  # noqa: BLE001 — token/RefreshError/etc — loud, never swallowed
            surface_fleet_andon(
                wid, run_id,
                f"unattended Drive publish ERRORED for {slug!r}: {exc!r}",
                check="publish_error", loop=self._loop,
            )
            return

        folder_link = result.get("folder_link")
        folder_id = result.get("folder_id")
        sink = _canonical_sink_for_skill(skill_id) or "forge"

        # ── mechanism 3 — local coherence (canonicalize + archive, publish-first
        #    satisfied). On failure the artifact is ON DRIVE but the portal would
        #    show it stuck; surface a loud Andon carrying the clickable link and
        #    STOP (never a misleading success info event over stuck local state).
        try:
            canonical_files = self._canonicalize_and_archive(home, sink, slug, resolved)
        except OSError as exc:
            surface_fleet_andon(
                wid, run_id,
                f"unattended publish for {slug!r} is ON DRIVE ({folder_link}) but "
                f"the LOCAL promoted-state write FAILED: {exc} — the portal will "
                f"not show it promoted until repaired",
                check="publish_canonicalize_failed", loop=self._loop,
                extra={"folder_link": folder_link, "folder_id": folder_id},
            )
            return

        # ── mechanism 1 — honest-provenance audit (fleet memory event). An emit
        #    failure NEVER unwinds the publish/canonicalize (a confirmed Drive
        #    write is not rolled back), but it is SURFACED loudly (Andon-class) —
        #    the same no-silent-sovereign-write-record discipline as the two
        #    branches above. The Andon carries folder_link and is clear that the
        #    artifact IS on Drive and local state IS coherent; only the durable
        #    audit record failed. It replaces the success info event for this run.
        try:
            from datetime import datetime, timezone

            from grove.memory.events import FleetPublishedUnattended, new_event_id
            from grove.memory.store import MemoryStore

            MemoryStore(base_dir=home).append_event(FleetPublishedUnattended(
                event_id=new_event_id(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                unit_id=str(row_id),
                slug=slug,
                producer=skill_id,
                sink=sink,
                folder_link=folder_link,
                folder_id=folder_id,
                provenance="publication.unattended",
                canonical_files=list(canonical_files or []),
                status=result.get("status"),  # I1 — additive feed enrich (door
                #                                published-vs-exists; digest reads it)
            ))
        except Exception as exc:  # noqa: BLE001 — surface loudly; NEVER unwind the publish
            surface_fleet_andon(
                wid, run_id,
                f"unattended publish for {slug!r} succeeded and is ON DRIVE "
                f"({folder_link}) with local state coherent, but the durable AUDIT "
                f"record FAILED to persist: {exc!r} — the publish STANDS (not "
                f"unwound); the audit trail is incomplete",
                check="publish_audit_emit_failed", loop=self._loop,
                extra={"folder_link": folder_link, "folder_id": folder_id},
            )
            return

        # ── I1 (unattended-publish-legibility-v1 MOVE 5) — the per-publish
        #    operator ping is RETIRED here. The durable FleetPublishedUnattended
        #    event (appended above) is the feed; the windowed digest at the tick
        #    tail (_maybe_emit_publish_digest) is the SOLE operator surface now,
        #    deduped across runs. Only the local log floor stays (not a broadcast).
        logger.info(
            "[fleet.manager] unattended publish OK for %s (status=%s, folder=%s)",
            slug, result.get("status"), folder_link,
        )

    @staticmethod
    def _canonicalize_and_archive(home, sink: str, slug: str, resolved) -> list:
        """forge-unattended-publish-v1 P3 (mechanism 3) — filesystem coherence.

        Move the two drafts into the per-unit canonical subdir
        (``<home>/<sink>/<slug>/``), then archive the now-meta-only staged dir so
        ``staged-gone + canonical-present`` makes the fleet view resolve the unit
        ``promoted`` via rule 1 (grove/api/portal.py:962-975). Notion-free.
        Raises ``OSError`` on any move failure (the caller Andons carrying the
        folder_link). Returns the canonical file paths.
        """
        from datetime import datetime, timezone
        from pathlib import Path

        from grove.utils.fs_utils import canonicalize_files

        canonical_dir = home / sink / slug
        canonical_files = canonicalize_files(
            [Path(resolved.resume_path), Path(resolved.cover_path)], canonical_dir
        )
        staged_dir = resolved.slug_dir
        if staged_dir.is_dir():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = home / sink / ".archive" / f"{slug}-{ts}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged_dir.rename(dest)  # atomic within the one ~/.grove mount
        return canonical_files

    # ── dispatch ───────────────────────────────────────────────────────────────

    def _maybe_dispatch(self, now: datetime) -> None:
        # P4 — resolve the enable-flag overlay path and EDGE-TRIGGER the
        # fail-closed Andon at onset (once), log recovery, re-arm. The loader
        # already fails closed (all workers disabled) + logs CRITICAL; this
        # surfaces the transition to the operator bus without per-tick spam.
        from grove.fleet.config import fleet_workers_override_path, override_health

        ov_path = self._override_path or fleet_workers_override_path()
        health = override_health(ov_path)
        if health is not None and self._override_fail_reason is None:
            surface_fleet_andon(
                "<override>", "enable_override",
                f"enable-flag override unusable ({health}) — ALL fleet workers "
                f"disabled (fail-closed) until fixed",
                check="enable_override_fail_closed", loop=self._loop,
            )
            self._override_fail_reason = health
        elif health is None and self._override_fail_reason is not None:
            logger.info(
                "[fleet.manager] enable-flag override recovered — fleet re-armed."
            )
            self._override_fail_reason = None

        try:
            workers = load_fleet_workers(
                self._workers_path, override_path=ov_path
            )
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
