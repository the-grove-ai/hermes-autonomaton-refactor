"""Fast-path fixtures shared across tests/run_agent/.

Many tests in this directory exercise the retry/backoff paths in the
agent loop. Production code uses ``jittered_backoff(base_delay=5.0)``
with a ``while time.time() < sleep_end`` loop — a single retry test
spends 5+ seconds of real wall-clock time on backoff waits.

Mocking ``jittered_backoff`` to return 0.0 collapses the while-loop
to a no-op (``time.time() < time.time() + 0`` is false immediately),
which handles the most common case without touching ``time.sleep``.

We deliberately DO NOT mock ``time.sleep`` here — some tests
(test_interrupt_propagation, test_primary_runtime_restore, etc.) use
the real ``time.sleep`` for threading coordination or assert that it
was called with specific values. Tests that want to additionally
fast-path direct ``time.sleep(N)`` calls in production code should
monkeypatch ``run_agent.time.sleep`` locally (see
``test_anthropic_error_handling.py`` for the pattern).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Short-circuit retry backoff for all tests in this directory."""
    try:
        import run_agent
    except ImportError:
        return

    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


@pytest.fixture(autouse=True)
def _skip_self_routing_for_run_agent_tests(monkeypatch):
    """Opt every test in this directory out of run_conversation's
    self-route on entry (W3.0 webui-governance-pipeline-v1).

    Production semantics: AIAgent.run_conversation calls
    ``_maybe_route_for_turn(user_message)`` on entry unless the caller
    sets ``already_routed=True`` — both CLI (after _resolve_turn_agent_
    config) and webui (architecture-guarantee, the W3.0 bypass closer)
    converge on grove.providers.route_for_agent.

    Tests in this directory test run_conversation's internal behavior
    (retry loops, continuation logic, codex transport quirks, token
    accounting) with hand-constructed AIAgent skeletons whose mocked
    clients would be invalidated by a routing-driven tier swap. These
    tests are not governance tests; they are run_conversation internal
    tests. Auto-opting them out at the conftest level is the test-
    scaffolding equivalent of every CLI run_conversation call site
    setting ``already_routed=True`` — it does not bypass production
    architecture, it scopes the test to its actual subject.

    Tests that DO want to exercise the governance pipeline are at
    ``tests/test_w3_0_governance_pipeline.py`` (outside this directory,
    so this autouse does not apply); they invoke
    ``_maybe_route_for_turn`` directly on bare agent skeletons.
    """
    try:
        from run_agent import AIAgent
    except ImportError:
        return
    monkeypatch.setattr(AIAgent, "_maybe_route_for_turn", lambda self, msg: None)
