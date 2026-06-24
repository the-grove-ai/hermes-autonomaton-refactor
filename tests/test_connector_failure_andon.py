"""connector-failure-andon-v1 — the parallel connect-breaker + ephemeral
answer-then-surface Kaizen offering.

The breaker (record / cold-gate exclusion / auth-precedence / clear) is tested
against tools.mcp_tool; the offering (render / dedup / session-relevance /
retry-eviction / dismiss / boundaries) is tested against the AIAgent methods
with the module breaker dict set directly (no event loop needed). The
gather-record tests drive register_mcp_servers with a fast-failing
_connect_server.
"""

from __future__ import annotations

import types

import pytest

import tools.mcp_tool as mt
from run_agent import AIAgent
from tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_breaker(tmp_path, monkeypatch):
    # connector-dedup-persistence: each test gets an isolated cadence store so
    # persist calls in one test cannot bleed into agents created later in the
    # same function or in a subsequent test.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()
    yield
    mt._server_connect_failed.clear()
    mt._server_connect_auth_evidence.clear()
    mt._servers.clear()


def _agent(enabled_toolsets=None) -> AIAgent:
    """A bare AIAgent carrying only the state the connector-offer methods use."""
    a = AIAgent.__new__(AIAgent)
    a.enabled_toolsets = enabled_toolsets
    return a


# ── Breaker primitives — record + auth-wins (DoD 1, 14) ──────────────


def test_1_record_and_signature():
    mt._bump_connect_failed("notion", "reauth")
    mt._bump_connect_failed("github", "unreachable")
    assert mt.get_connect_failures() == {"notion": "reauth", "github": "unreachable"}


def test_14_auth_wins_overwrite_precedence():
    # generic does NOT clobber an existing reauth (late timeout must not downgrade)
    mt._bump_connect_failed("notion", "reauth")
    mt._bump_connect_failed("notion", "unreachable")
    assert mt.get_connect_failures()["notion"] == "reauth"
    # reauth DOES overwrite an existing unreachable (Ruling 3 amended)
    mt._bump_connect_failed("github", "unreachable")
    mt._bump_connect_failed("github", "reauth")
    assert mt.get_connect_failures()["github"] == "reauth"


def test_clear_is_the_only_clear_path():
    mt._bump_connect_failed("notion", "reauth")
    mt._clear_connect_failed("notion")
    assert "notion" not in mt.get_connect_failures()


# ── Gather record + cold-gate (DoD 2, 3, 12, 13) ─────────────────────


def _drive_failed_connect(monkeypatch, name, *, evidence=None, exc=None):
    """Drive register_mcp_servers with a _connect_server that fails fast,
    optionally stashing auth evidence (mimicking C2a) and raising *exc*.
    Returns the call counter."""
    if not getattr(mt, "_MCP_AVAILABLE", False):
        pytest.skip("MCP SDK not available")
    calls = []

    async def _fake_connect(srv_name, config, *, registry):
        calls.append(srv_name)
        if evidence is not None:
            with mt._lock:
                mt._server_connect_auth_evidence[srv_name] = evidence
        raise (exc if exc is not None else RuntimeError("boom"))

    monkeypatch.setattr(mt, "_connect_server", _fake_connect)
    mt.register_mcp_servers({name: {"url": "http://127.0.0.1:9/x"}}, registry=ToolRegistry())
    return calls


def test_13_cancellederror_at_gather_is_recorded(monkeypatch):
    # BaseException widen: a CancelledError (the 60s timeout-cancellation
    # symptom) IS caught and recorded — without this the breaker never trips.
    import asyncio
    _drive_failed_connect(monkeypatch, "notion", exc=asyncio.CancelledError())
    assert "notion" in mt.get_connect_failures()
    assert mt.get_connect_failures()["notion"] == "unreachable"  # no auth evidence


def test_2_cold_gate_excludes_kills_reentry(monkeypatch):
    # Second request must NOT re-attempt the connect (connect-count stays 1).
    import asyncio
    calls = _drive_failed_connect(monkeypatch, "notion", exc=asyncio.CancelledError())
    assert calls == ["notion"]  # first request attempted
    # second request: server is breaker-tripped → cold-gate excludes it
    mt.register_mcp_servers({"notion": {"url": "http://127.0.0.1:9/x"}}, registry=ToolRegistry())
    assert calls == ["notion"]  # NOT re-attempted — the ~60s re-entry tax is dead


def test_12_motivating_auth_evidence_records_reauth(monkeypatch):
    # The Notion case: task pre-marked self._error as auth, surface result is a
    # CancelledError (timeout) → records "reauth" (NOT "unreachable").
    import asyncio
    _drive_failed_connect(monkeypatch, "notion", evidence=True, exc=asyncio.CancelledError())
    assert mt.get_connect_failures()["notion"] == "reauth"  # auth-precedence over the symptom


def test_3_healthy_connector_not_recorded():
    # A connector that never failed is not in the breaker → no offer fires.
    assert mt.get_connect_failures() == {}
    out = _agent()._append_connector_failure_offer("the answer")
    assert out == "the answer"


# ── Offering: render + dedup + session-relevance (DoD 4, 5, 6) ───────


def test_6_signature_render_reauth_displayed():
    mt._bump_connect_failed("notion", "reauth")
    out = _agent()._append_connector_failure_offer("your calendar is clear")
    assert out.startswith("your calendar is clear")          # answer-then-surface
    assert "hermes mcp login notion" in out                   # re-auth DISPLAYED
    assert "Only select Retry after you have authenticated" in out
    # unreachable renders the bug-report branch, no re-auth command
    mt._clear_connect_failed("notion")
    mt._bump_connect_failed("github", "unreachable")
    out2 = _agent()._append_connector_failure_offer("done")
    assert "unreachable this session" in out2 and "bug report" in out2


def test_4_once_per_session_no_re_append():
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    first = agent._append_connector_failure_offer("answer one")
    assert "notion" in first and len(first) > len("answer one")
    # second turn on the SAME (cached) agent: shown-set suppresses re-append
    second = agent._append_connector_failure_offer("answer two")
    assert second == "answer two"


def test_5_session_relevance_both_branches():
    # Each call represents an INDEPENDENT session; give them distinct session_ids
    # so the cadence store for one does not pre-populate the shown-set of the
    # next (connector-dedup-persistence now persists the shown-set to disk).
    mt._bump_connect_failed("notion", "reauth")
    # session enabled only github tools → notion offer is NOT surfaced
    a_irrel = _agent(enabled_toolsets=["mcp-github"])
    a_irrel.session_id = "sess-5-irrelevant"
    irrelevant = a_irrel._append_connector_failure_offer("a")
    assert irrelevant == "a"
    # session enabled notion → surfaced
    a_rel = _agent(enabled_toolsets=["mcp-notion"])
    a_rel.session_id = "sess-5-relevant"
    relevant = a_rel._append_connector_failure_offer("a")
    assert "notion" in relevant
    # None enabled_toolsets = all enabled → surfaced
    a_all = _agent(enabled_toolsets=None)
    a_all.session_id = "sess-5-allon"
    allon = a_all._append_connector_failure_offer("a")
    assert "notion" in allon


# ── Dispositions: retry / dismiss (DoD 7, 8, 11) ─────────────────────


def test_7_retry_clears_breaker_and_re_attempts():
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    agent._append_connector_failure_offer("a")
    agent._connector_offer_retry("notion")
    assert "notion" not in mt.get_connect_failures()  # breaker cleared → cold-gate re-attempts


def test_8_dismiss_removes_without_clearing():
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    agent._append_connector_failure_offer("a")
    pid = agent._connector_failure_id("notion", "reauth")
    agent._connector_offer_dismiss(pid)
    assert pid not in agent._connector_failure_offers        # removed from list
    assert mt.get_connect_failures().get("notion") == "reauth"  # breaker STILL tripped


def test_11_premature_retry_resurfaces_no_silent_suppression():
    # LOAD-BEARING (Ruling 2): retry evicts the shown-set, so a same-signature
    # re-trip RE-SURFACES — the dedup must not silently swallow a real re-fail.
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    first = agent._append_connector_failure_offer("a")
    assert "notion" in first
    # operator hits Retry prematurely (before fixing auth)
    agent._connector_offer_retry("notion")
    # the connector re-fails on the next attempt → same signature re-trips
    mt._bump_connect_failed("notion", "reauth")
    resurfaced = agent._append_connector_failure_offer("b")
    assert "notion" in resurfaced  # NOT suppressed by the (evicted) shown-set


# ── governance-gateway-parity-v1 (Strike 1): text-disposition wiring ──
# The cross-surface Retry/Dismiss text disposition that finally CALLS the
# (previously unreachable) _connector_offer_retry/_dismiss handlers, plus the
# keystone re-offer on a failed re-connect.


def test_disposition_classifier_matches_named_and_bare():
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    assert a._classify_connector_disposition("Retry notion") == ("retry", "notion")
    assert a._classify_connector_disposition("retry") == ("retry", "notion")
    assert a._classify_connector_disposition("Reconnect it") == ("retry", "notion")
    assert a._classify_connector_disposition("Dismiss") == ("dismiss", "notion")
    assert a._classify_connector_disposition("dismiss notion") == ("dismiss", "notion")


def test_disposition_classifier_rejects_ordinary_messages():
    # A sole outstanding failure must NOT make every "retry…" sentence a
    # disposition — only the bare verb or a named connector counts.
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    assert a._classify_connector_disposition("retry the build please") is None
    assert a._classify_connector_disposition("what is the weather?") is None
    assert a._classify_connector_disposition("dismiss the meeting tomorrow") is None
    # No outstanding failure → nothing matches at all.
    mt._server_connect_failed.clear()
    assert a._classify_connector_disposition("Retry notion") is None


def test_disposition_ambiguous_dismiss_needs_a_name():
    # Two failures + a bare "dismiss" is ambiguous → not classified.
    mt._bump_connect_failed("notion", "reauth")
    mt._bump_connect_failed("github", "unreachable")
    a = _agent()
    assert a._classify_connector_disposition("dismiss") is None
    assert a._classify_connector_disposition("dismiss github") == ("dismiss", "github")


def test_retry_disposition_reconnect_success(monkeypatch, tmp_path):
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    a.session_id = "sess-reconnect"
    a._dispatcher_singleton = types.SimpleNamespace(registry=ToolRegistry())
    # learning-loop-bridge-v1 (Strike 2): a verified reconnect that followed a
    # real prior failure now records a correction IntentRecord. Isolate the
    # store so the test does not write to the operator's real ~/.grove.
    from grove.intent_store import IntentStore
    store = IntentStore(store_path=tmp_path / "intent_records.jsonl")
    monkeypatch.setattr("grove.intent_store.get_store", lambda: store)
    # Re-discovery that records no failure == a successful re-connect (the
    # breaker was already cleared by the retry).
    monkeypatch.setattr(mt, "discover_mcp_tools", lambda registry=None: [])
    msg = a._apply_connector_disposition("retry", "notion")
    assert "Reconnected" in msg
    assert "notion" not in mt.get_connect_failures()
    # The remediation was recorded (the block AND the fix).
    import json as _json
    records = [_json.loads(ln) for ln in
               store.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(records) == 1 and records[0]["outcome"] == "correction"


def test_retry_disposition_failed_reconnect_reoffers_keystone(monkeypatch):
    # KEYSTONE: a failed re-connect re-surfaces the governed offer (never a bare
    # "ok, retried" that the operator could mistake for success).
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    a._dispatcher_singleton = types.SimpleNamespace(registry=ToolRegistry())

    def _fake_discover(registry=None):
        mt._bump_connect_failed("notion", "reauth")  # re-connect fails again
        return []

    monkeypatch.setattr(mt, "discover_mcp_tools", _fake_discover)
    msg = a._apply_connector_disposition("retry", "notion")
    assert "Still couldn't reach" in msg
    assert "Retry notion" in msg                 # the re-rendered governed offer
    assert "notion" in mt.get_connect_failures()  # breaker re-tripped
    # Re-suppressed so the post-turn surface does not ALSO double-append it.
    assert a._connector_failure_id("notion", "reauth") in a._surfaced_connector_ids


def test_dismiss_disposition_acknowledges_and_keeps_breaker():
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    msg = a._apply_connector_disposition("dismiss", "notion")
    assert "proceeding without" in msg.lower()
    assert "notion" in mt.get_connect_failures()  # NOT cleared — connector stays down


def test_retry_without_registry_reports_honestly_not_false_success():
    # A bare agent (no dispatcher / registry) cannot DRIVE a re-discovery, so it
    # must NOT claim a verified reconnect. It clears the breaker (next discovery
    # re-attempts) and says so honestly — no crash, no false "Reconnected".
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    msg = a._apply_connector_disposition("retry", "notion")
    assert "Reconnected" not in msg
    assert "re-attempt" in msg.lower()
    assert "notion" not in mt.get_connect_failures()  # breaker cleared


def test_retry_rediscovery_throw_is_treated_as_failed_reconnect(monkeypatch):
    # A re-discovery that RAISES is a failed re-connect, not a success: the
    # breaker is re-recorded and the governed offer re-surfaces.
    mt._bump_connect_failed("notion", "reauth")
    a = _agent()
    a._dispatcher_singleton = types.SimpleNamespace(registry=ToolRegistry())

    def _boom(registry=None):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(mt, "discover_mcp_tools", _boom)
    msg = a._apply_connector_disposition("retry", "notion")
    assert "Reconnected" not in msg
    assert "Still couldn't reach" in msg
    assert "notion" in mt.get_connect_failures()  # re-recorded, not lost


# ── Boundaries: re-auth (DoD 9) + ephemeral storage (DoD 10) ─────────


def test_9_reauth_boundary_touches_no_credential_material(monkeypatch):
    # retry must NEVER touch token/credential material — only the breaker clear.
    import tools.mcp_oauth as oauth
    for attr in dir(oauth):
        fn = getattr(oauth, attr)
        if callable(fn) and not attr.startswith("__"):
            monkeypatch.setattr(
                oauth, attr,
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError(f"re-auth boundary violated: mcp_oauth.{attr} called")
                ),
                raising=False,
            )
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    agent._append_connector_failure_offer("a")     # render: displays command only
    agent._connector_offer_retry("notion")          # disposition: clears breaker only
    # no mcp_oauth call fired → boundary held


def test_10_connector_failure_never_written_to_proposal_queue(monkeypatch):
    # Ruling 1: the ephemeral offer must NEVER reach proposal_queue/proposals.jsonl.
    import grove.eval.proposal_queue as pq
    for attr in ("append", "queue_append", "stage_proposal", "write", "remove"):
        if hasattr(pq, attr):
            monkeypatch.setattr(
                pq, attr,
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError(f"Ruling 1 violated: proposal_queue.{attr} called")
                ),
                raising=False,
            )
    mt._bump_connect_failed("notion", "reauth")
    agent = _agent()
    agent._append_connector_failure_offer("a")
    agent._connector_offer_dismiss(agent._connector_failure_id("notion", "reauth"))
    # no proposal_queue write fired → ephemeral storage held
