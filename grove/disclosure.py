"""Grove agent-pull resolution — Sprint 74 context-jit-disclosure-v1 (Phase 3).

The disclosure round-trip: on T2/T3 the always-loaded index (one-liners) stands
in for eager schemas; when the agent needs a unit it isn't holding, it calls
``read_tool_schema(id)`` or ``read_goal_context(goal_id)``. These pure resolvers
turn an id into its full payload:

* :func:`resolve_pull` — a native tool's OpenAI schema, or ALL of an MCP
  server's tool schemas, plus the tool defs to splice into the live API surface
  so the model can call them on the next step. A goal/contract/unknown id is a
  loud error with no defs (goals are fetched via :func:`resolve_goal_record`).
* :func:`resolve_goal_record` — a Dock goal's full record (the budget-capped
  ``context_sources`` load), or a loud error.

Pure by design (no agent, no live model): the round-trip's resolution half is
unit-tested deterministically; the agent-loop interception that consumes it
(``run_agent._intercept_pull_intents``) is the only stateful piece.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

from grove.context_budget import _is_mcp, _mcp_server_of, _name_of

__all__ = [
    "PULL_TOOL_NAMES",
    "build_pull_tool_defs",
    "resolve_pull",
    "resolve_goal_record",
]

# The two net-new agent-pull tools. Held here so the agent loop's interception
# and the wiring agree on one source of truth for the names.
PULL_TOOL_NAMES = ("read_tool_schema", "read_goal_context")


def build_pull_tool_defs(units, eager_names) -> List[dict]:
    """OpenAI defs for the two pull tools, with the disclosure INDEX embedded in
    their descriptions.

    The index (one-liners) is the always-loaded layer: it rides the API tool
    surface as the read_tool_schema/read_goal_context descriptions, so on T2/T3
    the model sees every pullable unit by id+oneline without carrying a single
    full schema. ``eager_names`` are unit ids already loaded eagerly (core +
    matched MCP) — omitted from the pull index so the model does not pull what
    it already holds.
    """
    eager = set(eager_names or ())
    pullable = [u for u in units if u.kind in ("tool", "mcp") and u.id not in eager]
    goals = [u for u in units if u.kind == "goal"]
    idx = "\n".join(f"- {u.id}: {u.oneline}" for u in pullable) or "(none)"
    gidx = "\n".join(f"- {u.id}: {u.oneline}" for u in goals) or "(none)"
    return [
        {
            "type": "function",
            "function": {
                "name": "read_tool_schema",
                "description": (
                    "Load the full schema for a tool or MCP server that is "
                    "indexed but not currently loaded, so you can CALL it on your "
                    "next step. Pass the unit id. Tools/servers available to "
                    "pull:\n" + idx
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The unit id to load."}
                    },
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_goal_context",
                "description": (
                    "Load a Dock goal's full record (its long-running context) "
                    "by goal_id. Goals available:\n" + gidx
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "The goal id."}
                    },
                    "required": ["goal_id"],
                },
            },
        },
    ]


def resolve_pull(
    units, all_tools: List[dict], unit_id: str
) -> Tuple[str, List[dict]]:
    """Resolve a ``read_tool_schema`` pull.

    Args:
        units: the merged disclosure manifest (:class:`DisclosableUnit` seq).
        all_tools: the full registry tool list the agent holds (OpenAI defs).
        unit_id: the id the model asked to pull.

    Returns:
        ``(result_text, defs_to_add)``. ``result_text`` is the JSON the model
        reads (the schema, or a loud error). ``defs_to_add`` are the tool defs
        to splice into the live per-turn API surface — empty on any error so a
        failed pull never widens the surface.
    """
    unit = next((u for u in units if u.id == unit_id), None)
    if unit is None:
        return (
            json.dumps({
                "error": f"no disclosable unit {unit_id!r} in the index. Pull an "
                         f"id listed in the disclosure index.",
            }),
            [],
        )

    if unit.kind == "tool":
        d = next((t for t in all_tools if _name_of(t) == unit_id), None)
        if d is None:
            return (
                json.dumps({
                    "error": f"tool {unit_id!r} is indexed but has no schema in "
                             f"the live registry (not available this session).",
                }),
                [],
            )
        return (
            json.dumps({"id": unit_id, "kind": "tool", "schema": d}, ensure_ascii=False),
            [d],
        )

    if unit.kind == "mcp":
        defs = [
            t for t in all_tools
            if _is_mcp(_name_of(t)) and _mcp_server_of(_name_of(t)) == unit_id
        ]
        if not defs:
            return (
                json.dumps({
                    "error": f"MCP server {unit_id!r} is indexed but has no "
                             f"connected tools this session.",
                }),
                [],
            )
        return (
            json.dumps(
                {"id": unit_id, "kind": "mcp", "schemas": defs}, ensure_ascii=False
            ),
            defs,
        )

    # goal / contract_section — not a schema pull.
    return (
        json.dumps({
            "error": f"unit {unit_id!r} (kind={unit.kind!r}) is not a schema. Use "
                     f"read_goal_context for goals.",
        }),
        [],
    )


def resolve_goal_record(
    goal_id: str, *, dock: Optional[Any] = None, allow_load: bool = True
) -> str:
    """Resolve a ``read_goal_context`` pull to a goal's full record.

    Args:
        goal_id: the Dock goal id the model asked for.
        dock: an already-loaded Dock (tests inject this); when ``None`` and
            ``allow_load`` is set, the runtime Dock is loaded.
        allow_load: when ``False`` and ``dock`` is ``None``, skip the disk load
            (test hook) and report no-Dock.

    Returns:
        JSON text: the record, or a loud error. A malformed Dock raises through
        ``load_dock`` (fail-loud); a missing goal or absent Dock is a clean
        error string the model can act on.
    """
    if dock is None and allow_load:
        from grove.dock import load_dock
        dock = load_dock()
    if dock is None:
        return json.dumps({"error": "no Dock is installed; no goal records exist."})

    goal = next((g for g in dock.goals if g.id == goal_id), None)
    if goal is None:
        return json.dumps({
            "error": f"no goal {goal_id!r} in the Dock. Pull a goal id from the "
                     f"index.",
        })

    from grove.dock import load_goal_context
    try:
        record = load_goal_context(goal, dock.context_char_budget)
    except Exception as exc:  # DockBudgetAndon / read failure — report, don't crash the turn
        return json.dumps({
            "error": f"goal {goal_id!r} record could not be loaded: {exc}",
        })
    return json.dumps(
        {"id": goal_id, "name": goal.name, "record": record}, ensure_ascii=False
    )
