"""Sprint 67 (kaizen-governance-parity-v1) — Gap 2: web text-based governance.

The /v1/chat/completions surface replaces silent auto-allow with
store-and-resume governance: a gated action raises OperatorInputRequired
(butler prompt surfaced as the response); the operator's next message is
parsed into a disposition; approval primes a grant and the original
action is replayed.

These tests exercise the surface logic in isolation (a bare adapter +
an in-memory SessionDB stand-in) — no HTTP, no live LLM.
"""

from __future__ import annotations

import json
import time

import pytest

from grove.dispatcher import AndonHalt
from grove.intents import ToolIntent
from grove.operator_input import (
    OperatorInputRequired,
    PendingOperatorRequest,
    clarify_answer_key,
    governance_grant_key,
    state_key,
    TIMEOUT_SECONDS,
)
from grove.zones import ZoneResult
from gateway.platforms.api_server import (
    APIServerAdapter,
    _butler_governance_prompt,
    _classify_governance_reply,
)


class _FakeDB:
    """In-memory stand-in for SessionDB.state_meta (string KV store)."""

    def __init__(self):
        self.store = {}

    def get_meta(self, key):
        return self.store.get(key)

    def set_meta(self, key, value):
        # Mirrors the real upsert: an empty string is the cleared sentinel.
        self.store[key] = value


def _adapter(fake):
    a = object.__new__(APIServerAdapter)
    a._ensure_session_db = lambda: fake  # type: ignore[attr-assign]
    return a


def _halt(tool="mcp_notion_API_post_search", args=None):
    return AndonHalt(
        intents=[ToolIntent(tool_name=tool, arguments=args or {"query": "x"}, call_id="c1")],
        zone_results=[ZoneResult(zone="yellow", matched_rule="r", source="default")],
        triggering_index=0,
    )


# ── reply classifier ─────────────────────────────────────────────────


class TestClassifyGovernanceReply:
    @pytest.mark.parametrize("text,expected", [
        ("always allow this", "approve_always"),
        ("always", "approve_always"),
        ("remember it", "approve_always"),
        ("go ahead", "approve_once"),
        ("yes", "approve_once"),
        ("ok do it", "approve_once"),
        ("not this time", "deny"),
        ("no", "deny"),
        ("cancel that", "deny"),
        ("just do the weather instead", "dismiss"),
        ("ignore that, what's the weather", "dismiss"),
        ("what's the weather in Tokyo?", "unrelated"),
        ("", "unrelated"),
    ])
    def test_verdicts(self, text, expected):
        assert _classify_governance_reply(text) == expected

    def test_always_beats_generic_approve(self):
        # "yes, always" must read as always, not once.
        assert _classify_governance_reply("yes, always allow") == "approve_always"


class TestButlerPrompt:
    def test_notion_read_prompt_is_butler_register(self):
        p = _butler_governance_prompt("mcp_notion_API_post_search", {"query": "x"})
        assert "Notion" in p
        assert "go ahead" in p and "always" in p and "not this" in p

    def test_terminal_prompt_includes_command(self):
        p = _butler_governance_prompt("terminal", {"command": "rm -rf /tmp/x"})
        assert "rm -rf /tmp/x" in p


# ── governance handler (raise + replay) ──────────────────────────────


class TestWebGovernanceHandler:
    def test_first_encounter_raises_with_pending(self):
        adapter = _adapter(_FakeDB())
        handler = adapter._make_web_governance_handler("s1", "search notion for X")
        with pytest.raises(OperatorInputRequired) as ei:
            handler(_halt())
        pending = ei.value.pending
        assert pending.kind == "governance"
        assert pending.tool_name == "mcp_notion_API_post_search"
        assert pending.original_user_message == "search notion for X"
        assert pending.timeout_at - pending.created_at == TIMEOUT_SECONDS
        assert "Notion" in pending.prompt_text

    def test_replay_with_grant_returns_disposition_and_consumes(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        fake.set_meta(governance_grant_key("s1"), json.dumps({
            "disposition": "once",
            "tool_name": "mcp_notion_API_post_search",
        }))
        handler = adapter._make_web_governance_handler("s1", "search")
        assert handler(_halt(args={"query": "x"})) == "once"
        # Grant consumed (one-shot).
        assert not fake.get_meta(governance_grant_key("s1"))

    def test_grant_matches_same_tool_even_with_different_args(self):
        """The Sprint 67 smoke-test fix: the replay regenerates tool args
        non-deterministically, so a grant must match on tool_name ALONE —
        not an exact args hash — or the turn re-prompts forever."""
        fake = _FakeDB()
        adapter = _adapter(fake)
        fake.set_meta(governance_grant_key("s1"), json.dumps({
            "disposition": "always",
            "tool_name": "mcp_notion_API_post_search",
        }))
        handler = adapter._make_web_governance_handler("s1", "search")
        # Same tool, DIFFERENT args (the replay) — must still apply.
        assert handler(_halt(args={"query": "COMPLETELY DIFFERENT"})) == "always"

    def test_grant_for_a_different_tool_does_not_apply(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        fake.set_meta(governance_grant_key("s1"), json.dumps({
            "disposition": "always",
            "tool_name": "mcp_notion_API_post_search",
        }))
        handler = adapter._make_web_governance_handler("s1", "search")
        # A DIFFERENT protected tool must still halt for its own approval.
        with pytest.raises(OperatorInputRequired):
            handler(_halt(tool="mcp_notion_API_post_page", args={"title": "x"}))


# ── resolution of the operator's next message ────────────────────────


def _gov_pending(**kw):
    base = dict(
        kind="governance",
        prompt_text="I'd like to search your Notion workspace — go ahead?",
        original_user_message="search notion for the sprint plan",
        created_at=time.time(),
        timeout_at=time.time() + TIMEOUT_SECONDS,
        tool_name="mcp_notion_API_post_search",
        tool_args={"query": "sprint plan"},
    )
    base.update(kw)
    return PendingOperatorRequest(**base)


class TestResolvePendingGovernance:
    def test_approve_once_primes_grant_and_replays(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        pending = _gov_pending()
        out = adapter._resolve_pending_operator_input(
            pending.to_json(), "go ahead", fake, "s1",
        )
        assert out["effective_user_message"] == pending.original_user_message
        grant = json.loads(fake.get_meta(governance_grant_key("s1")))
        assert grant["disposition"] == "once"
        assert grant["tool_name"] == "mcp_notion_API_post_search"
        assert not fake.get_meta(state_key("s1"))  # pending cleared

    def test_approve_always_primes_always_grant(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        out = adapter._resolve_pending_operator_input(
            _gov_pending().to_json(), "always allow this", fake, "s1",
        )
        assert "effective_user_message" in out
        assert json.loads(fake.get_meta(governance_grant_key("s1")))["disposition"] == "always"

    def test_deny_returns_direct_reply_no_replay(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        out = adapter._resolve_pending_operator_input(
            _gov_pending().to_json(), "not this time", fake, "s1",
        )
        assert "direct_reply" in out
        assert not fake.get_meta(state_key("s1"))
        assert not fake.get_meta(governance_grant_key("s1"))

    def test_dismiss_processes_new_request(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        out = adapter._resolve_pending_operator_input(
            _gov_pending().to_json(), "just do the weather instead", fake, "s1",
        )
        # The NEW message is processed (not the original action).
        assert out["effective_user_message"] == "just do the weather instead"
        assert not fake.get_meta(state_key("s1"))

    def test_unrelated_holds_and_resurfaces(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        pending = _gov_pending()
        fake.set_meta(state_key("s1"), pending.to_json())
        out = adapter._resolve_pending_operator_input(
            fake.get_meta(state_key("s1")), "what's the weather in Tokyo?", fake, "s1",
        )
        assert "direct_reply" in out
        assert pending.prompt_text in out["direct_reply"]
        # The decision is HELD — pending not cleared.
        assert fake.get_meta(state_key("s1"))

    def test_timeout_auto_cancels_and_processes_fresh(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        stale = _gov_pending(
            created_at=time.time() - (TIMEOUT_SECONDS + 60),
            timeout_at=time.time() - 60,
        )
        out = adapter._resolve_pending_operator_input(
            stale.to_json(), "go ahead", fake, "s1",
        )
        # Auto-CANCEL: the stale "go ahead" is NOT applied as approval;
        # it is processed as a fresh message.
        assert out["effective_user_message"] == "go ahead"
        assert not fake.get_meta(governance_grant_key("s1"))  # no grant primed
        assert not fake.get_meta(state_key("s1"))

    def test_corrupt_pending_clears_and_processes_fresh(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        out = adapter._resolve_pending_operator_input(
            "{not valid json", "hello", fake, "s1",
        )
        assert out["effective_user_message"] == "hello"


class TestC0NonInteractiveSeal:
    """C0 (conformance-disarm-seal-v1) — the web store-and-resume surface
    is the one non-interactive path that CAN allow (Yellow, via operator
    text approval). Two C0 invariants apply to it:

      * Red is sovereign — never grantable over a non-interactive surface
        (step 5: Red on a non-keyboard/non-interactive surface → deny).
      * The pending state is PERSISTED and time-bounded, never an
        indefinitely-open connection (store-and-resume).
    """

    def _red_halt(self, tool="terminal", args=None):
        return AndonHalt(
            intents=[ToolIntent(
                tool_name=tool,
                arguments=args or {"command": "sudo rm -rf /"},
                call_id="c1",
            )],
            zone_results=[ZoneResult(
                zone="red", matched_rule="sovereign", source="rules",
            )],
            triggering_index=0,
        )

    def test_red_zone_denied_outright_no_pending_no_grant(self):
        # Step 5 — a Red action denies immediately: no OperatorInputRequired
        # (no butler prompt, no pending state), no grant ever primed. The
        # operator must act on a sovereign action directly.
        fake = _FakeDB()
        adapter = _adapter(fake)
        handler = adapter._make_web_governance_handler("s1", "rm the tree")
        assert handler(self._red_halt()) == "deny"
        assert not fake.get_meta(governance_grant_key("s1"))
        assert not fake.get_meta(state_key("s1"))

    def test_red_denied_even_with_a_primed_grant(self):
        # Belt-and-suspenders: even if a grant somehow exists for the tool,
        # a Red verdict is denied before the grant is consulted.
        fake = _FakeDB()
        adapter = _adapter(fake)
        fake.set_meta(governance_grant_key("s1"), json.dumps({
            "disposition": "always", "tool_name": "terminal",
        }))
        handler = adapter._make_web_governance_handler("s1", "rm the tree")
        assert handler(self._red_halt(tool="terminal")) == "deny"
        # Grant NOT consumed — Red never reaches the grant path.
        assert fake.get_meta(governance_grant_key("s1"))

    def test_pending_state_is_persistable_and_time_bounded(self):
        # Non-blocking acceptance — the first Yellow encounter yields a
        # PERSISTED, JSON-serializable, timeout-bounded pending request
        # (which _run_agent stores to state_meta and returns as a 200
        # awaiting_operator response, releasing the HTTP connection), never
        # an indefinitely-open connection.
        adapter = _adapter(_FakeDB())
        handler = adapter._make_web_governance_handler("s1", "search notion")
        with pytest.raises(OperatorInputRequired) as ei:
            handler(_halt())
        pending = ei.value.pending
        # Serializable → stored, not held open.
        round_trip = PendingOperatorRequest.from_json(pending.to_json())
        assert round_trip.tool_name == pending.tool_name
        # Time-bounded → never indefinite.
        assert pending.timeout_at - pending.created_at == TIMEOUT_SECONDS


class TestResolvePendingClarify:
    def test_clarify_seeds_answer_and_replays(self):
        fake = _FakeDB()
        adapter = _adapter(fake)
        pending = PendingOperatorRequest(
            kind="clarify",
            prompt_text="Which environment?",
            original_user_message="deploy the service",
            created_at=time.time(),
            timeout_at=time.time() + TIMEOUT_SECONDS,
            question="Which environment?",
            choices=["dev", "prod"],
        )
        out = adapter._resolve_pending_operator_input(
            pending.to_json(), "prod", fake, "s1",
        )
        assert out["effective_user_message"] == "deploy the service"
        assert fake.get_meta(clarify_answer_key("s1")) == "prod"
        assert not fake.get_meta(state_key("s1"))
