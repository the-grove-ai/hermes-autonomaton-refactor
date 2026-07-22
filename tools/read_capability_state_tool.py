"""read_capability_state — the agent's effective-state read (Green, baseline).

retrieval-ambient-class-v1 P3: answers "what are browser_read's intents,
effectively, right now, and why" through the SHARED composed path —
``grove.capability_registry.effective_admission_state`` binds
``_compose_state`` (the sole merge authority); this tool performs NO parallel
derivation. Per-field source tags: definition | overlay · approval <id> |
overlay · NO PROVENANCE (pre-canonical) | derived — re-anchored by merge.

The description below is SPEC-LOCKED (GATE-B ruling F): the tool is operator
transparency, NOT a pre-authorization check — no split-brain self-policing.
"""

from __future__ import annotations

import json

READ_CAPABILITY_STATE_SCHEMA = {
    "name": "read_capability_state",
    "description": (
        "Reports the agent's current effective capability state for operator "
        "transparency and debugging. This is NOT a pre-authorization check — "
        "the dispatcher is the sole authority on whether a tool executes. Do "
        "not use this to decide whether to attempt an action; attempt the "
        "action and let governance rule. Returns, per capability record: "
        "effective intents, tiers, disclosure class, zone, and per-field "
        "source (definition | overlay · approval <id> | derived). Pass "
        "record_id for one record; omit it for the full registry summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "record_id": {
                "type": "string",
                "description": (
                    "A capability record id (e.g. 'browser_read'). Omit for "
                    "all records (summary form)."
                ),
            },
        },
        "required": [],
    },
}


def read_capability_state(record_id: str = "") -> str:
    """Resolve the effective admission state through the shared composed path.

    * ``record_id`` given → that record's full field/source detail, or a loud
      unknown-id error naming nearby ids.
    * omitted → a summary of every record (id, zone, disclosure, has_state)
      plus full detail for the records that carry overlay state — the
      compact form keeps the payload readable at 150+ records.
    """
    from grove.capability_registry import effective_admission_state

    composed = effective_admission_state()
    records = composed["records"]

    if record_id:
        rec = records.get(record_id)
        if rec is None:
            close = sorted(r for r in records if record_id.lower() in r.lower())
            return json.dumps({
                "error": f"no capability record with id {record_id!r}",
                "near_matches": close[:8],
            }, ensure_ascii=False)
        return json.dumps(
            {"record_id": record_id, **rec,
             "state_dir_missing": composed["state_dir_missing"]},
            ensure_ascii=False,
        )

    summary = [
        {"record_id": rid, "zone": r["zone"], "disclosure": r["disclosure"],
         "has_state": r["has_state"]}
        for rid, r in sorted(records.items())
    ]
    overlaid = {
        rid: r for rid, r in records.items() if r["has_state"]
    }
    return json.dumps({
        "count": len(summary),
        "records": summary,
        "overlaid_detail": overlaid,
        "invalid": composed["invalid"],
        "orphans": composed["orphans"],
        "state_dir_missing": composed["state_dir_missing"],
    }, ensure_ascii=False)


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools."""
    reg.register(
        name="read_capability_state",
        toolset="governance",
        schema=READ_CAPABILITY_STATE_SCHEMA,
        handler=lambda args, **kw: read_capability_state(
            record_id=args.get("record_id") or "",
        ),
        emoji="🔎",
    )
