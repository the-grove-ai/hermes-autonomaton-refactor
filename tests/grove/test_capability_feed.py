"""Tests for the unified capability-telemetry feed (GRV-009 E3 C1).

Covers the feed module (record shape, rotation, A7 failure isolation,
flush-on-shutdown, enqueue budget) and the dual-write at AIAgent._invoke_tool
(attributed path, null-attribution path, error status, A7 never-into-turn).
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove import capability_feed


# ── Feed module ───────────────────────────────────────────────────────────────


@pytest.fixture
def feed_home(tmp_path, monkeypatch):
    """Redirect the grove home to a tmp dir and give each test a fresh feed."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    capability_feed.reset()
    yield tmp_path
    capability_feed.reset()


def _record(**over):
    r = {f: None for f in capability_feed.FIELDS}
    r.update({
        "ts": capability_feed.utc_now_iso(),
        "session_id": "s1", "turn_id": "s1#1", "tool_name": "calendar_list",
        "intent_class": "scheduling", "tier": "T1", "zone": "green",
        "invocation": "native", "result_status": "ok", "latency_ms": 12.5,
    })
    r.update(over)
    return r


def test_record_shape_persisted_with_all_thirteen_fields(feed_home):
    capability_feed.enqueue(_record(capability_id="workspace_read"))
    capability_feed.flush()
    path = feed_home / ".capability_feed" / "feed.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec.keys()) == set(capability_feed.FIELDS)
    assert len(capability_feed.FIELDS) == 13
    assert "invocation" in rec
    assert rec["capability_id"] == "workspace_read"
    assert rec["tool_name"] == "calendar_list"
    assert rec["invocation"] == "native"
    assert rec["result_status"] == "ok"


def test_null_attribution_record_persists(feed_home):
    # A non-capability invocation: capability_id stays null, record still lands.
    capability_feed.enqueue(_record(tool_name="terminal", capability_id=None))
    capability_feed.flush()
    rec = json.loads((feed_home / ".capability_feed" / "feed.jsonl").read_text().strip())
    assert rec["tool_name"] == "terminal"
    assert rec["capability_id"] is None


def test_size_based_rotation(feed_home, monkeypatch):
    monkeypatch.setattr(capability_feed, "_MAX_BYTES", 500)
    for i in range(60):
        capability_feed.enqueue(_record(turn_id=f"s1#{i}"))
    capability_feed.flush()
    d = feed_home / ".capability_feed"
    assert (d / "feed.jsonl").exists()
    rolled = list(d.glob("feed-*.jsonl"))
    assert rolled, "expected at least one rotated segment"


def test_flush_on_shutdown_is_durable(feed_home):
    for i in range(25):
        capability_feed.enqueue(_record(turn_id=f"s1#{i}"))
    capability_feed.flush()  # the shutdown-hook entry point
    lines = (feed_home / ".capability_feed" / "feed.jsonl").read_text().strip().splitlines()
    assert len(lines) == 25


def test_A7_write_failure_alerts_and_never_raises(feed_home, monkeypatch, caplog):
    # Force the drainer's file open to fail on every record.
    def _boom(self):
        raise OSError("disk on fire")
    monkeypatch.setattr(capability_feed._Feed, "_open", _boom)

    with caplog.at_level(logging.ERROR, logger="grove.observability"):
        capability_feed.enqueue(_record())   # must NOT raise on the turn path
        capability_feed.flush()

    alerts = [r for r in caplog.records
              if r.name == "grove.observability"
              and "observability_telemetry_failure" in r.getMessage()]
    assert alerts, "a write failure must raise the dedicated observability alert"


def test_A7_feed_module_never_references_capability_circuit_breaker():
    # Structural proof of A7: the feed cannot couple to capability execution.
    src = Path(capability_feed.__file__).read_text()
    assert "circuit_breaker" not in src
    assert "CircuitBreaker" not in src


def test_enqueue_budget_p99_under_50us(feed_home):
    capability_feed.enqueue(_record())  # warm up the drainer thread
    capability_feed.flush()
    samples = []
    rec = _record()
    for _ in range(5000):
        t0 = time.perf_counter()
        capability_feed.enqueue(rec)
        samples.append((time.perf_counter() - t0) * 1e6)  # microseconds
    capability_feed.flush()
    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]
    assert p99 < 50.0, f"enqueue p99={p99:.2f}us exceeds the 50us budget"


# ── Dual-write at AIAgent._invoke_tool ────────────────────────────────────────


def _agent(monkeypatch, captured, *, applied_zones=None, ws_verbs=frozenset()):
    import run_agent
    a = object.__new__(run_agent.AIAgent)
    a.session_id = "sess9"
    a._dispatcher_singleton = SimpleNamespace(_current_turn_id="sess9#3")
    a._capability_applied_zones = applied_zones or {}
    a._workspace_verb_names = lambda: ws_verbs
    # capture feed records instead of writing them
    monkeypatch.setattr("grove.capability_feed.enqueue", lambda rec: captured.append(rec))
    return a


def test_invoke_tool_emits_null_attribution_record(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "the result"

    out = a._invoke_tool("web_search", {"q": "x"}, "task1")

    assert out == "the result"               # impl result returned byte-for-byte
    assert len(captured) == 1
    rec = captured[0]
    assert rec["tool_name"] == "web_search"
    assert rec["result_status"] == "ok"
    assert rec["capability_id"] is None      # null-attribution path
    assert rec["invocation"] == "native"     # registry-dispatched verb
    assert rec["turn_id"] == "sess9#3"
    assert rec["latency_ms"] >= 0.0
    assert set(rec.keys()) == set(capability_feed.FIELDS)


def test_invocation_kind_classification(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "ok"

    a._invoke_tool("mcp_notion_API_post_search", {}, "t")   # mcp prefix
    a._invoke_tool("clarify", {}, "t")                       # agent inline builtin
    a._invoke_tool("calendar_list", {}, "t")                 # registry-dispatched

    kinds = {r["tool_name"]: r["invocation"] for r in captured}
    assert kinds["mcp_notion_API_post_search"] == "mcp"
    assert kinds["clarify"] == "agent-tool"
    assert kinds["calendar_list"] == "native"


def test_invoke_tool_attributed_path(monkeypatch):
    captured = []
    a = _agent(
        monkeypatch, captured,
        applied_zones={"workspace_read": "green", "workspace_write": "yellow"},
        ws_verbs=frozenset({"calendar_list", "gmail_send"}),
    )
    a._invoke_tool_impl = lambda *args, **kw: "[]"
    monkeypatch.setattr("grove.zones.classify",
                        lambda name: SimpleNamespace(zone="green"))

    a._invoke_tool("calendar_list", {}, "task1")

    assert captured[0]["capability_id"] == "workspace_read"   # zone-matched
    assert captured[0]["zone"] == "green"


def test_invoke_tool_non_carrier_tool_on_capability_turn_is_null(monkeypatch):
    captured = []
    a = _agent(
        monkeypatch, captured,
        applied_zones={"workspace_read": "green"},
        ws_verbs=frozenset({"calendar_list"}),
    )
    a._invoke_tool_impl = lambda *args, **kw: "ok"
    monkeypatch.setattr("grove.zones.classify",
                        lambda name: SimpleNamespace(zone="yellow"))

    a._invoke_tool("terminal", {"command": "ls"}, "task1")  # not a workspace verb

    assert captured[0]["capability_id"] is None


def test_invoke_tool_error_status_still_records_and_reraises(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)

    def _boom(*args, **kw):
        raise RuntimeError("tool blew up")
    a._invoke_tool_impl = _boom

    with pytest.raises(RuntimeError, match="tool blew up"):
        a._invoke_tool("terminal", {}, "task1")
    assert captured[0]["result_status"] == "error"   # recorded despite the raise


def test_A7_emit_failure_never_crosses_into_turn(monkeypatch):
    # If the feed assembly itself explodes, the turn must be unharmed.
    a = _agent(monkeypatch, [])
    a._invoke_tool_impl = lambda *args, **kw: "clean result"
    monkeypatch.setattr(
        "grove.capability_feed.enqueue",
        lambda rec: (_ for _ in ()).throw(RuntimeError("feed exploded")),
    )

    out = a._invoke_tool("web_search", {}, "task1")  # must not raise
    assert out == "clean result"


# ── GRV-009 E4 C3 — MCP feed attribution ──────────────────────────────────────


def test_invoke_tool_mcp_attributed_read(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "{}"
    monkeypatch.setattr("grove.zones.classify", lambda name: SimpleNamespace(zone="green"))
    a._invoke_tool("mcp_notion_notion_search", {}, "t")
    rec = captured[0]
    assert rec["invocation"] == "mcp"
    assert rec["capability_id"] == "notion_read"


def test_invoke_tool_mcp_attributed_write(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "{}"
    monkeypatch.setattr("grove.zones.classify", lambda name: SimpleNamespace(zone="yellow"))
    a._invoke_tool("mcp_notion_notion_create_pages", {}, "t")
    rec = captured[0]
    assert rec["invocation"] == "mcp"
    assert rec["capability_id"] == "notion_write"


def test_invoke_tool_mcp_api_variant_yellow_failsafe(monkeypatch):
    # Unmapped mcp_notion_API_* variants default to yellow -> notion_write.
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "{}"
    monkeypatch.setattr("grove.zones.classify", lambda name: SimpleNamespace(zone="yellow"))
    a._invoke_tool("mcp_notion_API_post_search", {}, "t")
    assert captured[0]["capability_id"] == "notion_write"


def test_invoke_tool_mcp_non_record_stays_null(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "ok"
    monkeypatch.setattr("grove.zones.classify", lambda name: SimpleNamespace(zone="green"))
    a._invoke_tool("mcp_unknownserver_do_thing", {}, "t")
    rec = captured[0]
    assert rec["invocation"] == "mcp"
    assert rec["capability_id"] is None   # non-record MCP server -> null-attributed


def test_A7_mcp_attribution_failure_never_crosses_into_turn(monkeypatch):
    captured = []
    a = _agent(monkeypatch, captured)
    a._invoke_tool_impl = lambda *args, **kw: "clean"
    monkeypatch.setattr("grove.zones.classify", lambda name: SimpleNamespace(zone="green"))
    # The attribution-map build explodes — must not reach the turn.
    monkeypatch.setattr(
        "grove.capability_registry.load_capabilities",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry boom")),
    )
    out = a._invoke_tool("mcp_notion_notion_search", {}, "t")  # must not raise
    assert out == "clean"
    assert captured[0]["invocation"] == "mcp"
    assert captured[0]["capability_id"] is None   # empty map -> null
