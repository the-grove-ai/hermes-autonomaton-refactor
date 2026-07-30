"""T0 grant recognition — deterministic pattern match for operator governance commands.

When the operator sends an explicit governance verb (e.g. "promote grove-site-fetch"),
try_mint_implicit_grant() returns a GrantToken before the LLM processes the message.
The token is injected into the kaizen handler closure and bypasses the sovereignty
prompt when the LLM subsequently executes the matching terminal command.

This module is STATIC — no imports from grove.zones, grove.capability_registry,
or any zone/capability system. The verb list is hardcoded here.

H2 (grant-mint-unification-v1): WRITE_CLASS_DECLARATION below is the SINGLE
write-class map. Every consumer — GOVERNANCE_VERBS, grant_covers_halt's native
coverage, the Dispatcher's ceremony set (_NATIVE_GOVERNANCE_TOOLS), the
resolve-side scope derivation, the Always mint, and the Always affordance
label — derives from it. Do not re-declare a verb→write_class literal
anywhere else: the fleet_purge bake miss (a fifth hand-copied map that
silently lacked one verb) is the class this declaration kills.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from time import time
from typing import Optional, Tuple
from uuid import uuid4


@dataclass
class GrantToken:
    id: str = field(default_factory=lambda: f"grant-{uuid4().hex[:8]}")
    source: str = ""          # "operator_telegram", "flywheel_approval", "standing"
    scope: str = ""           # target skill/resource name
    write_class: str = ""     # "andon_promote", "andon_reject", etc.
    timestamp: float = field(default_factory=time)
    disposition: str = "once" # "once", "session", "always"
    issued_at: str = ""       # ISO timestamp
    authorized_by: str = ""   # operator identifier
    revoked: bool = False


# ── H2: the single write-class declaration ──────────────────────────────────


@dataclass(frozen=True)
class WriteClassEntry:
    """One governance write-class action.

    routing_class: "native" — a registered tool carries the verb (the key is
        the tool name); "terminal" — the verb exists only as an operator CLI
        token (no native tool).
    write_class:   the GrantToken.write_class this action grants.
    scope_policy:  "args_derived" — scope comes from the halt's
        skill_name/grant_id arguments; "global" — the verb grants the fixed
        (write_class, write_class) pair, its args carry no per-target scope
        (fleet_purge R2 ruling — absorbs the old dispatcher hand-coded
        override).
    verb_tokens:   operator verb-token aliases that mint this write_class
        from a raw message (the old GOVERNANCE_VERBS keys).
    """

    routing_class: str
    write_class: str
    scope_policy: str
    verb_tokens: Tuple[str, ...] = ()


# THE map. Static list — no imports from zone/capability system.
# "revoke_grant"'s verb token maps via GRANTS_PATTERN ("hermes grants revoke
# <id>"); "purge" is promoted-artifact-persistence-v1 P5 — the operator's
# explicit "purge X" is an implicit grant for the RED fleet_purge verb.
WRITE_CLASS_DECLARATION: dict[str, WriteClassEntry] = {
    "andon_promote": WriteClassEntry(
        "native", "andon_promote", "args_derived", ("promote",)),
    "andon_reject": WriteClassEntry(
        "native", "andon_reject", "args_derived", ("reject",)),
    "andon_revoke": WriteClassEntry(
        "native", "andon_revoke", "args_derived", ("revoke",)),
    "revoke_grant": WriteClassEntry(
        "native", "grant_revoke", "args_derived", ("revoke_grant",)),
    "fleet_purge": WriteClassEntry(
        "native", "fleet_purge", "global", ("purge",)),
    "flywheel_approve": WriteClassEntry(
        "terminal", "flywheel_approve", "args_derived", ("approve",)),
    "flywheel_demote": WriteClassEntry(
        "terminal", "flywheel_demote", "args_derived", ("demote",)),
    "flywheel_downgrade": WriteClassEntry(
        "terminal", "flywheel_downgrade", "args_derived", ("downgrade",)),
}

# ── derivations — consumers read these; nobody re-declares the map ──────────

# Operator verb token → write_class (the historical GOVERNANCE_VERBS shape).
GOVERNANCE_VERBS: dict[str, str] = {
    token: entry.write_class
    for entry in WRITE_CLASS_DECLARATION.values()
    for token in entry.verb_tokens
}

# Native governance tool → write_class (the coverage/resolve/mint map).
NATIVE_TOOL_WRITE_CLASS: dict[str, str] = {
    name: entry.write_class
    for name, entry in WRITE_CLASS_DECLARATION.items()
    if entry.routing_class == "native"
}

# The Dispatcher's ceremony set (_NATIVE_GOVERNANCE_TOOLS) — routing-filtered.
NATIVE_GOVERNANCE_TOOLS: frozenset = frozenset(NATIVE_TOOL_WRITE_CLASS)


# ── standing-grants-v1 Phase 2 — capability-grant scope + mint-floor sets ─────
#
# STRUCTURAL FLOOR (code-level, never config): grant breadth is a security
# boundary. If config could reach these sets, a config write would silently
# widen every future capability grant — and a write is how an operator is
# minted. They live in code so the scope wall (which walls config/) also walls
# their definition; the door cannot green its own controls.
#
# Arg-bearing tools carry a per-FAMILY scope discriminator (Phase 2 FIX,
# GATE-B delta) — the "pattern_key else None" fallback is retired. Each family
# keys on what its OWN arguments carry; a pure-effect tool grants at bare tool
# granularity (D-A two-tier). A missing/unparseable discriminator for an
# arg-bearing tool is a REFUSAL, never a bare-tool fallback.
_ARG_BEARING_TOOLS: frozenset = frozenset({
    "write_file", "patch", "execute_code", "process",
    "mcp_notion_notion_create_comment", "mcp_notion_notion_create_database",
    "mcp_notion_notion_create_pages", "mcp_notion_notion_create_view",
    "mcp_notion_notion_duplicate_page", "mcp_notion_notion_move_pages",
    "mcp_notion_notion_update_data_source", "mcp_notion_notion_update_page",
    "mcp_notion_notion_update_view",
})

# Notion write verbs split by which id in ARGUMENTS identifies the grant
# target: creates key on the PARENT id, updates key on the object/PAGE id. Key
# on what the args carry — NO network lookups, NO derived ancestry.
_NOTION_CREATE_VERBS: frozenset = frozenset({
    "mcp_notion_notion_create_comment", "mcp_notion_notion_create_database",
    "mcp_notion_notion_create_pages", "mcp_notion_notion_create_view",
    "mcp_notion_notion_duplicate_page", "mcp_notion_notion_move_pages",
})
_NOTION_UPDATE_VERBS: frozenset = frozenset({
    "mcp_notion_notion_update_data_source", "mcp_notion_notion_update_page",
    "mcp_notion_notion_update_view",
})

# The grant-ledger identity set: a capability grant for one of these would let
# the agent stand-grant its own control surface (green its own revoke/review) —
# forbidden (D-C floor 2). Same code-level structural floor as above.
_GRANT_LEDGER_IDENTITY_TOOLS: frozenset = frozenset({
    "revoke_grant", "review_grants",
})


def _notion_target_id(tool_name: str, args: dict) -> "Optional[str]":
    """The id (parent for creates, object for updates) that a Notion write's
    ARGUMENTS carry, or None. No network, no derived ancestry — args only."""
    if tool_name in _NOTION_CREATE_VERBS:
        parent = args.get("parent")
        if isinstance(parent, str) and parent.strip():
            return parent.strip()
        if isinstance(parent, dict):
            for k in ("data_source_id", "database_id", "page_id", "id"):
                v = parent.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        for k in ("parent_id", "data_source_id", "database_id", "page_id"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    # update verbs — the object/page id
    for k in ("page_id", "data_source_id", "view_id", "id"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def capability_grant_scope(
    tool_name: str, args: dict, pattern_key: object,
) -> "Optional[str]":
    """The exact-match scope string for a capability grant (D-A two-tier,
    Phase 2 FIX per-family discriminator). Returns None when an arg-bearing
    tool's discriminator is absent/unparseable/empty — the mint REFUSES (deny
    with reason); there is NO bare-tool fallback for an arg-bearing tool.

    Mint and consult call this on the live halt and MUST produce the identical
    key; a mismatch or None is a non-match.

      * execute_code / process → ``f"{tool}::{pattern_key}"`` (the command
        effect signature); absent pattern_key → None.
      * write_file / patch → ``f"{tool}::{realpath(dirname(args['path']))}"``
        — normalized, absolute (relative and realpath spellings collapse to
        one key); absent/empty path → None.
      * Notion writes → ``f"{tool}::{parent-or-object-id}"`` from the args;
        absent id → None.
      * pure-effect (calendar_create, …) → bare ``tool_name``.
    """
    if tool_name in ("execute_code", "process"):
        if not pattern_key:
            return None
        return f"{tool_name}::{pattern_key}"
    if tool_name in ("write_file", "patch"):
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        parent = os.path.realpath(os.path.dirname(os.path.expanduser(path.strip())))
        return f"{tool_name}::{parent}"
    if tool_name in _ARG_BEARING_TOOLS:  # remaining arg-bearing == Notion writes
        tid = _notion_target_id(tool_name, args)
        if not tid:
            return None
        return f"{tool_name}::{tid}"
    return tool_name  # pure-effect → bare tool granularity


def capability_grant_breadth(tool_name: str, scope: str) -> str:
    """The grant's breadth in OPERATOR terms (Phase 2 FIX 3) — shown on the
    "Always" line at mint AND by review_grants, verbatim from the same scope."""
    disc = scope.split("::", 1)[1] if "::" in scope else ""
    if tool_name in ("write_file", "patch"):
        return f"all future {tool_name} in {disc}"
    if tool_name in ("execute_code", "process"):
        return f"this {tool_name} command signature"
    if tool_name in _NOTION_CREATE_VERBS:
        verb = tool_name.replace("mcp_notion_notion_", "notion ")
        return f"all future {verb} in {disc}"
    if tool_name in _NOTION_UPDATE_VERBS:
        verb = tool_name.replace("mcp_notion_notion_", "notion ")
        return f"all future {verb} on {disc}"
    return f"all future {tool_name}"


GOVERNANCE_PATTERN = re.compile(
    r"^/?(?:hermes\s+(?:andon|flywheel)\s+(?:patterns\s+)?)?"
    r"(" + "|".join(k for k in GOVERNANCE_VERBS if k != "revoke_grant") + r")"
    r"\s+([a-zA-Z0-9_.-]+)"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)

# Separate pattern for grant management commands: "hermes grants revoke <grant-id>"
GRANTS_PATTERN = re.compile(
    r"^/?hermes\s+grants\s+(revoke)\s+([a-zA-Z0-9_.-]+)(?:\s+.*)?$",
    re.IGNORECASE,
)


def try_mint_implicit_grant(
    raw_message: str,
    source: str = "operator_telegram",
) -> "GrantToken | None":
    """If the operator's message is an explicit governance verb, mint a grant.

    Returns None if the message is not a governance command.
    The grant is implicit — it derives its authority from the operator's
    choice of channel (already authenticated) and the explicit verb in their
    message. No secondary prompt is shown.

    Checks GRANTS_PATTERN first (hermes grants revoke <id>) so grant
    management commands get the correct write_class ("grant_revoke") rather
    than the standard "andon_revoke" that "revoke" in GOVERNANCE_VERBS maps to.
    """
    # Grant management commands take precedence.
    m = GRANTS_PATTERN.match(raw_message.strip())
    if m:
        _, target = m.group(1).lower(), m.group(2)
        return GrantToken(
            source=source,
            scope=target,
            write_class="grant_revoke",
            disposition="once",
            authorized_by=source,
        )
    # Standard governance-mutation verbs.
    m = GOVERNANCE_PATTERN.match(raw_message.strip())
    if not m:
        return None
    verb, target = m.group(1).lower(), m.group(2)
    return GrantToken(
        source=source,
        scope=target,
        write_class=GOVERNANCE_VERBS[verb],
        disposition="once",
        authorized_by=source,
    )


def grant_covers_halt(grant: "GrantToken", halt: object) -> bool:
    """Return True if the grant authorizes the halted action.

    Scope protection: BOTH checks use exact matching, not substring.
    A grant for "grove-site-fetch / andon_promote" does NOT cover
    "my-other-skill / andon_promote" or "grove-site-fetch / flywheel_approve".

    Handles two intent shapes:
    - Terminal commands: ``arguments["command"]`` contains the command string;
      scope and verb are matched as whitespace-delimited tokens.
    - Native andon tool calls: ``arguments["skill_name"]`` (or ``grant_id``)
      is the scope; write_class is matched against the tool_name directly.

    H2: the native tool → write_class map is the module-level derivation of
    WRITE_CLASS_DECLARATION (the old inline copy is gone). Global-scope verbs
    (fleet_purge, R2) carry no skill_name/grant_id, so the scope check passes
    vacuously and the write_class exact-match is the guard.
    """
    try:
        triggering = halt.intents[halt.triggering_index]  # type: ignore[attr-defined]
        tool_name = getattr(triggering, "tool_name", "") or ""
        args = getattr(triggering, "arguments", None) or {}

        if tool_name in NATIVE_TOOL_WRITE_CLASS:
            # Native tool call: verify write_class and scope exactly.
            expected_write_class = NATIVE_TOOL_WRITE_CLASS[tool_name]
            if grant.write_class and grant.write_class != expected_write_class:
                return False
            scope_from_args = str(
                args.get("skill_name") or args.get("grant_id") or ""
            ).strip()
            if grant.scope and scope_from_args and grant.scope != scope_from_args:
                return False
            return True

        # Terminal command path: token-exact matching.
        cmd = str(args.get("command", "")).lower()
        if not cmd:
            return False
        tokens = cmd.split()
        if grant.scope and grant.scope.lower() not in tokens:
            return False
        if grant.write_class:
            expected_verb = next(
                (k for k, v in GOVERNANCE_VERBS.items() if v == grant.write_class),
                None,
            )
            if expected_verb and expected_verb not in tokens:
                return False
        return True
    except Exception:
        return False


# ── H2: declaration-driven target + store resolution ────────────────────────


def resolve_native_grant_target(
    tool_name: str, args: dict,
) -> "Optional[Tuple[str, str]]":
    """``(scope, write_class)`` for a native governance-tool halt, or None.

    scope_policy drives the scope: ``global`` verbs grant the fixed
    (write_class, write_class) pair — their args carry no per-target scope
    (fleet_purge R2 ruling, previously a hand-coded dispatcher override).
    ``args_derived`` verbs read skill_name/grant_id; an empty scope resolves
    None — no mintable target, so the Always affordance must not render and
    the mint path fails loud.
    """
    entry = WRITE_CLASS_DECLARATION.get(tool_name)
    if entry is None or entry.routing_class != "native":
        return None
    if entry.scope_policy == "global":
        return entry.write_class, entry.write_class
    scope = str(args.get("skill_name") or args.get("grant_id") or "").strip()
    if not scope:
        return None
    return scope, entry.write_class


def resolve_always_store(halt: object) -> "Optional[tuple]":
    """Which store an operator "Always" on this halt writes.

    Returns:
      ``("standing_grant", scope, write_class)`` — governance-mutation halt
        (native declared verb, or a parseable terminal governance command).
      ``("standing_capability", scope, write_class)`` — promotable
        non-governance YELLOW halt (standing-grants-v1 Phase 2, D-A/D-D). scope
        is the two-tier capability key; write_class is the halt's effect class.
      ``None`` — no store applies. The Always affordance must not render,
        and the Dispatcher's mint floor raises if "always" arrives anyway.

    Totality (H2 GATE-B F2): every currently-mintable halt resolves. None is
    reachable only for governance-shaped halts whose target cannot be
    derived — an unparseable terminal governance command (verb token present,
    no target), or a native args_derived verb with empty arguments — OR (Phase-2
    Change 2) a NON-PROMOTABLE classification (bucket-3 UNRESOLVED_WRITER / any RED
    shell chain): such a halt has no Always store, so the affordance must not
    render and the Dispatcher's mint floor must refuse.
    """
    # containment Phase-2 Change 2 — refuse a standing store for a non-promotable
    # classification. Read the triggering ZoneResult's is_promotable when present.
    try:
        _zr = halt.zone_results[halt.triggering_index]  # type: ignore[attr-defined]
        if getattr(_zr, "is_promotable", True) is False:
            return None
    except (AttributeError, IndexError, TypeError):
        pass
    try:
        triggering = halt.intents[halt.triggering_index]  # type: ignore[attr-defined]
    except Exception:
        return None
    tool_name = getattr(triggering, "tool_name", "") or ""
    args = getattr(triggering, "arguments", None) or {}
    if not isinstance(args, dict):
        args = {}

    entry = WRITE_CLASS_DECLARATION.get(tool_name)
    if entry is not None and entry.routing_class == "native":
        target = resolve_native_grant_target(tool_name, args)
        if target is None:
            return None
        return ("standing_grant",) + target

    cmd = str(args.get("command", ""))
    tokens = cmd.lower().split()
    if any(verb in tokens for verb in GOVERNANCE_VERBS):
        parsed = try_mint_implicit_grant(cmd, source="standing_lookup")
        if parsed is None:
            return None
        return ("standing_grant", parsed.scope, parsed.write_class)

    # standing-grants-v1 Phase 2 (D-A): a PROMOTABLE non-governance YELLOW halt
    # resolves a scope-keyed CAPABILITY store — the conformant successor to the
    # retired zone_rule Always (R-3c). The scope is the two-tier key; the
    # write_class is the halt's effect class (D-D). This re-derives zone +
    # promotability from the ZoneResult and refuses anything that is not a
    # promotable YELLOW carrying an effect class — the mint floor is never
    # trusted to a single site (the Dispatcher's _add_capability_grant_from_halt
    # re-derives the RED / scope-defining floors independently, D-C).
    try:
        _cap_zr = halt.zone_results[halt.triggering_index]  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return None
    if getattr(_cap_zr, "zone", None) != "yellow":
        return None
    if getattr(_cap_zr, "is_promotable", False) is not True:
        return None
    effect_class = getattr(_cap_zr, "effect_class", None)
    if not effect_class:
        return None
    pattern_key = getattr(_cap_zr, "pattern_key", None)
    scope = capability_grant_scope(tool_name, args, pattern_key)
    if scope is None:
        # Arg-bearing tool with an absent/unparseable discriminator — REFUSE
        # (no bare-tool fallback for an arg-bearing tool, Phase 2 FIX).
        return None
    return ("standing_capability", scope, effect_class)


# Operator-facing store names for the Always affordance. The affordance MUST
# name the store it writes; a None resolution renders no Always option.
ALWAYS_STORE_LABELS: dict[str, str] = {
    "standing_grant": "standing grant",
    "standing_capability": "standing capability",
    "zone_rule": "zone rule",
}


def always_store_label(halt: object) -> "Optional[str]":
    """The text the Always affordance shows, or None (no affordance).

    Governance halts name the store ("standing grant"); a capability halt shows
    its exact BREADTH in operator terms (Phase 2 FIX 3) — "all future write_file
    in /path", "all future page creation in <id>", "this command signature" —
    so the operator sees what a persistent grant would cover before tapping
    Always. review_grants renders the same breadth from the same scope string.
    """
    store = resolve_always_store(halt)
    if store is None:
        return None
    if store[0] == "standing_capability":
        try:
            triggering = halt.intents[halt.triggering_index]  # type: ignore[attr-defined]
            tool_name = getattr(triggering, "tool_name", "") or ""
        except Exception:
            tool_name = ""
        return capability_grant_breadth(tool_name, store[1])
    return ALWAYS_STORE_LABELS[store[0]]
