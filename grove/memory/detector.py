"""Context Persistence Detector — the T1 Haiku memory extractor.

Runs on dormant sessions (swept by the Implicit Success Sweep, Phase 3) and
crystallizes tacit operator knowledge into staged Kaizen proposals. It never
writes the active memory graph — it stages proposals the operator reviews.

The T1 call reuses the T-telemetry tier binding from ``routing.config.yaml``
(``grove.classify._telemetry_tier_runtime``) so the detector rides the same
cheap-cognition tier as the classifier, and shares its spend tracker.

Idempotency (Gemini GB-1 + ratification hardening): a per-session processing
lock is written to ``memory_proposals.jsonl`` BEFORE the model call. A second
sweep of the same session — even across a crash — sees the lock (or staged
``pending`` proposals) and returns 0, so a dormant session is extracted once
and only once.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from grove.memory.store import MemoryStore
from grove.memory.transcript_filter import filter_transcript_for_extraction

logger = logging.getLogger(__name__)

__all__ = ["ContextPersistenceDetector", "DETECTOR_SYSTEM_PROMPT"]


# Room for up to three proposals with justifications.
_MAX_OUTPUT_TOKENS = 1500

# Active-index summary caps for the prompt budget.
_MAX_INDEX_RECORDS = 50
_INDEX_CONTENT_CHARS = 200

# Hard ceiling on proposals per session (SPEC budget rule 5).
_MAX_PROPOSALS = 3

# Statuses that mark a session as already handled — the idempotency anchor.
_BLOCKING_STATUSES = frozenset({"processing", "pending"})

# memory-operational-hardening-v1:
#   Fix 3 — minimum operator messages for a session to be worth extracting.
_MIN_USER_MESSAGES = 3
#   Fix 2 — only rejected proposals newer than this feed the rejection-memory
#   block (older ones are stale; the operator may have changed their mind).
_REJECTION_LOOKBACK_DAYS = 30
# Annotation marking an index record already targeted by a staged supersede.
_PENDING_SUPERSESSION_MARK = " [PENDING SUPERSESSION]"


def _session_worth_extracting(transcript: List[Dict[str, Any]]) -> bool:
    """Return True if the session has enough substance to extract from.

    Fix 3 (minimum complexity gate). Runs on the FILTERED transcript so it
    counts real operator turns, not stripped tool output. A trivial session
    (one Q&A, no tools) is not worth a T1 call.
    """
    user_messages = [m for m in transcript if m.get("role") == "user"]
    assistant_messages = [m for m in transcript if m.get("role") == "assistant"]
    has_tool_use = any(m.get("tool_calls") for m in assistant_messages)
    return len(user_messages) >= _MIN_USER_MESSAGES or has_tool_use


# Phase 2 base verbatim; memory-operational-hardening-v1 appended rules 7
# (REJECTION MEMORY) and 8 (PENDING MUTATIONS). Do not modify otherwise.
DETECTOR_SYSTEM_PROMPT = """\
You are the Context Persistence Detector for a strictly governed
Autonomaton. Your job: read the session transcript and crystallize
tacit operator knowledge, preferences, and project states into
structured MemoryRecords.

You do not write to the active memory graph. You generate Kaizen
proposals that the operator will review.

You also receive the operator's active Dock goals — their declared
strategic priorities. Observations relating to active goals are
higher-value than general observations. Use the goals to sharpen
entity_type assignment:
- Observations tied to a specific Dock goal with milestones = ProjectState
- General knowledge not tied to a goal = DomainFact

Extraction Rules:
1. Extract factual domain knowledge, explicit operator preferences,
   and definitive project state changes.
2. Ignore ephemeral troubleshooting, transient bugs, pleasantries.
3. If a new observation contradicts an existing record in the
   active_memory_index, draft a "supersede" proposal citing the old ID.
4. Confidence scores (0.0-1.0):
   - Explicit operator directives = 0.9+
   - Inferred preferences = 0.5-0.7
   - Observed patterns (not stated) = 0.3-0.5
5. STRICT BUDGET: AT MOST 3 proposals. Rank by value, cut. Quality
   over quantity. Prefer goal-related observations over general ones.
6. If a proposal relates to a Dock goal, set dock_goal_ref to the
   goal slug.
7. REJECTION MEMORY: You are also provided recently_rejected_proposals.
   Do not propose facts or preferences that logically match or closely
   resemble any rejected proposal. The operator has already reviewed and
   declined these. Proposing similar content wastes their time.
8. PENDING MUTATIONS: Records marked [PENDING SUPERSESSION] in the
   active_memory_index are already targeted for update by a staged
   proposal. Do not propose conflicting changes to these records.

Output ONLY valid JSON:
{"proposals": [{"action": "create"|"supersede",
  "target_id": "mem_xxx"|null, "dock_goal_ref": "goal-slug"|null,
  "proposed_record": {"entity_type": "DomainFact"|"OperatorPreference"|
  "ProjectState"|"ArchitecturalRule", "content": "Standalone statement.",
  "confidence": 0.85, "justification": "Why this matters."}}]}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # drop the opening fence (``` or ```json) and a closing fence if present
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class ContextPersistenceDetector:
    """Stage memory proposals from a dormant session's transcript."""

    def __init__(self, store: MemoryStore, base_dir: Path) -> None:
        self._store = store
        self._proposals_path = Path(base_dir) / "memory_proposals.jsonl"

    @property
    def proposals_path(self) -> Path:
        return self._proposals_path

    def detect_and_stage(
        self,
        session_id: str,
        transcript: List[Dict[str, Any]],
        active_dock_goals: List[Dict[str, Any]],
    ) -> int:
        """Extract and stage proposals for ``session_id``. Returns the count.

        Returns 0 if the session was already processed (idempotency), the
        transcript yielded nothing, or the model returned no usable
        proposals.
        """
        # 1. Idempotency check — skip if a lock or pending proposal exists.
        if self._already_processed(session_id):
            logger.debug(
                "[grove.memory.detector] session %s already processed; skipping",
                session_id,
            )
            return 0

        # 2. Deterministic pre-filter.
        filtered = filter_transcript_for_extraction(transcript)

        # 3. Fix 3 — minimum complexity gate (on the FILTERED transcript).
        #    Below threshold: no processing lock, no T1 call.
        if not _session_worth_extracting(filtered):
            user_count = sum(1 for m in filtered if m.get("role") == "user")
            has_tools = any(m.get("tool_calls") for m in filtered)
            logger.debug(
                "[grove.memory.detector] session %s below extraction threshold "
                "(%d user messages, tools_used=%s). Skipping.",
                session_id, user_count, has_tools,
            )
            return 0

        # 4. Write the processing lock BEFORE the model call.
        self._append_record({
            "session_id": session_id,
            "status": "processing",
            "timestamp": _now_iso(),
        })

        # 5. Active-index summary, annotated with staged supersessions (Fix 4).
        pending_supersede_ids = self._pending_supersession_target_ids()
        active_summary = self._active_index_summary(
            pending_supersede_ids=pending_supersede_ids,
        )

        # 6. Dock-goals summary.
        dock_summary = [
            {"slug": g.get("slug"), "name": g.get("name"), "status": g.get("status")}
            for g in active_dock_goals
        ]

        # 7. Recently-rejected proposals (Fix 2 — rejection memory).
        recently_rejected = self._recently_rejected()

        # 8. T1 Haiku call (mockable seam).
        raw = self._call_detector(
            filtered, active_summary, dock_summary, recently_rejected,
        )

        # 9. Parse (markdown-fence tolerant; malformed → 0).
        proposals = self._parse_proposals(raw)

        # 10. Stage each proposal as pending.
        for proposal in proposals:
            self._append_record({
                "session_id": session_id,
                "status": "pending",
                "timestamp": _now_iso(),
                "proposal": proposal,
            })

        return len(proposals)

    # ── idempotency + proposals file ─────────────────────────────────────

    def _read_records(self) -> List[Dict[str, Any]]:
        if not self._proposals_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for line in self._proposals_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[grove.memory.detector] malformed proposals line: %r", exc
                )
        return records

    def _already_processed(self, session_id: str) -> bool:
        for rec in self._read_records():
            if rec.get("session_id") == session_id and \
                    rec.get("status") in _BLOCKING_STATUSES:
                return True
        return False

    def _append_record(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        self._proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._proposals_path, "a", encoding="utf-8") as fh:
            fh.write(line)

    # ── prompt inputs ────────────────────────────────────────────────────

    def _active_index_summary(
        self, *, pending_supersede_ids: frozenset = frozenset(),
    ) -> List[Dict[str, Any]]:
        summary: List[Dict[str, Any]] = []
        for rec in self._store.projected_records().values():
            if rec.status != "active":
                continue
            content = rec.content[:_INDEX_CONTENT_CHARS]
            # Fix 4 — flag records already targeted by a staged supersession
            # so T1 does not draft a conflicting change.
            if rec.id in pending_supersede_ids:
                content += _PENDING_SUPERSESSION_MARK
            summary.append({
                "id": rec.id,
                "content": content,
                "entity_type": rec.entity_type,
            })
            if len(summary) >= _MAX_INDEX_RECORDS:
                break
        return summary

    def _pending_supersession_target_ids(self) -> frozenset:
        """Record ids targeted by a staged (pending/processing) supersede.

        Fix 4 — a supersede staged but not yet approved still shows its
        target as ``active`` in the index; without this the detector would
        draft a second, conflicting supersession.
        """
        ids = set()
        for rec in self._read_records():
            if rec.get("status") not in _BLOCKING_STATUSES:
                continue
            proposal = rec.get("proposal")
            if not isinstance(proposal, dict):
                continue
            if proposal.get("action") == "supersede" and proposal.get("target_id"):
                ids.add(proposal["target_id"])
        return frozenset(ids)

    def _recently_rejected(
        self, within_days: int = _REJECTION_LOOKBACK_DAYS,
    ) -> List[Dict[str, Any]]:
        """Lightweight summary of proposals rejected within ``within_days``.

        Fix 2 — fed to T1 so it stops re-proposing what the operator already
        declined. Uses the proposal record's timestamp (the staging time,
        which run_digest preserves on the reject flip).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=within_days)
        ).isoformat()
        out: List[Dict[str, Any]] = []
        for rec in self._read_records():
            if rec.get("status") != "rejected":
                continue
            if rec.get("timestamp", "") < cutoff:
                continue
            proposal = rec.get("proposal")
            if not isinstance(proposal, dict):
                continue
            proposed = proposal.get("proposed_record", {})
            out.append({
                "content": proposed.get("content"),
                "entity_type": proposed.get("entity_type"),
            })
        return out

    # ── T1 call ──────────────────────────────────────────────────────────

    def _call_detector(
        self,
        filtered_transcript: List[Dict[str, Any]],
        active_memory_index: List[Dict[str, Any]],
        active_dock_goals: List[Dict[str, Any]],
        recently_rejected_proposals: List[Dict[str, Any]],
    ) -> str:
        """Make the T1 Haiku extraction call; return the raw assistant text.

        Reuses the T-telemetry tier binding and spend tracker from
        ``grove.classify`` so the detector rides the same cheap tier as the
        classifier. API/router errors propagate (fail loud) — the only
        commanded graceful degradation is malformed JSON (handled in
        :meth:`_parse_proposals`).
        """
        from agent.anthropic_adapter import build_anthropic_client
        from grove.classify import _telemetry_tier_runtime, _track_cost

        runtime, tier_config = _telemetry_tier_runtime()
        client = build_anthropic_client(
            api_key=runtime.get("api_key") or "",
            base_url=runtime.get("base_url") or None,
        )
        user_payload = json.dumps({
            "transcript": filtered_transcript,
            "active_memory_index": active_memory_index,
            "active_dock_goals": active_dock_goals,
            "recently_rejected_proposals": recently_rejected_proposals,
        })
        response = client.messages.create(
            model=runtime["model"],
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=DETECTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_payload}],
        )
        _track_cost(response.usage, tier_config=tier_config)
        texts = [
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        return "".join(texts)

    # ── parse ────────────────────────────────────────────────────────────

    def _parse_proposals(self, raw: str) -> List[Dict[str, Any]]:
        if not isinstance(raw, str):
            logger.warning(
                "[grove.memory.detector] T1 returned non-text; staging 0"
            )
            return []
        text = _strip_code_fences(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "[grove.memory.detector] malformed T1 JSON; staging 0 proposals"
            )
            return []
        if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
            logger.warning(
                "[grove.memory.detector] T1 response missing proposals array; "
                "staging 0"
            )
            return []
        proposals = data["proposals"]
        if len(proposals) > _MAX_PROPOSALS:
            logger.warning(
                "[grove.memory.detector] T1 returned %d proposals; "
                "truncating to %d", len(proposals), _MAX_PROPOSALS,
            )
            proposals = proposals[:_MAX_PROPOSALS]
        return proposals
