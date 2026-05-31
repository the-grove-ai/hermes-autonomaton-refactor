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
    """Opt every test in this directory out of dispatch_turn's
    self-route on entry (W3.0 webui-governance-pipeline-v1).

    Production semantics: ``grove.dispatcher.Dispatcher.dispatch_turn``
    calls ``_classify_and_bind_turn(agent, user_message, ledger)`` on
    entry unless the caller sets ``already_routed=True`` — both CLI
    (after _resolve_turn_agent_config) and webui (architecture-
    guarantee, the W3.0 bypass closer) converge on
    grove.providers.route_for_agent.

    Sprint 35 moved the routing decision from
    ``AIAgent._maybe_route_for_turn`` (deleted) to
    ``Dispatcher._classify_and_bind_turn`` (grove/dispatcher.py:1980
    docstring names the replacement explicitly). The fixture target
    was updated to follow so the isolation guarantee the fixture has
    always provided is preserved.

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
    so this autouse does not apply); those tests assert against the
    deleted ``_maybe_route_for_turn`` and are marked
    ``@pytest.mark.skip`` per Sprint 52 GATE-B (legacy pre-Dispatcher
    internal call stack).
    """
    try:
        from grove.dispatcher import Dispatcher
    except ImportError:
        return
    monkeypatch.setattr(
        Dispatcher,
        "_classify_and_bind_turn",
        lambda self, agent, user_message, ledger: None,
    )
