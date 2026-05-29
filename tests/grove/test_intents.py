"""Tests for grove.intents — GRV-005 § IV Intent Protocol v1 data types.

Sprint 26 Phase 2. The intent types are pure data — no behavior wired
yet. These tests verify the contract shape: frozen-dataclass
immutability, required vs optional fields, default-factory semantics,
and the exhaustive v1 enumeration matches GRV-005 § IV.

Subsequent phases (Phase 3 onward) extend with behavior tests around
the generator-shaped agent loop, Andon classification at intent-yield,
and disposition routing.
"""

from __future__ import annotations

import pytest

from grove.intents import (
    ClarificationRequest,
    EscalationRequest,
    FinalResponse,
    Observation,
    ToolIntent,
)


# ── Module surface ────────────────────────────────────────────────────────


def test_module_exports_v1_enumeration():
    """The __all__ surface is the GRV-005 § IV v1 types plus the carriers
    later sprints added.

    Sprint 31 Phase 2 added ``ToolBatchYield`` (per-batch scalar carrier;
    not a § IV v1 intent but exported for symmetry). Sprint 39 added
    ``SessionRotateIntent`` and ``SessionUpdateTokensIntent`` as the
    session-authority extraction's declarative writes — the Agent yields
    these and the Dispatcher executes against ``self.session``, mirroring
    the Sprint 26 ``ToolIntent`` mediation pattern.
    """
    import grove.intents as intents
    assert set(intents.__all__) == {
        "ToolIntent",
        "ToolBatchYield",
        "EscalationRequest",
        "FinalResponse",
        "ClarificationRequest",
        "Observation",
        "SessionRotateIntent",
        "SessionUpdateTokensIntent",
        "MemoryWriteIntent",
        "MemoryWriteResult",
        "MemoryLifecycleIntent",
    }


def test_remaining_horizons_not_exported():
    """GRV-005 § X horizons that are still deferred MUST NOT be exported.

    Sprint 40 realized ``MemoryWriteIntent`` (paired with the
    ``MemoryWriteResult`` return container and the fire-and-forget
    ``MemoryLifecycleIntent``) — the operator-memory authority that used
    to live on ``AIAgent._memory_store`` / ``_memory_manager`` now flows
    as declarative intents the Dispatcher catches. The remaining § X
    horizons (``SubAgentIntent``, ``OperatorDispositionObservation``)
    stay deferred.
    """
    import grove.intents as intents
    for name in (
        "SubAgentIntent",
        "OperatorDispositionObservation",
    ):
        assert not hasattr(intents, name), (
            f"{name} is a GRV-005 § X horizon and MUST NOT be exported yet"
        )


# ── ToolIntent ────────────────────────────────────────────────────────────


class TestToolIntent:
    def test_minimal_construction(self):
        intent = ToolIntent(tool_name="memory")
        assert intent.tool_name == "memory"
        assert intent.arguments == {}
        assert intent.call_id is None

    def test_full_construction(self):
        intent = ToolIntent(
            tool_name="terminal",
            arguments={"command": "ls -la", "cwd": "/tmp"},
            call_id="call_abc123",
        )
        assert intent.arguments == {"command": "ls -la", "cwd": "/tmp"}
        assert intent.call_id == "call_abc123"

    def test_is_frozen(self):
        intent = ToolIntent(tool_name="x")
        with pytest.raises(Exception):  # FrozenInstanceError
            intent.tool_name = "y"  # type: ignore[misc]

    def test_arguments_default_factory_is_per_instance(self):
        """Each instance gets its own empty dict, not a shared singleton."""
        a = ToolIntent(tool_name="x")
        b = ToolIntent(tool_name="y")
        assert a.arguments is not b.arguments


# ── EscalationRequest ────────────────────────────────────────────────────


class TestEscalationRequest:
    def test_requires_reason(self):
        with pytest.raises(TypeError):
            EscalationRequest()  # type: ignore[call-arg]

    def test_minimal_construction(self):
        req = EscalationRequest(reason="context window exceeded")
        assert req.reason == "context window exceeded"
        assert req.request == {}

    def test_full_construction(self):
        req = EscalationRequest(
            reason="task complexity warrants apex tier",
            request={"tier": "T3", "max_tokens": 16384},
        )
        assert req.request["tier"] == "T3"

    def test_is_frozen(self):
        req = EscalationRequest(reason="x")
        with pytest.raises(Exception):
            req.reason = "y"  # type: ignore[misc]


# ── FinalResponse ────────────────────────────────────────────────────────


class TestFinalResponse:
    def test_requires_content(self):
        with pytest.raises(TypeError):
            FinalResponse()  # type: ignore[call-arg]

    def test_minimal_construction(self):
        resp = FinalResponse(content="Hello, world.")
        assert resp.content == "Hello, world."
        assert resp.metadata == {}

    def test_metadata_round_trip(self):
        resp = FinalResponse(
            content="Done.",
            metadata={"tier": "T2", "tokens_out": 142, "latency_ms": 850},
        )
        assert resp.metadata["tier"] == "T2"

    def test_is_frozen(self):
        resp = FinalResponse(content="x")
        with pytest.raises(Exception):
            resp.content = "y"  # type: ignore[misc]


# ── ClarificationRequest ─────────────────────────────────────────────────


class TestClarificationRequest:
    def test_requires_question(self):
        with pytest.raises(TypeError):
            ClarificationRequest()  # type: ignore[call-arg]

    def test_open_ended(self):
        req = ClarificationRequest(question="What's the target environment?")
        assert req.question == "What's the target environment?"
        assert req.choices is None

    def test_multiple_choice(self):
        req = ClarificationRequest(
            question="Pick a deployment target.",
            choices=["staging", "production", "preview"],
        )
        assert req.choices == ["staging", "production", "preview"]

    def test_is_frozen(self):
        req = ClarificationRequest(question="x")
        with pytest.raises(Exception):
            req.question = "y"  # type: ignore[misc]


# ── Observation ───────────────────────────────────────────────────────────


class TestObservation:
    def test_minimal_construction(self):
        # intent_id and success are required; value defaults None, metadata {}
        obs = Observation(intent_id="call_abc123", success=True)
        assert obs.intent_id == "call_abc123"
        assert obs.success is True
        assert obs.value is None
        assert obs.metadata == {}

    def test_intent_id_can_be_none(self):
        # Some observations (e.g. ClarificationRequest replies) don't tie
        # to a call_id — intent_id=None is valid.
        obs = Observation(intent_id=None, success=True, value="operator-reply")
        assert obs.intent_id is None
        assert obs.value == "operator-reply"

    def test_failure_observation(self):
        obs = Observation(
            intent_id="call_xyz",
            success=False,
            value="Tool execution timed out",
            metadata={"error_type": "TimeoutError", "latency_ms": 30000},
        )
        assert obs.success is False
        assert obs.metadata["error_type"] == "TimeoutError"

    def test_is_frozen(self):
        obs = Observation(intent_id="x", success=True)
        with pytest.raises(Exception):
            obs.success = False  # type: ignore[misc]


# ── Cross-type contract ──────────────────────────────────────────────────


class TestIntentProtocolContract:
    def test_observation_can_carry_tool_intent_result(self):
        # End-to-end shape check: a ToolIntent's call_id is mirrored back
        # in the Observation's intent_id for matching.
        intent = ToolIntent(
            tool_name="terminal",
            arguments={"command": "ls"},
            call_id="call_42",
        )
        obs = Observation(
            intent_id=intent.call_id,
            success=True,
            value="file1\nfile2\n",
            metadata={"latency_ms": 12, "exit_code": 0},
        )
        assert obs.intent_id == intent.call_id

    def test_observation_can_carry_escalation_decision(self):
        req = EscalationRequest(
            reason="apex tier needed",
            request={"tier": "T3"},
        )
        # EscalationRequest has no call_id; the Dispatcher matches at
        # the dispatcher level (Phase 3 wires this).
        obs = Observation(
            intent_id=None,
            success=True,
            value={"granted_tier": "T3"},
            metadata={"policy": "auto-grant", "reason": req.reason},
        )
        assert obs.value["granted_tier"] == "T3"

    def test_observation_can_carry_clarification_reply(self):
        req = ClarificationRequest(
            question="Pick one.",
            choices=["a", "b"],
        )
        obs = Observation(
            intent_id=None,
            success=True,
            value="a",  # operator's reply
        )
        assert obs.value == "a"
