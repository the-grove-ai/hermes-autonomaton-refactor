from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional

DEFAULT_PRICING = {"input": 0.0, "output": 0.0}

_ZERO = Decimal("0")
_ONE_MILLION = Decimal("1000000")

CostStatus = Literal["actual", "estimated", "included", "unknown"]
# binding-opacity-v1: cost is a DECLARED fact of the model binding. The only
# sources left are the declared per-token rates and the absence-of-cost route.
CostSource = Literal["declared", "none"]


@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw_usage: Optional[dict[str, Any]] = None

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass(frozen=True)
class CostResult:
    amount_usd: Optional[Decimal]
    status: CostStatus
    source: CostSource
    label: str
    fetched_at: Optional[datetime] = None
    pricing_version: Optional[str] = None
    notes: tuple[str, ...] = ()


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def normalize_usage(
    response_usage: Any,
    *,
    provider: Optional[str] = None,
    api_mode: Optional[str] = None,
) -> CanonicalUsage:
    """Normalize raw API response usage into canonical token buckets.

    Handles three API shapes:
    - Anthropic: input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens
    - Codex Responses: input_tokens includes cache tokens; input_tokens_details.cached_tokens separates them
    - OpenAI Chat Completions: prompt_tokens includes cache tokens; prompt_tokens_details.cached_tokens separates them

    In both Codex and OpenAI modes, input_tokens is derived by subtracting cache
    tokens from the total — the API contract is that input/prompt totals include
    cached tokens and the details object breaks them out.

    binding-opacity-v1 note: this is TOKEN normalization keyed on the api_mode /
    provider MECHANICS, never on the model slug — it derives no cost and looks up
    no name-keyed table.
    """
    if not response_usage:
        return CanonicalUsage()

    provider_name = (provider or "").strip().lower()
    mode = (api_mode or "").strip().lower()

    if mode == "anthropic_messages" or provider_name == "anthropic":
        input_tokens = _to_int(getattr(response_usage, "input_tokens", 0))
        output_tokens = _to_int(getattr(response_usage, "output_tokens", 0))
        cache_read_tokens = _to_int(getattr(response_usage, "cache_read_input_tokens", 0))
        cache_write_tokens = _to_int(getattr(response_usage, "cache_creation_input_tokens", 0))
    elif mode == "codex_responses":
        input_total = _to_int(getattr(response_usage, "input_tokens", 0))
        output_tokens = _to_int(getattr(response_usage, "output_tokens", 0))
        details = getattr(response_usage, "input_tokens_details", None)
        cache_read_tokens = _to_int(getattr(details, "cached_tokens", 0) if details else 0)
        cache_write_tokens = _to_int(
            getattr(details, "cache_creation_tokens", 0) if details else 0
        )
        input_tokens = max(0, input_total - cache_read_tokens - cache_write_tokens)
    else:
        prompt_total = _to_int(getattr(response_usage, "prompt_tokens", 0))
        output_tokens = _to_int(getattr(response_usage, "completion_tokens", 0))
        details = getattr(response_usage, "prompt_tokens_details", None)
        # Primary: OpenAI-style prompt_tokens_details. Fallback: Anthropic-style
        # top-level fields that some OpenAI-compatible proxies (OpenRouter, Vercel
        # AI Gateway, Cline) expose when routing Claude models — without this
        # fallback, cache writes are undercounted as 0 and cache reads can be
        # missed when the proxy only surfaces them at the top level.
        # Port of cline/cline#10266.
        cache_read_tokens = _to_int(getattr(details, "cached_tokens", 0) if details else 0)
        if not cache_read_tokens:
            cache_read_tokens = _to_int(getattr(response_usage, "cache_read_input_tokens", 0))
        cache_write_tokens = _to_int(
            getattr(details, "cache_write_tokens", 0) if details else 0
        )
        if not cache_write_tokens:
            cache_write_tokens = _to_int(
                getattr(response_usage, "cache_creation_input_tokens", 0)
            )
        input_tokens = max(0, prompt_total - cache_read_tokens - cache_write_tokens)

    reasoning_tokens = 0
    output_details = getattr(response_usage, "output_tokens_details", None)
    if output_details:
        reasoning_tokens = _to_int(getattr(output_details, "reasoning_tokens", 0))

    return CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def _subscription_included(provider: Optional[str]) -> bool:
    """Zero-cost subscription route, detected WITHOUT parsing the model slug.

    Only the declared ``provider`` token decides this — a Codex subscription
    bills nothing per token. No slug inspection, no name-keyed table.
    """
    return (provider or "").strip().lower() == "openai-codex"


def estimate_usage_cost(
    model_name: str,
    usage: CanonicalUsage,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    input_cost_per_mtok: Optional[float] = None,
    output_cost_per_mtok: Optional[float] = None,
) -> CostResult:
    """Compute per-turn cost from DECLARED model facts — never from the slug.

    binding-opacity-v1: per-turn cost is a declared physical fact of the model
    binding (``ModelFacts.cost_per_mtok_input`` / ``cost_per_mtok_output``, USD
    per MILLION tokens), threaded in by the caller. ``model_name`` is an OPAQUE
    token here: it is neither parsed nor used to look up a name-keyed pricing
    table.

    * BOTH declared rates present -> compute directly from the rates and the
      usage token counts; return an ``estimated`` result (source ``declared``).
    * subscription-included route (``provider == "openai-codex"``) -> zero-cost
      ``included`` result, detected without touching the slug.
    * otherwise (undeclared) -> ``unknown`` (amount None). Unknown-and-loud:
      there is NO fallback to a name-derived pricing table.

    ``base_url`` / ``api_key`` are retained for call-site compatibility only;
    they are no longer consulted (no out-of-band pricing fetch remains).
    """
    if _subscription_included(provider):
        return CostResult(
            amount_usd=_ZERO,
            status="included",
            source="none",
            label="included",
            pricing_version="included-route",
        )

    if input_cost_per_mtok is None or output_cost_per_mtok is None:
        # Undeclared -> cost is genuinely unknown. Do not guess from the slug.
        return CostResult(amount_usd=None, status="unknown", source="none", label="n/a")

    input_rate = Decimal(str(input_cost_per_mtok))
    output_rate = Decimal(str(output_cost_per_mtok))

    amount = _ZERO
    amount += Decimal(usage.input_tokens) * input_rate / _ONE_MILLION
    amount += Decimal(usage.output_tokens) * output_rate / _ONE_MILLION

    notes: list[str] = []
    # The declared binding carries a single input rate and a single output rate;
    # it does not declare separate cache-read / cache-write tiers. Apply the
    # declared input rate to cache tokens as a first-order approximation and say
    # so, rather than dropping cache spend silently.
    cache_tokens = usage.cache_read_tokens + usage.cache_write_tokens
    if cache_tokens:
        amount += Decimal(cache_tokens) * input_rate / _ONE_MILLION
        notes.append(
            "cache tokens billed at the declared input rate "
            "(binding declares no separate cache-tier rate)."
        )

    return CostResult(
        amount_usd=amount,
        status="estimated",
        source="declared",
        label=f"~${amount:.2f}",
        notes=tuple(notes),
    )


def has_known_pricing(
    model_name: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    input_cost_per_mtok: Optional[float] = None,
    output_cost_per_mtok: Optional[float] = None,
) -> bool:
    """Whether per-turn cost is KNOWN for this binding.

    binding-opacity-v1: "known" is a property of the DECLARED binding — the
    per-mtok cost facts are present, or the route is subscription-included. It
    is never inferred from the model slug. ``model_name`` / ``base_url`` /
    ``api_key`` are accepted for call-site compatibility and are not inspected.
    """
    if _subscription_included(provider):
        return True
    return input_cost_per_mtok is not None and output_cost_per_mtok is not None


def format_duration_compact(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"


def format_token_count_compact(value: int) -> str:
    abs_value = abs(int(value))
    if abs_value < 1_000:
        return str(int(value))

    sign = "-" if value < 0 else ""
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for threshold, suffix in units:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            if scaled < 10:
                text = f"{scaled:.2f}"
            elif scaled < 100:
                text = f"{scaled:.1f}"
            else:
                text = f"{scaled:.0f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"

    return f"{value:,}"
