"""Per-turn context-token accounting for the `/context` slash command.

Sprint 24a (context-instrumentation-v1). Pure measurement: no prompt-text
changes, no behavior changes. The operator runs `/context` in an active
session and sees exactly where the per-turn input tokens go — system
prompt by sub-section, tool schemas grouped by source, conversation
history, cellar context, and the grand total.

Design decisions live on the Sprint 24a Notion page:
    https://www.notion.so/36b780a78eef813ca0edd78e87c0066f

Three functions form the public surface:

* ``build_context_report(agent, ...)`` — assemble the per-section token
  counts from the agent's current state. Reads the labeled section
  breakdown the Sprint 24a refactor added to
  ``AIAgent._build_system_prompt_parts``, enumerates ``agent.tools``
  for the schema budget, tokenises the caller-supplied conversation
  history and the cellar block carried on
  ``agent.ephemeral_system_prompt``.
* ``format_context_report(report)`` — render the D5 table to a string
  suitable for stdout. Sorted by tokens descending within each bucket,
  percentages of the grand total, snapshot path appended.
* ``persist_context_report(report, base_dir=...)`` — write the JSON
  snapshot to ``~/.grove/.context_snapshots/<session>_<turn>.json``
  per D4 schema. Returns the written ``Path``.

The token counts come from ``agent.model_metadata.estimate_tokens_rough``
(the runtime's existing chars/4 estimator). No new dependency, no
network call. The grand total this reports is the same estimator the
agent's pre-flight checks use, so it should be in the ballpark of the
provider's reported ``input_tokens`` — but exact parity is not promised
(images, prompt-cache mechanics, and provider-specific tokenisation
all introduce drift). If the gap on a real session is large enough to
matter, that becomes a Sprint 24b/24c input rather than something this
module tries to reconcile.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent.model_metadata import (
    estimate_messages_tokens_rough,
    estimate_tokens_rough,
)

logger = logging.getLogger(__name__)


DEFAULT_SNAPSHOT_DIR = Path.home() / ".grove" / ".context_snapshots"


@dataclass(frozen=True)
class ContextReport:
    """One per-turn snapshot of where the input tokens go.

    Field shapes mirror the D4 snapshot JSON schema:

    * ``system_prompt_sections`` maps label → tokens for every
      sub-section produced by ``_build_system_prompt_parts``. Includes
      a ``"_total"`` key for the bucket sum (so callers can read either
      the per-section breakdown or just the rollup).
    * ``tool_schemas`` maps group-name → tokens for each tool group
      derived from ``agent.tools``. Also includes ``"_total"``.
    * ``conversation_history`` is a single integer (all messages
      combined).
    * ``cellar_context`` is a single integer (the ephemeral block).
    * ``grand_total`` is the sum of the four bucket totals.
    * Sprint 73 (D10) tier-budget provenance — WHY this payload:
      ``applied_tier`` (the routed tier), ``excluded_context_blocks`` (the
      gateable blocks the tier gated OFF, from the retained
      ``ComposedPrompt.gated_context_blocks``), ``excluded_mcp`` (MCP servers
      the tier excluded) and ``stripped_groups`` (intent groups the tier
      capped). Empty on a non-budgeted turn.
    """

    session_id: str
    turn: int
    timestamp: str
    model: Optional[str]
    system_prompt_sections: Dict[str, int]
    tool_schemas: Dict[str, int]
    conversation_history: int
    cellar_context: int
    grand_total: int
    snapshot_path: Path = field(default_factory=lambda: Path(""))
    applied_tier: Optional[str] = None
    excluded_context_blocks: List[str] = field(default_factory=list)
    excluded_mcp: List[str] = field(default_factory=list)
    stripped_groups: List[str] = field(default_factory=list)
    # Sprint 74 Phase 4 — disclosure observability.
    #  * ``always_loaded`` — the itemized always-loaded FLOOR (the Sprint 75
    #    scoreboard): identity sub-parts, the Dock goal-index, the disclosure
    #    tool-index, plus ``_total``. Re-measured from live config; read-only.
    #  * ``disclosed_payloads`` — every unit whose full payload entered context
    #    this turn: ``{unit_id, kind, tokens, reason}`` where reason is
    #    intent-match / keyword-match / dock-match (eager) or agent-pull.
    #  * ``disclosure_tier_mode`` — ``eager-core`` (T1), ``index+pull`` (T2/T3),
    #    or ``eager`` (no tier / no disclosure).
    always_loaded: Dict[str, int] = field(default_factory=dict)
    disclosed_payloads: List[Dict[str, Any]] = field(default_factory=list)
    disclosure_tier_mode: Optional[str] = None


def measure_always_loaded_floor(
    manifest: Optional[Any] = None, tier: Optional[str] = None
) -> Dict[str, int]:
    """Itemize the always-loaded FLOOR — the Sprint 75 scoreboard (Phase 4).

    Breaks the irreducible per-turn prefill into named lines so the operator can
    see what rides EVERY turn regardless of disclosure: the identity composition
    (by sub-part), the Dock goal-index, and the disclosure tool-index (the
    one-liner manifest). Read-only — re-measures from live config via the
    chars/4 estimator; never mutates state.

    Args:
        manifest: a pre-built merged manifest (the agent's, when available) for
            the tool-index line; falls back to the declarative-only manifest.
        tier: Sprint 75 — the routed tier. The identity is composed at THIS
            tier, so gated sub-parts (affordances/operator/capabilities on T1)
            read 0 and the floor reflects what was actually sent.

    Returns:
        ``{label: tokens}`` with a ``_total`` rollup.
    """
    out: Dict[str, int] = {}

    # Identity floor — the dominant component (named here so Sprint 75 can see it).
    try:
        from grove.identity import load_identity, _strip_frontmatter

        comp = load_identity(tier=tier)
        parts = {
            "identity.constitution": comp.constitution,
            "identity.soul": _strip_frontmatter(comp.soul),
            "identity.register": comp.register_overlay,
            "identity.affordances": comp.affordances,
            "identity.capabilities": comp.capabilities,
            "identity.operator": comp.operator,
        }
        for label, text in parts.items():
            out[label] = estimate_tokens_rough(text or "")
        # The Dock goal-index rides the identity layer but is its own line.
        out["dock_goal_index"] = estimate_tokens_rough(comp.goals or "")
    except Exception as exc:  # observability must never crash a turn
        logger.debug("[context_report] identity floor unavailable: %r", exc)

    # Disclosure tool-index — the always-loaded one-liner manifest.
    try:
        units = manifest
        if units is None:
            from grove.manifest import load_manifest
            units = load_manifest()
        idx = "\n".join(f"- {u.id}: {u.oneline}" for u in units)
        out["tool_index"] = estimate_tokens_rough(idx)
    except Exception as exc:
        logger.debug("[context_report] tool-index floor unavailable: %r", exc)
        out["tool_index"] = 0

    out["_total"] = sum(v for k, v in out.items() if k != "_total")
    return out


def _tier_mode(tier: Optional[str]) -> str:
    """The disclosure mode for a routed tier: T1 eager-core, T2/T3 index+pull,
    anything else (no tier / cloud-without-tier) eager."""
    if tier == "T1":
        return "eager-core"
    if tier in ("T2", "T3"):
        return "index+pull"
    return "eager"


def snapshot_path_for(
    session_id: str,
    turn: int,
    base_dir: Optional[Path] = None,
) -> Path:
    """Compute the deterministic snapshot path for a session/turn pair.

    Shared by ``persist_context_report`` (where it gets written) and
    ``format_context_report`` (where it gets shown to the operator) so
    the displayed path matches the file that actually lands on disk.
    """
    base = Path(base_dir) if base_dir else DEFAULT_SNAPSHOT_DIR
    safe_session = (session_id or "no-session").strip() or "no-session"
    return base / f"{safe_session}_{turn}.json"


# ── Tool-schema grouping ─────────────────────────────────────────────────────


def _tool_group_for(tool_name: str) -> str:
    """Bucket a tool name into an operator-readable group.

    Heuristic: the first segment before ``__``, ``_``, or ``-`` is the
    group, when one exists. Bare names (no separator) get their own group.
    This is intentionally simple — Sprint 24c+ can introduce a richer
    namespace registry if cluster shapes get unhelpful.

    Examples:
        ``mcp__notion__search`` → ``mcp`` (the SDK wraps MCP tools under
            an ``mcp__`` prefix; the wrapper IS the group for v0.1)
        ``notion_search``        → ``notion``
        ``notion-search``        → ``notion``
        ``gws_calendar_list``    → ``gws``
        ``terminal``             → ``terminal``
        ``memory``               → ``memory``
    """
    if not tool_name:
        return "_unknown"
    name = str(tool_name)
    for sep in ("__", "_", "-"):
        if sep in name:
            head = name.split(sep, 1)[0]
            return head or "_unknown"
    return name


def _serialize_tool_for_tokens(tool: Mapping[str, Any]) -> str:
    """Render one tool definition to the string the API would carry.

    OpenAI-format tool definitions are ``{"function": {...}}`` dicts;
    Anthropic-format are ``{"name": ..., "input_schema": {...}}``. Both
    serialise cleanly as JSON, and the JSON size is the right proxy for
    what the provider tokenises.
    """
    try:
        return json.dumps(tool, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        # Non-serialisable artefacts (callables, unhashable wrappers) —
        # fall back to repr so the count is at least directionally right.
        return repr(tool)


def _tool_name_of(tool: Any) -> str:
    """Best-effort name extraction across the tool-definition shapes
    Hermes hands the providers (OpenAI ``{"function": {"name": ...}}``,
    Anthropic ``{"name": ...}``, bare dict)."""
    if not isinstance(tool, Mapping):
        return ""
    fn = tool.get("function")
    if isinstance(fn, Mapping):
        return str(fn.get("name") or "")
    return str(tool.get("name") or "")


# ── build / format / persist ─────────────────────────────────────────────────


def build_context_report(
    agent: Any,
    *,
    conversation_history: Optional[Sequence[Mapping[str, Any]]] = None,
    session_id: Optional[str] = None,
    turn: Optional[int] = None,
    system_message: Optional[str] = None,
    snapshot_base_dir: Optional[Path] = None,
) -> ContextReport:
    """Assemble the per-section token counts for the current turn.

    Args:
        agent: the live ``AIAgent`` (or any object that exposes
            ``_build_system_prompt_parts``, ``tools``,
            ``ephemeral_system_prompt``, ``session_id``, ``model``).
        conversation_history: the message list as the caller would
            pass to ``agent.run_conversation``. The slash-command
            handler reads this from the REPL's
            ``self.conversation_history``. When omitted, the bucket
            reports zero tokens.
        session_id / turn: identifiers stamped on the snapshot file
            name. When ``session_id`` is omitted, ``agent.session_id``
            is tried; if that's also empty, ``"no-session"`` is used.
            ``turn`` defaults to ``0`` — the handler should pass the
            REPL's running turn counter.
        system_message: caller-supplied system_message override, if
            the agent is configured to accept one for the current
            turn. Passed straight through to
            ``_build_system_prompt_parts`` so the section breakdown
            includes it.
        snapshot_base_dir: override the snapshot directory (tests use
            ``tmp_path``).

    Returns:
        A ``ContextReport`` ready for ``format_context_report`` /
        ``persist_context_report``.
    """
    resolved_session = session_id or getattr(agent, "session_id", "") or ""
    resolved_turn = int(turn) if turn is not None else 0
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    model = getattr(agent, "model", None)

    # System prompt — read the RETAINED ComposedPrompt: the prompt actually
    # injected this turn, NOT a recompose. Sprint 36 (GRV-007) extracted the
    # old ``_build_system_prompt_parts`` method into the PromptComposer; the
    # canonical source is the composition RESULT the compose path stashes on
    # ``agent._composed_prompt`` (Sprint 73 Phase 5). Reading data — never
    # re-running compose — keeps provenance truthful: a fresh compose could
    # diverge from what was sent (volatile providers, carrier/state drift).
    # ``system_message`` is intentionally unused here; it was already folded
    # into the retained result at compose time.
    composed = getattr(agent, "_composed_prompt", None)
    sections_raw = getattr(composed, "sections", None)
    gated_blocks = getattr(composed, "gated_context_blocks", None) or frozenset()
    system_prompt_sections: Dict[str, int] = {}
    if isinstance(sections_raw, Mapping):
        for label, text in sections_raw.items():
            system_prompt_sections[str(label)] = estimate_tokens_rough(str(text or ""))
    system_prompt_sections["_total"] = sum(
        v for k, v in system_prompt_sections.items() if k != "_total"
    )

    # Tool schemas — enumerate the per-turn tool view the LLM actually
    # received and bucket by name prefix. Sprint 29 introduced
    # ``_tools_for_api`` on AIAgent: it returns the per-turn filtered
    # set when ``_maybe_apply_tool_filter`` ran (Sprint 29 Phase 2),
    # otherwise the full ``self.tools`` registry. Prefer the property
    # so /context reflects what was sent to the model on this turn,
    # not the full registry the agent still holds for fallback. Falls
    # back to ``agent.tools`` for legacy / test agents that don't
    # carry the Sprint 29 property; empty / missing → zero-tokens.
    tools_attr = (
        getattr(agent, "_tools_for_api", None)
        or getattr(agent, "tools", None)
        or []
    )
    tool_schemas: Dict[str, int] = {}
    for tool in tools_attr:
        if not isinstance(tool, Mapping):
            continue
        group = _tool_group_for(_tool_name_of(tool))
        tokens = estimate_tokens_rough(_serialize_tool_for_tokens(tool))
        tool_schemas[group] = tool_schemas.get(group, 0) + tokens
    tool_schemas["_total"] = sum(v for k, v in tool_schemas.items() if k != "_total")

    # Conversation history — tokenise via the runtime's existing
    # message-list estimator so image costs are counted correctly.
    history_total = 0
    if conversation_history:
        history_total = estimate_messages_tokens_rough(list(conversation_history))

    # Cellar context — Sprint 13 rides cellar retrieval through
    # ``agent.ephemeral_system_prompt``. Other ephemeral content (if
    # any) is also captured here; sub-dis-aggregation is a Sprint 24c+
    # concern per the design notes.
    cellar_text = getattr(agent, "ephemeral_system_prompt", None) or ""
    cellar_tokens = estimate_tokens_rough(str(cellar_text))

    grand_total = (
        system_prompt_sections["_total"]
        + tool_schemas["_total"]
        + history_total
        + cellar_tokens
    )

    # Tier-budget provenance (D10) — WHY this payload. The applied tier and the
    # tool-side exclusions ride ``_last_tool_selection`` (enriched in Phase 4b);
    # the gated context blocks come from the retained ComposedPrompt above.
    sel = getattr(agent, "_last_tool_selection", None) or {}
    applied_tier = sel.get("tier")
    excluded_mcp = sorted(str(s) for s in (sel.get("excluded_mcp") or []))
    stripped_groups = sorted(str(s) for s in (sel.get("stripped_groups") or []))
    excluded_context_blocks = sorted(str(b) for b in gated_blocks)

    # Sprint 74 Phase 4 — the floor scoreboard + the disclosure ledger.
    # Sprint 75 — compose the floor at the routed tier so it reflects the
    # per-tier identity actually sent (T1 drops affordances/operator/capabilities).
    always_loaded = measure_always_loaded_floor(
        getattr(agent, "_disclosure_manifest", None),
        tier=applied_tier,
    )
    disclosed_payloads = [
        dict(entry) for entry in (getattr(agent, "_disclosure_log", None) or [])
        if isinstance(entry, Mapping)
    ]
    disclosure_tier_mode = _tier_mode(applied_tier)

    snapshot = snapshot_path_for(resolved_session, resolved_turn, snapshot_base_dir)

    return ContextReport(
        session_id=resolved_session,
        turn=resolved_turn,
        timestamp=timestamp,
        model=str(model) if model else None,
        system_prompt_sections=system_prompt_sections,
        tool_schemas=tool_schemas,
        conversation_history=history_total,
        cellar_context=cellar_tokens,
        grand_total=grand_total,
        snapshot_path=snapshot,
        applied_tier=applied_tier,
        excluded_context_blocks=excluded_context_blocks,
        excluded_mcp=excluded_mcp,
        stripped_groups=stripped_groups,
        always_loaded=always_loaded,
        disclosed_payloads=disclosed_payloads,
        disclosure_tier_mode=disclosure_tier_mode,
    )


def _fmt_int(n: int) -> str:
    """Right-aligned thousands-separated integer (``"  1,247"``)."""
    return f"{n:>9,}"


def _fmt_pct(numerator: int, denominator: int) -> str:
    """Percentage of total, one decimal place, suffixed with ``%``."""
    if not denominator:
        return f"{0.0:>5.1f}%"
    return f"{(100.0 * numerator / denominator):>5.1f}%"


def format_context_report(report: ContextReport) -> str:
    """Render the D5 table to a string.

    Sort discipline: each bucket's sub-entries are sorted by token count
    descending so the dominant lines are on top. ``"_total"`` keys are
    rendered as the bucket header line, not as a sub-entry.
    """
    out: List[str] = []
    header_session = report.session_id or "(no session)"
    header_model = f" · {report.model}" if report.model else ""
    out.append(
        f"Context breakdown — session {header_session}, "
        f"turn {report.turn}, {report.timestamp}{header_model}"
    )
    out.append("")
    out.append(f"{'Section':<32}{'Tokens':>10}{'%':>8}")
    out.append("─" * 50)

    grand = report.grand_total or 0

    # System prompt bucket header + sub-sections.
    sp_total = report.system_prompt_sections.get("_total", 0)
    out.append(
        f"{'System prompt total':<32}{_fmt_int(sp_total)}{_fmt_pct(sp_total, grand):>8}"
    )
    sp_subs = [
        (k, v) for k, v in report.system_prompt_sections.items() if k != "_total"
    ]
    sp_subs.sort(key=lambda kv: kv[1], reverse=True)
    for label, tokens in sp_subs:
        out.append(
            f"  {label:<30}{_fmt_int(tokens)}{_fmt_pct(tokens, grand):>8}"
        )
    out.append("")

    # Tool schemas bucket header + per-group lines.
    ts_total = report.tool_schemas.get("_total", 0)
    out.append(
        f"{'Tool schemas total':<32}{_fmt_int(ts_total)}{_fmt_pct(ts_total, grand):>8}"
    )
    ts_subs = [(k, v) for k, v in report.tool_schemas.items() if k != "_total"]
    ts_subs.sort(key=lambda kv: kv[1], reverse=True)
    for label, tokens in ts_subs:
        out.append(
            f"  {label:<30}{_fmt_int(tokens)}{_fmt_pct(tokens, grand):>8}"
        )
    out.append("")

    # Single-line buckets.
    out.append(
        f"{'Conversation history':<32}"
        f"{_fmt_int(report.conversation_history)}"
        f"{_fmt_pct(report.conversation_history, grand):>8}"
    )
    out.append(
        f"{'Cellar context (this turn)':<32}"
        f"{_fmt_int(report.cellar_context)}"
        f"{_fmt_pct(report.cellar_context, grand):>8}"
    )

    out.append("─" * 50)
    out.append(
        f"{'Per-turn input total':<32}{_fmt_int(grand)}{_fmt_pct(grand, grand):>8}"
    )

    # Tier-budget provenance (D10) — WHY this payload is the size it is.
    if (
        report.applied_tier
        or report.excluded_context_blocks
        or report.excluded_mcp
        or report.stripped_groups
    ):
        out.append("")
        out.append(f"Tier budget: {report.applied_tier or '(none)'}")
        if report.excluded_context_blocks:
            out.append(
                f"  context blocks gated off : {', '.join(report.excluded_context_blocks)}"
            )
        if report.excluded_mcp:
            out.append(
                f"  MCP servers excluded     : {', '.join(report.excluded_mcp)}"
            )
        if report.stripped_groups:
            out.append(
                f"  tool groups stripped     : {', '.join(report.stripped_groups)} "
                f"(escalation requested — see ledger)"
            )

    # Sprint 74 Phase 4 — the always-loaded FLOOR (the Sprint 75 scoreboard).
    # What rides EVERY turn regardless of tier or disclosure. Identity dominates
    # here by design; that is the residual Sprint 75 targets.
    if report.always_loaded:
        out.append("")
        floor_total = report.always_loaded.get("_total", 0)
        out.append(
            f"{'Always-loaded floor (every turn)':<32}{_fmt_int(floor_total)}"
        )
        floor_subs = [
            (k, v) for k, v in report.always_loaded.items() if k != "_total"
        ]
        floor_subs.sort(key=lambda kv: kv[1], reverse=True)
        for label, tokens in floor_subs:
            out.append(f"  {label:<30}{_fmt_int(tokens)}")

    # Sprint 74 Phase 4 — the disclosure ledger. The mode (T1 eager-core vs
    # T2/T3 index+pull) plus each payload that entered context this turn and WHY.
    if report.disclosure_tier_mode:
        out.append("")
        out.append(f"Disclosure mode: {report.disclosure_tier_mode}")
        if report.disclosed_payloads:
            disc_total = sum(int(p.get("tokens", 0)) for p in report.disclosed_payloads)
            out.append(
                f"  Disclosed this turn ({len(report.disclosed_payloads)} unit(s), "
                f"{disc_total:,} tok):"
            )
            for p in sorted(
                report.disclosed_payloads,
                key=lambda x: int(x.get("tokens", 0)), reverse=True,
            ):
                out.append(
                    f"    {str(p.get('unit_id','?')):<24}"
                    f"{_fmt_int(int(p.get('tokens', 0)))}  "
                    f"[{p.get('kind','?')} · {p.get('reason','?')}]"
                )
        elif report.disclosure_tier_mode == "index+pull":
            out.append("  Disclosed this turn: (none — index alone covered the turn)")

    out.append("")
    out.append(f"Snapshot: {report.snapshot_path}")
    return "\n".join(out)


def persist_context_report(
    report: ContextReport,
    *,
    base_dir: Optional[Path] = None,
) -> Path:
    """Write the D4-shaped JSON snapshot to disk and return the path.

    Creates parent directories as needed. The directory lives under
    ``~/.grove/`` which is operator-state, not repo-tracked; SPEC
    post-condition 10 covers the ``.gitignore`` entry separately for
    the (unlikely) case that a future operator runs from a working
    copy that sits inside the repo tree.
    """
    target = (
        Path(base_dir) / f"{report.session_id or 'no-session'}_{report.turn}.json"
        if base_dir is not None
        else report.snapshot_path
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": report.session_id,
        "turn": report.turn,
        "timestamp": report.timestamp,
        "model": report.model,
        "sections": {
            "system_prompt": report.system_prompt_sections,
            "tool_schemas": report.tool_schemas,
            "conversation_history": report.conversation_history,
            "cellar_context": report.cellar_context,
        },
        "grand_total": report.grand_total,
        # Sprint 73 (D10) — tier-budget provenance: why this payload.
        "tier_budget": {
            "applied_tier": report.applied_tier,
            "excluded_context_blocks": report.excluded_context_blocks,
            "excluded_mcp": report.excluded_mcp,
            "stripped_groups": report.stripped_groups,
        },
    }
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return target
