"""Regression pin for the hermes-severance-v1 T5.1 live incident.

T5 severed the ACP transport and removed the AIAgent constructor's ACP
parameters (``acp_command`` / ``acp_args`` / ``command`` / ``args``).  It
retired the threading in run_agent / cli / cron/scheduler / delegate_task,
but MISSED the gateway's per-turn runtime builder: ``_resolve_turn_agent_config``
kept emitting ``"command"`` / ``"args"`` keys into the ``runtime`` dict, and
the dispatch path splats that dict wholesale into the agent
(``AIAgent(model=..., **turn_route["runtime"], ...)`` at run.py:10795 / 15435).

Result: every Telegram/gateway dispatch raised
``TypeError: AIAgent.__init__() got an unexpected keyword argument 'command'``.

The closing invariant missed it because T5's proof was gateway *boot* + config
boot-proof.  Boot constructs the agent once through a clean path; the crash is
per-*dispatch*, at turn time, through the runtime dict boot never builds.

This pins the live constructor contract the banked way — integration assertion
through the REAL flow: it calls the gateway's own ``_resolve_turn_agent_config``
and then constructs a REAL ``AIAgent`` through the exact ``**turn_route["runtime"]``
splat the dispatcher uses, rather than a hand-picked direct construction.  If the
turn-config builder ever puts a key in ``runtime`` that the AIAgent signature
rejects, this splat raises TypeError and the pin fails.
"""

from types import SimpleNamespace

from tests._runtime_ctx import MOCK_RUNTIME_CTX, MOCK_CAPABILITY_PROVIDER


def test_gateway_turn_runtime_splats_into_real_aiagent():
    from gateway.run import GatewayRunner
    from run_agent import AIAgent

    # A representative runtime_kwargs as the runtime-provider resolver yields it.
    runner = SimpleNamespace(_service_tier=None)
    runtime_kwargs = {
        "api_key": "test-key",
        "base_url": "http://localhost:1234/v1",
        "provider": "openai",
        "api_mode": "chat_completions",
        "credential_pool": None,
    }

    # REAL turn-config builder — the exact method run.py:10770 calls.
    bound = GatewayRunner._resolve_turn_agent_config.__get__(runner)
    turn_route = bound("hello", "test/model", runtime_kwargs)

    # REAL construction through the dispatcher's own splat shape (run.py:10795).
    # Any severed ACP kwarg surviving in runtime -> TypeError here.
    agent = AIAgent(
        model=turn_route["model"],
        **turn_route["runtime"],
        runtime_ctx=MOCK_RUNTIME_CTX,
        get_available_tools=MOCK_CAPABILITY_PROVIDER,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    assert agent is not None

    # The runtime dict must be a strict subset of the live AIAgent signature —
    # no severed ACP residue (command / args / acp_command / acp_args).
    import inspect

    accepted = set(inspect.signature(AIAgent.__init__).parameters)
    stray = set(turn_route["runtime"]) - accepted
    assert stray == set(), f"runtime carries kwargs AIAgent rejects: {stray}"
    for severed in ("command", "args", "acp_command", "acp_args"):
        assert severed not in turn_route["runtime"]
