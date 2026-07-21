"""capability-mutation-surface-v1 T2 — REGRESSION PIN (Gate P0 ruling A-2).

Pins that a file-tool write targeting ``<grove_home>/capabilities/state/*.yaml``
classifies RED at the Stage-04 scope wall. Phase-0 recon found this behavior
ALREADY LIVE — ``capabilities`` is a ``_SCOPE_DEFINING_DIR_PREFIXES`` entry
(grove/utils/fs_utils.py:174-182, comment at :180: "capabilities/state/ needs
no entry — the `capabilities` prefix covers it") — so this file is a pin, not
a failing test. It is EXCLUDED from the P1 confirm-RED requirement and must
never regress: raw file-tool writes to the state overlay stay RED; the only
autonomous door is the sanctioned admission writer (T1/T4/T6 contract).
"""

from __future__ import annotations

from types import SimpleNamespace

from hermes_constants import get_hermes_home


def _state_target() -> str:
    return str(get_hermes_home() / "capabilities" / "state" / "x.yaml")


def test_is_scope_defining_covers_capability_state(hermetic_grove_home):
    from grove.utils.fs_utils import is_scope_defining

    assert is_scope_defining(_state_target()), (
        "REGRESSION: ~/.grove/capabilities/state/ fell out of the "
        "scope-defining wall (the `capabilities` dir prefix must cover it)"
    )


def test_write_file_intent_to_capability_state_classifies_red_scope_wall(
    hermetic_grove_home,
):
    """The Seam-beta target-keyed wall (Dispatcher._classify_one_intent):
    a ``write_file`` intent on the state overlay returns
    ZoneResult(zone="red", source="scope_wall")."""
    from grove.dispatcher import Dispatcher

    intent = SimpleNamespace(
        tool_name="write_file",
        arguments={"path": _state_target(), "content": "id: x\n"},
    )
    result = Dispatcher._classify_one_intent(intent, _grove_dispatch=None)
    assert result.zone == "red", (
        f"REGRESSION: expected RED for capability-state write, got "
        f"{result.zone!r} (rule={result.matched_rule!r})"
    )
    assert result.source == "scope_wall", (
        f"REGRESSION: expected source='scope_wall', got {result.source!r}"
    )


def test_patch_intent_to_capability_state_classifies_red_scope_wall(
    hermetic_grove_home,
):
    from grove.dispatcher import Dispatcher

    intent = SimpleNamespace(
        tool_name="patch",
        arguments={"path": _state_target(), "mode": "replace", "content": "y"},
    )
    result = Dispatcher._classify_one_intent(intent, _grove_dispatch=None)
    assert (result.zone, result.source) == ("red", "scope_wall"), (
        f"REGRESSION: patch to capability state classified "
        f"({result.zone!r}, {result.source!r}), expected ('red', 'scope_wall')"
    )
