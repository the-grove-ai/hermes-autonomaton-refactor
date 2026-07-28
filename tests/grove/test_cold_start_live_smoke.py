"""GROVE_LIVE_TESTS-gated cold-start smoke — GATE-B F5b
(instance-cold-start-parity-v1 P5). Authored here; consumed by P6 CI/README.

The end-to-end live proof of the P1 GROVE_HOME unification + the feed-first
receipt: materialize a FRESH instance into a SCRATCH GROVE_HOME (never the
operator's real ~/.grove), boot it, run ONE real routed turn, and assert the
turn completes with a nonempty reply AND leaves a telemetry row —
``intent_records.jsonl`` — the feed-first receipt that every turn is designed to
produce.

SINGLE real turn: a green run evidences that a cold instance boots and routes,
NOT reliability. It exercises the same routed path (`_run_agent` → classify →
tier select → provider call → intent record) a normal `autonomaton chat` turn
takes, so it doubles as the live proof that materialize + routing agree on one
GROVE_HOME.

Run:  GROVE_LIVE_TESTS=1 pytest tests/grove/test_cold_start_live_smoke.py -q -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _load_user_env() -> None:
    """Load ``~/.grove/.env`` so live runs pick up ``OPENROUTER_API_KEY`` without
    the runner shell-sourcing it (mirrors tests/run_agent/
    test_sequential_chats_live.py). Silent if absent; never clobbers a set var."""
    env_file = Path.home() / ".grove" / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        # Skip empty-valued lines: a bare ``KEY=`` placeholder must not shadow a
        # real assignment for the same key later in the file (setdefault locks in
        # the first value seen).
        if v:
            os.environ.setdefault(k.strip(), v)


_load_user_env()

LIVE = os.environ.get("GROVE_LIVE_TESTS") == "1"
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
pytestmark = [
    pytest.mark.skipif(not LIVE, reason="live-only: set GROVE_LIVE_TESTS=1"),
    pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not configured"),
]

# The error-sentinel shapes AIAgent returns as a STRING (not an exception) when
# the provider call fails past retries — a naive truthiness check would rubber-
# stamp them (borrowed from test_sequential_chats_live.py).
_ERROR_MARKERS = (
    "api call failed",
    "connection error",
    "client has been closed",
    "cannot send a request",
    "max retries",
)


def test_cold_start_materialize_boot_route_leaves_receipt(tmp_path, monkeypatch, capsys):
    # Re-load the key HERE: tests/conftest.py's autouse ``_hermetic_environment``
    # strips every ``*_API_KEY`` per-test (unit-test isolation), running AFTER the
    # module-import load. A GROVE_LIVE_TESTS-gated test opts INTO the real
    # credential; repopulate it from the walled ~/.grove/.env for the routed turn.
    _load_user_env()

    from grove import router as grove_router
    from grove.cold_start import invalidate_cache, materialize_instance

    # A genuinely FRESH, unmarked scratch home — NOT the operator's ~/.grove, and
    # not the conftest per-test home (which is pre-marked an instance). GROVE_HOME
    # is read live by get_hermes_home(), so this redirects the whole turn.
    scratch = tmp_path / "grove_scratch"
    monkeypatch.setenv("GROVE_HOME", str(scratch))
    monkeypatch.setenv("GROVE_YOLO_MODE", "1")  # non-interactive: auto-approve

    # (1) MATERIALIZE — the P1 unification: a cold instance is born here.
    invalidate_cache(scratch)
    report = materialize_instance(scratch)
    assert report.state == "fresh", f"expected a fresh birth, got {report.state!r}"
    assert (scratch / ".grove_instance").exists(), "instance marker not written"

    # (2) BOOT — point the router singleton at the scratch instance's routing.
    grove_router.initialize()

    # (3) ONE REAL ROUTED TURN through the GOVERNED path — the intent-recording
    # dispatch_turn (GRV-005 § II), not the lightweight `-z` oneshot (which wires
    # NO intent_store and so leaves no feed-first receipt). Mirrors
    # hermes_cli.oneshot._run_agent's route+runtime resolution, then binds an
    # IntentStore at the SCRATCH home and drives dispatch_turn.
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from grove.dispatcher import Dispatcher
    from grove.intent_store import IntentStore
    from grove.providers import route_for_agent

    prompt = "Say hello in exactly one short sentence."
    routed = route_for_agent(message=prompt, explicit_model=None, classify=True)
    assert routed is not None, "router resolved no tier for the fresh instance"
    model = routed.tier_config.model
    runtime = resolve_runtime_provider(
        requested=routed.tier_config.provider, target_model=model or None
    )

    try:
        from hermes_state import SessionDB
        session_db = SessionDB()
    except Exception:
        session_db = None

    store = IntentStore(scratch / "intent_records.jsonl")
    dispatcher = Dispatcher(
        session_db=session_db,
        intent_store=store,
        agent_kwargs=dict(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            model=model,
            enabled_toolsets=[],  # no tools → a single text reply
            quiet_mode=True,
            platform="cli",
            credential_pool=runtime.get("credential_pool"),
        ),
    )
    result = dispatcher.dispatch_turn(
        dispatcher.agent, user_message=prompt, already_routed=True
    )
    response = (result or {}).get("final_response") or ""

    # turn completes, reply is a real model reply (not an error-sentinel string)
    assert response and response.strip(), f"empty reply: {response!r}"
    low = response.lower()
    assert not any(m in low for m in _ERROR_MARKERS), (
        f"reply is an error-sentinel, not a model reply: {response!r}"
    )

    # (4) THE FEED-FIRST RECEIPT — the governed turn wrote an intent row under the
    # SCRATCH home (proving materialize + routing + the intent feed share one
    # GROVE_HOME — the P1 unification, end to end).
    feed = store.path
    assert feed.exists(), f"no intent_records.jsonl receipt at {feed}"
    rows = [json.loads(ln) for ln in feed.read_text().splitlines() if ln.strip()]
    assert rows, "intent_records.jsonl is empty — no feed-first receipt"

    # Report which binding the default tier resolved to (for the P5 evidence log).
    last = rows[-1]
    resolved = {
        "routed_tier": getattr(routed.tier_config, "tier", None),
        "routed_model": model,
        "record_tier_selected": last.get("tier_selected"),
        "record_model_used": last.get("model_used") or last.get("model"),
    }
    with capsys.disabled():
        print(f"\n[cold-start-smoke] default tier resolved: {resolved}")
