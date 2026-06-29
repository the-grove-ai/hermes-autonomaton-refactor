"""write-workspace-grant-flow-v1 — the in-conversation workspace grant tool.

``add_write_workspace`` lets the autonomaton expand its own write allow-list
WITHOUT a YAML-editing context switch. When a write is refused as outside the
declared workspaces, the agent calls this tool with the directory root; the
Dispatcher classifies it YELLOW (unlisted tool → default-yellow), so the
existing sovereignty prompt fires — the operator's approval of THAT prompt is
the grant (no second confirmation, no new approval surface). On approval the
handler appends the root to ``write_workspaces.yaml`` (comment-preserving,
hot-reload) via the sanctioned ``append_write_workspace`` door and tells the
agent to retry.

Guards (the door is sovereign, so it polices its own input): the path must be
ABSOLUTE and must not be a secret/protected path — no laundering a walled path
into the write allow-list.
"""

from __future__ import annotations

import os

from tools.registry import tool_error

ADD_WRITE_WORKSPACE_SCHEMA = {
    "name": "add_write_workspace",
    "description": (
        "Add an absolute directory root to your declared write workspaces so you "
        "can write files there. Use this when a write was refused because the "
        "path is outside your workspaces — the operator is asked to approve the "
        "grant. Directory roots only (writes recurse into every subdirectory); "
        "NO glob patterns. After approval, retry your original write."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute directory path to grant (e.g. /Users/you/project). "
                    "Not a glob; everything under it becomes writable."
                ),
            }
        },
        "required": ["path"],
    },
}


def add_write_workspace(path: str, task_id: str = "default") -> str:
    """Grant *path* as a write workspace (post-Stage-04 sanctioned effect).

    By the time this runs the Dispatcher has classified the call YELLOW and the
    operator has approved — the append IS the approved effect. Validates the
    input loudly (absolute + non-secret) before touching the manifest."""
    from grove.utils.fs_utils import append_write_workspace, is_secret_path

    if not isinstance(path, str) or not path.strip():
        return tool_error(
            "add_write_workspace requires a 'path' — the absolute directory root "
            "to grant."
        )
    raw = path.strip()
    if not os.path.isabs(os.path.expanduser(raw)):
        return tool_error(
            f"add_write_workspace requires an ABSOLUTE directory path; got a "
            f"relative path: {raw!r}. Pass the full path (e.g. /Users/you/project)."
        )
    # No secret-laundering: a protected/secret path can never become a workspace.
    if is_secret_path(raw):
        return tool_error(
            f"Refused — {raw} is a protected/secret path and cannot be added as a "
            "write workspace."
        )
    granted = append_write_workspace(raw)
    return (
        f"Workspace {granted} added to your write workspaces. You can now write "
        "to files in that directory — retry your original write."
    )


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools. Registered under
    the ``file`` toolset — the operator-approved companion to write_file's
    confinement wall."""
    reg.register(
        name="add_write_workspace",
        toolset="file",
        schema=ADD_WRITE_WORKSPACE_SCHEMA,
        handler=lambda args, **kw: add_write_workspace(
            path=args.get("path", ""),
            task_id=kw.get("task_id", "default"),
        ),
        emoji="📂",
    )
