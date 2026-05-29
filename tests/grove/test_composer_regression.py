"""Sprint 36 — PromptComposer regression test (GRV-007).

THE mandatory Phase 1b artifact: the composer's output MUST match the
pre-Sprint-36 ``AIAgent._build_system_prompt_parts`` output byte-for-
byte across the section coverage the test cases below exercise.

Datetime is frozen at the test layer (Pre-execution Patch 1) so the
``timestamp`` section's ``Conversation started: …`` line is reproducible.

Tier-level + per-section equality is asserted. The joined system prompt
the Agent eventually receives is the concatenation
``stable + "\\n\\n" + context + "\\n\\n" + volatile`` — verifying tiers
match implies the full string matches when joined.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Set
from unittest.mock import MagicMock

import pytest

from grove.prompt.composer import (
    ComposedPrompt,
    PromptComposer,
    build_default_composer,
)


# ── Helpers ──────────────────────────────────────────────────────────


_FROZEN = datetime(2026, 5, 29, 17, 30, 0)


def _frozen_now():
    return _FROZEN


def _build_legacy_parts(
    *,
    valid_tool_names: Set[str],
    model: str = "claude-sonnet-4-6",
    provider: str = "anthropic",
    platform: str = "cli",
    session_id: str = "sess_test",
    skip_context_files: bool = True,
    load_soul_identity: bool = False,
    memory_enabled: bool = False,
    user_profile_enabled: bool = False,
    pass_session_id: bool = False,
    system_message: str | None = None,
    session_register: str | None = None,
    tool_use_enforcement: Any = "auto",
    memory_store=None,
    memory_manager=None,
    monkeypatch=None,
) -> Dict[str, str]:
    """Construct a bare AIAgent via ``__new__`` (no __init__) and call
    its legacy ``_build_system_prompt_parts``. This is the byte-for-byte
    reference the new composer must match.

    Tests must monkeypatch ``hermes_time.now`` to return ``_FROZEN`` so
    the timestamp matches the composer's frozen ``now_fn``.
    """
    import run_agent

    assert monkeypatch is not None, "tests must pass monkeypatch"
    monkeypatch.setattr("hermes_time.now", _frozen_now)

    agent = object.__new__(run_agent.AIAgent)
    agent.valid_tool_names = set(valid_tool_names)
    agent.model = model
    agent.provider = provider
    agent.platform = platform
    agent.session_id = session_id
    agent.session_register = session_register
    agent.load_soul_identity = load_soul_identity
    agent.skip_context_files = skip_context_files
    agent._memory_enabled = memory_enabled
    agent._user_profile_enabled = user_profile_enabled
    agent.pass_session_id = pass_session_id
    agent._tool_use_enforcement = tool_use_enforcement
    # Sprint 40 back-references for memory access.
    fake_disp = MagicMock()
    fake_disp.memory_store = memory_store
    fake_disp.memory_manager = memory_manager
    agent._dispatcher_singleton = fake_disp
    # _env_or reads TERMINAL_CWD; legacy method calls self._env_or which
    # routes through runtime_ctx. Use a minimal ctx that returns ""
    # so the env-driven cwd path skips.
    from tests._runtime_ctx import MOCK_RUNTIME_CTX
    agent._runtime_ctx = MOCK_RUNTIME_CTX

    return agent._build_system_prompt_parts(system_message=system_message)


def _compose_new(
    *,
    valid_tool_names: Set[str],
    model: str = "claude-sonnet-4-6",
    provider: str = "anthropic",
    platform: str = "cli",
    session_id: str = "sess_test",
    skip_context_files: bool = True,
    load_soul_identity: bool = False,
    memory_enabled: bool = False,
    user_profile_enabled: bool = False,
    pass_session_id: bool = False,
    system_message: str | None = None,
    session_register: str | None = None,
    tool_use_enforcement: Any = "auto",
    memory_store=None,
    memory_manager=None,
) -> ComposedPrompt:
    composer = build_default_composer()
    return composer.compose(
        valid_tool_names=valid_tool_names,
        model=model,
        provider=provider,
        platform=platform,
        session_id=session_id,
        skip_context_files=skip_context_files,
        load_soul_identity=load_soul_identity,
        memory_enabled=memory_enabled,
        user_profile_enabled=user_profile_enabled,
        pass_session_id=pass_session_id,
        system_message=system_message,
        session_register=session_register,
        tool_use_enforcement=tool_use_enforcement,
        memory_store=memory_store,
        memory_manager=memory_manager,
        terminal_cwd=None,
        identity_loaded=False,
        now_fn=_frozen_now,
    )


# ── Regression scenarios ────────────────────────────────────────────


class TestComposerByteForByteEquality:
    """Each scenario constructs the legacy parts and the new composed
    prompt with identical inputs and asserts byte-for-byte equality on
    the three tiers plus the per-section dict."""

    def _assert_equal(self, legacy: Dict[str, str], composed: ComposedPrompt) -> None:
        assert legacy["stable"] == composed.tiers["stable"], (
            f"stable tier diverged:\n--- legacy ---\n{legacy['stable']!r}\n"
            f"--- composed ---\n{composed.tiers['stable']!r}"
        )
        assert legacy["context"] == composed.tiers["context"], (
            f"context tier diverged:\n--- legacy ---\n{legacy['context']!r}\n"
            f"--- composed ---\n{composed.tiers['context']!r}"
        )
        assert legacy["volatile"] == composed.tiers["volatile"], (
            f"volatile tier diverged:\n--- legacy ---\n{legacy['volatile']!r}\n"
            f"--- composed ---\n{composed.tiers['volatile']!r}"
        )
        assert legacy["_sections"] == composed.sections, (
            f"_sections diverged:\n--- legacy keys ---\n"
            f"{sorted(legacy['_sections'].keys())}\n"
            f"--- composed keys ---\n{sorted(composed.sections.keys())}"
        )

    def test_minimal_cli_session(self, monkeypatch):
        """Sparse CLI: no memory, no profile, no system_message, batch
        identity fallback (skip_context_files=True + load_soul_identity=False)."""
        kwargs = dict(
            valid_tool_names=set(),
            skip_context_files=True,
            load_soul_identity=False,
            platform="cli",
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_with_memory_and_skills_tools(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"memory", "skill_manage", "skills_list", "skill_view"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_with_escalate_and_session_search(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"escalate", "session_search"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_with_kanban(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"kanban_show", "memory"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_with_computer_use(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"computer_use", "memory"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_gpt_model_enforcement(self, monkeypatch):
        # gpt model substring triggers tool_use_enforcement (auto) +
        # OPENAI_MODEL_EXECUTION_GUIDANCE.
        kwargs = dict(
            valid_tool_names={"memory"},
            model="openai/gpt-5",
            provider="openai",
            platform="cli",
            tool_use_enforcement="auto",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_gemini_model_enforcement(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"memory"},
            model="google/gemini-3-pro",
            provider="google",
            platform="cli",
            tool_use_enforcement="auto",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_enforcement_disabled_explicitly(self, monkeypatch):
        # Explicit ``false`` skips tool_use_enforcement AND the cascade
        # OpenAI/Google operational guidance.
        kwargs = dict(
            valid_tool_names={"memory"},
            model="openai/gpt-5",
            provider="openai",
            platform="cli",
            tool_use_enforcement=False,
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_alibaba_provider_override(self, monkeypatch):
        kwargs = dict(
            valid_tool_names={"memory"},
            model="alibaba/qwen3-coder",
            provider="alibaba",
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_with_system_message(self, monkeypatch):
        kwargs = dict(
            valid_tool_names=set(),
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
            system_message="You are a helpful assistant working on a coding task.",
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_pass_session_id_emits_session_in_timestamp(self, monkeypatch):
        kwargs = dict(
            valid_tool_names=set(),
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
            pass_session_id=True,
            session_id="sess_test_12345",
            model="claude-opus-4-7",
            provider="anthropic",
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_memory_store_with_content(self, monkeypatch):
        store = MagicMock()
        store.format_for_system_prompt.side_effect = (
            lambda which: f"<<MEMORY:{which.upper()}>>"
        )
        kwargs = dict(
            valid_tool_names={"memory"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
            memory_enabled=True,
            user_profile_enabled=True,
            memory_store=store,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)

    def test_external_memory_manager(self, monkeypatch):
        manager = MagicMock()
        manager.build_system_prompt.return_value = "<<HONCHO_BLOCK>>"
        kwargs = dict(
            valid_tool_names={"memory"},
            platform="cli",
            skip_context_files=True,
            load_soul_identity=False,
            memory_manager=manager,
        )
        legacy = _build_legacy_parts(monkeypatch=monkeypatch, **kwargs)
        composed = _compose_new(**kwargs)
        self._assert_equal(legacy, composed)


# ── Composer mechanics (independent of regression) ────────────────────


class TestComposerMechanics:
    def test_register_and_compose_single_section(self):
        composer = PromptComposer()
        from grove.prompt.composer import SectionResult
        composer.register_section(
            "greeting",
            lambda ctx: SectionResult(label="greeting", text="hello world"),
            order=10, tier="stable",
        )
        result = composer.compose()
        assert result.text == "hello world"
        assert result.sections == {"greeting": "hello world"}
        assert result.tiers["stable"] == "hello world"

    def test_provider_returning_none_skips_section(self):
        composer = PromptComposer()
        composer.register_section(
            "always_skip", lambda ctx: None, order=10, tier="stable",
        )
        from grove.prompt.composer import SectionResult
        composer.register_section(
            "always_include",
            lambda ctx: SectionResult(label="always_include", text="kept"),
            order=20, tier="stable",
        )
        result = composer.compose()
        assert result.text == "kept"
        assert "always_skip" not in result.sections

    def test_empty_or_whitespace_text_is_dropped(self):
        composer = PromptComposer()
        from grove.prompt.composer import SectionResult
        composer.register_section(
            "empty",
            lambda ctx: SectionResult(label="empty", text="   "),
            order=10, tier="stable",
        )
        composer.register_section(
            "good",
            lambda ctx: SectionResult(label="good", text="content"),
            order=20, tier="stable",
        )
        result = composer.compose()
        assert result.text == "content"

    def test_config_disabled_skips_section(self):
        from grove.prompt.composer import SectionResult
        config = {"sections": {"opt_out": {"enabled": False}}}
        composer = PromptComposer(config=config)
        composer.register_section(
            "opt_out",
            lambda ctx: SectionResult(label="opt_out", text="should not appear"),
            order=10, tier="stable",
        )
        composer.register_section(
            "kept",
            lambda ctx: SectionResult(label="kept", text="kept"),
            order=20, tier="stable",
        )
        result = composer.compose()
        assert "opt_out" not in result.sections
        assert result.text == "kept"

    def test_config_order_overrides_in_code_default(self):
        from grove.prompt.composer import SectionResult
        # In-code default would put A first; config flips it.
        config = {"sections": {
            "A": {"order": 99},
            "B": {"order": 1},
        }}
        composer = PromptComposer(config=config)
        composer.register_section(
            "A",
            lambda ctx: SectionResult(label="A", text="A"),
            order=10, tier="stable",
        )
        composer.register_section(
            "B",
            lambda ctx: SectionResult(label="B", text="B"),
            order=20, tier="stable",
        )
        result = composer.compose()
        assert result.text == "B\n\nA"

    def test_tier_order_is_fixed_stable_context_volatile(self):
        from grove.prompt.composer import SectionResult
        composer = PromptComposer()
        composer.register_section(
            "v", lambda ctx: SectionResult(label="v", text="V"),
            order=10, tier="volatile",
        )
        composer.register_section(
            "c", lambda ctx: SectionResult(label="c", text="C"),
            order=10, tier="context",
        )
        composer.register_section(
            "s", lambda ctx: SectionResult(label="s", text="S"),
            order=10, tier="stable",
        )
        result = composer.compose()
        assert result.text == "S\n\nC\n\nV"

    def test_compose_is_reentrant(self):
        # GRV-007 § IX.3 — concurrent compose() calls MUST not interfere.
        # We don't actually thread here, but we verify the composer
        # holds no per-call state.
        from grove.prompt.composer import SectionResult
        composer = PromptComposer()
        composer.register_section(
            "echo",
            lambda ctx: SectionResult(label="echo", text=ctx["msg"]),
            order=10, tier="stable",
        )
        r1 = composer.compose(msg="first")
        r2 = composer.compose(msg="second")
        r3 = composer.compose(msg="third")
        assert r1.text == "first"
        assert r2.text == "second"
        assert r3.text == "third"

    def test_unknown_tier_raises(self):
        composer = PromptComposer()
        with pytest.raises(ValueError, match="unknown tier"):
            composer.register_section(
                "bogus", lambda ctx: None, order=10, tier="ephemeral",
            )

    def test_re_registration_overwrites(self):
        # Sprint 37 will swap a default provider for a contextual-
        # preamble-aware variant via a second register call. That must
        # work cleanly.
        from grove.prompt.composer import SectionResult
        composer = PromptComposer()
        composer.register_section(
            "section",
            lambda ctx: SectionResult(label="section", text="v1"),
            order=10, tier="stable",
        )
        composer.register_section(
            "section",
            lambda ctx: SectionResult(label="section", text="v2"),
            order=10, tier="stable",
        )
        result = composer.compose()
        assert result.text == "v2"
