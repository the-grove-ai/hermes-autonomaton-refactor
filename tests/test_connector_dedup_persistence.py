"""connector-dedup-persistence — rebuild-simulating acceptance tests.

Validates that _surfaced_connector_ids and _connector_failure_offers survive
the gateway's per-turn agent rebuild via the session-scoped .push_cadence.json
store.

Pattern mirrors test_push_cadence.py: each test that simulates a rebuild
creates a FRESH agent (AIAgent.__new__) rather than carrying Python object
state across turns.
"""
from __future__ import annotations

import json

import pytest

import tools.mcp_tool as mt
from run_agent import AIAgent
from tools.flywheel_review_tool import _read_push_cadence, _write_push_cadence


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_breaker():
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()
    yield
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    """Redirect the cadence file to a throwaway tmp dir."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _agent(session_id: str = "", enabled_toolsets=None) -> AIAgent:
    """Bare AIAgent with only the connector-offer state the methods use."""
    a = AIAgent.__new__(AIAgent)
    a.enabled_toolsets = enabled_toolsets
    a.session_id = session_id
    return a


# ── Test 1: _read returns empty connector_active_map when field is absent ─────


def test_read_returns_empty_connector_active_map_when_missing(grove_home):
    """Old store files without connector_active_map load cleanly (backward compat)."""
    path = grove_home / ".push_cadence.json"
    path.write_text(
        json.dumps({
            "session_id": "s1",
            "last_push_turn": 3,
            "surfaced_ids": [],
            "surfaced_connectors": [],
            # no connector_active_map key
        }),
        encoding="utf-8",
    )
    result = _read_push_cadence("s1")
    assert result["connector_active_map"] == {}


# ── Test 2: round-trip write/read ─────────────────────────────────────────────


def test_write_read_roundtrip_connector_active_map(grove_home):
    """connector_active_map written and read back intact."""
    S = "sess-roundtrip"
    _write_push_cadence(
        S,
        last_push_turn=2,
        surfaced_ids=set(),
        surfaced_connectors=set(),
        connector_active_map={"connector_failure:abc123": "notion"},
    )
    result = _read_push_cadence(S)
    assert result["connector_active_map"] == {"connector_failure:abc123": "notion"}


# ── Test 3: orphan self-heal — ANDON A1 ───────────────────────────────────────


def test_orphan_self_heal_evicts_from_shown_set(grove_home):
    """A pid in surfaced_connectors with no active-map entry is evicted on access.

    Without the map entry the pid can never be removed from shown, which would
    permanently suppress the connector.  The orphan-heal block must catch and
    evict it so the connector can surface again.
    """
    S = "sess-orphan"
    orphan_pid = "connector_failure:deadbeef"
    # Write a store with an orphan: pid in surfaced_connectors but no map entry.
    _write_push_cadence(
        S,
        last_push_turn=1,
        surfaced_ids=set(),
        surfaced_connectors={orphan_pid},
        connector_active_map={},  # deliberately empty → orphan
    )

    # Trip the breaker so the connector actually fires.
    mt._bump_connect_failed("notion", "reauth")

    agent = _agent(session_id=S)
    # First call initialises shown from store (orphan_pid is in it) and then the
    # orphan-heal block removes it before the loop.  The notion pid is NEW (not
    # the orphan), so the offer fires.
    result = agent._append_connector_failure_offer("the answer")
    assert "hermes mcp login notion" in result, (
        "offer must fire — orphan must not block unrelated connector"
    )

    # The orphan pid must no longer be in the shown set.
    assert orphan_pid not in agent._surfaced_connector_ids


# ── Test 4: hydration — shown-set populated from store on init ───────────────


def test_hydration_shown_set_populated_from_store(grove_home):
    """After a rebuild the agent inherits surfaced_connectors from the store."""
    S = "sess-hydrate"
    mt._bump_connect_failed("notion", "reauth")
    pid = AIAgent.__new__(AIAgent)._connector_failure_id("notion", "reauth")

    # Simulate: a prior agent turn persisted the pid.
    _write_push_cadence(
        S,
        last_push_turn=1,
        surfaced_ids=set(),
        surfaced_connectors={pid},
        connector_active_map={pid: "notion"},
    )

    # Fresh agent — shown-set is NOT pre-loaded in Python; it must come from store.
    agent = _agent(session_id=S)
    assert not hasattr(agent, "_surfaced_connector_ids") or agent._surfaced_connector_ids is None  # type: ignore[attr-defined]

    # _append_connector_failure_offer triggers hydration; pid already in store →
    # offer must NOT be re-appended.
    result = agent._append_connector_failure_offer("answer text")
    assert result == "answer text", (
        "offer must be suppressed — pid was already surfaced in prior turn"
    )


# ── Test 5: ADD persists to store ─────────────────────────────────────────────


def test_add_persists_to_store(grove_home):
    """When a connector failure offer is surfaced the store is updated."""
    S = "sess-add"
    mt._bump_connect_failed("notion", "reauth")

    agent = _agent(session_id=S)
    agent._append_connector_failure_offer("answer")

    cad = _read_push_cadence(S)
    pid = agent._connector_failure_id("notion", "reauth")
    assert pid in cad["surfaced_connectors"]
    assert pid in cad["connector_active_map"]
    assert cad["connector_active_map"][pid] == "notion"


# ── Test 6: EVICT persists to store ───────────────────────────────────────────


def test_evict_persists_to_store(grove_home):
    """After retry the eviction is written to the store."""
    S = "sess-evict"
    mt._bump_connect_failed("notion", "reauth")

    agent = _agent(session_id=S)
    agent._append_connector_failure_offer("answer")
    pid = agent._connector_failure_id("notion", "reauth")

    # Confirm pid was persisted on ADD.
    assert pid in _read_push_cadence(S)["surfaced_connectors"]

    agent._connector_offer_retry("notion")

    cad = _read_push_cadence(S)
    assert pid not in cad["surfaced_connectors"]
    assert pid not in cad["connector_active_map"]


# ── Test 7: DISMISS persists to store ─────────────────────────────────────────


def test_dismiss_persists_to_store(grove_home):
    """After dismiss the pid stays in surfaced_connectors (suppress) and drops
    from connector_active_map (no longer active)."""
    S = "sess-dismiss"
    mt._bump_connect_failed("notion", "reauth")

    agent = _agent(session_id=S)
    agent._append_connector_failure_offer("answer")
    pid = agent._connector_failure_id("notion", "reauth")

    agent._connector_offer_dismiss(pid)

    cad = _read_push_cadence(S)
    # Suppress must survive: pid stays in surfaced_connectors.
    assert pid in cad["surfaced_connectors"]
    # Active-map must not contain the dismissed pid (no longer being offered).
    assert pid not in cad["connector_active_map"]


# ── Test 8: backward compat — old store loads without connector_active_map ────


def test_backward_compat_old_store_without_active_map(grove_home):
    """A store written before this sprint (no connector_active_map) reads cleanly
    and the missing field defaults to {}."""
    S = "sess-compat"
    path = grove_home / ".push_cadence.json"
    path.write_text(
        json.dumps({
            "session_id": S,
            "last_push_turn": 7,
            "surfaced_ids": ["prop-abc"],
            "surfaced_connectors": ["connector_failure:xyz"],
            # no connector_active_map
        }),
        encoding="utf-8",
    )
    cad = _read_push_cadence(S)
    assert cad["last_push_turn"] == 7
    assert "prop-abc" in cad["surfaced_ids"]
    assert "connector_failure:xyz" in cad["surfaced_connectors"]
    assert cad["connector_active_map"] == {}  # default, not a KeyError


# ── Test 9: EVICT on rebuilt agent preserves other connectors ─────────────────


def test_evict_on_rebuilt_agent_preserves_other_connectors(grove_home):
    """_connector_offer_retry on a rebuilt agent (shown/active = None) evicts
    only the target connector's pids, leaving other connectors' entries intact.

    Regression: the old code used ``shown or set()`` which collapsed to an
    empty set when the agent was rebuilt, destroying all suppress entries for
    every connector — not just the one being retried.
    """
    S = "sess-rebuild-evict"
    pid_a = "connector_failure:pid_notion_aa"
    pid_b = "connector_failure:pid_github_bb"

    # Seed the store with two connectors already surfaced.
    _write_push_cadence(
        S,
        last_push_turn=3,
        surfaced_ids=set(),
        surfaced_connectors={pid_a, pid_b},
        connector_active_map={pid_a: "notion", pid_b: "github"},
    )

    # Fresh agent simulating a per-turn rebuild — no in-memory shown/active.
    agent = _agent(session_id=S)
    assert not hasattr(agent, "_surfaced_connector_ids") or agent._surfaced_connector_ids is None  # type: ignore[attr-defined]
    assert not hasattr(agent, "_connector_failure_offers") or agent._connector_failure_offers is None  # type: ignore[attr-defined]

    # Retry only notion.
    agent._connector_offer_retry("notion")

    cad = _read_push_cadence(S)
    # notion's pid_a must be evicted.
    assert pid_a not in cad["surfaced_connectors"], (
        "notion pid must be evicted from surfaced_connectors"
    )
    assert pid_a not in cad["connector_active_map"], (
        "notion pid must be evicted from connector_active_map"
    )
    # github's pid_b must survive.
    assert pid_b in cad["surfaced_connectors"], (
        "github pid must remain in surfaced_connectors"
    )
    assert pid_b in cad["connector_active_map"], (
        "github pid must remain in connector_active_map"
    )
    assert cad["connector_active_map"][pid_b] == "github"
