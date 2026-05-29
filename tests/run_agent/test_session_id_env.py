"""Test that GROVE_SESSION_ID is exposed as an env var and ContextVar."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from run_agent import AIAgent
from tests._runtime_ctx import MOCK_RUNTIME_CTX


@pytest.fixture(autouse=True)
def _cleanup_env():
    """Remove GROVE_SESSION_ID before/after each test."""
    os.environ.pop("GROVE_SESSION_ID", None)
    yield
    os.environ.pop("GROVE_SESSION_ID", None)


def test_session_id_env_broadcast_via_dispatcher():
    """Sprint 26 Phase 7 — GROVE_SESSION_ID env-write authority moved
    from AIAgent.__init__ to Dispatcher.broadcast_session_id per
    GRV-005 § II/III. The Agent declares; the Dispatcher writes.

    This test verifies the new substrate-write surface: calling
    Dispatcher.broadcast_session_id sets the env var. The
    AIAgent.__init__ env-write site is intentionally deleted in
    Phase 7; the Dispatcher writes at every dispatch_turn entry.
    """
    from grove.dispatcher import Dispatcher

    agent = AIAgent(runtime_ctx=MOCK_RUNTIME_CTX, 
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    # AIAgent no longer sets GROVE_SESSION_ID at construction.
    # Substrate writes are Dispatcher-owned per Sprint 26 Phase 7.
    Dispatcher.broadcast_session_id(agent.session_id)
    assert os.environ.get("GROVE_SESSION_ID") == agent.session_id
    assert len(agent.session_id) > 0


def test_session_id_env_broadcast_uses_provided_id():
    """Dispatcher.broadcast_session_id writes whatever session_id the
    caller passes — same behavior as the deleted AIAgent.__init__ path
    when an operator passed session_id explicitly to AIAgent(...).

    Sprint 26 Phase 7: the env-write surface is the Dispatcher, not
    the Agent. The Agent declares its session_id; the Dispatcher
    broadcasts when ownership transfers.
    """
    from grove.dispatcher import Dispatcher

    custom_id = "20260511_120000_abc12345"
    agent = AIAgent(runtime_ctx=MOCK_RUNTIME_CTX, 
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        session_id=custom_id,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    Dispatcher.broadcast_session_id(agent.session_id)
    assert os.environ["GROVE_SESSION_ID"] == custom_id
    assert agent.session_id == custom_id


def test_session_id_contextvar_set():
    """AIAgent.__init__ also sets the ContextVar for concurrency safety."""
    custom_id = "20260511_130000_def67890"
    AIAgent(runtime_ctx=MOCK_RUNTIME_CTX, 
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        session_id=custom_id,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    from gateway.session_context import get_session_env
    assert get_session_env("GROVE_SESSION_ID") == custom_id
