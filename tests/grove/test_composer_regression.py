"""Sprint 36 — PromptComposer mechanics + regression locks (GRV-007).

THE Phase 1b byte-for-byte regression test (13 ``TestComposerByteForByteEquality``
scenarios) ran against the live ``AIAgent._build_system_prompt_parts``
method and proved the composer matches the legacy output byte-for-byte
before Phase 2 deleted the legacy method. That proof is preserved in
the Phase 1b commit (``23fdc6c3b``) where the legacy method still
existed; running ``git checkout 23fdc6c3b -- tests/grove/test_composer_regression.py``
and ``pytest tests/grove/test_composer_regression.py`` reproduces the
13-scenario byte-for-byte assertion.

This file holds the **composer mechanics** tests that remain green
post-Phase-2: registration, gating, ordering, tier order, reentrancy,
validation, and re-registration semantics.
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
