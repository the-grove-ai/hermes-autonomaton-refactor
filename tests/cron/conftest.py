"""Scaffolding for tests in tests/cron/.

Opts every test in this directory out of dispatch_turn's self-route
on entry (W3.0 webui-governance-pipeline-v1).

Production semantics: ``grove.dispatcher.Dispatcher.dispatch_turn``
calls ``_classify_and_bind_turn(agent, user_message, ledger)`` on
entry unless the caller sets ``already_routed=True`` — both CLI (after
_resolve_turn_agent_config) and webui (architecture-guarantee, the
W3.0 bypass closer) converge on grove.providers.route_for_agent.

Sprint 35 moved the routing decision from
``AIAgent._maybe_route_for_turn`` (deleted) to
``Dispatcher._classify_and_bind_turn`` (docstring at
grove/dispatcher.py:1980 names the replacement explicitly).
The fixture target was updated to follow.

Tests in this directory test cron job execution paths (codex transport
handling, 401 refresh logic) with hand-constructed AIAgent skeletons
whose mocked clients would be invalidated by a routing-driven tier
swap. These tests are not governance tests; they are cron-path tests.
Auto-opting them out at the conftest level is the test-scaffolding
equivalent of every CLI run_conversation call site setting
``already_routed=True`` — it does not bypass production architecture,
it scopes the test to its actual subject.

Mirrors the pattern in tests/run_agent/conftest.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _skip_self_routing_for_cron_tests(monkeypatch):
    """Opt every test in this directory out of W3.0 self-routing.

    Sprint 35 moved the routing entry point from
    ``AIAgent._maybe_route_for_turn`` (deleted) to
    ``Dispatcher._classify_and_bind_turn``. This fixture targets the
    new method so the isolation guarantee the fixture has always
    provided is preserved.
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
