"""Phase 3 (Option 3, operator-ratified) — flywheel memory CLI surface.

The pull-review approval path: `flywheel memory list/show/approve/reject`.
Each routes through run_digest, so approval mints store events and records a
kaizen_disposition, identical to any other surface that drives the engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.memory.cli import (
    cli_memory_approve,
    cli_memory_list,
    cli_memory_reject,
    cli_memory_show,
    memory_proposal_short_id,
)
from grove.memory.store import MemoryStore


def _proposal(content="A fact.", action="create", target_id=None):
    return {
        "action": action,
        "target_id": target_id,
        "dock_goal_ref": None,
        "proposed_record": {
            "entity_type": "DomainFact",
            "content": content,
            "confidence": 0.9,
            "justification": "matters",
        },
    }


def _stage(base: Path, session_id: str, proposal: dict):
    ppath = base / "memory_proposals.jsonl"
    rec = {"session_id": session_id, "status": "pending",
           "timestamp": "2026-06-01T00:00:00+00:00", "proposal": proposal}
    with open(ppath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _records(base: Path):
    ppath = base / "memory_proposals.jsonl"
    return [json.loads(ln) for ln in ppath.read_text().splitlines() if ln.strip()]


def _dispositions(ledger_dir: Path):
    out = []
    for p in Path(ledger_dir).glob("*.jsonl"):
        for ln in p.read_text().splitlines():
            if ln.strip() and json.loads(ln).get("event_type") == "kaizen_disposition":
                out.append(json.loads(ln))
    return out


def test_list_shows_pending(tmp_path, capsys):
    _stage(tmp_path, "s1", _proposal(content="Notion is the tracker."))
    rc = cli_memory_list(base_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 pending" in out
    assert "Notion is the tracker." in out


def test_list_empty(tmp_path, capsys):
    rc = cli_memory_list(base_dir=tmp_path)
    assert rc == 0
    assert "No pending memory proposals" in capsys.readouterr().out


def test_approve_applies_to_store(tmp_path):
    prop = _proposal(content="Approved via CLI.")
    _stage(tmp_path, "s1", prop)
    short = memory_proposal_short_id(prop)

    rc = cli_memory_approve(short, base_dir=tmp_path, ledger_dir=tmp_path / "led")
    assert rc == 0

    store = MemoryStore(base_dir=tmp_path)
    active = [r for r in store.projected_records().values() if r.status == "active"]
    assert any(r.content == "Approved via CLI." for r in active)
    # record flipped + disposition recorded
    rec = [r for r in _records(tmp_path) if r.get("proposal")][0]
    assert rec["status"] == "approved"
    assert any(d["disposition"] == "applied" for d in _dispositions(tmp_path / "led"))


def test_reject_marks_and_records(tmp_path):
    prop = _proposal(content="Rejected via CLI.")
    _stage(tmp_path, "s1", prop)
    short = memory_proposal_short_id(prop)

    rc = cli_memory_reject(short, base_dir=tmp_path, reason="not useful",
                           ledger_dir=tmp_path / "led")
    assert rc == 0
    rec = [r for r in _records(tmp_path) if r.get("proposal")][0]
    assert rec["status"] == "rejected"
    store = MemoryStore(base_dir=tmp_path)
    assert all(r.content != "Rejected via CLI."
               for r in store.projected_records().values())
    assert any(d["disposition"] == "rejected" for d in _dispositions(tmp_path / "led"))


def test_approve_unknown_id_returns_1(tmp_path, capsys):
    _stage(tmp_path, "s1", _proposal())
    rc = cli_memory_approve("deadbeef", base_dir=tmp_path, ledger_dir=tmp_path / "led")
    assert rc == 1
    assert "No pending memory proposal" in capsys.readouterr().err


def test_approve_ambiguous_returns_1(tmp_path, capsys):
    # identical content → identical id → ambiguous selector
    _stage(tmp_path, "s1", _proposal(content="Dup."))
    _stage(tmp_path, "s2", _proposal(content="Dup."))
    short = memory_proposal_short_id(_proposal(content="Dup."))
    rc = cli_memory_approve(short, base_dir=tmp_path, ledger_dir=tmp_path / "led")
    assert rc == 1
    assert "matches" in capsys.readouterr().err.lower()


def test_show_renders_detail(tmp_path, capsys):
    prop = _proposal(content="Shown fact.")
    _stage(tmp_path, "s1", prop)
    short = memory_proposal_short_id(prop)
    rc = cli_memory_show(short, base_dir=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Shown fact." in out
    assert "matters" in out  # justification surfaced
