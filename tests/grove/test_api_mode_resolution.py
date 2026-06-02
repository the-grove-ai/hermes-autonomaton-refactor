"""Sprint 47 hotfix — api_mode detection: no silent fallback.

The hotfix deletes the ``self.api_mode = "chat_completions"`` silent
default in ``AIAgent.__init__``'s detection chain. The else branch
now raises ``grove.errors.ProviderDetectionError`` with a message
naming the inputs and the routing.config.yaml fix.

Defense-in-depth: falsy ``provider_name`` and ``api_mode`` values
normalize to ``None`` at the top of the chain so an empty string
from config resolution cannot bypass the ``is None`` gates.

Four test categories, per the hotfix spec:

* Test A — Empty provider + Anthropic URL → auto-detects to
  ``anthropic_messages``. The defense-in-depth normalization wins;
  the system never reaches the else branch.
* Test B — Unrecognized provider + unrecognized URL → raises
  ``ProviderDetectionError``. The silent fallback is dead.
* Test C — Each tier in ``config/routing.config.yaml`` constructs an
  agent with the correct api_mode. The missing test category the
  operator flagged.
* Test D — Explicit ``api_mode='chat_completions'`` is honored.
  Operators who KNOW what they want declare it; the system does not
  refuse a legitimate explicit override.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from grove.errors import ProviderDetectionError
from tests._runtime_ctx import MOCK_RUNTIME_CTX, MOCK_CAPABILITY_PROVIDER


def _build_agent(*, provider, api_mode=None, base_url, model):
    """Build a bare AIAgent with the SDK boot path mocked.

    Returns the agent so callers can inspect ``self.api_mode`` and
    ``self.provider`` without driving any real HTTP request.
    """
    with patch("run_agent.get_tool_definitions", return_value=[]), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI"):
        from run_agent import AIAgent
        return AIAgent(
            runtime_ctx=MOCK_RUNTIME_CTX,
            api_key="test-key",
            base_url=base_url,
            provider=provider,
            api_mode=api_mode,
            model=model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True, get_available_tools=lambda *_a, **_k: ([])
        )


# ── Test A — Empty provider + Anthropic URL auto-detects ──────────────


class TestEmptyProviderAnthropicUrl:
    """The exact reproduction from the hotfix ticket — bug must
    never regress."""

    def test_empty_string_provider_anthropic_url(self) -> None:
        agent = _build_agent(
            provider="",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "anthropic_messages"
        assert agent.provider == "anthropic"

    def test_none_provider_anthropic_url(self) -> None:
        agent = _build_agent(
            provider=None,
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "anthropic_messages"
        assert agent.provider == "anthropic"

    def test_false_provider_anthropic_url(self) -> None:
        """Defense-in-depth: any truly-falsy value (not just empty
        string) normalizes to None so the detection gate fires."""
        agent = _build_agent(
            provider=False,
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "anthropic_messages"

    def test_zero_provider_anthropic_url(self) -> None:
        agent = _build_agent(
            provider=0,
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "anthropic_messages"

    def test_empty_string_api_mode_normalizes_to_none(self) -> None:
        """An empty-string ``api_mode`` MUST behave like None —
        falling through to detection — rather than registering as an
        explicit operator override that would later trigger the
        codex-responses re-check at line ~1500."""
        agent = _build_agent(
            provider="",
            api_mode="",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "anthropic_messages"


# ── Test B — Unrecognized provider + unrecognized URL → raises ───────


class TestUnrecognizedProviderRaises:
    """The silent fallback is dead. Architecture is the guarantee."""

    def test_unknown_provider_unknown_url_raises(self) -> None:
        with pytest.raises(ProviderDetectionError) as exc_info:
            _build_agent(
                provider="unknown-provider",
                base_url="https://unknown.example.com",
                model="unknown-model",
            )
        message = str(exc_info.value)
        # The Andon message MUST name every relevant input so the
        # operator can locate the config-shape problem.
        assert "unknown-model" in message
        assert "unknown.example.com" in message
        assert "unknown-provider" in message
        # And it MUST point at the fix path.
        assert "routing.config.yaml" in message
        assert "api_mode" in message

    def test_empty_provider_unknown_url_raises(self) -> None:
        """An empty provider with no auto-detectable URL pattern MUST
        raise — the normalization gets the bypass but the chain still
        finds no positive match."""
        with pytest.raises(ProviderDetectionError):
            _build_agent(
                provider="",
                base_url="https://api.example.com/v1",
                model="some-model",
            )

    def test_provider_detection_error_is_value_error(self) -> None:
        """ProviderDetectionError MUST subclass ValueError so any
        existing ``except ValueError`` boundary picks it up."""
        with pytest.raises(ValueError):
            _build_agent(
                provider="unknown",
                base_url="https://unknown.example.com",
                model="m",
            )


# ── Test C — Repo config tier → correct api_mode ─────────────────────


class TestRepoTemplateTierResolution:
    """For each tier in ``config/routing.config.yaml``, the AIAgent
    constructs with the correct api_mode given that tier's settings.
    The operator-flagged "missing test category."
    """

    @pytest.fixture
    def repo_tiers(self) -> dict:
        repo_root = Path(__file__).resolve().parents[2]
        cfg_path = repo_root / "config" / "routing.config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return cfg["routing"]["tier_preferences"]

    def test_t1_anthropic_messages(self, repo_tiers: dict) -> None:
        spec = repo_tiers["T1"]
        agent = _build_agent(
            provider=spec.get("provider", ""),
            api_mode=spec.get("api_mode"),
            base_url="https://api.anthropic.com",
            model=spec["model"],
        )
        assert agent.api_mode == "anthropic_messages"

    def test_t2_anthropic_messages(self, repo_tiers: dict) -> None:
        spec = repo_tiers["T2"]
        agent = _build_agent(
            provider=spec.get("provider", ""),
            api_mode=spec.get("api_mode"),
            base_url="https://api.anthropic.com",
            model=spec["model"],
        )
        assert agent.api_mode == "anthropic_messages"

    def test_t3_anthropic_messages(self, repo_tiers: dict) -> None:
        spec = repo_tiers["T3"]
        agent = _build_agent(
            provider=spec.get("provider", ""),
            api_mode=spec.get("api_mode"),
            base_url="https://api.anthropic.com",
            model=spec["model"],
        )
        assert agent.api_mode == "anthropic_messages"


# ── Test D — Explicit api_mode is honored ────────────────────────────


class TestExplicitApiModeHonored:
    """The system refuses to guess. It does not refuse a legitimate
    explicit override."""

    def test_explicit_chat_completions_on_anthropic_url(self) -> None:
        """An operator who declares ``api_mode='chat_completions'``
        gets exactly that, even when the URL would otherwise
        auto-detect to ``anthropic_messages``."""
        agent = _build_agent(
            provider="",
            api_mode="chat_completions",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        assert agent.api_mode == "chat_completions"

    def test_explicit_anthropic_messages_on_unknown_url(self) -> None:
        """Explicit ``api_mode='anthropic_messages'`` is honored even
        when neither the provider nor the URL would auto-detect —
        the operator's declaration is the source of truth."""
        agent = _build_agent(
            provider="custom-provider",
            api_mode="anthropic_messages",
            base_url="https://custom.example.com/v1",
            model="custom-model",
        )
        assert agent.api_mode == "anthropic_messages"

    def test_explicit_bedrock_converse_honored(self) -> None:
        agent = _build_agent(
            provider="",
            api_mode="bedrock_converse",
            base_url="https://some.example.com/v1",
            model="m",
        )
        assert agent.api_mode == "bedrock_converse"

    def test_explicit_codex_responses_honored(self) -> None:
        agent = _build_agent(
            provider="",
            api_mode="codex_responses",
            base_url="https://api.example.com/v1",
            model="m",
        )
        assert agent.api_mode == "codex_responses"
