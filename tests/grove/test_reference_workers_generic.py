"""instance-cold-start-parity-v1 P4 — the generic fleet reference workers
(drafter / cultivator / researcher / scout) must dispatch green on a FRESH
instance that ships no operator voice/content skills. Voice is OPTIONAL: the
workers genericize their voice invocation, so a fresh instance with zero voice
skills carries no dangling skill reference.

This guard is deliberately NEEDLE-FREE — it names no removed skill. The
guarantee that specific operator-private skills stay out of the tree is enforced
by the out-of-tree severance needle sweep, not by an in-tree test that would
reintroduce those very strings. Here we prove the structural property instead:
every reference worker loads, and every related_skill it declares resolves to a
skill record that is actually present. A dangling reference to any removed skill
fails test_reference_worker_related_skills_all_exist without being spelled out.
"""

from __future__ import annotations

import pytest

from grove.capability import CapabilityKind
from grove.capability_registry import load_capabilities

pytestmark = pytest.mark.guard

_REFERENCE_WORKERS = [
    "skill.fleet.drafter",
    "skill.fleet.cultivator",
    "skill.fleet.researcher",
    "skill.fleet.scout",
]


def test_reference_workers_load_on_fresh_instance():
    caps = load_capabilities()
    for wid in _REFERENCE_WORKERS:
        assert wid in caps, f"reference worker {wid} did not load"
        assert caps[wid].kind is CapabilityKind.SKILL


def test_reference_worker_related_skills_all_exist():
    # A dangling related_skill (pointing at a skill that left the tree) would
    # break the graceful-absence contract — the worker would advertise a peer
    # skill a fresh instance does not have. Every related_skill must resolve to
    # a loaded record; the voice skills are simply not listed any more.
    caps = load_capabilities()
    loaded_slugs = {
        c.id.split(".")[-1] for c in caps.values() if c.kind is CapabilityKind.SKILL
    }
    for wid in _REFERENCE_WORKERS:
        related = getattr(caps[wid], "related_skills", None) or []
        for r in related:
            assert r in loaded_slugs, (
                f"{wid} related_skills names {r!r}, which no longer loads — "
                f"a dangling reference to a removed skill"
            )
