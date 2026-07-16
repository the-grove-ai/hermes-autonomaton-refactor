"""write-routing-coherence-v1 fix-part-3 — the in-conversation publication-autonomy
grant tool.

``set_publication_state`` is the operator's SANCTIONED route to grant (or revoke)
a fleet capability's ``governance.publication.unattended`` autonomy. It lands ONLY
in the deploy-immune ``~/.grove/capabilities/state`` overlay via the sanctioned
:func:`grove.capability_registry.set_publication_state` writer — which is
structurally incapable of touching ``config/capabilities/`` (the repo definition).

This closes the forge-arming misfire's incentive: before it existed, the only way
to set the grant was a raw file write, and an ``add_write_workspace`` + ``patch``
could land it in the DEPLOYED definition, where the next ``deploy.sh``
``git reset --hard origin/main`` silently reverted it. The grant now has a proper
door that writes operator STATE, so there is no reason to raw-write a definition.

The Dispatcher classifies this call YELLOW (zones.schema.yaml::tool_zones +
capability record ``publication_grant_write``), so the Sovereign Prompt fires — the
operator's approval of THAT prompt is the grant (no second confirmation).
"""

from __future__ import annotations

from tools.registry import tool_error

SET_PUBLICATION_STATE_SCHEMA = {
    "name": "set_publication_state",
    "description": (
        "Grant or revoke a fleet capability's unattended-publication autonomy "
        "(governance.publication.unattended). Writes ONLY the deploy-immune "
        "~/.grove/capabilities/state overlay — never the repo definition. Use the "
        "capability's record id (e.g. 'skill.fleet.forge-jobsearch'), NOT a "
        "filename. The operator is asked to approve the change."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "record_id": {
                "type": "string",
                "description": (
                    "The capability record id (dotted form, e.g. "
                    "'skill.fleet.forge-jobsearch'). A definition with this id must "
                    "exist or the grant is refused."
                ),
            },
            "unattended": {
                "type": "boolean",
                "description": (
                    "True to grant unattended publication, False to revoke it. "
                    "Explicit boolean — no default."
                ),
            },
        },
        "required": ["record_id", "unattended"],
    },
}


def set_publication_state(
    record_id: str, unattended: object, task_id: str = "default"
) -> str:
    """Grant/revoke publication.unattended (post-Stage-04 sanctioned effect).

    By the time this runs the Dispatcher has classified the call YELLOW and the
    operator has approved — the state write IS the approved effect. Validates the
    input loudly (non-empty id + real bool) before touching the overlay; the core
    writer re-validates and is repo-write-incapable by construction."""
    from grove.capability_registry import (
        set_publication_state as _set_publication_state,
        CapabilityLoadError,
    )

    if not isinstance(record_id, str) or not record_id.strip():
        return tool_error(
            "set_publication_state requires a 'record_id' — the capability record "
            "id to grant (e.g. 'skill.fleet.forge-jobsearch')."
        )
    # A real bool ONLY — reject 1/0/"true" so an ambiguous grant never slips in.
    if not isinstance(unattended, bool):
        return tool_error(
            "set_publication_state requires 'unattended' to be a boolean "
            f"(true/false); got {type(unattended).__name__}."
        )
    try:
        status = _set_publication_state(record_id.strip(), unattended)
    except CapabilityLoadError as exc:
        return tool_error(str(exc))
    except ValueError as exc:
        return tool_error(str(exc))

    if status == "deferred":
        return (
            f"publication.unattended grant for {record_id.strip()!r} is deferred — "
            "the capability state file is locked by another writer. Retry shortly."
        )
    verb = "granted" if unattended else "revoked"
    return (
        f"Unattended publication {verb} for {record_id.strip()!r} in the "
        "~/.grove/capabilities/state overlay (deploy-immune). The repo definition "
        "was not touched."
    )


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools. Registered under
    the ``governance`` toolset — the operator-approved publication-autonomy grant,
    sibling to add_write_workspace / revoke_grant."""
    reg.register(
        name="set_publication_state",
        toolset="governance",
        schema=SET_PUBLICATION_STATE_SCHEMA,
        handler=lambda args, **kw: set_publication_state(
            record_id=args.get("record_id", ""),
            unattended=args.get("unattended"),
            task_id=kw.get("task_id", "default"),
        ),
        emoji="📤",
    )
