"""learning-loop-bridge-v1 (Strike 2) — YELLOW promotion detector.

The dark path this closes (GATE-A Q8 / DARK Q8): the system records every
YELLOW ``andon_disposition`` the operator answers, but never reads them back
as a promotion signal. An operator who approves the same governed
``(tool, rule)`` at the Sovereign Prompt over and over is telling the system
the rule is too strict — yet the system keeps asking. This detector reads the
Kaizen ledger's ``andon_disposition`` events across sessions and, when the
operator has repeatedly approved the same ``(triggering_tool, matched_rule)``,
queues a *system-initiated* ``zone_promotion`` proposal so the operator can
promote it to green through the existing Flywheel CLI — and the system stops
re-asking.

Why a sibling detector and not a TierRatchet field (GATE-A A2): TierRatchet
aggregates ``IntentRecord``s from the intent store; the disposition signal
lives in a *different* store (the Kaizen ledger, append-only JSONL per
session). The clean integration is a separate ledger-reading detector that
reuses the proposal-emission tail (``RoutingProposal`` +
``compute_proposal_id`` + ``proposal_queue.append``) and the existing
``_approve_zone_promotion`` handler — not a refactor of TierRatchet's intake.

Proposal identity (dedup): the ``proposal_id`` is content-addressed over
``type | payload | evidence`` (``proposal_queue.compute_proposal_id``). To
keep one stable proposal per ``(tool, pattern)`` no matter how many approvals
accumulate, ``evidence`` is a stable synthetic token derived from the key, and
the accumulating audit detail (count, sessions, contributing dispositions)
rides in ``source_patterns`` — which ``compute_proposal_id`` excludes from the
id by design. A re-run over the same ledger state dedups; a new approval grows
the audit without stacking a duplicate.

Thresholds are declarative (Pattern v1.3 Commitment 1): the operator edits
``~/.grove/flywheel.config.yaml`` (template in ``config/flywheel.config.yaml``)
rather than a hardcoded module constant — the GATE-A critique of TierRatchet's
``MIN_SAMPLE = 5`` style constants. Wall-clock is injectable (``now``) so the
window is testable without monkeypatching ``datetime``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    _now_iso,
    compute_proposal_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PromotionThresholds",
    "load_promotion_thresholds",
    "DispositionPromotionDetector",
]


# An approval disposition is a durable permission grant the operator chose at
# the Sovereign Prompt. "once" is a single-turn grant (not a standing signal);
# "deny" is a veto. Only session/always count toward promotion.
_APPROVAL_DISPOSITIONS = frozenset({"session", "always"})
_VETO_DISPOSITION = "deny"


@dataclass(frozen=True)
class PromotionThresholds:
    """Declarative thresholds for the YELLOW promotion detector.

    Defaults are the documented baseline (``config/flywheel.config.yaml``).
    They are NOT a silent fallback that hides a failure: an absent operator
    config means "use the specified default", while a present-but-invalid
    value fails loud in :func:`load_promotion_thresholds`.
    """

    count: int = 3
    sessions: int = 2
    window_days: int = 30


def _require_positive_int(block: Dict[str, object], key: str, default: int) -> int:
    """Read ``key`` from a present config block, fail loud on a bad value.

    Absent key → documented default (nothing failed). Present key → must be
    an ``int`` (not ``bool``) and ``>= 1``; anything else raises with the key
    name and the constraint so the operator can fix the file.
    """
    if key not in block:
        return default
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"flywheel.config.yaml disposition_promotion.{key} must be an "
            f"integer, got {value!r} ({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(
            f"flywheel.config.yaml disposition_promotion.{key} must be "
            f">= 1, got {value}."
        )
    return value


def load_promotion_thresholds(
    config_path: Optional[Path] = None,
) -> PromotionThresholds:
    """Load thresholds from the operator's ``flywheel.config.yaml``.

    Resolution: an absent file or absent ``disposition_promotion`` block uses
    the documented defaults. A present block is validated key-by-key and any
    malformed value (wrong type, out of range, or the cross-field invariant
    ``threshold_sessions <= threshold_count``) raises LOUD — there is no
    silent fill of a broken block. Malformed YAML propagates from the parser.
    """
    if config_path is None:
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return PromotionThresholds()

    import yaml

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return PromotionThresholds()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_path} must be a YAML mapping, got "
            f"{type(raw).__name__}."
        )
    block = raw.get("disposition_promotion")
    if block is None:
        return PromotionThresholds()
    if not isinstance(block, dict):
        raise ValueError(
            f"{config_path} disposition_promotion must be a mapping, got "
            f"{type(block).__name__}."
        )

    count = _require_positive_int(block, "threshold_count", 3)
    sessions = _require_positive_int(block, "threshold_sessions", 2)
    window_days = _require_positive_int(block, "window_days", 30)
    if sessions > count:
        raise ValueError(
            f"flywheel.config.yaml disposition_promotion.threshold_sessions "
            f"({sessions}) cannot exceed threshold_count ({count}) — a "
            f"promotion can never reach the session floor."
        )
    return PromotionThresholds(
        count=count, sessions=sessions, window_days=window_days,
    )


@dataclass
class _GroupState:
    """Accumulated ledger evidence for one ``(tool, matched_rule)`` key."""

    approvals: List[Tuple[str, str]]  # (session_id, timestamp_iso)
    veto_count: int


class DispositionPromotionDetector:
    """Reads the Kaizen ledger and proposes zone promotions from repeated
    YELLOW approvals.

    Cross-session by construction: an operator may approve the same rule in
    different sessions, and each session writes its own ledger file. The
    detector globs every ``*.jsonl`` under ``ledger_dir`` (the established
    cross-session read pattern from ``_has_successful_quarantine_execution``).
    """

    def __init__(
        self,
        *,
        ledger_dir: Optional[Path] = None,
        thresholds: Optional[PromotionThresholds] = None,
    ) -> None:
        if ledger_dir is None:
            from hermes_constants import get_hermes_home
            ledger_dir = Path(get_hermes_home()) / ".kaizen_ledger"
        self._ledger_dir = Path(ledger_dir)
        self._thresholds = thresholds or PromotionThresholds()

    def detect(self, *, now: Optional[datetime] = None) -> List[RoutingProposal]:
        """Return the zone_promotion proposals the current ledger state earns.

        ``now`` is injectable for testing; production passes the real UTC
        clock. Proposals are returned in deterministic (sorted-key) order so a
        re-run yields a stable sequence.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        groups = self._aggregate(now=now)
        proposals: List[RoutingProposal] = []
        for (tool, matched_rule) in sorted(groups.keys()):
            state = groups[(tool, matched_rule)]
            if not self._meets_threshold(state):
                continue
            proposals.append(self._build_proposal(tool, matched_rule, state))
        return proposals

    # ── ledger intake ────────────────────────────────────────────────

    def _aggregate(
        self, *, now: datetime,
    ) -> Dict[Tuple[str, str], _GroupState]:
        """Group in-window andon_disposition events by (tool, matched_rule).

        Malformed JSON lines are skipped at debug (the operator-facing ledger
        must not crash a scan on one corrupt entry — matching the established
        ledger/queue read tolerance). A line that parses but is missing a
        required field is simply not a usable signal and is skipped.
        """
        window_start = now - timedelta(days=self._thresholds.window_days)
        groups: Dict[Tuple[str, str], _GroupState] = {}
        if not self._ledger_dir.is_dir():
            return groups

        for path in sorted(self._ledger_dir.glob("*.jsonl")):
            for event in self._read_events(path):
                if event.get("event_type") != "andon_disposition":
                    continue
                tool = event.get("triggering_tool")
                matched_rule = event.get("matched_rule")
                disposition = event.get("disposition")
                session_id = event.get("session_id")
                ts_raw = event.get("timestamp")
                if not (tool and matched_rule and disposition
                        and session_id and ts_raw):
                    continue
                # "default" is not a promotable pattern — it means the tool
                # fell to the default YELLOW with no specific rule, so there
                # is no green rule to write. Skip it loudly-by-omission.
                if matched_rule == "default":
                    continue
                event_time = self._parse_timestamp(ts_raw)
                if event_time is None or event_time < window_start:
                    continue

                key = (tool, matched_rule)
                state = groups.get(key)
                if state is None:
                    state = _GroupState(approvals=[], veto_count=0)
                    groups[key] = state
                if disposition in _APPROVAL_DISPOSITIONS:
                    state.approvals.append((session_id, str(ts_raw)))
                elif disposition == _VETO_DISPOSITION:
                    state.veto_count += 1
                # "once" and any other value are neither approval nor veto.
        return groups

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
                            "[disposition_promotion] malformed line %d in "
                            "%s: %r", line_no, path, exc,
                        )
        except OSError as exc:
            logger.debug(
                "[disposition_promotion] could not read ledger %s: %r",
                path, exc,
            )

    @staticmethod
    def _parse_timestamp(ts_raw: object) -> Optional[datetime]:
        """Parse an ISO-8601 ledger timestamp into a tz-aware UTC datetime.

        A naive timestamp is treated as UTC (the ledger writes
        ``datetime.now(timezone.utc).isoformat()``, so this is belt-and-
        suspenders for hand-edited files). An unparseable timestamp returns
        None and the event is skipped — it cannot be windowed.
        """
        if not isinstance(ts_raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(ts_raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    # ── threshold + emission ─────────────────────────────────────────

    def _meets_threshold(self, state: _GroupState) -> bool:
        """A (tool, rule) earns a proposal when the operator has approved it
        enough, across enough sessions, with no veto in the window."""
        if state.veto_count > 0:
            return False
        if len(state.approvals) < self._thresholds.count:
            return False
        distinct_sessions = {sid for (sid, _ts) in state.approvals}
        if len(distinct_sessions) < self._thresholds.sessions:
            return False
        return True

    def _build_proposal(
        self, tool: str, matched_rule: str, state: _GroupState,
    ) -> RoutingProposal:
        """Construct the zone_promotion proposal for one earned key.

        Built directly (not via ``build_zone_promotion_proposal``) because
        that helper derives the pattern from a raw command string for the live
        "Always" path; here the pattern IS the already-matched rule, and the
        evidence is an aggregate, not a single turn (GATE-A A2). The payload
        shape matches what ``_approve_zone_promotion`` consumes verbatim.
        """
        distinct_sessions = sorted({sid for (sid, _ts) in state.approvals})
        payload = {
            "tool": tool,
            "pattern": matched_rule,
            "zone": "green",
            # Stable, count-free reason → stable proposal_id per (tool, rule).
            "reason": (
                f"System-proposed: repeated YELLOW approvals for {tool} "
                f"({matched_rule}) — promote to green so the system stops "
                f"re-asking."
            ),
        }
        # Stable synthetic evidence token → one proposal per (tool, rule),
        # independent of how many approvals accrue.
        evidence: Tuple[str, ...] = (
            f"disposition_promotion:{tool}:{matched_rule}",
        )
        # The accumulating audit lives in source_patterns, which
        # compute_proposal_id excludes from identity — so new approvals enrich
        # the lineage without minting a duplicate proposal. zone_promotion
        # does NOT set requires_source_patterns, so this never trips the B2
        # no-cluster gate.
        source_patterns: Tuple[str, ...] = (
            f"approvals={len(state.approvals)}",
            f"sessions={len(distinct_sessions)}",
            f"window_days={self._thresholds.window_days}",
        ) + tuple(
            f"{sid}@{ts}" for (sid, ts) in sorted(state.approvals)
        )
        proposal_id = compute_proposal_id(
            type=PROPOSAL_TYPE_ZONE_PROMOTION,
            payload=payload,
            evidence=evidence,
        )
        return RoutingProposal(
            proposal_id=proposal_id,
            type=PROPOSAL_TYPE_ZONE_PROMOTION,
            payload=payload,
            evidence=evidence,
            eval_hash="",
            created_at=_now_iso(),
            source_patterns=source_patterns,
            proposer="disposition_promotion",  # proposal-proposer-attribution-v1 (#11)
        )
