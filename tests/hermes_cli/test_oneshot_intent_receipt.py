"""instance-cold-start-parity-v1 F1 (oneshot-feed-receipt) — a oneshot (-z) turn
now wires the intent store and leaves exactly ONE feed-first receipt, closing the
gap where the lightweight `-z` path ran real inference but recorded nothing.

Mocked provider: the turn's ``_run_turn_generator`` is replaced with a synthetic
FinalResponse (no network), so this asserts the WIRING + write contract, not LLM
behaviour. Mirrors the synthetic-generator discipline of
tests/grove/test_dispatcher_intent_records.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_agent
from grove import intent_store as _intent_store_mod
from grove.classify import ClassificationResult
from grove.intents import FinalResponse


def _final_only_generator(final_text: str):
    """A class-level replacement for ``AIAgent._run_turn_generator`` — bound, so
    it takes ``self``. Yields a single FinalResponse then returns the result dict
    dispatch_turn hands back."""

    def _gen(self, **kwargs):  # noqa: ANN001 — bound method stand-in
        def g():
            yield FinalResponse(content=final_text)
            return {"final_response": final_text}

        return g()

    return _gen


def test_oneshot_turn_writes_exactly_one_intent_record(tmp_path, monkeypatch):
    from hermes_cli import oneshot

    scratch = tmp_path / "grove_scratch"
    scratch.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(scratch))
    # Fresh intent-store singleton so get_store() rebinds under the scratch home.
    monkeypatch.setattr(_intent_store_mod, "_default_store", None)

    # route_for_agent → a synthetic routed tier (a real model slug, no live
    # classifier call). oneshot reads .tier_config.model/.provider off it.
    routed = SimpleNamespace(
        tier="T2",
        tier_config=SimpleNamespace(
            tier="T2", model="z-ai/glm-5.2", provider="openrouter"
        ),
    )
    monkeypatch.setattr("grove.providers.route_for_agent", lambda **kw: routed)

    # Canned runtime with a fake key so AIAgent constructs (the synthetic
    # generator means no real provider round-trip ever fires).
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "api_key": "sk-test-fake-key",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    )

    # A captured classification so the record carries real fields (the Dispatcher
    # snapshots grove.providers._last_classification).
    from grove import providers as _providers_mod
    monkeypatch.setattr(
        _providers_mod,
        "_last_classification",
        ClassificationResult(
            intent_class="conversation",
            pattern_hash="f1test",
            confidence=0.9,
            register_class="casual",
            complexity_signal="trivial",
            goal_alignment=None,
        ),
    )

    # The turn itself → synthetic FinalResponse (no provider round-trip).
    monkeypatch.setattr(
        run_agent.AIAgent,
        "_run_turn_generator",
        _final_only_generator("hello there"),
    )

    response, _summary = oneshot._run_agent("hi")

    # Output semantics unchanged: the response is the model reply.
    assert response == "hello there"

    # Exactly one feed-first receipt under the scratch home.
    store = _intent_store_mod.get_store()
    lines = [ln for ln in store.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one receipt, got {len(lines)}: {lines}"
