# Runtime Context Migration Guide

`AIAgent.__init__` requires a `grove.dispatcher.RuntimeContext`. There is
no None default, no silent substrate fallback. Construction without one
raises a named `ValueError` at the top of `__init__`, pointing at the
Dispatcher.

This guide is for callers that construct `AIAgent` directly — a path
that, after Sprint 33, is rare in production and reserved for tests.

## The expected path: construct via the Dispatcher

```python
from grove.dispatcher import Dispatcher

dispatcher = Dispatcher(
    agent_kwargs=dict(
        model="...",
        api_key="...",
        base_url="...",
        # everything else AIAgent takes
    ),
)
agent = dispatcher.agent
```

The Dispatcher captures its own substrate snapshot at construction
(`os.environ` + `hermes_cli.config.load_config()`), wraps it in a
`RuntimeContext`, and forwards it into the Agent via `agent_kwargs`'s
`runtime_ctx` slot. Callers don't supply a RuntimeContext — the
Dispatcher does it on their behalf.

Every production caller migrated in Sprint 33 already uses this path.
Nothing else is required.

## Constructing AIAgent directly (rare)

If you need to bypass the Dispatcher (specialized tests, internal review
forks):

```python
from grove.dispatcher import Dispatcher
from run_agent import AIAgent

dispatcher = Dispatcher()  # captures substrate
agent = AIAgent(
    model="...",
    runtime_ctx=dispatcher.runtime_ctx,
    # ...
)
```

`dispatcher.runtime_ctx` is the bare snapshot (env + config); use
`dispatcher.runtime_context_for(...)` if you also want the cached heavy
resources (tools registry, memory store, Anthropic client, compression
probe).

## Tests

Use the shared `MOCK_RUNTIME_CTX` constant or the `mock_runtime_ctx`
pytest fixture from `tests/conftest.py`:

```python
# module-level usage
from tests._runtime_ctx import MOCK_RUNTIME_CTX

agent = AIAgent(model="test/m", runtime_ctx=MOCK_RUNTIME_CTX, ...)

# fixture-parameter usage
def test_something(mock_runtime_ctx):
    agent = AIAgent(model="test/m", runtime_ctx=mock_runtime_ctx, ...)
```

`MOCK_RUNTIME_CTX` is a subclass of `RuntimeContext` that delegates
`env` and `config` attribute access to live `os.environ` and
`hermes_cli.config.load_config()`, so existing test patterns —
`monkeypatch.setenv(...)`, `patch("hermes_cli.config.load_config",
return_value=...)` — keep working without per-test ctx instantiation.

Tests that bypass `__init__` via `object.__new__(AIAgent)` and then call
methods reading `self._runtime_ctx` must set it manually:

```python
agent = object.__new__(AIAgent)
agent._runtime_ctx = MOCK_RUNTIME_CTX
# … set whatever other state the test needs …
```

## What used to work but no longer does

| Old | Why it broke | New |
|---|---|---|
| `AIAgent(...)` with no `runtime_ctx` | No default | Pass one explicitly |
| `AIAgent(runtime_ctx=None)` | Raises | Pass a real `RuntimeContext` |
| Helpers `_env_or` / `_config_load_or` falling through to `os.environ` / `load_config()` when `runtime_ctx` was None | Fallback arms deleted | All reads route through `runtime_ctx` |
| Tests that patched `os.environ` / `load_config` expecting the Agent's helpers to pick them up | `MOCK_RUNTIME_CTX` already delegates to live substrate; no change needed | Continue using `monkeypatch.setenv` / `patch("hermes_cli.config.load_config", ...)` |
