"""DeepSeek provider profile.

DeepSeek's V4 family (and the legacy ``deepseek-reasoner``) defaults to
thinking-mode ON when ``extra_body.thinking`` is unset.  The API then returns
``reasoning_content`` and starts enforcing the contract that subsequent turns
echo it back; combined with how Hermes replays history this lands on the
notorious HTTP 400 ``reasoning_content must be passed back`` error after the
first tool call (#15700, #17212, #17825).

This profile overrides :meth:`build_api_kwargs_extras` to mirror the Kimi /
Moonshot wire shape that DeepSeek's OpenAI-compat endpoint expects:

    {"reasoning_effort": "<low|medium|high|max>",
     "extra_body": {"thinking": {"type": "enabled" | "disabled"}}}

Non-thinking models (only ``deepseek-chat`` today, which is V3) are left as
no-ops so we don't perturb the V3 wire format.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class DeepSeekProfile(ProviderProfile):
    """DeepSeek — extra_body.thinking + top-level reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # binding-opacity-v1 P4b 1c — thinking-capability is a declared physics
        # fact (model_facts.reasoning_support), not a "deepseek-v4" name prefix.
        # The V4 family / deepseek-reasoner declare reasoning_support: true; V3
        # declares false. Undeclared -> false -> wire format untouched (safe/
        # loud, the pre-declaration behavior on the VM before the sovereign write).
        _mf = context.get("model_facts")
        if not getattr(_mf, "reasoning_support", False):
            # Not thinking-capable / undeclared — leave wire format untouched.
            return extra_body, top_level

        # Determine enabled/disabled.  Default is enabled to match DeepSeek's
        # API default; the API requires this to be set explicitly to avoid the
        # reasoning_content echo trap on subsequent turns.
        enabled = True
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            enabled = False

        extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

        if not enabled:
            return extra_body, top_level

        # Effort mapping.  Pass low/medium/high through; xhigh/max → max.
        # When no effort is set we omit reasoning_effort so DeepSeek applies
        # its server default (currently high).
        if isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if effort in ("xhigh", "max"):
                top_level["reasoning_effort"] = "max"
            elif effort in ("low", "medium", "high"):
                top_level["reasoning_effort"] = effort

        return extra_body, top_level


deepseek = DeepSeekProfile(
    name="deepseek",
    aliases=("deepseek-chat",),
    env_vars=("DEEPSEEK_API_KEY",),
    display_name="DeepSeek",
    description="DeepSeek — native DeepSeek API",
    signup_url="https://platform.deepseek.com/",
    fallback_models=(
        "deepseek-chat",
        "deepseek-reasoner",
    ),
    base_url="https://api.deepseek.com/v1",
)

register_provider(deepseek)
