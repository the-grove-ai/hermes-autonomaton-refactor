"""kaizen-fault-triage-v1 — recurring-fault Kaizen producer (T1: detect + stage).

The missing Kaizen leg of the fault loop: Andon reports per-instance
(``andon_halt`` / ``red_resolution`` in the Kaizen ledger — immediate, dumb on
purpose); this detector INTERPRETS — it aggregates recurring fault classes
cross-session and stages a ``fault_triage`` proposal that leads with a
deterministic judgment ("one defect, N instances, active, worsening"), so the
operator is asked for DIRECTION, never handed raw telemetry. No apply path at
T1 (render-only posture, the ``portal_action_failure`` precedent); remediation
is a future sprint behind its own GATE-B.

Shape copied from :class:`grove.eval.disposition_promotion.
DispositionPromotionDetector` (the shipped cross-session ledger-scan producer):
injectable ``ledger_dir`` / ``thresholds`` / ``now``, declarative thresholds
from ``flywheel.config.yaml``, stable-identity proposals via
``proposal_queue.append``, ridden by ``flywheel scan --propose``.

Identity (GATE-B Q1): the fault SIGNATURE only — ``(source, key fields,
error_signature)`` in the payload plus a stable synthetic evidence token. All
accumulating detail (counts, first/last seen, sampled raw events, the rendered
judgment) rides identity-EXCLUDED fields (``source_patterns`` /
``semantic_justification``), so a growing fault keeps one queue entry and any
future remediation enrichment never forks the id.

Group keys are schema-aware per source (GATE-B Q3 — the fleet fault stream
carries ``worker``/``check``/``detail`` and NO tool/rule):

* ``source: fleet_worker`` ``andon_halt`` → ``(worker, check, error_signature)``
* component ``andon_halt`` (``source`` in ``COMPONENT_SOURCES``) → ``(source, check)``
* dispatcher ``andon_halt``              → ``(intents[0].tool_name, matched_rule)``
* ``red_resolution``                     → ``(triggering_tool, matched_rule)``

Verb semantics (operator ruling — directions, not receipts):

* ``acknowledge`` = "seen, keep watching, tell me if it changes." The
  disposition records the acknowledged in-window count; this detector re-stages
  the SAME class only when it has materially changed (count grew past
  ``reraise_growth``, or a new session appeared and the count grew at all).
* ``dismiss`` = "not a real pattern, stop proposing" — suppressed for the
  remainder of the window.

Both are read back from the ledger's ``kaizen_disposition`` events — no new
state file, no new EVENT_TYPES entry (the enum is closed and fail-loud).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_FAULT_TRIAGE,
    RoutingProposal,
    _now_iso,
    compute_proposal_id,
)
from grove.kaizen.rendering import FaultSample, FaultTriageDetail

logger = logging.getLogger(__name__)

__all__ = [
    "COMPONENT_SOURCES",
    "FaultTriageThresholds",
    "load_fault_triage_thresholds",
    "FaultTriageDetector",
    "error_signature",
    "derive_activity",
    "judgment_line",
]


# silent-degradation-sweep-v1 — non-fleet components whose fail-loud filings
# (``andon_halt`` with this ``source`` + a ``check``) this detector classifies.
# Fault identity for these is ``(source, check)`` — the per-instance ``detail``
# (a repr'd exception) is deliberately identity-excluded, mirroring how the
# dispatcher/red shapes carry ``error_signature: ""``. A source NOT in this
# set falls through the dispatcher-shape check exactly as before.
COMPONENT_SOURCES = frozenset({
    "kaizen_push",
    "tier_ratchet",
    "proposal_queue",
    "portal_render",
})


# ── declarative thresholds ───────────────────────────────────────────────


@dataclass(frozen=True)
class FaultTriageThresholds:
    """Declarative thresholds for the fault-triage detector.

    Defaults are the documented baseline (``config/flywheel.config.yaml``,
    ``fault_triage`` block) — sized against the live 2026-07 ledger baseline
    so the active classes (shell.effect.default, the fleet worker faults)
    stage and the cold classes (legacy ``terminal``, ``execute_code``, the
    mcp_notion one-day burst) do not. An absent operator config means "use
    the specified default"; a present-but-invalid value fails loud in
    :func:`load_fault_triage_thresholds`.

    ``min_sessions`` defaults to 1: fleet worker ledgers key their session id
    on ``fleet:<worker>:<run>``, so a hot single-worker fault class can live
    in ONE session file — a >1 session floor would structurally silence the
    stream the producer exists to surface. ``min_events`` + the sliding
    window carry the noise floor; the operator raises ``min_sessions`` to
    demand cross-session spread.
    """

    min_events: int = 5
    min_sessions: int = 1
    window_days: int = 14
    reraise_growth: float = 1.5


def _require_positive_int(block: Dict[str, object], key: str, default: int) -> int:
    """Read ``key`` from a present config block, fail loud on a bad value."""
    if key not in block:
        return default
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"flywheel.config.yaml fault_triage.{key} must be an "
            f"integer, got {value!r} ({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(
            f"flywheel.config.yaml fault_triage.{key} must be >= 1, got {value}."
        )
    return value


def load_fault_triage_thresholds(
    config_path: Optional[Path] = None,
) -> FaultTriageThresholds:
    """Load thresholds from the operator's ``flywheel.config.yaml``.

    Mirrors :func:`grove.eval.disposition_promotion.load_promotion_thresholds`:
    absent file / absent ``fault_triage`` block → documented defaults; a
    present block is validated key-by-key and any malformed value raises
    LOUD. Malformed YAML propagates from the parser.
    """
    if config_path is None:
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return FaultTriageThresholds()

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return FaultTriageThresholds()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    block = raw.get("fault_triage")
    if block is None:
        return FaultTriageThresholds()
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} fault_triage must be a mapping, got "
            f"{type(block).__name__}."
        )

    min_events = _require_positive_int(block, "min_events", 5)
    min_sessions = _require_positive_int(block, "min_sessions", 1)
    window_days = _require_positive_int(block, "window_days", 14)
    growth_raw = block.get("reraise_growth", 1.5)
    if isinstance(growth_raw, bool) or not isinstance(growth_raw, (int, float)):
        raise ValueError(
            f"flywheel.config.yaml fault_triage.reraise_growth must be a "
            f"number, got {growth_raw!r} ({type(growth_raw).__name__})."
        )
    if growth_raw < 1.0:
        raise ValueError(
            f"flywheel.config.yaml fault_triage.reraise_growth must be "
            f">= 1.0, got {growth_raw}."
        )
    return FaultTriageThresholds(
        min_events=min_events,
        min_sessions=min_sessions,
        window_days=window_days,
        reraise_growth=float(growth_raw),
    )


# ── deterministic error-signature extractor (GATE-B Q3) ──────────────────

# Order matters: paths before hex (a path may contain hex segments), UUIDs
# before generic hex (a UUID is four hex groups). Placeholders are stable
# tokens so the SAME underlying fault with per-instance noise (ids, addresses,
# timestamps, paths) collapses to ONE signature, and distinct faults stay
# distinct.
_SIG_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    # ISO-8601 timestamps (with or without offset / fractional seconds).
    (re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
    ), "<ts>"),
    # UUIDs.
    (re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{12}\b"
    ), "<uuid>"),
    # Absolute paths (unix). Greedy segment run keeps one placeholder per path.
    (re.compile(r"(?:/[\w.\-]+){2,}"), "<path>"),
    # Hex addresses / long hex ids (0x… or bare ≥8 hex chars).
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
)

_SIG_MAX_CHARS = 160


def error_signature(detail: str) -> str:
    """Sanitize a raw fault detail into a stable class signature.

    Pure and deterministic: regex substitution only (UUIDs, hex addresses,
    ISO timestamps, absolute paths → placeholders), whitespace collapsed,
    truncated to a fixed cap. The raw unsanitized detail never enters
    proposal identity — it rides the identity-excluded sample fields only.
    """
    sig = detail or ""
    for pattern, placeholder in _SIG_RULES:
        sig = pattern.sub(placeholder, sig)
    sig = " ".join(sig.split())
    return sig[:_SIG_MAX_CHARS]


# ── judgment derivation (pure functions; amendment 3a) ──────────────────

_ACTIVE_WITHIN = timedelta(hours=48)


def derive_activity(
    timestamps: List[datetime], *, now: datetime, window_start: datetime,
) -> Tuple[str, bool]:
    """``(activity, worsening)`` for one group's in-window event timestamps.

    Deterministic: ``activity`` is ``"active"`` when the last event is within
    48h of ``now``, else ``"recurring"`` (both are in-window by construction).
    ``worsening`` is True when the event rate in the most recent half of the
    window exceeds the older half — the two halves are equal spans, so the
    rate comparison reduces to a count comparison.
    """
    last = max(timestamps)
    activity = "active" if (now - last) <= _ACTIVE_WITHIN else "recurring"
    midpoint = window_start + (now - window_start) / 2
    older = sum(1 for ts in timestamps if ts < midpoint)
    recent = sum(1 for ts in timestamps if ts >= midpoint)
    return activity, recent > older


def judgment_line(
    subject: str, descriptor: str, activity: str, worsening: bool,
) -> str:
    """The card's leading interpretation sentence — byte-stable per group.

    Kaizen speaking, not a louder Andon: a deterministic template-driven
    judgment, no LLM, no free text.
    """
    tail = f"{activity}, worsening" if worsening else activity
    return (
        f"{subject} is hitting the same {descriptor} repeatedly — "
        f"one defect, {tail}."
    )


def _normalize_sample(
    source: str, ts: datetime, event: Dict[str, Any],
) -> FaultSample:
    """One raw ledger event → a compact deterministic ``FaultSample``
    (proposal-card-legibility-v1 Phase 2).

    Pure per-source field mapping over the SAME schema-aware fields
    ``_classify`` keys on (GATE-B Q3) — no inference, no free text:

    * ``red_resolution``  → (date, triggering_tool, resolution)
    * ``dispatcher_halt`` → (date, intents[0].tool_name, matched_rule)
    * ``fleet_worker``    → (date, worker, check)

    ``ts`` is the event's already-parsed UTC timestamp (date-only on the
    card — no microsecond ISO noise). ``resolution`` is not a ``_classify``
    gate field, so it can legitimately be absent → ``"?"`` (the established
    missing-field placeholder), never a crash.
    """
    date = ts.date().isoformat()
    if source == "fleet_worker":
        return FaultSample(
            ts=date,
            subject=str(event.get("worker") or "?"),
            outcome=str(event.get("check") or "?"),
        )
    if source == "dispatcher_halt":
        intents = event.get("intents") or []
        tool = (
            intents[0].get("tool_name")
            if intents and isinstance(intents[0], dict) else None
        )
        return FaultSample(
            ts=date,
            subject=str(tool or "?"),
            outcome=str(event.get("matched_rule") or "?"),
        )
    if source == "red_resolution":
        return FaultSample(
            ts=date,
            subject=str(event.get("triggering_tool") or "?"),
            outcome=str(event.get("resolution") or "?"),
        )
    # silent-degradation-sweep-v1 — component filings mirror the fleet shape:
    # subject/outcome are the identity fields (source, check); dates carry the
    # per-instance variation, exactly like fleet_worker's (worker, check).
    if source in COMPONENT_SOURCES:
        return FaultSample(
            ts=date,
            subject=str(event.get("source") or "?"),
            outcome=str(event.get("check") or "?"),
        )
    # _classify emits only the sources above; a new source reaching here
    # without a mapping is a wiring defect — fail loud (no (?, ?) fold).
    raise ValueError(f"no FaultSample mapping for fault source {source!r}")


# ── group state ──────────────────────────────────────────────────────────


@dataclass
class _FaultGroup:
    """Accumulated in-window evidence for one fault-class key."""

    source: str
    payload: Dict[str, str]
    subject: str
    descriptor: str
    events: List[Tuple[datetime, str, Dict[str, Any]]] = field(
        default_factory=list
    )  # (parsed_ts, session_id, raw_event)


class FaultTriageDetector:
    """Reads the Kaizen ledger and stages recurring-fault proposals.

    Cross-session by construction: globs every ``*.jsonl`` under
    ``ledger_dir`` (the established pattern from
    ``DispositionPromotionDetector``). Reads ``andon_halt`` +
    ``red_resolution`` for fault classes and ``kaizen_disposition`` for the
    acknowledge/dismiss suppression signal — one store, read twice, no new
    state file.
    """

    def __init__(
        self,
        *,
        ledger_dir: Optional[Path] = None,
        thresholds: Optional[FaultTriageThresholds] = None,
    ) -> None:
        if ledger_dir is None:
            from grove.kaizen_ledger import default_ledger_dir
            ledger_dir = default_ledger_dir()
        self._ledger_dir = Path(ledger_dir)
        self._thresholds = thresholds or FaultTriageThresholds()

    # ── public API ───────────────────────────────────────────────────

    def detect(self, *, now: Optional[datetime] = None) -> List[RoutingProposal]:
        """Return the fault_triage proposals the current ledger state earns.

        Deterministic (sorted-key order) so a re-run yields a stable
        sequence; identity is stable per fault class, so the queue's
        content-addressed ``append`` dedups pending duplicates and the
        acknowledge/dismiss ledger reads govern re-staging after a
        disposition.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=self._thresholds.window_days)
        groups = self._aggregate(window_start=window_start)
        dispositions = self._latest_dispositions()

        proposals: List[RoutingProposal] = []
        for key in sorted(groups.keys()):
            group = groups[key]
            if not self._meets_threshold(group):
                continue
            proposal = self._build_proposal(
                group, now=now, window_start=window_start,
            )
            if self._suppressed(
                proposal.proposal_id, group, dispositions,
                window_start=window_start,
            ):
                continue
            proposals.append(proposal)
        return proposals

    # ── ledger intake ────────────────────────────────────────────────

    def _aggregate(
        self, *, window_start: datetime,
    ) -> Dict[Tuple[str, ...], _FaultGroup]:
        """Group in-window fault events by their schema-aware class key."""
        groups: Dict[Tuple[str, ...], _FaultGroup] = {}
        if not self._ledger_dir.is_dir():
            return groups
        for path in sorted(self._ledger_dir.glob("*.jsonl")):
            for event in self._read_events(path):
                classified = self._classify(event)
                if classified is None:
                    continue
                key, source, payload, subject, descriptor = classified
                ts = self._parse_timestamp(event.get("timestamp"))
                if ts is None or ts < window_start:
                    continue
                session_id = str(
                    event.get("session_id") or path.stem
                )
                group = groups.get(key)
                if group is None:
                    group = _FaultGroup(
                        source=source, payload=payload,
                        subject=subject, descriptor=descriptor,
                    )
                    groups[key] = group
                group.events.append((ts, session_id, event))
        return groups

    def _classify(
        self, event: Dict[str, Any],
    ) -> Optional[Tuple[Tuple[str, ...], str, Dict[str, str], str, str]]:
        """``(key, source, payload, subject, descriptor)`` for one event.

        Schema-aware per source (GATE-B Q3). Events missing their source's
        key fields are not usable signals and are skipped — never folded
        into a ``(?, ?)`` bucket.
        """
        event_type = event.get("event_type")
        if event_type == "andon_halt":
            if event.get("source") == "fleet_worker":
                worker = event.get("worker")
                check = event.get("check")
                if not (worker and check):
                    return None
                sig = error_signature(str(event.get("detail") or ""))
                payload = {
                    "source": "fleet_worker",
                    "worker": str(worker),
                    "check": str(check),
                    "error_signature": sig,
                }
                key = ("fleet_worker", str(worker), str(check), sig)
                descriptor = f"{check} fault ({sig})" if sig else f"{check} fault"
                return key, "fleet_worker", payload, str(worker), descriptor
            # silent-degradation-sweep-v1 — component filings (kaizen push,
            # tier ratchet, proposal queue, portal render). Identity is
            # ``(source, check)``; the repr'd exception in ``detail`` is
            # per-instance color, never identity. Missing ``check`` → not a
            # usable signal → skipped (the established missing-field rule).
            component_source = event.get("source")
            if component_source in COMPONENT_SOURCES:
                check = event.get("check")
                if not check:
                    return None
                payload = {
                    "source": str(component_source),
                    "check": str(check),
                    "error_signature": "",
                }
                key = (str(component_source), str(check))
                return (
                    key, str(component_source), payload,
                    str(component_source), f"{check} failure",
                )
            intents = event.get("intents") or []
            tool = None
            if intents and isinstance(intents[0], dict):
                tool = intents[0].get("tool_name")
            matched_rule = event.get("matched_rule")
            if not (tool and matched_rule):
                return None
            payload = {
                "source": "dispatcher_halt",
                "tool": str(tool),
                "matched_rule": str(matched_rule),
                "error_signature": "",
            }
            key = ("dispatcher_halt", str(tool), str(matched_rule), "")
            return key, "dispatcher_halt", payload, str(tool), str(matched_rule)
        if event_type == "red_resolution":
            tool = event.get("triggering_tool")
            matched_rule = event.get("matched_rule")
            if not (tool and matched_rule):
                return None
            payload = {
                "source": "red_resolution",
                "tool": str(tool),
                "matched_rule": str(matched_rule),
                "error_signature": "",
            }
            key = ("red_resolution", str(tool), str(matched_rule), "")
            return (
                key, "red_resolution", payload, str(tool),
                f"RED {matched_rule}",
            )
        return None

    @staticmethod
    def _read_events(path: Path):
        """Yield parsed events from one ledger file; skip malformed lines."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        logger.debug(
                            "[fault_triage] malformed line %d in %s: %r",
                            line_no, path, exc,
                        )
        except OSError as exc:
            logger.debug(
                "[fault_triage] could not read ledger %s: %r", path, exc,
            )

    @staticmethod
    def _parse_timestamp(ts_raw: object) -> Optional[datetime]:
        """ISO-8601 → tz-aware UTC; naive treated as UTC; unparseable → None."""
        if not isinstance(ts_raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(ts_raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    # ── thresholds + suppression ─────────────────────────────────────

    def _meets_threshold(self, group: _FaultGroup) -> bool:
        if len(group.events) < self._thresholds.min_events:
            return False
        sessions = {sid for (_ts, sid, _e) in group.events}
        return len(sessions) >= self._thresholds.min_sessions

    def _latest_dispositions(self) -> Dict[str, Dict[str, Any]]:
        """proposal_id → its most recent ``kaizen_disposition`` event.

        Read from the same ledger glob — the acknowledge/dismiss signal lives
        where every other disposition lives (no new state file).
        """
        latest: Dict[str, Dict[str, Any]] = {}
        if not self._ledger_dir.is_dir():
            return latest
        for path in sorted(self._ledger_dir.glob("*.jsonl")):
            for event in self._read_events(path):
                if event.get("event_type") != "kaizen_disposition":
                    continue
                pid = event.get("proposal_id")
                if not pid:
                    continue
                prior = latest.get(pid)
                if prior is None or str(event.get("timestamp") or "") > str(
                    prior.get("timestamp") or ""
                ):
                    latest[pid] = event
        return latest

    def _suppressed(
        self,
        proposal_id: str,
        group: _FaultGroup,
        dispositions: Dict[str, Dict[str, Any]],
        *,
        window_start: datetime,
    ) -> bool:
        """Honor the operator's last direction for this fault class.

        * dismissed/rejected in-window → suppressed for the remainder of the
          window ("not a real pattern, stop proposing").
        * acknowledged → "keep watching": re-stage only when the class has
          MATERIALLY changed — in-window count exceeds the acknowledged count
          by ``reraise_growth``, OR a new distinct session appeared since the
          ack and the count grew at all.
        """
        disposition_event = dispositions.get(proposal_id)
        if disposition_event is None:
            return False
        disposition = disposition_event.get("disposition")
        disposition_ts = self._parse_timestamp(
            disposition_event.get("timestamp")
        )
        if disposition in ("rejected", "dismissed"):
            return disposition_ts is not None and disposition_ts >= window_start
        if disposition == "acknowledged":
            current = len(group.events)
            raw_count = disposition_event.get("acknowledged_count")
            # A legacy/foreign ack without the recorded count cannot carry
            # the "tell me if it changes" baseline — treat as 0 so the class
            # re-surfaces loudly rather than going quietly dark.
            acked = raw_count if isinstance(raw_count, int) and not isinstance(
                raw_count, bool
            ) else 0
            if current > acked * self._thresholds.reraise_growth:
                return False
            if disposition_ts is not None:
                before = {
                    sid for (ts, sid, _e) in group.events
                    if ts <= disposition_ts
                }
                after = {
                    sid for (ts, sid, _e) in group.events
                    if ts > disposition_ts
                }
                if (after - before) and current > acked:
                    return False
            return True
        return False

    # ── emission ─────────────────────────────────────────────────────

    def _build_proposal(
        self,
        group: _FaultGroup,
        *,
        now: datetime,
        window_start: datetime,
    ) -> RoutingProposal:
        """One fault-class proposal: stable identity, interpreted card body.

        Identity = ``type | payload (fault signature) | evidence (stable
        synthetic token)``. Everything that accumulates — count, spread,
        first/last, samples, the rendered judgment — rides identity-excluded
        fields (``source_patterns`` for the machine-readable audit the
        acknowledge path reads back, ``semantic_justification`` for the
        operator-facing card body).
        """
        events = sorted(group.events, key=lambda item: item[0])
        timestamps = [ts for (ts, _sid, _e) in events]
        sessions = sorted({sid for (_ts, sid, _e) in events})
        count = len(events)
        first_seen = timestamps[0].isoformat()
        last_seen = timestamps[-1].isoformat()

        activity, worsening = derive_activity(
            timestamps, now=now, window_start=window_start,
        )
        judgment = judgment_line(
            group.subject, group.descriptor, activity, worsening,
        )

        # Sampled raw events: first / middle / last (≤3), truncated —
        # evidence for the operator, identity-excluded by construction.
        sample_indexes = sorted({0, len(events) // 2, len(events) - 1})
        samples = []
        for i in sample_indexes:
            raw = json.dumps(events[i][2], sort_keys=True, default=str)
            samples.append(raw[:400])

        # proposal-card-legibility-v1 Phase 2 — the SAME sampled events,
        # normalized per-source into the structured, identity-excluded
        # ``detail`` envelope (date · subject · outcome) so render surfaces
        # never parse the sj text. The sj body below is UNCHANGED — it
        # remains the verbatim fallback source.
        detail = FaultTriageDetail(samples=[
            _normalize_sample(group.source, events[i][0], events[i][2])
            for i in sample_indexes
        ]).to_dict()

        body = (
            f"{judgment}\n"
            f"Seen {count} times across {len(sessions)} session(s) in the "
            f"last {self._thresholds.window_days}d "
            f"(first {first_seen}, last {last_seen}).\n"
            f"Samples: " + " | ".join(samples)
        )

        evidence: Tuple[str, ...] = (
            "fault_triage:" + ":".join(
                group.payload.get(k, "") for k in sorted(group.payload)
            ),
        )
        proposal_id = compute_proposal_id(
            type=PROPOSAL_TYPE_FAULT_TRIAGE,
            payload=group.payload,
            evidence=evidence,
        )
        return RoutingProposal(
            proposal_id=proposal_id,
            type=PROPOSAL_TYPE_FAULT_TRIAGE,
            payload=dict(group.payload),
            evidence=evidence,
            eval_hash="",
            created_at=_now_iso(),
            # Machine-readable accumulating audit (identity-excluded): the
            # acknowledge path reads count=N back as the re-raise baseline.
            source_patterns=(
                f"count={count}",
                f"sessions={len(sessions)}",
                f"first_seen={first_seen}",
                f"last_seen={last_seen}",
                f"activity={activity}",
                f"worsening={str(worsening).lower()}",
            ),
            semantic_justification=body,
            proposer="fault_triage",
            detail=detail,
        )
