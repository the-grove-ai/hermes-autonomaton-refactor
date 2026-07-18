"""Grove Kaizen Ledger — out-of-band structured telemetry per GRV-005 § IX(4).

Sprint 26 Phase 6. The Kaizen Ledger is the persistent, structured,
async-queryable record of operational telemetry the Dispatcher emits
during a session. It is the foreground/background split's *background*
half: every turn-level event that is NOT part of the operator's
active conversational context routes here.

GRV-005 § IX(4) MUSTs realized in this module:

* **Foreground/Background Split.** Upon ``FinalResponse``, the
  Dispatcher decouples the conversational payload (which goes to the
  active context window) from the operational telemetry. Only the
  former is written to the active context; everything else lands here.

* **Kaizen Telemetry Routing.** Generator traces (which intent batches
  were yielded), intent metadata (tool names, args, call_ids), tool
  latencies, Andon halts and their disposition outcomes, and final
  response token counts all route out-of-band to this ledger.

* **No Mid-Stream Injection.** The ledger is write-only from the
  Dispatcher's perspective during a turn. The Agent's reasoning loop
  has no read access. Future Skill Flywheel queries (v0.2) run
  asynchronously against the persisted ledger, never against the
  in-flight session's state.

* **Skill Flywheel Interface.** The ledger persists as JSON Lines
  (``.jsonl``) — one event per line, append-only, structured fields.
  Offline pattern recognition tools (the Sprint 06b Curator, future
  Skill Flywheel detectors) can stream-read the ledger without
  blocking the runtime.

Storage layout: ``~/.grove/.kaizen_ledger/<session_id>.jsonl``. The
session-id-per-file split keeps query scopes narrow and lets
operators inspect a single session's ledger without parsing a
shared multi-session file.

The authoritative set of event types is ``KaizenLedger.EVENT_TYPES``, enforced
fail-loud in :meth:`record`. It has grown well beyond the original Phase-6 six
as the governance surface expanded; consult that frozenset for the exact,
current list. Broadly the ledger records per-turn lifecycle (``final_response``),
tool-set selection (``tool_selection``), Andon halts and their resolutions
(``andon_halt`` / ``andon_disposition`` / ``red_resolution``), tier and routing
changes (``tier_override`` / ``tier_fallback`` / ``routing_config_mutation``),
governance and capability writes (``governance_change`` /
``capability_binding_mutation`` / ``grant_execution``), skill-flywheel
dispositions, containment and write-confinement events, and session-cache
telemetry.

Green tool executions are NOT recorded here: the capability feed
(``grove/capability_feed.py``, per-invocation) has been the sole path for
invocation usage since GRV-009 E3 C4 (12438f1b6 retired ``tool_batch_executed``).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["KAIZEN_LEDGER_DIRNAME", "KaizenLedger", "default_ledger_dir"]


# kaizen-ledger-retention-v1 P1 — the ONE spelling of the ledger directory
# name. Every production construction site consumes default_ledger_dir();
# the dotted literal appears nowhere else.
KAIZEN_LEDGER_DIRNAME = ".kaizen_ledger"


def default_ledger_dir() -> Path:
    """Resolve ``~/.grove/.kaizen_ledger`` via the standard hermes_home."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / KAIZEN_LEDGER_DIRNAME


class KaizenLedger:
    """Append-only structured event log for a session.

    One ledger per session. The Dispatcher constructs a ledger when
    ``dispatch_turn`` first runs for a session; subsequent turns reuse
    the same ledger instance and append to the same file.

    The ledger is thread-safe via a single ``threading.Lock`` around
    appends. Reads (``events`` / ``events_by_type``) are not locked;
    the jsonl format tolerates concurrent read-while-write because
    each event is one complete line and ``open(..., "a")`` writes are
    atomic for short records on POSIX.

    Operators inspect a session's ledger with ``jq`` or similar:

        cat ~/.grove/.kaizen_ledger/<session_id>.jsonl | jq .
        jq 'select(.event_type == "andon_halt")' < ...

    Phase 7 cleanup may wire a `/kaizen` slash command to surface the
    ledger interactively.
    """

    EVENT_TYPES = frozenset({
        # ledger-eventtype-hygiene-v1 retirements (registrations removed; both
        # emitters are already gone, so nothing can file them):
        #   * tool_batch_executed — retired by 12438f1b6 (GRV-009 E3 C4): the
        #     capability feed is the sole path for invocation usage.
        #   * turn_dropped — retired by e46de6efb: the operator "Drop" disposition
        #     branch (and its emitter) was removed with the v1.0 disposition aliases.
        "andon_halt",
        "andon_disposition",
        "final_response",
        "tier_override",
        # Sprint 29 Phase 2 — per-turn tool-set selection. Dispatcher
        # writes this after _maybe_apply_tool_filter runs in the agent,
        # capturing intent_class + complexity + selected/full counts
        # plus the fallback flag so the operator can audit how often
        # the optimizer fell back to the full registry.
        "tool_selection",
        # Sprint 30 — per-EscalationRequest outcome. Dispatcher writes
        # this in _handle_escalation_request with the granted flag,
        # the declarative request (depth/context/blocker), the policy
        # decision reason, current/target tiers, and the per-turn /
        # per-session escalation counters. Both grant and deny land
        # here so the operator can audit how often the Agent asked
        # for capacity and how often the policy said yes.
        "escalation_decision",
        # Sprint 53.2 — skill quarantine pipeline. quarantine_skill_disposition
        # is the additive, skill-scoped signal `flywheel approve --strict`
        # reads to confirm a quarantined skill actually ran under "allow
        # once" (GATE-A decision 2 — kept separate from andon_disposition,
        # which is tool-scoped). The rest record the post-execution prompt
        # outcome and the promotion/denial acts for the operator's audit.
        "quarantine_skill_disposition",
        "post_execution_kaizen",
        "skill_promotion_queued",
        "skill_promotion_denied",
        "skill_promoted",
        # skill-adoption-v1 C1 — load-side primacy resolution Andon. Fired by the
        # capability_registry primacy-map builder when a primacy claim cannot be
        # honored: reason="subset_violation" (a primary_intents entry is not a
        # subset of trigger.intents — that intent is dropped from the claim), or
        # reason="collision" (two+ ENABLED records claim primacy for one intent
        # class — ALL are demoted, no tie-break). Never a boot failure: the map
        # degrades, the gateway loads. Payload carries the intent_class + the
        # offending record_id(s)/slug(s).
        "skill_primacy_collision",
        # skill-adoption-v1 C2 — compose-time payload integrity failure. The
        # skill_payload provider computed a primary skill's payload but an
        # integrity gate failed: reason="body_hash" (sha256(context.payload) !=
        # lifecycle.body_hash — the committed definition anchor) or
        # reason="promotion_pin" (a promoted skill's approved_payload_sha256 pin
        # no longer matches the active SKILL.md). Either → the payload is NOT
        # injected (fail-closed, nudge-only stands). Payload carries slug +
        # record_id + reason.
        "skill_payload_integrity_violation",
        # skill-adoption-v1 C5b — SYSTEM-DERIVED contract-execution provenance. The
        # governed write path saw a write land inside the ACTIVE primary skill's
        # declared write_zone while that skill's payload was in context — i.e. the
        # skill executed its contract (wrote its artifact to its sink). Derived
        # purely from the C3 active-primary tracker + the record's governance
        # write_zone; NO model-authored tags are consulted. Payload: slug + path +
        # turn_id.
        "contract_execution",
        # GRV-010 C1b — a governance-config change written through the
        # propose_governance_change Stage-04 door (rationale + diff hashes +
        # disposition). The paired andon_disposition entry carries the precise
        # once/session/always verdict.
        "governance_change",
        # GRV-010 C2d — governed tier downshift. The current tier's model was
        # unreachable and the tier declared a fallback_tier; the Dispatcher
        # re-routed the turn through the Cognitive Router at the fallback tier.
        # Carries failed_tier / fallback_tier / provider / model / reason.
        "tier_fallback",
        # GRV-005 §VI (kaizen-voice Sprint B1) — a RED workflow RESOLUTION. RED
        # severs the temporal dispositions, so it records here instead of
        # andon_disposition: resolution (cancel / descoped) + zone + matched_rule
        # + triggering_tool. The vocabulary moves; the volume is preserved — every
        # RED halt that formerly emitted one andon_disposition now emits one
        # red_resolution.
        "red_resolution",
        # learning-loop-bridge-v1 (Strike 2) — the operator's disposition on a
        # queued Flywheel proposal. Written at flywheel_cli.cli_approve /
        # cli_reject for EVERY proposal type at the single registry-dispatch
        # boundary, so a proposal no longer vanishes silently on approval.
        # Carries proposal_id + proposal_type + disposition (applied/rejected)
        # + evidence_count, and for applied the applied_result dict, for
        # rejected the optional reason.
        "kaizen_disposition",
        # binding-governance-surfaces-v1 — a model_binding write through the
        # sanctioned CapabilityBindingWriter (capability_registry.
        # set_model_binding). The writer files this ITSELF on success
        # (adjudication R5 — config writers must not audit by backup+logger
        # alone). Carries skill + record_id + previous_binding + new_binding
        # + surface (portal / proposal_apply / …) + proposal_id (null unless
        # the write is a proposal apply).
        "capability_binding_mutation",
        # Sprint 50 — routing config mutations through the sanctioned writer
        # (RoutingConfigWriter.apply_mutation). Written by the writer itself
        # AFTER the atomic swap lands, so the ledger entry is an audit trail
        # that the config file moved independently of any session's tool calls.
        # containment Phase-1 step-4: restored here — grove/config/routing_writer.py
        # files this type but origin/main's allowlist omitted it (latent bug: every
        # routing mutation hit the error-floor). Was an uncommitted VM hand-patch;
        # committed inline so the code deploy does not regress routing-config filing.
        "routing_config_mutation",
        # execute-code-meta-surface-containment-v1 Phase-1 — a sandboxed write that
        # hit the kernel read-only governance boundary (repo config/ tree,
        # ReadOnlyPaths). Filed by tools/code_execution_tool.py on EROFS detection.
        # Carries target + boundary_class + errno + tool + exit_code.
        "containment_violation",
        # execute-code-meta-surface-containment-v1 Phase-2 Change 2 — a bucket-3
        # UNRESOLVED_WRITER RED that was dropped (headless Cancel/De-scope) on an
        # UNREACHABLE surface (no operator to approve). Filed by _resolve_red_halt so
        # a silently-cancelled fail-closed write is observable. Carries resolution +
        # pattern_key + triggering_tool + surface.
        "headless_governance_block",
        # execute-code-meta-surface-containment-v1 Phase-2 Change 3 — attempt-stamp
        # for an escalated shell write, filed BEFORE execution (YELLOW) or at store
        # time (RED). Carries actor + surface + write_target + write_class +
        # pattern_key + resolution + grant_id. Diagnostic-grade until the broker
        # sprint hardens the ledger to append-only.
        "escalated_write_attempt",
        # ledger-eventtype-hygiene-v1 — three emitters that shipped without an
        # allowlist entry, so every emission hit the fail-loud floor (ValueError,
        # swallowed by the caller's try/except → the ledger entry was silently
        # dropped). Registered here to close the orphan class.
        #
        # write-confinement-v1 (babd703f5) — a write batch refused by the single
        # write-confinement evaluator before dispatch. Emitted grove/dispatcher.py:4609.
        "write_confinement_refusal",
        # grant provenance stamp — a scope-defining execution authorized by a
        # standing/operator grant. Emitted grove/dispatcher.py:5790.
        "grant_execution",
        # T0/session pattern-cache hit telemetry. Emitted grove/dispatcher.py:6288.
        "session_cache_hit",
    })

    def __init__(self, session_id: str, ledger_dir: Optional[Path] = None) -> None:
        """Initialize the ledger for one session.

        Args:
            session_id: unique session identifier. Used as the ledger
                filename stem after sanitization (alphanumeric + ``-_``,
                truncated to 128 chars).
            ledger_dir: directory holding ledger files. Defaults to
                ``~/.grove/.kaizen_ledger/``. Tests pass a tmp path.
        """
        if ledger_dir is None:
            ledger_dir = default_ledger_dir()
        self._dir = Path(ledger_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_id = str(session_id)
        safe_id = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in self._session_id
        )[:128]
        self._path = self._dir / f"{safe_id}.jsonl"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The ledger file path for this session."""
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    def record(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        """Append one event to the ledger and return the persisted event dict.

        Per GRV-005 § IX(4) "No Mid-Stream Injection": this method is
        write-only from the runtime's perspective. The returned dict is
        a courtesy for callers that want to forward the same payload to
        the standard logger or attach it to a return value — the ledger
        is the source of truth.

        Args:
            event_type: one of ``KaizenLedger.EVENT_TYPES``. Unknown
                event types raise ValueError (fail loud per the
                Architectural Prime Directive — silently accepting
                unknown event types would let typos accumulate as
                untyped ledger entries).
            **fields: structured payload. Values must be JSON-serializable.
                Reserved keys ``event_type``, ``session_id``,
                ``timestamp`` are populated by this method; passing them
                in raises ValueError.
        """
        if event_type not in self.EVENT_TYPES:
            raise ValueError(
                f"unknown kaizen event_type {event_type!r}; "
                f"expected one of {sorted(self.EVENT_TYPES)}"
            )
        reserved = {"event_type", "session_id", "timestamp"}
        collisions = reserved & set(fields.keys())
        if collisions:
            raise ValueError(
                f"reserved fields cannot be overridden: "
                f"{sorted(collisions)}"
            )
        event: Dict[str, Any] = {
            "event_type": event_type,
            "session_id": self._session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(event, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return event

    def events(self) -> Iterator[Dict[str, Any]]:
        """Stream the ledger's events in append order.

        Each event is a dict parsed from one line of the jsonl file.
        Malformed lines are skipped (logged at debug); the runtime
        prefers a partial read over crashing on a corrupt entry.
        """
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "[grove.kaizen_ledger] malformed event line %d "
                        "in %s: %r",
                        line_no, self._path, exc,
                    )

    def events_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        """Return all events of one type for this session.

        Convenience wrapper around ``events()`` with a type filter.
        Returns a materialized list — for very long sessions the
        async-queryable Skill Flywheel pipeline should use ``events()``
        directly to stream.
        """
        return [e for e in self.events() if e.get("event_type") == event_type]
