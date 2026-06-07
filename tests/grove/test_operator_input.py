"""Sprint 67 (kaizen-governance-parity-v1) — OperatorInputRequired contract.

The store-and-resume primitive's load-bearing property: it is a
control-flow interrupt that MUST NOT be swallowed by the ~20 generic
``except Exception`` blocks between the raise site and the surface's
terminal catch. That is guaranteed by subclassing ``BaseException``.
"""

from __future__ import annotations

from grove.operator_input import (
    OperatorInputRequired,
    PendingOperatorRequest,
    clarify_answer_key,
    governance_grant_key,
    state_key,
    TIMEOUT_SECONDS,
)


def _pending(kind="governance"):
    return PendingOperatorRequest(
        kind=kind,
        prompt_text="I'd like to search your Notion workspace — go ahead?",
        original_user_message="search notion for the sprint plan",
        created_at=1000.0,
        timeout_at=1000.0 + TIMEOUT_SECONDS,
        tool_name="mcp_notion_API_post_search",
        tool_args={"query": "sprint plan"},
    )


class TestExceptionContract:
    def test_subclasses_base_exception_not_exception(self):
        # The whole design rests on this: control-flow interrupt, not error.
        assert issubclass(OperatorInputRequired, BaseException)
        assert not issubclass(OperatorInputRequired, Exception)

    def test_not_caught_by_except_exception(self):
        """A generic ``except Exception`` MUST let it propagate."""
        outcome = None
        try:
            try:
                raise OperatorInputRequired(_pending())
            except Exception:  # noqa: BLE001 — deliberately testing the gap
                outcome = "swallowed"
        except OperatorInputRequired:
            outcome = "propagated"
        assert outcome == "propagated"

    def test_carries_pending_and_prompt(self):
        p = _pending()
        exc = OperatorInputRequired(p)
        assert exc.pending is p
        assert str(exc) == p.prompt_text


class TestPendingSerialization:
    def test_round_trip(self):
        p = _pending()
        restored = PendingOperatorRequest.from_json(p.to_json())
        assert restored == p

    def test_clarify_round_trip(self):
        p = PendingOperatorRequest(
            kind="clarify",
            prompt_text="Which environment?\n1. dev\n2. prod",
            original_user_message="deploy the thing",
            created_at=5.0,
            timeout_at=5.0 + TIMEOUT_SECONDS,
            question="Which environment?",
            choices=["dev", "prod"],
        )
        restored = PendingOperatorRequest.from_json(p.to_json())
        assert restored == p
        assert restored.choices == ["dev", "prod"]


class TestStateKeys:
    def test_keys_are_session_scoped_and_distinct(self):
        keys = {
            state_key("s1"),
            governance_grant_key("s1"),
            clarify_answer_key("s1"),
        }
        assert len(keys) == 3
        # No key for s1 collides with a key for s2 (no cross-session leak).
        assert state_key("s1") != state_key("s2")
        assert "s1" in state_key("s1")
