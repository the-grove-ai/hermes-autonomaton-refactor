"""PL-1 — oneshot (-z) mode must fail loud on a missing model.

Sprint 19 (v0.1-release-prep-v1). Before this fix, a oneshot run with no
model configured (no --model, no GROVE_INFERENCE_MODEL, no config model,
no routing.config.yaml) ran the agent with model="", failed deep inside
the provider call, had the error swallowed by the devnull redirect, and
exited 0 with no output. The fix raises ModelConfigError before the agent
runs and surfaces it past the redirect.
"""

from __future__ import annotations

import logging
import os

import pytest


@pytest.fixture(autouse=True)
def _oneshot_cleanup():
    """run_oneshot disables logging and sets GROVE_YOLO_MODE /
    GROVE_ACCEPT_HOOKS as process-global side effects; restore them so the
    tests in this file do not leak state into the rest of the suite."""
    yield
    logging.disable(logging.NOTSET)
    for var in ("GROVE_YOLO_MODE", "GROVE_ACCEPT_HOOKS"):
        os.environ.pop(var, None)


def test_run_agent_empty_model_raises_model_config_error(monkeypatch):
    """No model from --model, the env var, config, or the Cognitive Router
    → ModelConfigError, raised before the agent runs."""
    from hermes_cli import oneshot

    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_PROVIDER", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})
    monkeypatch.setattr("grove.providers.route_for_agent", lambda **kwargs: None)

    with pytest.raises(oneshot.ModelConfigError):
        oneshot._run_agent("hello")


def test_empty_model_error_message_names_the_fix(monkeypatch):
    """The error message must name the operator's remedy — not be silent."""
    from hermes_cli import oneshot

    monkeypatch.delenv("GROVE_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("GROVE_INFERENCE_PROVIDER", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})
    monkeypatch.setattr("grove.providers.route_for_agent", lambda **kwargs: None)

    with pytest.raises(oneshot.ModelConfigError) as excinfo:
        oneshot._run_agent("hello")

    message = str(excinfo.value)
    assert "No model configured" in message
    assert "autonomaton model" in message
    assert "--model" in message


def test_run_oneshot_model_config_error_returns_2(monkeypatch, capsys):
    """run_oneshot catches ModelConfigError, writes a plain message to the
    real stderr, and exits non-zero — it never returns 0 with no output."""
    from hermes_cli import oneshot

    def _raise(*args, **kwargs):
        raise oneshot.ModelConfigError(
            "No model configured. Run `autonomaton model` to set one, "
            "or pass --model."
        )

    monkeypatch.setattr(oneshot, "_run_agent", _raise)

    exit_code = oneshot.run_oneshot("hello")

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "autonomaton -z:" in err
    assert "No model configured" in err


def test_tier_cost_summary_none_routing_returns_none():
    """No routing decision (vanilla install) → no summary; stdout stays pure."""
    from hermes_cli.oneshot import _tier_cost_summary

    assert _tier_cost_summary(None, object()) is None


def test_tier_cost_summary_local_model_is_zero():
    """A local-provider tier reads 'local ($0)' in the stderr summary."""
    from types import SimpleNamespace

    from hermes_cli.oneshot import _tier_cost_summary

    routed = SimpleNamespace(
        tier="T2",
        tier_config=SimpleNamespace(model="gemma4", provider="ollama"),
    )
    agent = SimpleNamespace(session_input_tokens=1000, session_output_tokens=500)
    summary = _tier_cost_summary(routed, agent)

    assert "T2 Gemma 4" in summary
    assert "1,500 tokens" in summary
    assert "local ($0)" in summary
