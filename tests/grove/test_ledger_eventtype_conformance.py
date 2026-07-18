"""ledger-eventtype-hygiene-v1 — class-retirement conformance.

Statically extract every literal event-type string passed to a
``KaizenLedger.record()`` call across the source tree (grove/ + tools/, the
Phase-0 sweep surface) and assert each is registered in
``KaizenLedger.EVENT_TYPES``. This closes the orphan class where an emitter
shipped without its allowlist entry — the fail-loud ValueError floor was
swallowed by the caller's ``try/except`` and the ledger entry silently dropped.

The ``log_pattern_cache_event(event_type=...)`` keyword sink is a DIFFERENT
telemetry channel not bound by ``EVENT_TYPES``; it is naturally excluded here
because it is not a ``.record()`` attribute call.
"""
from __future__ import annotations

import ast
from pathlib import Path

from grove.kaizen_ledger import KaizenLedger

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("grove", "tools")


def _record_event_type_sites():
    """(event_type, relpath, lineno) for every ``.record("literal", …)`` call in
    the scan surface. Skips test files; only literal string first-args count (a
    variable event_type cannot be statically checked and is out of scope)."""
    sites = []
    for d in _SCAN_DIRS:
        for py in sorted((_REPO_ROOT / d).rglob("*.py")):
            if "test" in py.name:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "record"):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    sites.append(
                        (first.value, str(py.relative_to(_REPO_ROOT)), node.lineno)
                    )
    return sites


def test_every_recorded_event_type_is_registered():
    sites = _record_event_type_sites()
    assert sites, "extraction found no .record() sites — the scan surface broke"
    orphans = [
        (et, path, ln)
        for (et, path, ln) in sites
        if et not in KaizenLedger.EVENT_TYPES
    ]
    assert not orphans, (
        "emitted-but-unregistered kaizen event types (add to "
        "KaizenLedger.EVENT_TYPES):\n"
        + "\n".join(f"  {et!r} at {path}:{ln}" for et, path, ln in orphans)
    )


def test_scan_surface_covers_the_retired_orphans():
    # Guard the extractor itself: the three formerly-orphan emitters must remain
    # visible to the scan, so a future extractor regression that stops seeing one
    # is caught here rather than silently un-scanning it.
    found = {et for et, _, _ in _record_event_type_sites()}
    for et in ("write_confinement_refusal", "grant_execution", "session_cache_hit"):
        assert et in found, f"scan lost sight of emitter {et!r}"
