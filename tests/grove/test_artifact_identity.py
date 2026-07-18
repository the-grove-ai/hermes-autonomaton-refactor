"""artifact-identity-v1 C1 — artifact_written identity event + ID derivation.

The write-confinement seam (grove/dispatcher.py::_enforce_write_confinement)
stamps an artifact_written event for EVERY file-tool write that exits ALLOWED:
{path, artifact_id, turn_id, active_primary_skill_slug, intent_class, tool}.
Emission is write-strict/read-resilient — a filing fault logs loud and the
write proceeds. artifact_id = sha256(canonical path)[:16], byte-identical
derivation input to the cellar's 8-hex source hash (prefix-join proof below).
"""

from __future__ import annotations

import hashlib
import os
import types

from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.dispatcher import Dispatcher
from grove.kaizen_ledger import KaizenLedger
from grove.utils import fs_utils


class _FakeLedger:
    def __init__(self):
        self.events = []

    def record(self, event_type, **fields):
        self.events.append((event_type, fields))
        return {}


class _RaisingLedger:
    def record(self, event_type, **fields):
        raise RuntimeError("ledger unavailable")


def _write_intent(path, call_id="c1"):
    return types.SimpleNamespace(
        tool_name="write_file", arguments={"path": path, "content": "x"},
        call_id=call_id,
    )


def _shell(monkeypatch, *, active_slug="researcher", intent_class="research"):
    """A minimal Dispatcher carrying exactly the per-turn state the seam
    reads (the test_contract_provenance precedent)."""
    d = Dispatcher.__new__(Dispatcher)
    d._last_loaded_primary_slug = active_slug
    d._current_turn_id = "sess#7"
    d._current_turn_classification = (
        types.SimpleNamespace(intent_class=intent_class)
        if intent_class is not None else None
    )
    d._current_turn_tool_invocations = []
    # Isolate from fleet-sink governance and the workspace policy — this file
    # exercises the identity emission, not the gates.
    monkeypatch.setattr(d, "_fleet_governance", lambda: [], raising=False)
    monkeypatch.setattr(fs_utils, "is_write_allowed", lambda *a, **k: True)
    return d


# ── ALLOWED write emits, all six fields populated (REAL ledger — proves the
#    EVENT_TYPES registration; an unregistered type would ValueError inside
#    the wrap and silently drop, the ledger-eventtype-hygiene orphan class) ──


def test_allowed_write_emits_artifact_written(
    monkeypatch, tmp_path, hermetic_grove_home
):
    d = _shell(monkeypatch)
    ledger = KaizenLedger("artifact-id-test", ledger_dir=tmp_path / "ledger")
    target = str(tmp_path / "sink" / "brief-x.json")

    out = d._enforce_write_confinement([_write_intent(target)], None, ledger)

    assert out is None  # write allowed, batch proceeds
    events = [e for e in ledger.events() if e["event_type"] == "artifact_written"]
    assert len(events) == 1
    ev = events[0]
    canonical = canonical_artifact_path(target)
    assert ev["path"] == canonical
    assert ev["artifact_id"] == artifact_id(canonical)
    assert ev["turn_id"] == "sess#7"
    assert ev["active_primary_skill_slug"] == "researcher"
    assert ev["intent_class"] == "research"
    assert ev["tool"] == "write_file"


# ── Denied write emits NOTHING ───────────────────────────────────────────────


def test_denied_write_emits_no_artifact_written(monkeypatch, tmp_path):
    d = _shell(monkeypatch)
    monkeypatch.setattr(fs_utils, "is_write_allowed", lambda *a, **k: False)
    ledger = _FakeLedger()

    out = d._enforce_write_confinement(
        [_write_intent(str(tmp_path / "x.json"))], None, ledger,
    )

    assert out is not None  # batch refused
    assert not [e for e in ledger.events if e[0] == "artifact_written"]


# ── Emit failure: write proceeds, loud log, unmistakably NOT a write failure ─


def test_emit_failure_never_denies_the_write(monkeypatch, tmp_path, caplog):
    d = _shell(monkeypatch)

    with caplog.at_level("WARNING"):
        out = d._enforce_write_confinement(
            [_write_intent(str(tmp_path / "x.json"))], None, _RaisingLedger(),
        )

    assert out is None  # the write proceeds untouched
    warnings = [
        r.getMessage() for r in caplog.records
        if "artifact_written EMISSION failed" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "the write itself proceeds untouched" in warnings[0]


# ── ID derivation: deterministic, 16 hex, prefix-joins to the cellar hash ────


def test_artifact_id_deterministic_16_hex():
    p = canonical_artifact_path("~/sink/brief-x.json")
    a, b = artifact_id(p), artifact_id(p)
    assert a == b  # same path → same ID
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_canonical_path_is_unresolved_abspath():
    # expanduser + abspath, NEVER realpath — the cellar hashes the unresolved
    # form (VM ~/.grove symlink), so identity must preserve symlinks.
    p = canonical_artifact_path("~/sink/brief-x.json")
    assert p == os.path.abspath(os.path.expanduser("~/sink/brief-x.json"))
    assert os.path.isabs(p)


def test_artifact_id_prefix_joins_cellar_hash():
    # The cellar derives sha256(page.source)[:_HASH_LEN] over the same input
    # (grove/wiki/pipeline.py::_write_page). Prove ID[:8] == that hash.
    from grove.wiki.pipeline import _HASH_LEN

    p = canonical_artifact_path("~/.grove/researcher/brief-x.json")
    cellar_hash = hashlib.sha256(p.encode("utf-8")).hexdigest()[:_HASH_LEN]
    assert artifact_id(p)[:_HASH_LEN] == cellar_hash
