"""A retry after an empty model response must not be answered from the
OpenRouter response cache (live: three "retries" returned the cached empty
response in under 0.2s each and the turn ended with no answer)."""

import contextlib

import run_agent

_CACHE_HEADERS = {
    "HTTP-Referer": "https://example.test",
    "X-OpenRouter-Cache": "true",
    "X-OpenRouter-Cache-TTL": "300",
}


def _agent(retries):
    agent = object.__new__(run_agent.AIAgent)
    agent._client_kwargs = {
        "api_key": "k", "base_url": "https://openrouter.ai/api/v1",
        "default_headers": dict(_CACHE_HEADERS),
    }
    agent._empty_content_retries = retries
    agent._ensure_primary_openai_client = lambda reason: object()
    agent._openai_client_lock = contextlib.nullcontext
    captured = {}

    def _create(kwargs, *, reason, shared):
        captured.update(kwargs)
        return object()

    agent._create_openai_client = _create
    return agent, captured


def test_normal_request_keeps_the_response_cache_headers():
    agent, captured = _agent(0)
    agent._create_request_openai_client(reason="t")
    assert captured["default_headers"] == _CACHE_HEADERS


def test_empty_response_retry_drops_the_response_cache_headers():
    agent, captured = _agent(1)
    agent._create_request_openai_client(reason="t")
    assert captured["default_headers"] == {"HTTP-Referer": "https://example.test"}
    # the agent's own client settings are untouched — the next normal request
    # uses the cache again
    assert agent._client_kwargs["default_headers"] == _CACHE_HEADERS


def test_empty_terminal_marker_is_never_decorated():
    # Live 20261005_213546_ea5d69a1#1: the Cellar footer was appended to the
    # "(empty)" marker, so neither the Dispatcher nor the gateway recognised it.
    # Every reply-decorating block after the loop must sit behind the one guard
    # that excludes the empty terminal.
    import inspect

    import run_agent

    src = inspect.getsource(run_agent)
    guard = src.index("_decorate_reply = (")
    result_build = src.index('"turn_exit_reason": _turn_exit_reason,')
    tail = src[guard:result_build]
    assert '_turn_exit_reason != "empty_response_exhausted"' in tail[:400]
    for appender in (
        "_apply_mutation_verifier(", "transform_llm_output",
        "_append_pending_offer(", "_append_connector_failure_offer(",
        "_append_artifact_links(", "_append_cellar_citations(",
    ):
        at = tail.index(appender)
        opened = tail.rfind("\n        if ", 0, at)
        assert tail[opened:].startswith("\n        if _decorate_reply:"), appender
