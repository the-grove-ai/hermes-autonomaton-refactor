"""A model call that goes past its declared time budget ends the attempt.

The budget is set on the agent by the Dispatcher for a goal that declares one
(``call_budget_seconds``). The agent's part is narrow: stop waiting, close the
connection, raise ``CallOverBudget`` — and never retry at the same tier.
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tests._runtime_ctx import MOCK_CAPABILITY_PROVIDER, MOCK_RUNTIME_CTX


def _agent():
    from run_agent import AIAgent

    agent = AIAgent(
        runtime_ctx=MOCK_RUNTIME_CTX, api_key="test-key", base_url="https://example.com/v1",
        model="test/model", api_mode="chat_completions", quiet_mode=True,
        skip_context_files=True, skip_memory=True,
        get_available_tools=MOCK_CAPABILITY_PROVIDER)
    agent.api_mode = "chat_completions"
    return agent


def _hung_client(calls, release):
    """A client whose call never answers until released (a stalled provider)."""
    def create(*args, **kwargs):
        calls.append(1)
        release.wait(timeout=20)
        raise ConnectionError("closed")
    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize("call", ["_interruptible_streaming_api_call",
                                  "_interruptible_api_call"])
def test_a_stalled_call_is_stopped_at_the_budget_and_not_retried(call):
    from run_agent import CallOverBudget

    agent = _agent()
    agent._call_budget_seconds = 0.6
    calls, release = [], threading.Event()
    with patch("run_agent.AIAgent._create_request_openai_client",
               return_value=_hung_client(calls, release)), \
         patch("run_agent.AIAgent._close_request_openai_client",
               side_effect=lambda *a, **k: release.set()):
        started = time.time()
        with pytest.raises(CallOverBudget) as over:
            getattr(agent, call)({"model": "test/model", "messages": []})
        waited = time.time() - started
    assert 0.6 <= waited < 5                       # at the budget, not at the stale timeout
    assert over.value.budget == 0.6 and over.value.model == "test/model"
    assert len(calls) == 1                         # one attempt: never retried at this tier


def test_no_declared_budget_changes_nothing():
    agent = _agent()
    assert getattr(agent, "_call_budget_seconds", None) is None
    assert agent._over_call_budget(10_000.0, {"model": "m"}) is None
    agent._call_budget_seconds = 30
    assert agent._over_call_budget(29.9, {"model": "m"}) is None
    assert agent._over_call_budget(30.1, {"model": "m"}).waited == 30.1
    # Declared tier by tier: the turn's routed tier decides.
    agent._call_budget_seconds = {"T1": 5.0, "T3": None, "default": 40.0}
    agent._tier_name = "T1"
    assert agent._over_call_budget(6, {"model": "m"}).budget == 5.0
    agent._tier_name = "T2"
    assert agent._over_call_budget(6, {"model": "m"}) is None
    assert agent._over_call_budget(41, {"model": "m"}).budget == 40.0
    agent._tier_name = "T3"
    assert agent._over_call_budget(10_000, {"model": "m"}) is None


def test_the_turn_ends_without_a_retry_and_the_reply_is_reviewed():
    """Pinned on the source: the over-budget branch sits between the interrupt
    branch and the retrying one, ends the turn, and leaves ``interrupted``
    alone so the Dispatcher's review of the reply still runs."""
    import inspect

    import run_agent

    src = inspect.getsource(run_agent.AIAgent)
    i = src.index("except CallOverBudget as _over:")
    assert src.index("except InterruptedError:") < i < src.index(
        "except Exception as api_error:", i - 3000)
    branch = src[i:src.index("except Exception as api_error:", i)]
    assert "self._call_over_budget = {" in branch and "break" in branch
    assert "interrupted = True" not in branch and "retry_count" not in branch
    assert '_turn_exit_reason = "call_over_budget"' in src
