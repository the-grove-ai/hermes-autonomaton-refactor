from types import SimpleNamespace

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    has_known_pricing,
    normalize_usage,
)


def test_normalize_usage_anthropic_keeps_cache_buckets_separate():
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=2000,
        cache_creation_input_tokens=400,
    )

    normalized = normalize_usage(usage, provider="anthropic", api_mode="anthropic_messages")

    assert normalized.input_tokens == 1000
    assert normalized.output_tokens == 500
    assert normalized.cache_read_tokens == 2000
    assert normalized.cache_write_tokens == 400
    assert normalized.prompt_tokens == 3400


def test_normalize_usage_openai_subtracts_cached_prompt_tokens():
    usage = SimpleNamespace(
        prompt_tokens=3000,
        completion_tokens=700,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1800),
    )

    normalized = normalize_usage(usage, provider="openai", api_mode="chat_completions")

    assert normalized.input_tokens == 1200
    assert normalized.cache_read_tokens == 1800
    assert normalized.output_tokens == 700


def test_normalize_usage_openai_reads_top_level_anthropic_cache_fields():
    """Some OpenAI-compatible proxies (OpenRouter, Vercel AI Gateway, Cline) expose
    Anthropic-style cache token counts at the top level of the usage object when
    routing Claude models, instead of nesting them in prompt_tokens_details.

    Regression guard for the bug fixed in cline/cline#10266 — before this fix,
    the chat-completions branch of normalize_usage() only read
    prompt_tokens_details.cache_write_tokens and completely missed the
    cache_creation_input_tokens case, so cache writes showed as 0 and reflected
    inputTokens were overstated by the cache-write amount.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    # Expected: cache read from prompt_tokens_details.cached_tokens (preferred),
    # cache write from top-level cache_creation_input_tokens (fallback).
    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    # input_tokens = prompt_total - cache_read - cache_write = 1000 - 500 - 300 = 200
    assert normalized.input_tokens == 200
    assert normalized.output_tokens == 200


def test_normalize_usage_openai_reads_top_level_cache_read_when_details_missing():
    """Some proxies expose only top-level Anthropic-style fields with no
    prompt_tokens_details object. Regression guard for cline/cline#10266.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    assert normalized.input_tokens == 200


def test_normalize_usage_openai_prefers_prompt_tokens_details_over_top_level():
    """When both prompt_tokens_details and top-level Anthropic fields are
    present, we prefer the OpenAI-standard nested fields. Top-level Anthropic
    fields are only a fallback when the nested ones are absent/zero.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=600, cache_write_tokens=150),
        # Intentionally different values — proving we ignore these when details exist.
        cache_read_input_tokens=999,
        cache_creation_input_tokens=999,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 600
    assert normalized.cache_write_tokens == 150


# ── binding-opacity-v1: cost from DECLARED facts, never the slug ──────────────

def test_estimate_usage_cost_computes_from_declared_rates():
    """Both declared per-mtok rates present -> compute directly, source
    'declared'. The slug is opaque; only the declared rates drive the number."""
    result = estimate_usage_cost(
        "any-opaque-slug",
        CanonicalUsage(input_tokens=1_000_000, output_tokens=500_000),
        input_cost_per_mtok=3.0,
        output_cost_per_mtok=15.0,
    )

    assert result.status == "estimated"
    assert result.source == "declared"
    # 1M input × $3/M + 500K output × $15/M = $3.00 + $7.50 = $10.50
    assert float(result.amount_usd) == 10.5


def test_estimate_usage_cost_unknown_when_rates_undeclared():
    """No declared rates -> unknown-and-loud. No fallback to a name table."""
    result = estimate_usage_cost(
        "anthropic/claude-sonnet-4-20250514",
        CanonicalUsage(input_tokens=1000, output_tokens=500),
        provider="anthropic",
    )

    assert result.status == "unknown"
    assert result.amount_usd is None
    assert result.source == "none"


def test_estimate_usage_cost_unknown_when_only_one_rate_declared():
    """A half-declared binding is still unknown — both rates are required."""
    result = estimate_usage_cost(
        "any-slug",
        CanonicalUsage(input_tokens=1000, output_tokens=500),
        input_cost_per_mtok=3.0,
    )

    assert result.status == "unknown"
    assert result.amount_usd is None


def test_estimate_usage_cost_bills_cache_tokens_at_declared_input_rate():
    """The declared binding has no separate cache tier; cache tokens are billed
    at the declared input rate and a note records that."""
    result = estimate_usage_cost(
        "any-slug",
        CanonicalUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=2000,
            cache_write_tokens=400,
        ),
        input_cost_per_mtok=3.0,
        output_cost_per_mtok=15.0,
    )

    assert result.status == "estimated"
    # input 1000×3 + output 500×15 + cache (2000+400)×3 (input rate), all per-M
    expected = (1000 * 3.0 + 500 * 15.0 + (2000 + 400) * 3.0) / 1_000_000
    assert float(result.amount_usd) == round(expected, 10)
    assert any("cache" in n for n in result.notes)


def test_estimate_usage_cost_marks_subscription_routes_included():
    """Subscription route is detected from the provider token, without parsing
    the slug, and bills nothing."""
    result = estimate_usage_cost(
        "gpt-5.3-codex",
        CanonicalUsage(input_tokens=1000, output_tokens=500),
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )

    assert result.status == "included"
    assert float(result.amount_usd) == 0.0


def test_has_known_pricing_follows_declared_facts():
    """'known' is a property of the declared binding, never the slug."""
    assert has_known_pricing("any-slug", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0) is True
    # subscription route is known even without per-token rates
    assert has_known_pricing("gpt-5.3-codex", provider="openai-codex") is True
    # undeclared -> unknown, regardless of how "commercial" the slug looks
    assert has_known_pricing("anthropic/claude-sonnet-4-20250514") is False
    assert has_known_pricing("gpt-4o", provider="openai") is False
    assert has_known_pricing("my-custom-model") is False
