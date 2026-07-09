"""dock-as-mutation-target-v1 — the Memory→Dock proposal detector.

Closes the second half of the Memory↔Dock loop. The read side already exists:
active Dock goals boost memory retrieval (``grove/memory/provider.py``). This
module is the WRITE side — when the memory substrate accumulates active
``ProjectState`` / ``DomainFact`` records that no Dock goal tracks
(``dock_goal_ref is None``), the detector asks a T1 (Haiku) call whether those
unattached records share a coherent strategic theme. If so it stages ONE
``dock_mutation`` proposal; the operator approves through the normal Kaizen
flow, which appends a ``staging`` goal to ``dock.autonomaton.yaml`` (the machine
file — a GREEN granted workspace, never the RED operator ``dock.yaml``).

Init-safety (Andon A6): the T1 call is bounded by a short client timeout and
any failure (timeout / API / malformed JSON) returns ``None`` — detection is
SKIPPED this session rather than blocking Dispatcher init. The detector is also
single-proposal-per-session (``MAX_PROPOSALS_PER_SESSION``) and the downstream
writer + queue dedup on goal id / proposal id, so a recurring unattached cluster
never stacks duplicates.

Pattern parity: ``detect`` + ``stage_proposals`` mirror
``grove.eval.consolidation_ratchet.ConsolidationRatchet`` exactly, so the
Dispatcher init wiring is uniform across detectors.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

__all__ = ["DockMutationDetector"]

# The two record kinds that describe ongoing strategic work — the only kinds a
# Dock goal would ever track. OperatorPreference / ArchitecturalRule are stable
# facts, not goal-worthy themes.
_GOAL_WORTHY_TYPES = frozenset({"ProjectState", "DomainFact"})

# Per-record content sent to T1 is truncated (parity with the persistence
# detector's _INDEX_CONTENT_CHARS) so a runaway record can't blow the prompt.
_RECORD_CONTENT_CHARS = 240

# Hard ceiling on records fed to T1 — keeps the synthesis prompt bounded.
_MAX_RECORDS_TO_T1 = 10

# T1 client timeout (seconds). Andon A6: detection must never block Dispatcher
# init; a slow T1 raises, is caught, and detection is skipped this session.
_T1_TIMEOUT_SECONDS = 5.0
_T1_MAX_OUTPUT_TOKENS = 400

_SYNTHESIS_SYSTEM_PROMPT = (
    "You name strategic themes for an operator's goal board (the Dock). You are "
    "given memory records that accumulated WITHOUT any Dock goal tracking them. "
    "Decide whether they share ONE coherent strategic theme worth tracking as a "
    "goal. Be conservative: only propose a theme when the records clearly "
    "cohere. Respond with JSON ONLY, no prose. If there is a coherent theme: "
    '{"name": "Short Goal Name", "keywords": ["kw1", "kw2", "kw3"]}. '
    'If there is no coherent theme: {"name": null}.'
)


def _slugify(name: str) -> str:
    """Lowercase, hyphenated slug for an ``auto-`` goal id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "untitled"


class DockMutationDetector:
    """Detect unattached memory clusters and propose a tracking Dock goal."""

    # Minimum active unattached ProjectState/DomainFact records before the
    # detector even consults T1 — below this, an emerging theme is too thin.
    UNATTACHED_THRESHOLD = 5
    MAX_PROPOSALS_PER_SESSION = 1

    def detect(
        self,
        memory_store: Any,
        active_dock_goal_slugs: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return at most one ``create_goal`` proposal dict, or ``[]``.

        1. Collect active ``ProjectState`` / ``DomainFact`` records whose
           ``dock_goal_ref`` is None (unattached to any goal).
        2. Below :attr:`UNATTACHED_THRESHOLD` → ``[]``.
        3. Ask T1 for a coherent theme over the (capped) record contents.
        4. T1 names a theme → one proposal; T1 declines / errors → ``[]``.
        """
        slugs = active_dock_goal_slugs or set()
        unattached = self._unattached_records(memory_store)
        if len(unattached) < self.UNATTACHED_THRESHOLD:
            return []

        sample = unattached[:_MAX_RECORDS_TO_T1]
        contents = [r.content[:_RECORD_CONTENT_CHARS] for r in sample]
        theme = self._synthesize_goal(contents)
        if theme is None:
            return []

        name = theme["name"]
        goal_id = f"auto-{_slugify(name)}"
        # Don't re-propose a goal whose slug already names an existing goal.
        if goal_id in slugs or _slugify(name) in slugs:
            logger.debug(
                "[dock-mutation] synthesized theme %r already tracked — skip",
                name,
            )
            return []

        proposal = {
            "action": "create_goal",
            "goal": {
                "id": goal_id,
                "name": name,
                "keywords": theme["keywords"],
                "vector": "personal",
                "status": "staging",
                "definition_of_done": "",
                "source_record_ids": [r.id for r in sample],
            },
        }
        return [proposal][: self.MAX_PROPOSALS_PER_SESSION]

    def stage_proposals(
        self, proposals: List[Dict[str, Any]], session_id: str
    ) -> int:
        """Wrap each proposal in a ``dock_mutation`` RoutingProposal and append
        to the routing proposal queue. The id is computed from the STABLE
        identity (the goal id) — excluding the volatile ``source_record_ids`` —
        so a re-run over the same theme dedups instead of stacking. Returns the
        number actually appended.
        """
        from grove.eval.proposal_queue import (
            PROPOSAL_TYPE_DOCK_MUTATION,
            RoutingProposal,
            _now_iso,
            append,
            compute_proposal_id,
        )

        staged = 0
        for proposal in proposals:
            goal = proposal["goal"]
            identity = {"action": "create_goal", "goal_id": goal["id"]}
            record = RoutingProposal(
                proposal_id=compute_proposal_id(
                    type=PROPOSAL_TYPE_DOCK_MUTATION,
                    payload=identity,
                    evidence=(),
                ),
                type=PROPOSAL_TYPE_DOCK_MUTATION,
                payload=proposal,
                evidence=tuple(goal.get("source_record_ids", ())),
                eval_hash="",
                created_at=_now_iso(),
                proposer="dock_detector",  # proposal-proposer-attribution-v1 (#13)
            )
            if append(record):
                staged += 1
        return staged

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _unattached_records(memory_store: Any) -> List[Any]:
        """Active ProjectState/DomainFact records with ``dock_goal_ref`` None."""
        out: List[Any] = []
        for rec in memory_store.projected_records().values():
            if rec.status != "active":
                continue
            if rec.entity_type not in _GOAL_WORTHY_TYPES:
                continue
            if rec.dock_goal_ref is not None:
                continue
            out.append(rec)
        return out

    def _synthesize_goal(
        self, record_contents: List[str]
    ) -> Optional[Dict[str, Any]]:
        """One bounded T1 (Haiku) call → ``{"name", "keywords"}`` or ``None``.

        Andon A6: a ``_T1_TIMEOUT_SECONDS`` client timeout caps the call, and
        ANY failure (timeout / transport / malformed or no-theme JSON) returns
        ``None`` so detection is skipped this session rather than blocking init.
        """
        numbered = "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(record_contents)
        )
        user = (
            "These memory records accumulated without a Dock goal:\n"
            f"{numbered}\n\n"
            "Is there a coherent strategic theme? Respond with JSON only."
        )
        try:
            from agent.anthropic_adapter import build_anthropic_client
            from grove.classify import _telemetry_tier_runtime, _track_cost

            runtime, tier_config = _telemetry_tier_runtime()
            client = build_anthropic_client(
                api_key=runtime.get("api_key") or "",
                base_url=runtime.get("base_url") or None,
                timeout=_T1_TIMEOUT_SECONDS,
            )
            response = client.messages.create(
                model=runtime["model"],
                max_tokens=_T1_MAX_OUTPUT_TOKENS,
                system=_SYNTHESIS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            _track_cost(response.usage, tier_config=tier_config)
            text = "".join(
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            )
        except Exception as exc:  # timeout / transport / config — skip session
            logger.warning(
                "[dock-mutation] T1 synthesis unavailable (%r) — "
                "skipping dock-mutation detection this session", exc,
            )
            return None

        return self._parse_theme(text)

    @staticmethod
    def _parse_theme(text: str) -> Optional[Dict[str, Any]]:
        """Parse the T1 JSON. Returns the goal theme or None (no coherent
        theme / unparseable / missing fields). Defensive: a malformed synthesis
        never yields a proposal."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # strip a ```json … ``` fence
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.debug("[dock-mutation] T1 returned non-JSON: %r", text[:200])
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not name or not isinstance(name, str):
            return None  # {"name": null} — no coherent theme
        keywords = data.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k) for k in keywords if str(k).strip()]
        return {"name": name.strip(), "keywords": keywords}
