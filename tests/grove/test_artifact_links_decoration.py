"""artifact-identity-v1 C3 — artifact-links answer decoration.

Seam side: the write-confinement ALLOWED path stashes {artifact_id,
display_name} on the agent (the _tier_fallback_notice mirror), deduped by id,
only for RECORDED events. Hook side: _append_artifact_links consumes the stash
once and renders the template-locked frame; base-URL failure or any exception
leaves the answer byte-identical (loud log).
"""

from __future__ import annotations

import types

import pytest

import run_agent
from grove.artifact_identity import artifact_id, canonical_artifact_path
from grove.dispatcher import Dispatcher
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


def _shell(monkeypatch):
    d = Dispatcher.__new__(Dispatcher)
    d._last_loaded_primary_slug = None
    d._current_turn_id = "sess#9"
    d._current_turn_classification = None
    d._current_turn_tool_invocations = []
    monkeypatch.setattr(d, "_fleet_governance", lambda: [], raising=False)
    monkeypatch.setattr(fs_utils, "is_write_allowed", lambda *a, **k: True)
    return d


def _agent():
    return run_agent.AIAgent.__new__(run_agent.AIAgent)


def _aid(path_str):
    return artifact_id(canonical_artifact_path(path_str))


# ── seam side: stash population ──────────────────────────────────────────────


def test_two_writes_stash_two_links(monkeypatch, tmp_path):
    d = _shell(monkeypatch)
    agent = _agent()
    a, b = str(tmp_path / "a.md"), str(tmp_path / "b.json")
    out = d._enforce_write_confinement(
        [_write_intent(a, "c1"), _write_intent(b, "c2")], agent, _FakeLedger(),
    )
    assert out is None
    links = agent._artifact_links_notice
    assert links == [
        {"artifact_id": _aid(a), "display_name": "a.md"},
        {"artifact_id": _aid(b), "display_name": "b.json"},
    ]


def test_same_path_twice_stashes_one_link(monkeypatch, tmp_path):
    d = _shell(monkeypatch)
    agent = _agent()
    p = str(tmp_path / "same.md")
    out = d._enforce_write_confinement(
        [_write_intent(p, "c1"), _write_intent(p, "c2")], agent, _FakeLedger(),
    )
    assert out is None
    assert agent._artifact_links_notice == [
        {"artifact_id": _aid(p), "display_name": "same.md"},
    ]


def test_unrecorded_event_stashes_nothing(monkeypatch, tmp_path):
    # Emission failed → no ledger event → no link (a stashed id the route
    # cannot resolve would render a dead link).
    d = _shell(monkeypatch)
    agent = _agent()
    out = d._enforce_write_confinement(
        [_write_intent(str(tmp_path / "x.md"))], agent, _RaisingLedger(),
    )
    assert out is None  # write still proceeds
    assert getattr(agent, "_artifact_links_notice", None) is None


def test_stash_accumulates_across_batches_same_turn(monkeypatch, tmp_path):
    d = _shell(monkeypatch)
    agent = _agent()
    a, b = str(tmp_path / "a.md"), str(tmp_path / "b.md")
    d._enforce_write_confinement([_write_intent(a)], agent, _FakeLedger())
    d._enforce_write_confinement([_write_intent(b)], agent, _FakeLedger())
    assert [l["display_name"] for l in agent._artifact_links_notice] == [
        "a.md", "b.md",
    ]


# ── hook side: decoration ────────────────────────────────────────────────────


def test_decoration_frame_exact(monkeypatch, tmp_path):
    import grove.prompt.portal_links as portal_links

    monkeypatch.setattr(
        portal_links, "resolve_portal_base_url", lambda config=None: "http://ts.example:8642",
    )
    a = _agent()
    id1, id2 = "a" * 16, "b" * 16
    a._artifact_links_notice = [
        {"artifact_id": id1, "display_name": "brief.md"},
        {"artifact_id": id2, "display_name": "notes.txt"},
    ]
    out = a._append_artifact_links("Here is your answer.")
    assert out == (
        "Here is your answer."
        "\n\nArtifacts written this turn:\n"
        f"brief.md: http://ts.example:8642/artifact/{id1}\n"
        f"notes.txt: http://ts.example:8642/artifact/{id2}"
    )


def test_no_writes_answer_byte_identical():
    a = _agent()
    a._artifact_links_notice = None
    assert a._append_artifact_links("answer") == "answer"
    b = _agent()  # attribute never set at all
    assert b._append_artifact_links("answer") == "answer"


def test_base_url_unresolvable_skips_loudly(monkeypatch, caplog):
    import grove.prompt.portal_links as portal_links

    def _boom(config=None):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(portal_links, "resolve_portal_base_url", _boom)
    a = _agent()
    a._artifact_links_notice = [
        {"artifact_id": "a" * 16, "display_name": "x.md"},
    ]
    with caplog.at_level("WARNING"):
        out = a._append_artifact_links("The answer.")
    assert out == "The answer."  # byte-identical, never a broken link
    assert any("[artifact-links]" in r.getMessage() for r in caplog.records)


def test_base_url_falsy_skips_loudly(monkeypatch, caplog):
    import grove.prompt.portal_links as portal_links

    monkeypatch.setattr(
        portal_links, "resolve_portal_base_url", lambda config=None: "",
    )
    a = _agent()
    a._artifact_links_notice = [
        {"artifact_id": "a" * 16, "display_name": "x.md"},
    ]
    with caplog.at_level("WARNING"):
        out = a._append_artifact_links("The answer.")
    assert out == "The answer."
    assert any("unresolvable" in r.getMessage() for r in caplog.records)


def test_stash_consumed_no_leak_across_turns(monkeypatch):
    import grove.prompt.portal_links as portal_links

    monkeypatch.setattr(
        portal_links, "resolve_portal_base_url", lambda config=None: "http://h:1",
    )
    a = _agent()
    a._artifact_links_notice = [
        {"artifact_id": "a" * 16, "display_name": "x.md"},
    ]
    first = a._append_artifact_links("turn one answer")
    assert "Artifacts written this turn:" in first
    assert a._artifact_links_notice is None  # consumed
    # Next turn, no new writes: nothing rides.
    assert a._append_artifact_links("turn two answer") == "turn two answer"


def test_empty_response_not_decorated(monkeypatch):
    a = _agent()
    a._artifact_links_notice = [
        {"artifact_id": "a" * 16, "display_name": "x.md"},
    ]
    assert a._append_artifact_links("") == ""


def test_dispatcher_turn_reset_wipes_stale_stash():
    """Source pin: the dispatcher's per-turn reset block (the agent._tier_budget
    carrier wipe) must also wipe the artifact-links stash, so a turn whose
    response path never reached the consume hook cannot leak links into the
    next turn's answer. Pinned at source level because the reset lives deep in
    dispatch_turn (driving a full turn here would be an integration test)."""
    import inspect

    import grove.dispatcher as dispatcher_mod

    src = inspect.getsource(dispatcher_mod)
    reset_idx = src.find("agent._artifact_links_notice = None")
    anchor_idx = src.find("agent._tier_budget = None")
    assert reset_idx != -1, "per-turn artifact-links stash reset is missing"
    assert anchor_idx != -1
    # Same reset block: the wipe sits within the tier-carrier reset region.
    assert 0 < reset_idx - anchor_idx < 600
