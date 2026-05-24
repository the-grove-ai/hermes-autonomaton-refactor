"""Scaffolding for tests in tests/cron/.

Opts every test in this directory out of run_conversation's self-route
on entry (W3.0 webui-governance-pipeline-v1).

Production semantics: AIAgent.run_conversation calls
``_maybe_route_for_turn(user_message)`` on entry unless the caller sets
``already_routed=True`` — both CLI (after _resolve_turn_agent_config)
and webui (architecture-guarantee, the W3.0 bypass closer) converge on
grove.providers.route_for_agent.

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
    """Opt every test in this directory out of W3.0 self-routing."""
    try:
        from run_agent import AIAgent
    except ImportError:
        return
    monkeypatch.setattr(AIAgent, "_maybe_route_for_turn", lambda self, msg: None)
