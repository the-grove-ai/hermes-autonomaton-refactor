"""instance-cold-start-parity-v1 P6 (F5a) — the MOCKED-provider cold-start CI
assertion. Runs where NO credentials exist (the suite blanks the provider keys):

    fresh GROVE_HOME  ->  materialize  ->  contract validation (boot preflight)
    ->  one routed turn against a MOCKED provider  ->  feed-first receipt.

What this proves and does NOT prove (state it exactly): it proves a cold instance
MATERIALIZES to a contract-valid state and BOOTS + ROUTES a turn with a stubbed
provider — no network. It does NOT prove the install-and-go live path; that claim
is carried by tests/grove/test_cold_start_live_smoke.py (GROVE_LIVE_TESTS-gated),
which runs where real credentials exist.

The provider is stubbed (synthetic _run_turn_generator + canned runtime), mirroring
tests/hermes_cli/test_oneshot_intent_receipt.py — this asserts cold-start wiring,
not model behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import run_agent
from grove import intent_store as _intent_store_mod
from grove.classify import ClassificationResult
from grove.cold_start import materialize_instance
from grove.instance_health import preflight_graduated_files
from grove.intents import FinalResponse


def _final_only_generator(final_text: str):
    def _gen(self, **kwargs):  # bound method stand-in (self first)
        def g():
            yield FinalResponse(content=final_text)
            return {"final_response": final_text}

        return g()

    return _gen


def test_cold_start_materialize_validate_and_route_mocked(tmp_path, monkeypatch):
    scratch = tmp_path / "grove_scratch"
    monkeypatch.setenv("GROVE_HOME", str(scratch))
    monkeypatch.setenv("GROVE_YOLO_MODE", "1")

    # (1) MATERIALIZE — a cold instance is born.
    report = materialize_instance(scratch, bypass_cache=True)
    assert report.state == "fresh", f"expected a fresh birth, got {report.state!r}"
    assert report.marker_written is True
    assert (scratch / ".grove_instance").exists()

    # (2) CONTRACT VALIDATION — the boot preflight (D3 Andon). Returns the
    # classifications on success; raises InstanceFileError on any malformed /
    # unreadable graduated file. A clean return == contract satisfied.
    classifications = preflight_graduated_files(scratch)
    assert classifications, "preflight returned no classifications"
    assert all(not c.is_fault for c in classifications)

    # (3) ONE ROUTED TURN against a MOCKED provider — boot + route with no network.
    from hermes_cli import oneshot

    monkeypatch.setattr(_intent_store_mod, "_default_store", None)
    routed = SimpleNamespace(
        tier="T2",
        tier_config=SimpleNamespace(
            tier="T2", model="z-ai/glm-5.2", provider="openrouter"
        ),
    )
    monkeypatch.setattr("grove.providers.route_for_agent", lambda **kw: routed)
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
    from grove import providers as _providers_mod
    monkeypatch.setattr(
        _providers_mod,
        "_last_classification",
        ClassificationResult(
            intent_class="conversation",
            pattern_hash="f5atest",
            confidence=0.9,
            register_class="casual",
            complexity_signal="trivial",
            goal_alignment=None,
        ),
    )
    monkeypatch.setattr(
        run_agent.AIAgent, "_run_turn_generator", _final_only_generator("hello there")
    )

    response, _summary = oneshot._run_agent("hi")
    assert response == "hello there"

    # (4) FEED-FIRST RECEIPT — the routed turn left exactly one record under the
    # freshly-materialized scratch home (materialize + routing + feed agree on it).
    store = _intent_store_mod.get_store()
    lines = [ln for ln in store.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one receipt, got {len(lines)}"
