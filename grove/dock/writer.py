"""Dock writer — the first mutation path for the operator's sovereign dock.yaml.

Sprint P4 (portal-action-surface-v1). The Dock has been read-only until now
(``load_dock``); the Operator Portal needs to flip a goal's ``status`` from the
browser. This module is that single write seam.

Comment preservation is MANDATORY — ``dock.yaml`` is a hand-authored sovereign
file (routing_hints, operator_preferences, milestone narratives, inline
rationale). A naive ``yaml.safe_dump`` round-trip would strip every comment and
reflow the file. So this module REQUIRES ruamel.yaml and imports it at module
load: a missing dependency fails the gateway loudly at startup, never silently
at first write. There is no pyyaml fallback (PM ruling, Sprint P4 Phase 0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Fail-loud at gateway startup if ruamel is absent — no pyyaml fallback. The
# operator's dock.yaml is sovereign; a comment-stripping write is not an option.
from ruamel.yaml import YAML

from grove.dock import _VALID_STATUSES, _resolve_dock_path


def update_dock_goal_status(
    goal_id: str,
    new_status: str,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Set ``goals[id==goal_id].status = new_status`` in the operator's dock.yaml.

    Round-trips with ruamel.yaml so comments, key order, quoting style, and
    every untouched key survive the write. Writes atomically via a temp file
    + replace, mirroring the dock detector / zone_rules write discipline.

    Args:
        goal_id: the ``id`` of the goal to update.
        new_status: the new status. MUST be in
            :data:`grove.dock._VALID_STATUSES` — the loader rejects anything
            else on the next read, so writing an off-set value would brick the
            Dock. Validation here is the writer's own fail-loud floor; the HTTP
            handler validates first and returns 400.
        path: explicit dock.yaml path (tests pass this). When None, resolves the
            runtime sovereign path (``$GROVE_HOME/dock/dock.yaml``).

    Returns:
        True when a goal matched and was rewritten; False when no goal had
        ``id == goal_id`` (the caller maps this to 404).

    Raises:
        ValueError: ``new_status`` is not a valid Dock status, or the manifest
            is structurally malformed (no ``goals`` list).
        FileNotFoundError: the dock.yaml does not exist — fail loud rather than
            create a partial sovereign file from a status toggle.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid dock status {new_status!r}; expected one of "
            f"{sorted(_VALID_STATUSES)}"
        )

    target = Path(path) if path is not None else _resolve_dock_path()
    if not target.exists():
        raise FileNotFoundError(
            f"dock.yaml not found at {target} — cannot update goal status on a "
            f"Dock that is not installed"
        )

    # Per-call YAML instance: aiohttp handlers can run concurrently and a shared
    # round-trip parser holds per-document state. preserve_quotes keeps the
    # operator's chosen quoting style intact.
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True

    with target.open(encoding="utf-8") as fh:
        data = yaml_rt.load(fh)

    goals = data.get("goals") if isinstance(data, dict) else None
    if not isinstance(goals, list):
        raise ValueError(
            f"dock.yaml at {target}: goals must be a list (got "
            f"{type(goals).__name__})"
        )

    for goal in goals:
        if isinstance(goal, dict) and goal.get("id") == goal_id:
            goal["status"] = new_status
            tmp = target.with_suffix(target.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                yaml_rt.dump(data, fh)
            tmp.replace(target)
            return True

    return False
