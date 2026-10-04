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
single-proposal-per-session (``MAX_PROPOSALS_PER_SESSION``).

Known defect + DEFERRAL (dock-detector-dedup-v1). The two existing dedups — the
goal-slug check in ``detect`` and the goal-id-keyed proposal id in
``stage_proposals`` — both key on the NAME, and T1 words the same theme
differently every session, so the same records re-propose under a new name.

The RIGHT fix is to set ``MemoryRecord.dock_goal_ref`` when a goal is approved
— the field ``_unattached_records`` already filters on. That needs a new memory
event type + fold rule (records are a projection of the event log), so it is
deferred. FOLLOW-UP: ``memory-goal-attached-event`` — until it lands,
``dock_goal_ref`` still reads None for records an approved machine goal tracks.

The interim guards below are OPT-IN (both default OFF in the
``dock_mutation_detector`` block of ``~/.grove/flywheel.config.yaml``) and are
fed by the CALLER — the detector itself reads nothing:

1. ``dedup_claimed_records`` — records listed in the ``source_record_ids`` of an
   existing goal or pending ``dock_mutation`` proposal are dropped before the
   threshold check. A second source of truth for "attached"; retire it when the
   follow-up lands.
2. ``dedup_theme_overlap`` — the synthesized name + keywords are normalized to
   tokens and compared against existing themes; overlap at or above
   ``theme_overlap_threshold`` is skipped.

Both make the detector strictly MORE conservative and can drop it under the
unattached floor, so every suppression logs at INFO with its numbers — a
suppressed card must never look like "nothing to propose".

Pattern parity: ``detect`` + ``stage_proposals`` mirror
``grove.eval.consolidation_ratchet.ConsolidationRatchet`` exactly, so the
Dispatcher init wiring is uniform across detectors.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
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
# Code default; the operator value is ``dock_mutation_detector.max_records_to_t1``.
_MAX_RECORDS_TO_T1 = 10

# Code default for ``dock_mutation_detector.theme_overlap_threshold`` — the
# share of the SMALLER token set that must be shared for two themes to count as
# the same theme reworded.
_THEME_OVERLAP_THRESHOLD = 0.5

# Words that carry no theme signal — dropped before the overlap comparison.
_THEME_STOPWORDS = frozenset({
    "a", "an", "and", "as", "for", "in", "of", "on", "the", "to", "with",
})

# Tokens are compared on a short prefix so inflections collapse
# (architecture / architectural, evolve / evolving, model / models).
_THEME_STEM_CHARS = 5

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
    '{"name": "Short Goal Name", "keywords": ["kw1", "kw2", "kw3"], '
    '"rationale": "One plain sentence on what these records have in common.", '
    '"definition_of_done": "One plain sentence on what finished looks like.", '
    '"vector": "one of: apex_strategic, strategic, operational, product, '
    'personal"}. '
    'If there is no coherent theme: {"name": null}.'
)


def _slugify(name: str) -> str:
    """Lowercase, hyphenated slug for an ``auto-`` goal id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "untitled"


@dataclass(frozen=True)
class DockDetectorConfig:
    """The ``dock_mutation_detector`` block — code defaults when absent.

    ``_T1_TIMEOUT_SECONDS`` is deliberately NOT here: it is the Andon A6
    init-safety floor, a code constant by design."""

    unattached_threshold: int = 5
    max_records_to_t1: int = _MAX_RECORDS_TO_T1
    # Vector for a proposed goal when T1 names none (or an invalid one).
    default_vector: str = "personal"
    # Interim dedup guards — OPT-IN (see the module docstring's DEFERRAL note).
    dedup_claimed_records: bool = False
    dedup_theme_overlap: bool = False
    theme_overlap_threshold: float = _THEME_OVERLAP_THRESHOLD


def load_dock_detector_config(
    config_path: Optional[Path] = None,
) -> DockDetectorConfig:
    """Load ``dock_mutation_detector`` from ``~/.grove/flywheel.config.yaml``.

    An absent file or absent block uses the code defaults (the
    admission_friction precedent). A PRESENT key is validated fail-loud."""
    import yaml

    from grove.dock import VECTOR_RANK

    if config_path is None:
        from hermes_constants import get_hermes_home

        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return DockDetectorConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    block = raw.get("dock_mutation_detector")
    if not isinstance(block, dict):
        return DockDetectorConfig()
    where = "flywheel.config.yaml dock_mutation_detector"

    def _pos_int(key: str, default: int) -> int:
        v = block.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(f"{where}.{key} must be an integer >= 1, got {v!r}")
        return v

    def _flag(key: str) -> bool:
        v = block.get(key, False)
        if not isinstance(v, bool):
            raise ValueError(f"{where}.{key} must be true or false, got {v!r}")
        return v

    overlap = block.get("theme_overlap_threshold", _THEME_OVERLAP_THRESHOLD)
    if (
        isinstance(overlap, bool)
        or not isinstance(overlap, (int, float))
        or not 0 < overlap <= 1
    ):
        raise ValueError(
            f"{where}.theme_overlap_threshold must be a number in (0, 1], "
            f"got {overlap!r}"
        )
    vector = block.get("default_vector", "personal")
    if vector not in VECTOR_RANK:
        raise ValueError(
            f"{where}.default_vector must be one of {sorted(VECTOR_RANK)}, "
            f"got {vector!r}"
        )
    return DockDetectorConfig(
        unattached_threshold=_pos_int("unattached_threshold", 5),
        max_records_to_t1=_pos_int("max_records_to_t1", _MAX_RECORDS_TO_T1),
        default_vector=vector,
        dedup_claimed_records=_flag("dedup_claimed_records"),
        dedup_theme_overlap=_flag("dedup_theme_overlap"),
        theme_overlap_threshold=float(overlap),
    )


def _theme_tokens(name: str, keywords: Any) -> Set[str]:
    """Normalize a theme (goal name + keywords) to a comparable token set:
    lowercase, split on non-alphanumerics, stopwords dropped, each token cut to
    a short stem so inflections collapse."""
    words: List[str] = re.split(r"[^a-z0-9]+", str(name or "").lower())
    for kw in keywords or ():
        words.extend(re.split(r"[^a-z0-9]+", str(kw).lower()))
    return {
        w[:_THEME_STEM_CHARS]
        for w in words
        if w and w not in _THEME_STOPWORDS
    }


def _theme_overlap(a: Set[str], b: Set[str]) -> float:
    """Shared tokens over the SMALLER set (overlap coefficient) — a short
    theme fully contained in a longer one scores 1.0."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def existing_goal_themes() -> List[Dict[str, Any]]:
    """CALLER-side input for the opt-in dedup guards (the Dispatcher gathers it
    and passes it to ``detect`` — the detector reads nothing itself).

    Every theme the Dock already tracks or is already being offered:
    operator goals, ALL machine goals (read raw — ``load_dock`` caps the merged
    machine goals, and a capped-out goal still claims its records), and pending
    ``dock_mutation`` proposals. Each entry is
    ``{"name", "keywords", "source_record_ids"}``. Read-only."""
    import yaml

    from grove.dock import MACHINE_DOCK_FILENAME, _machine_dock_dir, load_dock
    from grove.eval.proposal_queue import PROPOSAL_TYPE_DOCK_MUTATION, read_all

    themes: List[Dict[str, Any]] = []

    def _add(goal: Any) -> None:
        if not isinstance(goal, dict) or not goal.get("name"):
            return
        ids = goal.get("source_record_ids")
        kws = goal.get("keywords")
        themes.append({
            "name": str(goal["name"]),
            "keywords": list(kws) if isinstance(kws, (list, tuple)) else [],
            "source_record_ids": list(ids) if isinstance(ids, (list, tuple)) else [],
        })

    dock = load_dock()
    if dock is not None:
        for g in dock.goals:
            _add({"name": g.name, "keywords": list(g.keywords)})

    machine_path = _machine_dock_dir() / MACHINE_DOCK_FILENAME
    if machine_path.exists():
        raw = yaml.safe_load(machine_path.read_text(encoding="utf-8")) or {}
        goals = raw.get("goals") if isinstance(raw, dict) else None
        for g in goals if isinstance(goals, list) else []:
            _add(g)

    for proposal in read_all():
        if proposal.type == PROPOSAL_TYPE_DOCK_MUTATION:
            _add((proposal.payload or {}).get("goal"))
    return themes


class DockMutationDetector:
    """Detect unattached memory clusters and propose a tracking Dock goal."""

    # Minimum active unattached ProjectState/DomainFact records before the
    # detector even consults T1 — below this, an emerging theme is too thin.
    # Code default; the operator value is
    # ``dock_mutation_detector.unattached_threshold``.
    UNATTACHED_THRESHOLD = 5
    MAX_PROPOSALS_PER_SESSION = 1

    def detect(
        self,
        memory_store: Any,
        active_dock_goal_slugs: Optional[Set[str]] = None,
        *,
        existing_themes: Optional[List[Dict[str, Any]]] = None,
        config: Optional[DockDetectorConfig] = None,
    ) -> List[Dict[str, Any]]:
        """Return at most one ``create_goal`` proposal dict, or ``[]``.

        1. Collect active ``ProjectState`` / ``DomainFact`` records whose
           ``dock_goal_ref`` is None (unattached to any goal).
        2. (opt-in ``dedup_claimed_records``) drop records ``existing_themes``
           already claims.
        3. Below the unattached threshold → ``[]``.
        4. Ask T1 for a coherent theme over the (capped) record contents.
        5. T1 names a theme → skip a slug already tracked, and (opt-in
           ``dedup_theme_overlap``) a theme that rewords one in
           ``existing_themes``; else one proposal. T1 declines → ``[]``; a T1
           FAILURE raises (detector-sweep-resilience-v1 R-2) — contained and
           filed by the Dispatcher's per-producer sweep guard.

        PURE: both ``existing_themes`` and ``config`` come from the caller
        (``None`` → no themes / code defaults); nothing is read here.
        """
        slugs = active_dock_goal_slugs or set()
        cfg = config if config is not None else DockDetectorConfig()
        themes = existing_themes or []

        unattached = self._unattached_records(memory_store)
        if cfg.dedup_claimed_records:
            claimed: Set[str] = set()
            for t in themes:
                claimed.update(str(i) for i in t.get("source_record_ids") or ())
            before = len(unattached)
            unattached = [r for r in unattached if r.id not in claimed]
            if before != len(unattached):
                logger.info(
                    "[dock-mutation] dedup_claimed_records: %d of %d unattached "
                    "records already claimed by a goal or pending proposal — "
                    "%d remain (threshold %d)",
                    before - len(unattached), before, len(unattached),
                    cfg.unattached_threshold,
                )
        if len(unattached) < cfg.unattached_threshold:
            return []

        sample = unattached[: cfg.max_records_to_t1]
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

        if cfg.dedup_theme_overlap:
            # Don't re-propose a theme that only REWORDS one already tracked.
            tokens = _theme_tokens(name, theme["keywords"])
            for t in themes:
                overlap = _theme_overlap(
                    tokens, _theme_tokens(t.get("name", ""), t.get("keywords"))
                )
                if overlap >= cfg.theme_overlap_threshold:
                    logger.info(
                        "[dock-mutation] dedup_theme_overlap: synthesized theme "
                        "%r overlaps existing theme %r (%.2f >= %.2f) — "
                        "SUPPRESSED, no proposal staged",
                        name, t.get("name"), overlap,
                        cfg.theme_overlap_threshold,
                    )
                    return []

        proposal = {
            "action": "create_goal",
            "goal": {
                "id": goal_id,
                "name": name,
                "keywords": theme["keywords"],
                "vector": theme.get("vector") or cfg.default_vector,
                "status": "staging",
                "definition_of_done": theme.get("definition_of_done", ""),
                "source_record_ids": [r.id for r in sample],
            },
        }
        rationale = theme.get("rationale")
        if rationale:
            # Carried beside the payload; stage_proposals lifts it into the
            # proposal's semantic_justification (the card's "why").
            proposal["rationale"] = rationale
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
            proposal = dict(proposal)
            rationale = str(proposal.pop("rationale", "") or "")
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
                semantic_justification=rationale,
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

        A ``_T1_TIMEOUT_SECONDS`` client timeout caps the call.
        detector-sweep-resilience-v1 R-2: transport / timeout / config
        failures RAISE — the Dispatcher's per-producer sweep guard contains
        and files them as ``producer_failure`` (the former broad swallow
        hid them as a warning line and an empty detection). ``None`` is
        reserved for T1 answering "no coherent theme"
        (``_parse_theme``'s defensive parse, unchanged).
        """
        numbered = "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(record_contents)
        )
        user = (
            "These memory records accumulated without a Dock goal:\n"
            f"{numbered}\n\n"
            "Is there a coherent strategic theme? Respond with JSON only."
        )
        from grove.classify import _telemetry_tier_runtime, _track_cost

        runtime, tier_config = _telemetry_tier_runtime()
        api_mode = runtime.get("api_mode")

        # dock-detector-provider-agnostic-v1: branch on the telemetry
        # tier's wire protocol, mirroring call_t1 / _call_classifier /
        # the memory detector. anthropic_messages is preserved
        # byte-for-byte; chat_completions drives any OpenAI-compatible
        # provider (the OpenRouter telemetry tier the classifier already
        # runs on) — without it, messages.create hits /v1/messages and
        # gets an HTML error.
        if api_mode == "chat_completions":
            # Lazy import keeps the ~800ms openai/pydantic load off the
            # module-import path, matching the Anthropic branch's local
            # import.
            from openai import OpenAI
            from grove.providers import openrouter_provider_pref

            client = OpenAI(
                api_key=runtime.get("api_key") or "",
                base_url=runtime.get("base_url") or None,
                timeout=_T1_TIMEOUT_SECONDS,
            )
            kwargs: Dict[str, Any] = {
                "model": runtime["model"],
                "max_tokens": _T1_MAX_OUTPUT_TOKENS,
                "messages": [
                    {
                        "role": "system",
                        "content": _SYNTHESIS_SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": user},
                ],
            }
            # openrouter-zero-retention-routing-v1: attach the operator's
            # OpenRouter provider routing verbatim when this telemetry
            # call is OpenRouter-bound. No-op otherwise.
            _pp = openrouter_provider_pref(runtime)
            if _pp:
                kwargs["extra_body"] = {"provider": _pp}
            response = client.chat.completions.create(**kwargs)
            _track_cost(
                getattr(response, "usage", None), tier_config=tier_config
            )
            text = response.choices[0].message.content or ""
        elif api_mode == "anthropic_messages":
            from agent.anthropic_adapter import build_anthropic_client

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
        else:
            # Any other api_mode (bedrock_converse, codex_responses, …)
            # is not a surface this synthesis speaks. Fail loud — the
            # raise propagates to the Dispatcher's per-producer sweep
            # guard (detector-sweep-resilience-v1 R-2), which contains it
            # and files producer_failure with this diagnostic.
            raise RuntimeError(
                f"_synthesize_goal: unsupported telemetry api_mode "
                f"{api_mode!r} (model={runtime.get('model')!r}); bind "
                f"the telemetry tier to an anthropic_messages or "
                f"chat_completions provider"
            )

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
        theme: Dict[str, Any] = {"name": name.strip(), "keywords": keywords}
        # Optional card fields — each present only when T1 gave a usable value.
        for key in ("rationale", "definition_of_done"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                theme[key] = value.strip()
        from grove.dock import VECTOR_RANK

        vector = data.get("vector")
        if isinstance(vector, str) and vector.strip() in VECTOR_RANK:
            theme["vector"] = vector.strip()
        elif vector:
            logger.warning(
                "[dock-mutation] T1 named an invalid vector %r — the configured "
                "default_vector is used", vector,
            )
        return theme
