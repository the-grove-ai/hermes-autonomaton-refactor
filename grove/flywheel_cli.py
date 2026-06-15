"""GRV-008 § IV operator review surface for Sprint 47 routing proposals.

CLI renderers reachable via ``autonomaton flywheel
{list,show,approve,reject}``. Each renderer prints to stdout/stderr,
sets a UNIX exit code, and writes telemetry events the Kaizen Ledger
records as operator sovereignty acts.

The four operations:

* :func:`cli_list` — show every pending proposal in
  ``~/.grove/proposals.jsonl`` as one human-readable line each.
* :func:`cli_show` — show one proposal's payload, evidence, and the
  diff it would apply to ``routing.autonomaton.yaml``.
* :func:`cli_approve` — apply the proposal's payload to the machine
  file (set-union per GRV-008 § III); remove from queue.
* :func:`cli_reject` — remove from queue; no config change.

Per GRV-008 § III the machine file is the only path the renderers
write to. ``routing.config.yaml`` is never opened in write mode.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import yaml

from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PATTERN_DEMOTION,
    PROPOSAL_TYPE_PATTERN_PROMOTION,
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT,
    PROPOSAL_TYPE_SKILL_PROMOTION,
    PROPOSAL_TYPE_SKILL_SYNTHESIS,
    PROPOSAL_TYPE_ZONE_PROMOTION,
    RoutingProposal,
    default_queue_path,
    read,
    read_all,
    remove,
)
from grove.router_merge import apply_diff_to_machine_config

# The Sprint 47 v0.1 spelling. Honored as an alias for routing_adjustment on
# read (proposal_queue back-compat) and resolved to the routing_adjustment
# handler in ONE place — :func:`_handler_for`. B1 GATE-B: keep + flag (the live
# VM queue could not be verified empty of this spelling at build time).
_LEGACY_ROUTING_TYPE = "routing_update"

logger = logging.getLogger(__name__)


__all__ = [
    "cli_list",
    "cli_show",
    "cli_approve",
    "cli_reject",
]


def _machine_config_path() -> Path:
    """The hermes_home routing.autonomaton.yaml path."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "routing.autonomaton.yaml"


def _routing_adjustment_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a routing_adjustment proposal into a routing-config diff.

    The diff is a partial routing config shape suitable for
    ``apply_diff_to_machine_config`` — the set-union semantics in the
    merger handle the intent-list combination with any pre-existing
    machine additions.
    """
    rule = proposal.payload.get("rule")
    add_intents = list(proposal.payload.get("add_intents") or [])
    if rule not in ("downward", "upward") or not add_intents:
        raise ValueError(
            f"malformed routing_adjustment payload: {proposal.payload!r}"
        )
    return {
        "routing": {
            "routing_rules": {
                rule: {
                    "match": {
                        "intents": add_intents,
                    },
                },
            },
        },
    }


def _diff_pattern_demotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 49 — the pattern is already suspended (auto, on correction).
    # The "diff" the operator confirms is pulling it from T0 to T1.
    return {
        "pattern_demotion": {
            "intent_class": proposal.payload.get("intent_class", "?"),
            "tier": "T0 → T1 (drift: corrected after a cache hit)",
            "trigger": proposal.payload.get("trigger", "correction_drift"),
            "correction_turn_id": proposal.payload.get("correction_turn_id", "?"),
            "reverse_with": "autonomaton flywheel reject <id>",
        },
    }


def _diff_pattern_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 48 — the "diff" is retiring a stable pattern to the
    # deterministic T0 cache (the compiled entry already exists,
    # suspended, in pattern_cache.db; approve flips it to active).
    ev = proposal.payload.get("promotion_evidence", {})
    return {
        "pattern_promotion": {
            "intent_class": proposal.payload.get("intent_class", "?"),
            "cacheable_type": proposal.payload.get("cacheable_type", "?"),
            "tier": "T1 → T0 (deterministic; no model call)",
            "evidence": ev,
            "sample_queries": proposal.payload.get("sample_queries", []),
        },
    }


def _diff_skill_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Sprint 53.2 — the "diff" the operator reviews is the promotion
    # act: move the skill out of quarantine and greenlight its path.
    name = proposal.payload.get("skill_name", "?")
    return {
        "skill_promotion": {
            "skill_name": name,
            "from": f"~/.grove/skills/.andon/{name}/",
            "to": f"~/.grove/skills/{name}/",
            "zone_rule": {
                "match_pattern": rf".*\.grove/skills/{name}/.*",
                "zone": "green",
            },
        },
    }


def _diff_zone_promotion(proposal: RoutingProposal) -> Dict[str, Any]:
    # Zone promotions don't translate to a routing-config diff —
    # they write directly to zones.schema.yaml via save_zone_rule.
    # The "diff" displayed to the operator is the YAML-shaped
    # rule that would be appended.
    return {
        "tool_zones": {
            proposal.payload.get("tool", "?"): {
                "rules": [
                    {
                        "match_pattern": proposal.payload.get("pattern", ""),
                        "zone": proposal.payload.get("zone", "?"),
                        "reason": proposal.payload.get("reason", ""),
                    },
                ],
            },
        },
    }


def _diff_skill_synthesis(proposal: RoutingProposal) -> Dict[str, Any]:
    # B1 (Fork B) — the "diff" the operator reviews is staging the drafted
    # SKILL.md into quarantine. Approve materializes it to .andon/ and mints
    # the proposed (non-executable) record; a follow-on skill_promotion takes
    # it active. The full SKILL.md text rides in the payload (shown by cli_show).
    name = proposal.payload.get("skill_name", "?")
    return {
        "skill_synthesis": {
            "skill_name": name,
            "stages_to": f"~/.grove/skills/.andon/{name}/",
            "record_state": "proposed (non-executable until promoted)",
            "when_to_use": proposal.payload.get("when_to_use", ""),
            "tool_sequence": proposal.payload.get("tool_sequence", []),
            "next": "promote via `hermes andon promote` or a skill_promotion proposal",
        },
    }


def _proposal_to_diff(proposal: RoutingProposal) -> Dict[str, Any]:
    """Translate a proposal payload into the diff the operator reviews.

    B1 — single registry dispatch (no if/elif ladder). Unknown type raises
    via :func:`_handler_for` — never a silent fallback render.
    """
    return _handler_for(proposal.type).diff_renderer(proposal)


def _summary_routing_adjustment(proposal: RoutingProposal) -> str:
    rule = proposal.payload.get("rule", "?")
    intents = ", ".join(proposal.payload.get("add_intents", []))
    return f"add {intents} to routing.{rule}"


def _summary_pattern_promotion(proposal: RoutingProposal) -> str:
    ic = proposal.payload.get("intent_class", "?")
    ct = proposal.payload.get("cacheable_type", "?")
    samples = proposal.payload.get("sample_queries") or []
    sample = f" “{samples[0][:40]}”" if samples else ""
    return f"retire {ic} [{ct}] pattern{sample} to T0 cache"


def _summary_pattern_demotion(proposal: RoutingProposal) -> str:
    ic = proposal.payload.get("intent_class", "?")
    return f"demote {ic} pattern (drift: corrected after a T0 hit)"


def _summary_skill_promotion(proposal: RoutingProposal) -> str:
    name = proposal.payload.get("skill_name", "?")
    return f"promote quarantined skill {name!r} → trusted"


def _summary_zone_promotion(proposal: RoutingProposal) -> str:
    tool = proposal.payload.get("tool", "?")
    pattern = proposal.payload.get("pattern", "?")
    return f"greenlight {tool} pattern={pattern!r}"


def _summary_skill_synthesis(proposal: RoutingProposal) -> str:
    name = proposal.payload.get("skill_name", "?")
    return f"stage drafted skill {name!r} → quarantine for review"


def _format_summary(proposal: RoutingProposal) -> str:
    """One-line operator-facing summary of a proposal.

    B1 — single registry dispatch (no if/elif ladder, no silent payload-
    preview fallback). The shared framing (id, type, evidence count, created
    timestamp) lives here; only the per-type body comes from the handler row.
    Unknown type raises via :func:`_handler_for`.
    """
    short_id = proposal.proposal_id.split(":")[-1][:12]
    n_evidence = len(proposal.evidence)
    body = _handler_for(proposal.type).summary_renderer(proposal)
    return (
        f"{short_id}  {proposal.type:<22}  "
        f"{body}  "
        f"(evidence: {n_evidence} turn(s))  "
        f"{proposal.created_at}"
    )


# ── list ─────────────────────────────────────────────────────────────


def cli_list(*, queue_path: Optional[Path] = None) -> int:
    """Show every pending proposal in the queue.

    Returns exit code 0. Empty queue prints a friendly message.
    """
    proposals = read_all(path=queue_path or default_queue_path())
    if not proposals:
        print(
            "No pending Flywheel proposals. The TierRatchet emits "
            "proposals once usage patterns shift a class into the "
            "qualifying band (see GRV-008 § I)."
        )
        return 0
    print(f"{len(proposals)} pending proposal(s) in the queue:")
    print()
    for proposal in proposals:
        print("  " + _format_summary(proposal))
    print()
    print(
        "Inspect: autonomaton flywheel show <id>\n"
        "Approve: autonomaton flywheel approve <id>\n"
        "Reject:  autonomaton flywheel reject <id> [--reason \"...\"]"
    )
    return 0


# ── scan (T0 pattern cache — Sprint 48) ──────────────────────────────


def cli_scan(
    *,
    store: Optional[Any] = None,
    propose: bool = False,
    queue_path: Optional[Path] = None,
) -> int:
    """Scan the intent store for T0 pattern-cache candidates.

    Read-only by default: groups Flywheel evidence by a conservative
    normalized key and prints the patterns that meet the configured
    thresholds. With ``propose=True`` it also compiles each safely-compilable
    candidate and queues a ``pattern_promotion`` proposal for operator
    approval (skipping patterns already known/rejected)."""
    from grove.eval.pattern_compiler import (
        scan_candidates, load_pattern_cache_config,
    )
    cfg = load_pattern_cache_config()
    if not cfg.get("enabled", True):
        print("Pattern cache disabled (pattern_cache.enabled: false).")
        return 0
    if store is None:
        from grove.intent_store import get_store
        store = get_store()

    candidates = scan_candidates(store, cfg)
    if not candidates:
        print("No T0 pattern-cache candidates.")
        print(
            f"  Thresholds: >={cfg['min_repetitions']} reps within "
            f"{cfg['within_days']}d, <={cfg['max_rejections']} corrections; "
            f"excluding {cfg['exclude_intents']}."
        )
        return 0

    print(f"{len(candidates)} T0 pattern-cache candidate(s):")
    print()
    for c in candidates:
        print(
            f"  [{c.cacheable_type}] {c.intent_class}  "
            f"{c.repetition_count}x over {c.time_span_days}d  "
            f"corrections={c.rejection_count}  "
            f"key={c.t0_key.split(':')[-1][:12]}"
        )
        for q in c.sample_queries:
            print(f"      • {q[:80]}")
        print()

    if not propose:
        print("These are candidates only. Re-run with --propose to queue "
              "promotion proposals for approval.")
        return 0

    from grove.eval.pattern_compiler import (
        propose_pattern_promotions, DISPOSITION_PROPOSED, DISPOSITION_SKIPPED_KNOWN,
    )
    from grove.pattern_cache import PatternCacheStore
    result = propose_pattern_promotions(
        store, PatternCacheStore(), queue_path=queue_path, config=cfg,
    )
    proposed = [d for d in result.dispositions if d.status == DISPOSITION_PROPOSED]
    known = [d for d in result.dispositions if d.status == DISPOSITION_SKIPPED_KNOWN]
    dropped = [
        d for d in result.dispositions
        if d.status not in (DISPOSITION_PROPOSED, DISPOSITION_SKIPPED_KNOWN)
    ]

    # Loud feedback (Sprint 56 Fix #1): every candidate is accounted for —
    # nothing is dropped silently.
    print(
        f"\nProposed {len(proposed)} of {len(result.dispositions)} candidate(s)"
        + (f" ({len(known)} already in cache)" if known else "")
        + (f" ({len(dropped)} dropped)" if dropped else "")
        + ":"
    )
    for d in proposed:
        print(f"  ✓ proposed  [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  → {d.proposal_id.split(':')[-1][:12]}")
    for d in known:
        print(f"  • skipped   [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  — {d.detail}")
    for d in dropped:
        print(f"  ✗ dropped   [{d.cacheable_type}] {d.intent_class}  "
              f"“{d.sample_query[:48]}”  — {d.detail}")
    if proposed:
        print("\nReview: autonomaton flywheel list / approve <id>.")
    return 0


# ── patterns (T0 cache operator controls — Sprint 49) ────────────────

# Estimated average T1 interaction size, used by ``patterns stats`` to turn a
# per-million-token price into a per-interaction savings estimate. These are
# deliberately conservative rough averages, NOT measured — the stats output
# labels the savings as an estimate so the operator reads it as such.
_T1_AVG_INPUT_TOKENS = 1800
_T1_AVG_OUTPUT_TOKENS = 400


def _t1_interaction_cost_usd() -> Optional[float]:
    """Estimated USD cost of one averted T1 interaction.

    Reads ``tier_preferences.T1.cost_per_mtok_input/output`` from
    routing.config.yaml (operator copy wins over the repo default, same
    precedence as the pattern_cache config) and multiplies by the assumed
    average interaction size. Returns None when the T1 tier declares no
    cost — the caller then reports savings as unavailable rather than $0."""
    candidates = [
        Path.home() / ".grove" / "routing.config.yaml",
        Path(__file__).resolve().parents[1] / "config" / "routing.config.yaml",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        t1 = (
            ((data.get("routing") or {}).get("tier_preferences") or {}).get("T1")
            or {}
        )
        cost_in = t1.get("cost_per_mtok_input")
        cost_out = t1.get("cost_per_mtok_output")
        if cost_in is None or cost_out is None:
            return None
        return (
            _T1_AVG_INPUT_TOKENS / 1_000_000 * float(cost_in)
            + _T1_AVG_OUTPUT_TOKENS / 1_000_000 * float(cost_out)
        )
    return None


def cli_patterns_list(*, store: Optional[Any] = None) -> int:
    """Show every compiled T0 pattern with its lifecycle + hit telemetry."""
    if store is None:
        from grove.pattern_cache import PatternCacheStore
        store = PatternCacheStore()
    patterns = store.all()
    if not patterns:
        print(
            "No compiled T0 patterns. The compiler proposes them once a "
            "query repeats with a stable result — see "
            "`autonomaton flywheel scan`."
        )
        return 0
    active = sum(1 for p in patterns if p.status == "active")
    print(f"{len(patterns)} compiled T0 pattern(s) ({active} active):")
    print()
    print(
        f"  {'pattern':<14}{'intent_class':<18}{'type':<11}"
        f"{'status':<11}{'hits':>5}  {'last_hit':<20}sample"
    )
    for p in patterns:
        # promotion_evidence is JSON; show a short sample query if present.
        sample_q = ""
        try:
            ev = json.loads(p.promotion_evidence) if p.promotion_evidence else {}
            sqs = ev.get("sample_queries") if isinstance(ev, dict) else None
            if isinstance(sqs, list) and sqs:
                sample_q = str(sqs[0])[:40]
        except (ValueError, TypeError):
            sample_q = ""
        last_hit = (p.last_hit_at or "—")[:19]
        print(
            f"  {p.pattern_id.split(':')[-1][:12]:<14}"
            f"{p.intent_class:<18}{p.cacheable_type:<11}"
            f"{p.status:<11}{p.hit_count:>5}  {last_hit:<20}{sample_q}"
        )
    print()
    print(
        "Demote: autonomaton flywheel patterns demote <pattern>\n"
        "Stats:  autonomaton flywheel patterns stats"
    )
    return 0


def cli_patterns_demote(
    partial_id: str,
    *,
    store: Optional[Any] = None,
    assume_yes: bool = False,
) -> int:
    """Manually demote an active T0 pattern back to T1 (GATE-A D4 / 3b).

    Resolves ``partial_id`` against compiled pattern ids (full ``sha256:``,
    bare hash, or a ≥8-char prefix), confirms with the operator (unless
    ``assume_yes`` or stdin is not a TTY in an already-confirmed flow), sets
    the status to demoted, and logs a ``pattern_demoted`` event."""
    from grove.pattern_cache import PatternCacheStore, STATUS_DEMOTED
    from grove.telemetry import log_pattern_cache_event

    if store is None:
        store = PatternCacheStore()
    pattern = _resolve_pattern(partial_id, store)
    if pattern is None:
        print(
            f"No compiled pattern matches {partial_id!r}.", file=sys.stderr,
        )
        return 1
    if pattern.status == STATUS_DEMOTED:
        print(f"Pattern {pattern.pattern_id.split(':')[-1][:12]} is already demoted.")
        return 0

    short = pattern.pattern_id.split(":")[-1][:12]
    if not assume_yes:
        prompt = (
            f"Demote {pattern.intent_class} [{pattern.cacheable_type}] "
            f"pattern {short} (served {pattern.hit_count}x)? It falls back to "
            f"T1 inference. [y/N]: "
        )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n(no input — leaving the pattern active)")
            return 1
        if answer not in ("y", "yes"):
            print("Cancelled — pattern left active.")
            return 1

    store.set_status(pattern.pattern_id, STATUS_DEMOTED)
    log_pattern_cache_event(
        event_type="pattern_demoted",
        pattern_id=pattern.pattern_id,
        intent_class=pattern.intent_class,
        cacheable_type=pattern.cacheable_type,
    )
    print(
        f"Demoted {pattern.intent_class} pattern {short} → T1. "
        f"It no longer serves from cache."
    )
    return 0


def cli_patterns_stats(*, store: Optional[Any] = None) -> int:
    """Show T0 hit volume, hit rate, and estimated inference savings (3c)."""
    if store is None:
        from grove.pattern_cache import PatternCacheStore
        store = PatternCacheStore()
    patterns = store.all()
    active = [p for p in patterns if p.status == "active"]
    total_hits = sum(p.hit_count for p in active)

    print("T0 Pattern Cache — stats")
    print()
    print(f"  Active patterns:      {len(active)}")
    print(f"  Total patterns:       {len(patterns)}")
    print(f"  Total hits (active):  {total_hits}")

    # Hit rate is derived from the intent store (T0 hits write tier_selected
    # == "T0"); telemetry itself is log-only, not a queryable store.
    try:
        from grove.intent_store import get_store as _get_intent_store
        records = list(_get_intent_store().records())
        total_turns = len(records)
        t0_turns = sum(1 for r in records if (r.tier_selected or "") == "T0")
        if total_turns:
            rate = t0_turns / total_turns * 100.0
            print(
                f"  T0 hit rate:          {rate:.1f}%  "
                f"({t0_turns}/{total_turns} recorded turns)"
            )
        else:
            print("  T0 hit rate:          n/a (no recorded turns yet)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[flywheel] hit-rate read failed: %r", exc)
        print("  T0 hit rate:          n/a (intent store unavailable)")

    per_interaction = _t1_interaction_cost_usd()
    if per_interaction is None:
        print(
            "  Estimated savings:    n/a (T1 tier declares no cost_per_mtok "
            "in routing.config.yaml)"
        )
    else:
        savings = total_hits * per_interaction
        print(
            f"  Estimated savings:    ~${savings:.4f}  "
            f"(={total_hits} hits × ~${per_interaction:.5f}/interaction, "
            f"assuming ~{_T1_AVG_INPUT_TOKENS}in/{_T1_AVG_OUTPUT_TOKENS}out "
            f"T1 tokens — rough estimate)"
        )
    return 0


def _resolve_pattern(partial_id: str, store: Any) -> Optional[Any]:
    """Resolve a compiled pattern by full id, bare hash, or ≥8-char prefix."""
    # Exact id (with or without the sha256: prefix).
    direct = store.get(partial_id)
    if direct is not None:
        return direct
    if not partial_id.startswith("sha256:"):
        direct = store.get(f"sha256:{partial_id}")
        if direct is not None:
            return direct
    bare = partial_id.split(":")[-1]
    if len(bare) < 8:
        return None
    matches = [
        p for p in store.all()
        if p.pattern_id.split(":")[-1].startswith(bare)
    ]
    return matches[0] if len(matches) == 1 else None


# ── show ─────────────────────────────────────────────────────────────


def _resolve_proposal(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
) -> Optional[RoutingProposal]:
    """Resolve a proposal by full or short id.

    Accepts the full ``sha256:...`` id, the bare hash, or a unique
    short prefix (≥ 8 chars).
    """
    target = queue_path or default_queue_path()
    proposal = read(partial_id, path=target)
    if proposal is not None:
        return proposal
    bare = partial_id.split(":")[-1]
    if len(bare) < 8:
        return None
    matches = [
        p for p in read_all(path=target)
        if p.proposal_id.split(":")[-1].startswith(bare)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def cli_show(
    partial_id: str,
    *,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Show one proposal's payload, evidence, and the YAML diff it
    would apply to the machine routing file.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}. "
            f"Run `autonomaton flywheel list` to see pending ids.",
            file=sys.stderr,
        )
        return 1

    # Sprint 60 — concierge recommendation register. Lead with a plain
    # sentence keyed to the proposal type, keep the verbatim payload and
    # diff (the operator approves the REAL change, never a paraphrase),
    # and demote the id / hash / evidence to a reference footer.
    _LEAD = {
        "skill_promotion": "Here's a skill I'd like to promote",
        "zone_promotion": "Here's a zone rule I'd like to add",
        "routing_adjustment": "Here's a routing change I'd recommend",
        "routing_update": "Here's a routing change I'd recommend",
    }
    lead = _LEAD.get(proposal.type, "Here's a change I'd recommend")
    short_id = proposal.proposal_id.split(":")[-1][:12]

    print(f"{lead} — your review before anything changes.")
    print()
    print("Here's what I'd put in place:")
    print(yaml.safe_dump(proposal.payload, sort_keys=False, default_flow_style=False))

    diff = _proposal_to_diff(proposal)
    print(f"What changes if you approve (run `flywheel approve {short_id}`):")
    print(yaml.safe_dump(diff, sort_keys=False, default_flow_style=False))

    print(
        f"Reference · ID {proposal.proposal_id} · type {proposal.type} · "
        f"eval hash {proposal.eval_hash or '(unset — pre-gate)'} · "
        f"{len(proposal.evidence)} turn(s)"
    )
    return 0


# ── approve ──────────────────────────────────────────────────────────


def _approve_routing_adjustment(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Apply a routing_adjustment proposal to routing.autonomaton.yaml.

    Returns the (target_path, applied_diff) pair so the caller can
    print the result. Per GRV-008 § III this NEVER touches
    ``routing.config.yaml``.
    """
    diff = _routing_adjustment_to_diff(proposal)
    target = machine_path or _machine_config_path()
    apply_diff_to_machine_config(diff, target)
    return target, diff


def _approve_zone_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Apply a zone_promotion proposal to zones.schema.yaml.

    Sprint 32 Phase 2c — delegates to
    :func:`grove.zone_rules.save_zone_rule` which already exists from
    Sprint 22 and writes through ruamel.yaml (preserving comments)
    with a synchronous ``zones.reload()`` at the tail. Returns the
    (rendered-rule-summary, applied-rule-dict) pair so the caller can
    print the result.
    """
    from grove.zone_rules import save_zone_rule

    tool = proposal.payload.get("tool")
    pattern = proposal.payload.get("pattern")
    zone = proposal.payload.get("zone", "green")
    reason = proposal.payload.get("reason", "")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(
            f"zone_promotion payload missing 'tool': {proposal.payload!r}"
        )
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(
            f"zone_promotion payload missing 'pattern': {proposal.payload!r}"
        )
    save_zone_rule(
        tool_id=tool, pattern=pattern, zone=zone, reason=reason,
    )
    applied = {
        "match_pattern": pattern,
        "zone": zone,
        "reason": reason,
    }
    return f"tool_zones.{tool}.rules", applied


def _approve_skill_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Apply a skill_promotion proposal (Sprint 53.2).

    Moves the skill out of quarantine via :func:`grove.sovereignty.promote`
    (NOT re-implemented) and writes a green zone rule for the promoted
    path via :func:`grove.zone_rules.save_zone_rule`, then drops the
    skills prompt cache so the promoted skill appears active. Returns the
    (target-label, applied-dict) pair for the caller to print.
    """
    from grove.sovereignty import promote as _promote
    from grove.zone_rules import save_zone_rule

    name = proposal.payload.get("skill_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"skill_promotion payload missing 'skill_name': {proposal.payload!r}"
        )

    _promote(name)
    pattern = rf".*\.grove/skills/{name}/.*"
    save_zone_rule(
        tool_id="terminal",
        pattern=pattern,
        zone="green",
        reason=f"Skill '{name}' promoted from quarantine (Sprint 53.2).",
    )
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[flywheel] skills prompt cache invalidation after promoting "
            "%r failed (non-fatal): %r", name, exc,
        )
    applied = {
        "skill_name": name,
        "promoted_to": f"~/.grove/skills/{name}/",
        "zone_rule": {"match_pattern": pattern, "zone": "green"},
    }
    return f"skill '{name}' (move + green rule)", applied


def _has_successful_quarantine_execution(skill_name: str) -> bool:
    """True if a quarantine_skill_disposition('once') event for ``skill_name``
    exists in any Kaizen ledger (Sprint 53.2 Phase 4 — strict gate).

    Scans every session file under ``~/.grove/.kaizen_ledger/`` because the
    "allow once" execution may have happened in any session before the
    operator runs ``flywheel approve --strict``.
    """
    from hermes_constants import get_hermes_home
    ledger_dir = Path(get_hermes_home()) / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return False
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        event.get("event_type") == "quarantine_skill_disposition"
                        and event.get("skill_name") == skill_name
                        and event.get("disposition") == "once"
                    ):
                        return True
        except OSError:
            continue
    return False


def _enforce_strict_skill_promotion(proposal: RoutingProposal) -> bool:
    """Gate a strict skill promotion. Returns True to proceed (Phase 4b).

    Enforces: (a) a review diff of the promotion act, (b) at least one
    logged successful "allow once" execution of the skill in the Kaizen
    ledger, and (c) explicit y/N confirmation. Any failure returns False
    and the caller aborts — the skill stays quarantined.
    """
    name = proposal.payload.get("skill_name", "?")

    # (a) Review diff.
    print(f"Strict promotion review — skill {name!r}:")
    print(yaml.safe_dump(
        _proposal_to_diff(proposal), sort_keys=False, default_flow_style=False,
    ))

    # (b) Require a logged successful execution.
    if not _has_successful_quarantine_execution(name):
        print(
            f"Refusing: no successful 'allow once' execution of {name!r} is "
            f"logged in the Kaizen ledger. Run the skill once (and allow it) "
            f"before promoting under --strict.",
            file=sys.stderr,
        )
        return False

    # (c) Explicit confirmation.
    try:
        answer = input(
            f"Promote skill {name!r} to the trusted set? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer not in ("y", "yes"):
        print(f"Aborted — skill {name!r} remains quarantined.")
        return False
    return True


def _approve_pattern_promotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Activate a compiled T0 pattern (Sprint 48 Phase 3).

    The compiled entry already lives (status=suspended) in
    ``pattern_cache.db``; approval flips it to ``active`` so Sprint 49's T0
    path will serve it, and logs a ``pattern_promoted`` telemetry event. The
    system never self-activates — this only runs on operator approval."""
    from datetime import datetime, timezone
    from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE

    pattern_id = proposal.payload.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id.strip():
        raise ValueError(
            f"pattern_promotion payload missing 'pattern_id': {proposal.payload!r}"
        )
    store = PatternCacheStore()
    now = datetime.now(timezone.utc).isoformat()
    if not store.set_status(pattern_id, STATUS_ACTIVE, promoted_at=now):
        raise ValueError(
            f"compiled pattern {pattern_id!r} not found in pattern_cache.db — "
            f"cannot activate (was it compiled?)."
        )
    intent_class = proposal.payload.get("intent_class", "?")
    cacheable_type = proposal.payload.get("cacheable_type", "?")
    samples = proposal.payload.get("sample_queries") or []
    sample = samples[0] if samples else "?"
    # Read what the pattern will actually serve so the operator sees it.
    activated = store.get(pattern_id)
    cached = activated.cached_response if activated else None
    invocation = activated.compiled_invocation if activated else None
    logger.info(
        "[flywheel] pattern_promoted: pattern_id=%s intent_class=%s "
        "cacheable_type=%s — now active at T0",
        pattern_id, intent_class, cacheable_type,
    )
    applied = {
        "pattern_id": pattern_id,
        "sample_query": sample,
        "intent_class": intent_class,
        "cacheable_type": cacheable_type,
        "cached_response": cached,
        "compiled_invocation": invocation,
        "status": STATUS_ACTIVE,
        "tier": "T0",
        "effect": "the next matching query resolves from T0 — no model call",
    }
    if cacheable_type == "static":
        label = (
            f"pattern for “{sample}” ({intent_class}, static). "
            f"Cached response: {cached!r}. "
            f"Next matching query resolves from T0 — no model call."
        )
    else:
        label = (
            f"pattern for “{sample}” ({intent_class}, executable). "
            f"Compiled invocation: {invocation}. "
            f"Next matching query executes the tool model-free."
        )
    return label, applied


def _approve_pattern_demotion(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Confirm a drift-triggered demotion (Sprint 49 Phase 2).

    The Dispatcher already auto-SUSPENDED the pattern when the operator
    corrected a T0 hit. Approving the proposal confirms the disposition:
    flip suspended → demoted (the pattern falls back to T1 inference and
    stays out of the cache). Rejecting the proposal reverses it (see
    ``cli_reject``), re-activating the pattern."""
    from grove.pattern_cache import PatternCacheStore, STATUS_DEMOTED
    from grove.telemetry import log_pattern_cache_event

    pattern_id = proposal.payload.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id.strip():
        raise ValueError(
            f"pattern_demotion payload missing 'pattern_id': {proposal.payload!r}"
        )
    store = PatternCacheStore()
    if not store.set_status(pattern_id, STATUS_DEMOTED):
        raise ValueError(
            f"compiled pattern {pattern_id!r} not found in pattern_cache.db — "
            f"cannot demote."
        )
    intent_class = proposal.payload.get("intent_class", "?")
    log_pattern_cache_event(
        event_type="pattern_demoted",
        pattern_id=pattern_id,
        intent_class=intent_class,
        correction_turn_id=proposal.payload.get("correction_turn_id"),
    )
    applied = {
        "pattern_id": pattern_id,
        "intent_class": intent_class,
        "status": STATUS_DEMOTED,
        "tier": "T0 → T1 (falls back to inference)",
        "trigger": proposal.payload.get("trigger", "correction_drift"),
    }
    return f"{intent_class} pattern", applied


def _approve_skill_synthesis(
    proposal: RoutingProposal,
    *,
    machine_path: Optional[Path] = None,  # uniform registry signature; unused
) -> Tuple[str, Dict[str, Any]]:
    """Materialize a drafted skill into quarantine (B1 Fork B — unify).

    This is the SINGLE door by which a ``skill_synthesis`` draft becomes a
    proposed (non-executable) record on disk. It performs the EXACT two writes
    the retired ``Dispatcher._maybe_materialize_synthesized_skills`` did:
    ``write_proposal`` (the SKILL.md into ``.andon/<name>/``) + the best-effort
    ``register_proposed_skill`` record mint. It does NOT chain promotion — the
    skill stays ``proposed`` and a follow-on ``skill_promotion`` (or
    ``hermes andon promote``) takes it active, exactly as before.

    Idempotent: a skill already on disk (active or quarantined) is a no-op
    staging — the proposal is still consumed (removed) by the caller.
    """
    from grove.skills import active_path, proposal_path, write_proposal

    name = proposal.payload.get("skill_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"skill_synthesis payload missing 'skill_name': {proposal.payload!r}"
        )
    name = name.strip()

    if active_path(name).exists() or proposal_path(name).exists():
        applied = {
            "skill_name": name,
            "staged_to": f"~/.grove/skills/.andon/{name}/",
            "note": "already on disk — staging is a no-op",
        }
        return f"skill {name!r} (already staged)", applied

    skill_md = proposal.payload.get("skill_md")
    if not isinstance(skill_md, str) or not skill_md.strip():
        raise ValueError(
            f"skill_synthesis payload missing 'skill_md': {proposal.payload!r}"
        )

    write_proposal(name, skill_md)
    # GRV-009 E6b C2 — mint the state:proposed record alongside the .andon body
    # (proposed is the sole review lock; non-executable behind the 4.1
    # checkpoint). Best-effort: a mint failure leaves the body staged and the
    # quarantine gate still fires; it is flagged LOUD, never silently swallowed.
    record_minted = True
    try:
        from grove.capability_registry import (
            _frontmatter_value,
            register_proposed_skill,
        )
        _cat = _frontmatter_value(skill_md, "category") or ""
        register_proposed_skill(name, _cat, skill_md)
    except Exception:  # noqa: BLE001
        record_minted = False
        logger.warning(
            "[flywheel] proposed-record mint failed for %r (proposal staged to "
            ".andon/, record not minted — reconcile manually)", name,
            exc_info=True,
        )
    applied = {
        "skill_name": name,
        "staged_to": f"~/.grove/skills/.andon/{name}/",
        "record_state": "proposed (non-executable until promoted)",
        "record_minted": record_minted,
        "next": "promote via `hermes andon promote` or a skill_promotion proposal",
    }
    return f"skill {name!r} (.andon/ + proposed record)", applied


def cli_approve(
    partial_id: str,
    *,
    strict: bool = False,
    queue_path: Optional[Path] = None,
    machine_path: Optional[Path] = None,
) -> int:
    """Apply the proposal; remove from queue.

    B1 — the approved-write gate. Dispatch is a single :data:`PROPOSAL_HANDLERS`
    registry lookup (:func:`_handler_for`): the row's ``apply_callback`` performs
    the write and the row's ``apply_label_prefix`` labels the result. Adding a
    new approved-write class is a new row, not a new branch here. ``--strict``
    runs only the row's optional ``strict_gate`` (today: skill_promotion).

    Per GRV-008 § III the routing path NEVER opens ``routing.config.yaml`` for
    writing — operator-authored configuration is inviolate.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    # B1 — single registry dispatch. Unknown type is a loud, NON-destructive
    # failure at the CLI boundary: stderr message + rc=1, and the proposal
    # stays in the queue for the operator to handle (Sprint 32 contract).
    try:
        handler = _handler_for(proposal.type)
    except ValueError:
        print(
            f"Cannot approve proposal type {proposal.type!r}. Supported: "
            f"routing_adjustment, zone_promotion, skill_promotion, "
            f"pattern_promotion, pattern_demotion, skill_synthesis.",
            file=sys.stderr,
        )
        return 1

    # Phase 4 — strict mode gates only the types that declare a strict_gate
    # (skill_promotion: diff + logged execution + confirmation). Normal approve
    # is unchanged; --strict is a no-op for every other type.
    if strict and handler.strict_gate is not None and not handler.strict_gate(proposal):
        return 1

    target, applied = handler.apply_callback(proposal, machine_path=machine_path)
    applied_label = f"{handler.apply_label_prefix}{target}"

    removed = remove(
        proposal.proposal_id,
        path=queue_path or default_queue_path(),
    )
    if not removed:
        logger.warning(
            "[flywheel] proposal %s was applied but had already been "
            "removed from the queue", proposal.proposal_id,
        )

    print(f"Approved {proposal.proposal_id}")
    print(applied_label)
    print()
    print("Applied:")
    print(yaml.safe_dump(applied, sort_keys=False, default_flow_style=False))
    return 0


# ── reject ───────────────────────────────────────────────────────────


def _reject_pattern_promotion(proposal: RoutingProposal) -> None:
    # Sprint 48 — a rejected pattern is marked rejected in pattern_cache.db so
    # the scanner NEVER re-proposes the same pattern (3e). The compiled entry
    # stays as a tombstone; the proposer skips any pattern already in the store.
    pattern_id = proposal.payload.get("pattern_id")
    if isinstance(pattern_id, str) and pattern_id:
        try:
            from grove.pattern_cache import PatternCacheStore, STATUS_REJECTED
            PatternCacheStore().set_status(pattern_id, STATUS_REJECTED)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flywheel] could not mark pattern %s rejected: %r",
                pattern_id, exc,
            )


def _reject_pattern_demotion(proposal: RoutingProposal) -> None:
    # Sprint 49 — rejecting a drift-triggered demotion REVERSES it: the
    # pattern was auto-suspended when the operator corrected a T0 hit, and
    # rejecting the demotion proposal means "keep it active" — re-activate so
    # it serves again. The operator overrules the drift signal.
    pattern_id = proposal.payload.get("pattern_id")
    if isinstance(pattern_id, str) and pattern_id:
        try:
            from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE
            from datetime import datetime, timezone
            PatternCacheStore().set_status(
                pattern_id, STATUS_ACTIVE,
                promoted_at=datetime.now(timezone.utc).isoformat(),
            )
            print(
                f"Reversed: pattern {pattern_id.split(':')[-1][:12]} "
                f"re-activated (drift signal overruled).",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flywheel] could not re-activate pattern %s: %r",
                pattern_id, exc,
            )


def cli_reject(
    partial_id: str,
    *,
    reason: Optional[str] = None,
    queue_path: Optional[Path] = None,
) -> int:
    """Remove a proposal from the queue. No config change.

    ``reason`` is recorded at INFO level for the Kaizen Ledger to
    pick up. Sprint 47 does not require structured rejection
    telemetry — that lands when the Kaizen Ledger integration sprint
    ships.
    """
    proposal = _resolve_proposal(partial_id, queue_path=queue_path)
    if proposal is None:
        print(
            f"No proposal matches {partial_id!r}.", file=sys.stderr,
        )
        return 1

    # B1 — single registry dispatch for the OPTIONAL pre-removal cleanup
    # (pattern_promotion → tombstone; pattern_demotion → reverse). Reject is
    # "remove from the queue" for every type; the cleanup is type-specific and
    # most types declare none. An unrecognised type has no handler and is still
    # removable — the operator must always be able to dismiss a queued item, so
    # this lookup is deliberately tolerant (no raise) where approve/render are
    # strict.
    try:
        handler: Optional[ProposalHandler] = _handler_for(proposal.type)
    except ValueError:
        handler = None
    if handler is not None and handler.reject_callback is not None:
        handler.reject_callback(proposal)

    target = queue_path or default_queue_path()
    removed = remove(proposal.proposal_id, path=target)
    if not removed:
        print(
            f"Proposal {proposal.proposal_id} was not in the queue "
            f"(already removed?).",
            file=sys.stderr,
        )
        return 1
    if reason:
        logger.info(
            "[flywheel] proposal %s rejected — %s",
            proposal.proposal_id, reason,
        )
    print(f"Rejected {proposal.proposal_id}")
    if reason:
        print(f"Reason:   {reason}")
    return 0


# ── proposal handler registry (B1 Fork A, FULL) ──────────────────────
#
# One row per proposal type. Adding a new approved-write class is a new row in
# this table — never a new branch in four if/elif ladders (anti-cruft #1). The
# four operator surfaces (_proposal_to_diff, _format_summary, cli_approve,
# cli_reject) all dispatch through :func:`_handler_for`; none of them branch on
# ``proposal.type`` directly.


@dataclass(frozen=True)
class ProposalHandler:
    """The per-type behavior for one proposal class.

    * ``summary_renderer`` — the per-type BODY of the one-line list summary
      (shared framing lives in :func:`_format_summary`).
    * ``diff_renderer`` — the YAML diff ``cli_show`` displays.
    * ``apply_callback`` — the approved write. Signature
      ``(proposal, *, machine_path=None) -> (target_label, applied_dict)``;
      ``machine_path`` is consumed only by routing_adjustment.
    * ``apply_label_prefix`` — prepended to the apply target for the
      ``cli_approve`` "Applied/Promoted/Demoted/Staged" line.
    * ``reject_callback`` — OPTIONAL pre-removal cleanup on reject (None for
      types whose reject is a plain queue removal).
    * ``strict_gate`` — OPTIONAL ``--strict`` gate; return False to abort the
      approve (only skill_promotion declares one).
    """

    summary_renderer: Callable[[RoutingProposal], str]
    diff_renderer: Callable[[RoutingProposal], Dict[str, Any]]
    apply_callback: Callable[..., Tuple[Any, Dict[str, Any]]]
    apply_label_prefix: str
    reject_callback: Optional[Callable[[RoutingProposal], None]] = None
    strict_gate: Optional[Callable[[RoutingProposal], bool]] = None


PROPOSAL_HANDLERS: Dict[str, ProposalHandler] = {
    PROPOSAL_TYPE_ROUTING_ADJUSTMENT: ProposalHandler(
        summary_renderer=_summary_routing_adjustment,
        diff_renderer=_routing_adjustment_to_diff,
        apply_callback=_approve_routing_adjustment,
        apply_label_prefix="Applied to: ",
    ),
    PROPOSAL_TYPE_ZONE_PROMOTION: ProposalHandler(
        summary_renderer=_summary_zone_promotion,
        diff_renderer=_diff_zone_promotion,
        apply_callback=_approve_zone_promotion,
        apply_label_prefix="Applied to: ",
    ),
    PROPOSAL_TYPE_SKILL_PROMOTION: ProposalHandler(
        summary_renderer=_summary_skill_promotion,
        diff_renderer=_diff_skill_promotion,
        apply_callback=_approve_skill_promotion,
        apply_label_prefix="Promoted: ",
        strict_gate=_enforce_strict_skill_promotion,
    ),
    PROPOSAL_TYPE_PATTERN_PROMOTION: ProposalHandler(
        summary_renderer=_summary_pattern_promotion,
        diff_renderer=_diff_pattern_promotion,
        apply_callback=_approve_pattern_promotion,
        apply_label_prefix="Promoted to T0: ",
        reject_callback=_reject_pattern_promotion,
    ),
    PROPOSAL_TYPE_PATTERN_DEMOTION: ProposalHandler(
        summary_renderer=_summary_pattern_demotion,
        diff_renderer=_diff_pattern_demotion,
        apply_callback=_approve_pattern_demotion,
        apply_label_prefix="Demoted from T0: ",
        reject_callback=_reject_pattern_demotion,
    ),
    PROPOSAL_TYPE_SKILL_SYNTHESIS: ProposalHandler(
        summary_renderer=_summary_skill_synthesis,
        diff_renderer=_diff_skill_synthesis,
        apply_callback=_approve_skill_synthesis,
        apply_label_prefix="Staged: ",
    ),
}


def _handler_for(proposal_type: str) -> ProposalHandler:
    """Resolve a proposal type to its handler row.

    Resolves the legacy ``routing_update`` spelling to the routing_adjustment
    row in this ONE place (the alias no longer leaks into every surface).
    Raises ``ValueError`` on an unrecognised type — there is no silent
    fallback handler (fail loud, GRV operating principle #1).
    """
    canonical = (
        PROPOSAL_TYPE_ROUTING_ADJUSTMENT
        if proposal_type == _LEGACY_ROUTING_TYPE
        else proposal_type
    )
    try:
        return PROPOSAL_HANDLERS[canonical]
    except KeyError:
        raise ValueError(
            f"unsupported proposal type {proposal_type!r}; recognised: "
            f"{', '.join(sorted(PROPOSAL_HANDLERS))}"
        )
