"""Unit tests for the researcher retrieval-broker resolver (P2 wiring).

Verifies the REORDERED lifecycle (broker before claim; claim only on success),
the partition invariant (declarative stays True; no "rows" key — structurally),
the loop-plumbing prohibition (no asyncio.run fallback), and registration.
resolve_file_source is not exercised or modified here. All offline.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

import pytest

from grove.fleet import resolvers
from grove.fleet.errors import FleetWorkerAndon
from grove.fleet.resolvers import (
    _RESOLVERS,
    _production_broker_drive,
    make_researcher_broker_resolver,
    set_broker_loop,
)
from grove.fleet.retrieval_broker import BrokerBudgetExceeded
from grove.fleet.worker_entry import _is_declarative_payload

_INPUT_STATE = {
    "type": "researcher_broker",
    "source_dir": "research-requests",
    "pattern": "*.json",
    "slug_regex": r"^(.+)\.json$",
    "lifecycle": "one_shot",
    "required_keys": ["operator_intent", "topic"],
    "select_one": True,
    "skip_already_staged": True,
}

_REQUEST = {
    "operator_intent": {"angle": "x", "audience": "eng", "thesis": "t"},
    "topic": "climate policy",
    "slug": "climate-x",
    "origin": "operator",
}

_EXPECTED_KEYS = {
    "units", "source_dir", "source_path", "source_name", "unit_id",
    "request_claim", "request_content", "broker",
}


def _setup_requests(tmp_path, monkeypatch, *, name="req1.json", body=None):
    """Hermetic GROVE_HOME with one request file; isolate the unit-state store."""
    # Hermetic GROVE_HOME: the real unit-state context builds empty here, so
    # nothing is excluded (a fresh grove has no staged/dead-lettered units).
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    req_dir = tmp_path / "research-requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / name).write_text(json.dumps(body or _REQUEST), encoding="utf-8")
    return req_dir


def _fake_broker_dict():
    return {
        "queries_issued": ["climate policy 2030"],
        "phase_duration_ms": 42,
        "materials": [{"source_id": "src-0000", "url": "https://a", "content": "body"}],
    }


# ── partition invariant ───────────────────────────────────────────────────────
def test_broker_payload_is_declarative_and_has_no_rows(tmp_path, monkeypatch):
    _setup_requests(tmp_path, monkeypatch)
    resolver = make_researcher_broker_resolver(drive=lambda rb, wid: _fake_broker_dict())
    payload = resolver(_INPUT_STATE, "researcher")

    assert _is_declarative_payload(payload) is True   # units present, rows absent
    assert "rows" not in payload
    assert set(payload.keys()) == _EXPECTED_KEYS       # fixed set; adding rows breaks this
    assert "units" in payload and payload["units"]
    # the two sibling keys, broker dict verbatim
    assert payload["broker"] == _fake_broker_dict()
    assert payload["request_content"] == {
        "topic": "climate policy",
        "operator_intent": _REQUEST["operator_intent"],
        "slug": "climate-x",
        "origin": "operator",
    }


def test_no_rows_key_structurally():
    # STRUCTURAL, not a fixture spot-check: parse the resolver factory's AST and
    # assert NO dict literal anywhere in it uses a "rows" key. Ignores comments/
    # docstrings, so it proves no code path in the resolver can emit a rows key.
    src = textwrap.dedent(inspect.getsource(make_researcher_broker_resolver))
    tree = ast.parse(src)
    rows_keys = [
        k
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and k.value == "rows"
    ]
    assert rows_keys == []


def test_partition_holds_across_varied_broker_results(tmp_path, monkeypatch):
    _setup_requests(tmp_path, monkeypatch)
    for broker in ({"materials": []}, {"a": 1, "b": [1, 2]}, _fake_broker_dict()):
        resolver = make_researcher_broker_resolver(drive=lambda rb, wid, b=broker: b)
        # fresh request each iteration (prior claim moved it away)
        (tmp_path / "research-requests" / "req1.json").write_text(
            json.dumps(_REQUEST), encoding="utf-8"
        )
        payload = resolver(_INPUT_STATE, "researcher")
        assert _is_declarative_payload(payload) is True
        assert "rows" not in payload


# ── reorder: claim only on broker success ─────────────────────────────────────
def test_success_claims_request_after_broker(tmp_path, monkeypatch):
    req_dir = _setup_requests(tmp_path, monkeypatch)
    resolver = make_researcher_broker_resolver(drive=lambda rb, wid: _fake_broker_dict())
    payload = resolver(_INPUT_STATE, "researcher")

    # request moved OUT of the request dir INTO .processing/ (claimed on success)
    assert not (req_dir / "req1.json").exists()
    assert (req_dir / ".processing" / "req1.json").exists()
    assert payload["request_claim"]["path"] == str(req_dir / ".processing" / "req1.json")
    assert payload["source_path"] == str(req_dir / ".processing" / "req1.json")


def test_broker_error_does_not_claim_and_raises(tmp_path, monkeypatch):
    req_dir = _setup_requests(tmp_path, monkeypatch)

    def drive_halt(request_body, worker_id):
        raise BrokerBudgetExceeded("budget blown")

    resolver = make_researcher_broker_resolver(drive=drive_halt)
    with pytest.raises(BrokerBudgetExceeded):
        resolver(_INPUT_STATE, "researcher")

    # NOT claimed — request stays queued in the request dir; no .processing/ move.
    assert (req_dir / "req1.json").exists()
    assert not (req_dir / ".processing" / "req1.json").exists()


def test_broker_error_logs_typed_cause(tmp_path, monkeypatch, caplog):
    _setup_requests(tmp_path, monkeypatch)

    def drive_halt(request_body, worker_id):
        raise BrokerBudgetExceeded("budget blown at 46s")

    resolver = make_researcher_broker_resolver(drive=drive_halt)
    with caplog.at_level("ERROR"):
        with pytest.raises(BrokerBudgetExceeded):
            resolver(_INPUT_STATE, "researcher")
    msg = " ".join(r.message for r in caplog.records)
    assert "BrokerBudgetExceeded" in msg      # typed cause, not bare "resolver_failed"
    assert "budget blown at 46s" in msg
    assert "NOT claimed" in msg


# ── no work ───────────────────────────────────────────────────────────────────
def test_no_request_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "research-requests").mkdir(parents=True)
    resolver = make_researcher_broker_resolver(drive=lambda rb, wid: _fake_broker_dict())
    assert resolver(_INPUT_STATE, "researcher") is None


# ── loop plumbing (Ruling 2): no asyncio.run fallback ────────────────────────
def test_missing_loop_is_loud_andon(monkeypatch):
    monkeypatch.setattr(resolvers, "_broker_loop", None)
    with pytest.raises(FleetWorkerAndon) as ei:
        _production_broker_drive({"topic": "x"}, "researcher")
    assert ei.value.check == "broker_no_loop"


def test_set_broker_loop_threads_the_loop():
    sentinel = object()
    try:
        set_broker_loop(sentinel)
        assert resolvers._broker_loop is sentinel
    finally:
        set_broker_loop(None)


def test_manager_init_threads_gateway_loop():
    from grove.fleet.manager import FleetManager

    sentinel = object()
    try:
        FleetManager(loop=sentinel)
        assert resolvers._broker_loop is sentinel
    finally:
        set_broker_loop(None)


# ── registration ──────────────────────────────────────────────────────────────
def test_broker_resolver_registered():
    assert "researcher_broker" in _RESOLVERS
    # file_source (the other declarative workers' resolver) is untouched + distinct.
    assert "file_source" in _RESOLVERS
    assert _RESOLVERS["researcher_broker"] is not _RESOLVERS["file_source"]
